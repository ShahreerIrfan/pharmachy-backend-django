"""
backend/tests/test_p272_supplier_payable_ledger.py
Architecture Decision: ADR-0272 (P272).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Supplier Accounts Payable (AP) Ledger:
1. Valid chronological AP postings (GRN Receipt -> Payment -> Credit Note) with running balance and reconciliation.
2. Incomplete and invalid input rejection (negative amounts, zero amounts, missing references, invalid types).
3. Fine-grained RBAC matrix (Cashier denied view/post, Pharmacist read-only, Manager/Owner authorized).
4. Cross-tenant isolation strictly enforced with HTTP 403.
5. Idempotency protection against duplicate network retries.
6. Compensating reversal workflow (inversion of entries, balance restoration, duplicate reversal block).
7. Suspended tenant blocked with HTTP 403 (ERR_TENANT_SUSPENDED).
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    Supplier,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.supplier_payable_ledger_service import SupplierPayableLedgerService
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSupplierPayableLedgerIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Square Branch",
        )

        # 2. Foreign Tenant
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Beximco Health {self.suffix}",
            subdomain=f"bx-{self.suffix}",
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
            email=f"owner_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.branch_manager = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"rx_{self.suffix}@square.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@beximco.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@susp.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Suspended Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 5. Primary Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-p272-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Incepta Pharmaceuticals Depot {self.suffix}",
            current_balance=Decimal("0.0000"),
            credit_limit=Decimal("500000.0000"),
            is_active=True,
        )

    def test_01_valid_chronological_ap_postings_and_reconciliation(self):
        """Scenario 1: Chronological AP postings (GRN -> Payment -> Credit Note) and balance reconciliation."""
        self.client.force_authenticate(user=self.branch_manager)
        url_post = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/post-ledger/"
        url_rec = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/reconcile/"

        # 1. Post GRN Invoice (Liability Credit of 100,000.0000 BDT)
        res_grn = self.client.post(
            url_post,
            {
                "posting_type": "GRN_INVOICE",
                "reference_number": "GRN-2026-001",
                "credit_amount": "100000.0000",
                "notes": "Antibiotics batch delivery received",
            },
            format="json",
        )
        assert res_grn.status_code == status.HTTP_201_CREATED
        assert Decimal(res_grn.data["balance_after"]) == Decimal("100000.0000")
        self.supplier.refresh_from_db()
        assert self.supplier.current_balance == Decimal("100000.0000")

        # 2. Post Payment (Liability Debit of 40,000.0000 BDT)
        res_pay = self.client.post(
            url_post,
            {
                "posting_type": "PAYMENT",
                "reference_number": "VOUCH-7001",
                "debit_amount": "40000.0000",
                "notes": "Direct bank transfer payment",
            },
            format="json",
        )
        assert res_pay.status_code == status.HTTP_201_CREATED
        assert Decimal(res_pay.data["balance_after"]) == Decimal("60000.0000")
        self.supplier.refresh_from_db()
        assert self.supplier.current_balance == Decimal("60000.0000")

        # 3. Post Supplier Credit Note (Liability Debit of 10,000.0000 BDT)
        res_scn = self.client.post(
            url_post,
            {
                "posting_type": "SUPPLIER_CREDIT_NOTE",
                "reference_number": "SCN-301",
                "debit_amount": "10000.0000",
                "notes": "Special prompt payment discount credit note",
            },
            format="json",
        )
        assert res_scn.status_code == status.HTTP_201_CREATED
        assert Decimal(res_scn.data["balance_after"]) == Decimal("50000.0000")
        self.supplier.refresh_from_db()
        assert self.supplier.current_balance == Decimal("50000.0000")

        # 4. Verify Reconciliation Summary
        res_rec = self.client.get(url_rec)
        assert res_rec.status_code == status.HTTP_200_OK
        rec_data = res_rec.data
        assert rec_data["is_reconciled"] is True
        assert Decimal(rec_data["total_credits"]) == Decimal("100000.0000")
        assert Decimal(rec_data["total_debits"]) == Decimal("50000.0000")
        assert Decimal(rec_data["actual_closing_balance"]) == Decimal("50000.0000")
        assert Decimal(rec_data["supplier_current_balance"]) == Decimal("50000.0000")
        assert rec_data["total_postings"] == 3

    def test_02_invalid_and_incomplete_inputs_rejected(self):
        """Scenario 2: Validation rejects negative amounts, zero amounts, and missing references."""
        self.client.force_authenticate(user=self.branch_manager)
        url_post = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/post-ledger/"

        # Case A: Missing reference number
        res_a = self.client.post(url_post, {"posting_type": "PAYMENT", "debit_amount": "100.00"}, format="json")
        assert res_a.status_code == status.HTTP_400_BAD_REQUEST
        assert res_a.data["error_code"] == "ERR_MISSING_REFERENCE_NUMBER"

        # Case B: Negative amount
        res_b = self.client.post(
            url_post,
            {"posting_type": "PAYMENT", "reference_number": "REF-001", "debit_amount": "-50.00"},
            format="json",
        )
        assert res_b.status_code == status.HTTP_400_BAD_REQUEST
        assert res_b.data["error_code"] == "ERR_INVALID_AMOUNT"

        # Case C: Zero amount (both 0)
        res_c = self.client.post(
            url_post,
            {"posting_type": "PAYMENT", "reference_number": "REF-002", "debit_amount": "0.00", "credit_amount": "0.00"},
            format="json",
        )
        assert res_c.status_code == status.HTTP_400_BAD_REQUEST
        assert res_c.data["error_code"] == "ERR_ZERO_AMOUNT"

        # Case D: Invalid posting type
        res_d = self.client.post(
            url_post,
            {"posting_type": "INVALID_TYPE", "reference_number": "REF-003", "debit_amount": "100.00"},
            format="json",
        )
        assert res_d.status_code == status.HTTP_400_BAD_REQUEST
        assert res_d.data["error_code"] == "ERR_INVALID_POSTING_TYPE"

        # Case E: Manual adjustment without reason
        res_e = self.client.post(
            url_post,
            {"posting_type": "ADJUSTMENT", "reference_number": "ADJ-001", "credit_amount": "100.00", "notes": ""},
            format="json",
        )
        assert res_e.status_code == status.HTTP_400_BAD_REQUEST
        assert res_e.data["error_code"] == "ERR_MISSING_ADJUSTMENT_REASON"

    def test_03_role_authorization_and_cross_tenant_isolation(self):
        """Scenario 3: Cashier blocked, Pharmacist read-only, Cross-tenant access blocked."""
        url_post = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/post-ledger/"
        url_ledger = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/ledger/"

        # 1. Cashier denied both list and post
        self.client.force_authenticate(user=self.cashier)
        res_c_list = self.client.get(url_ledger)
        assert res_c_list.status_code == status.HTTP_403_FORBIDDEN
        assert res_c_list.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        res_c_post = self.client.post(
            url_post,
            {"posting_type": "PAYMENT", "reference_number": "REF-ILLEGAL", "debit_amount": "100.00"},
            format="json",
        )
        assert res_c_post.status_code == status.HTTP_403_FORBIDDEN
        assert res_c_post.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Pharmacist permitted to view, but blocked from posting
        self.client.force_authenticate(user=self.pharmacist)
        res_rx_list = self.client.get(url_ledger)
        assert res_rx_list.status_code == status.HTTP_200_OK

        res_rx_post = self.client.post(
            url_post,
            {"posting_type": "PAYMENT", "reference_number": "REF-ILLEGAL-RX", "debit_amount": "100.00"},
            format="json",
        )
        assert res_rx_post.status_code == status.HTTP_403_FORBIDDEN
        assert res_rx_post.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 3. Foreign tenant access blocked
        self.client.force_authenticate(user=self.foreign_manager)
        res_for_get = self.client.get(url_ledger)
        assert res_for_get.status_code == status.HTTP_403_FORBIDDEN
        assert res_for_get.data["error_code"] in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION"]

    def test_04_idempotency_and_compensating_reversal_workflow(self):
        """Scenario 4: Idempotency suppresses duplicates, Reversal restores balance without deletion."""
        self.client.force_authenticate(user=self.owner)
        url_post = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/post-ledger/"
        url_rev = f"/api/v2/procurement/suppliers/{self.supplier.supplier_id}/reverse-ledger/"

        idem_key = f"idem-key-{self.suffix}"

        # 1. Post initial invoice with idempotency key
        res_1 = self.client.post(
            url_post,
            {
                "posting_type": "INVOICE",
                "reference_number": "INV-P272-99",
                "credit_amount": "25000.0000",
                "idempotency_key": idem_key,
            },
            format="json",
        )
        assert res_1.status_code == status.HTTP_201_CREATED
        posting_id = res_1.data["posting_id"]
        assert Decimal(res_1.data["balance_after"]) == Decimal("25000.0000")

        # 2. Duplicate retry with same idempotency key returns exact same record without double charge
        res_2 = self.client.post(
            url_post,
            {
                "posting_type": "INVOICE",
                "reference_number": "INV-P272-99",
                "credit_amount": "25000.0000",
                "idempotency_key": idem_key,
            },
            format="json",
        )
        assert res_2.status_code == status.HTTP_201_CREATED
        assert res_2.data["posting_id"] == posting_id
        self.supplier.refresh_from_db()
        assert self.supplier.current_balance == Decimal("25000.0000")

        # 3. Compensating reversal inverts liability and updates balance
        res_rev = self.client.post(
            url_rev,
            {"posting_id": posting_id, "reason": "Invoice billed to wrong branch depot"},
            format="json",
        )
        assert res_rev.status_code == status.HTTP_201_CREATED
        assert Decimal(res_rev.data["debit_amount"]) == Decimal("25000.0000")
        assert Decimal(res_rev.data["credit_amount"]) == Decimal("0.0000")
        self.supplier.refresh_from_db()
        assert self.supplier.current_balance == Decimal("0.0000")

        # 4. Attempting duplicate reversal fails
        res_rev_dup = self.client.post(
            url_rev,
            {"posting_id": posting_id, "reason": "Attempting duplicate reversal"},
            format="json",
        )
        assert res_rev_dup.status_code == status.HTTP_400_BAD_REQUEST
        assert res_rev_dup.data["error_code"] == "ERR_ALREADY_REVERSED"

    def test_05_suspended_tenant_blocked(self):
        """Scenario 5: Suspended tenant blocked from ledger interactions with HTTP 403."""
        # Create supplier under suspended tenant
        sup_susp = Supplier.objects.create(
            supplier_id=f"sup-susp-{self.suffix}",
            tenant=self.suspended_tenant,
            name=f"Suspended Supplier {self.suffix}",
        )

        self.client.force_authenticate(user=self.suspended_user)
        url_post = f"/api/v2/procurement/suppliers/{sup_susp.supplier_id}/post-ledger/"

        res = self.client.post(
            url_post,
            {"posting_type": "PAYMENT", "reference_number": "REF-SUSP", "debit_amount": "500.00"},
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        data = res.data if hasattr(res, "data") else res.json()
        assert data.get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]
