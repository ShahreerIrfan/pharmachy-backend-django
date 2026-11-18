"""
backend/tests/test_p273_vendor_payment_allocation.py
Architecture Decision: ADR-0273 (P273).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Vendor Payment Disbursements & Invoice Allocations:
1. Multi-invoice allocation with partial settlement and unapplied advance balance tracking.
2. Anti-overpayment validation: allocation exceeding invoice unpaid balance is rejected.
3. Total allocations capped at payment disbursement amount.
4. RBAC authorization matrix: Cashier and Staff Pharmacist denied (403), Owner & Manager permitted.
5. Cross-tenant isolation strictly enforced for suppliers and invoices.
6. Idempotency replay protection for reliable POS/mobile offline sync retries.
7. Subsequent allocation of unapplied advance balance to newly pending invoices.
8. Suspended tenant account write blocking.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    Supplier,
    SupplierInvoice,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
    VendorPayment,
    VendorPaymentInvoiceAllocation,
    VendorPaymentStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestVendorPaymentAllocationIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Dhanmondi Branch",
        )

        # 2. Foreign Tenant
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Foreign Health {self.suffix}",
            subdomain=f"for-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 4. Users
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.branch_manager = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@popular.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@popular.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@popular.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 5. Suppliers
        self.supplier = Supplier.objects.create(
            name=f"Square Pharmaceuticals {self.suffix}",
            code=f"SQ-{self.suffix[:4].upper()}",
            tenant=self.tenant_a,
            current_balance=Decimal("15000.0000"),
        )

        self.foreign_supplier = Supplier.objects.create(
            name=f"Foreign Pharma {self.suffix}",
            code=f"FP-{self.suffix[:4].upper()}",
            tenant=self.tenant_b,
            current_balance=Decimal("5000.0000"),
        )

        # 6. Supplier Invoices
        self.invoice_1 = SupplierInvoice.objects.create(
            invoice_id=f"sinv-1-{self.suffix}",
            invoice_number=f"INV-{self.suffix}-01",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            supplier_name=self.supplier.name,
            invoice_date=timezone.now().date(),
            total_amount=Decimal("5000.0000"),
        )

        self.invoice_2 = SupplierInvoice.objects.create(
            invoice_id=f"sinv-2-{self.suffix}",
            invoice_number=f"INV-{self.suffix}-02",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            supplier_name=self.supplier.name,
            invoice_date=timezone.now().date(),
            total_amount=Decimal("3000.0000"),
        )

        self.foreign_invoice = SupplierInvoice.objects.create(
            invoice_id=f"sinv-f-{self.suffix}",
            invoice_number=f"INV-{self.suffix}-F",
            tenant=self.tenant_b,
            supplier=self.foreign_supplier,
            supplier_name=self.foreign_supplier.name,
            invoice_date=timezone.now().date(),
            total_amount=Decimal("2000.0000"),
        )

    def test_01_multi_invoice_allocation_with_advance_unapplied(self):
        """Disburse 10,000 BDT: partial settlement for Inv1 (4000/5000), full for Inv2 (3000/3000), 3000 advance."""
        self.client.force_authenticate(user=self.branch_manager)

        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/allocate-payment/"
        payload = {
            "total_amount": "10000.0000",
            "payment_method": "BANK_TRANSFER",
            "reference_number": "EFT-8833991",
            "notes": "Weekly batch settlement for Square Pharma",
            "idempotency_key": f"idem-p273-{self.suffix}-1",
            "allocations": [
                {
                    "invoice_id": self.invoice_1.invoice_id,
                    "allocated_amount": "4000.0000",
                    "notes": "Partial settlement for INV-01",
                },
                {
                    "invoice_id": self.invoice_2.invoice_id,
                    "allocated_amount": "3000.0000",
                    "notes": "Full settlement for INV-02",
                },
            ],
        }

        res = self.client.post(url, payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED, res.data
        data = res.data

        assert Decimal(data["total_amount"]) == Decimal("10000.0000")
        assert Decimal(data["allocated_amount"]) == Decimal("7000.0000")
        assert Decimal(data["unallocated_amount"]) == Decimal("3000.0000")
        assert data["status"] == VendorPaymentStatus.PARTIALLY_ALLOCATED
        assert len(data["allocations"]) == 2

        # Check itemized allocations
        alloc_1 = next(a for a in data["allocations"] if a["invoice_id"] == self.invoice_1.invoice_id)
        assert Decimal(alloc_1["allocated_amount"]) == Decimal("4000.0000")
        assert Decimal(alloc_1["previous_unpaid_amount"]) == Decimal("5000.0000")
        assert Decimal(alloc_1["remaining_unpaid_amount"]) == Decimal("1000.0000")

        alloc_2 = next(a for a in data["allocations"] if a["invoice_id"] == self.invoice_2.invoice_id)
        assert Decimal(alloc_2["allocated_amount"]) == Decimal("3000.0000")
        assert Decimal(alloc_2["previous_unpaid_amount"]) == Decimal("3000.0000")
        assert Decimal(alloc_2["remaining_unpaid_amount"]) == Decimal("0.0000")

        # Verify AP ledger entry created
        ledger_entry_id = data.get("ledger_entry_id")
        assert ledger_entry_id is not None
        ledger_entry = SupplierLedgerEntry.objects.get(posting_id=ledger_entry_id)
        assert ledger_entry.posting_type == SupplierPostingType.PAYMENT
        assert ledger_entry.debit_amount == Decimal("10000.0000")

        # Verify query unpaid invoices endpoint
        url_unpaid = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/unpaid-invoices/"
        res_unpaid = self.client.get(url_unpaid)
        assert res_unpaid.status_code == status.HTTP_200_OK
        unpaid_list = res_unpaid.data["invoices"]
        # Only Inv1 is unpaid (Inv2 is settled and excluded by default)
        assert len(unpaid_list) == 1
        assert unpaid_list[0]["invoice_id"] == self.invoice_1.invoice_id
        assert Decimal(unpaid_list[0]["unpaid_balance"]) == Decimal("1000.0000")
        assert unpaid_list[0]["settlement_status"] == "PARTIALLY_PAID"

    def test_02_allocation_exceeding_invoice_unpaid_balance_rejected(self):
        """Allocation amount greater than remaining invoice unpaid balance must be rejected."""
        self.client.force_authenticate(user=self.branch_manager)

        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/allocate-payment/"
        payload = {
            "total_amount": "6000.0000",
            "allocations": [
                {
                    "invoice_id": self.invoice_1.invoice_id,
                    "allocated_amount": "5500.0000",  # Invoice total is 5000.0000
                }
            ],
        }

        res = self.client.post(url, payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_ALLOCATION_EXCEEDS_INVOICE_BALANCE"

    def test_03_allocations_exceeding_total_payment_amount_rejected(self):
        """Total invoice allocations cannot exceed total payment disbursement amount."""
        self.client.force_authenticate(user=self.owner)

        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/allocate-payment/"
        payload = {
            "total_amount": "4000.0000",
            "allocations": [
                {"invoice_id": self.invoice_1.invoice_id, "allocated_amount": "3000.0000"},
                {"invoice_id": self.invoice_2.invoice_id, "allocated_amount": "2000.0000"},
            ],
        }

        res = self.client.post(url, payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_TOTAL_ALLOCATION_EXCEEDS_PAYMENT"

    def test_04_role_authorization_cashier_and_pharmacist_denied(self):
        """Cashier and Staff Pharmacist are barred from disbursing vendor payments."""
        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/allocate-payment/"
        payload = {
            "total_amount": "1000.0000",
            "allocations": [{"invoice_id": self.invoice_1.invoice_id, "allocated_amount": "1000.0000"}],
        }

        # 1. Cashier -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        res_cashier = self.client.post(url, payload, format="json")
        assert res_cashier.status_code == status.HTTP_403_FORBIDDEN
        assert res_cashier.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Staff Pharmacist -> 403 Forbidden
        self.client.force_authenticate(user=self.pharmacist)
        res_pharm = self.client.post(url, payload, format="json")
        assert res_pharm.status_code == status.HTTP_403_FORBIDDEN
        assert res_pharm.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_05_cross_tenant_supplier_and_invoice_isolation(self):
        """Accessing a foreign supplier or foreign invoice raises HTTP 403 Forbidden."""
        self.client.force_authenticate(user=self.branch_manager)

        # 1. Foreign supplier
        url_foreign_sup = f"/api/v2/procurement/suppliers/{self.foreign_supplier.supplier_id}/allocate-payment/"
        res_sup = self.client.post(url_foreign_sup, {"total_amount": "1000.0000"}, format="json")
        assert res_sup.status_code == status.HTTP_403_FORBIDDEN
        assert res_sup.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

        # 2. Foreign invoice in tenant_a disbursement
        url_local_sup = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/allocate-payment/"
        payload_foreign_inv = {
            "total_amount": "1000.0000",
            "allocations": [{"invoice_id": self.foreign_invoice.invoice_id, "allocated_amount": "1000.0000"}],
        }
        res_inv = self.client.post(url_local_sup, payload_foreign_inv, format="json")
        assert res_inv.status_code == status.HTTP_403_FORBIDDEN
        assert res_inv.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_06_idempotency_replay_protection(self):
        """Network retry with identical idempotency key returns existing payment without double deduction."""
        self.client.force_authenticate(user=self.branch_manager)

        url = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/allocate-payment/"
        idem_key = f"idem-replay-{self.suffix}"
        payload = {
            "total_amount": "2500.0000",
            "idempotency_key": idem_key,
            "allocations": [{"invoice_id": self.invoice_1.invoice_id, "allocated_amount": "2000.0000"}],
        }

        res_1 = self.client.post(url, payload, format="json")
        assert res_1.status_code == status.HTTP_201_CREATED
        p1_id = res_1.data["payment_id"]

        # Re-post with same idempotency key
        res_2 = self.client.post(url, payload, format="json")
        assert res_2.status_code in [status.HTTP_200_OK, status.HTTP_201_CREATED]
        assert res_2.data["payment_id"] == p1_id
        assert res_2.data.get("is_idempotent_replay") is True

        # Total payments in db must remain 1
        assert VendorPayment.objects.filter(idempotency_key=idem_key).count() == 1

    def test_07_allocate_unapplied_advance_balance(self):
        """Allocating remaining unapplied advance from a payment to another invoice."""
        self.client.force_authenticate(user=self.branch_manager)

        # 1. Create payment of 5000 with 3000 allocated to Inv1 (2000 unapplied advance)
        url_create = "/api/v2/procurement/vendor-payments/"
        payload_create = {
            "supplier_id": self.supplier.supplier_id,
            "total_amount": "5000.0000",
            "allocations": [{"invoice_id": self.invoice_1.invoice_id, "allocated_amount": "3000.0000"}],
        }
        res_create = self.client.post(url_create, payload_create, format="json")
        assert res_create.status_code == status.HTTP_201_CREATED
        payment_id = res_create.data["payment_id"]
        assert Decimal(res_create.data["unallocated_amount"]) == Decimal("2000.0000")

        # 2. Allocate 1500 of the 2000 advance to Inv2
        url_advance = f"/api/v2/procurement/vendor-payments/{payment_id}/allocate-unapplied/"
        payload_advance = {
            "allocations": [{"invoice_id": self.invoice_2.invoice_id, "allocated_amount": "1500.0000"}],
        }
        res_advance = self.client.post(url_advance, payload_advance, format="json")
        assert res_advance.status_code == status.HTTP_200_OK
        data_adv = res_advance.data

        assert Decimal(data_adv["allocated_amount"]) == Decimal("4500.0000")
        assert Decimal(data_adv["unallocated_amount"]) == Decimal("500.0000")
        assert data_adv["status"] == VendorPaymentStatus.PARTIALLY_ALLOCATED
        assert len(data_adv["allocations"]) == 2

    def test_08_suspended_tenant_blocked(self):
        """Suspended tenant is barred from vendor payment operations."""
        self.client.force_authenticate(user=self.suspended_user)
        url = "/api/v2/procurement/vendor-payments/"
        res = self.client.post(url, {"supplier_id": self.supplier.supplier_id, "total_amount": "1000.0000"}, format="json")
        data = res.data if hasattr(res, "data") else res.json()
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert data.get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]
