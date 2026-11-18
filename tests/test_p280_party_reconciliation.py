"""
backend/tests/test_p280_party_reconciliation.py
Architecture Decision: ADR-0280 (P280).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying End-to-End Party Reconciliation:
1. Customer AR full cycle reconciliation (sale due -> collection -> refund -> zero variance BALANCED).
2. Customer split payment & refund reconciliation with accurate partial balance.
3. Customer invoice reversal reconciliation (sale -> reversal -> net zero).
4. Discrepancy detection when recorded balance drifts from source ledger.
5. Branch Manager auto-adjustment blocked (ERR_AUTO_ADJUST_UNAUTHORIZED, 403).
6. Pharmacy Owner auto-adjustment succeeds with compensating adjustment entry.
7. Supplier AP full cycle reconciliation (GRN bill -> partial payment -> credit note -> balance).
8. Supplier payment reversal reconciliation.
9. Portfolio-wide reconciliation summary endpoint.
10. Cashier and staff pharmacist strictly denied (ERR_STAFF_ROLE_DENIED, 403).
11. Cross-tenant isolation strictly enforced (ERR_CROSS_TENANT_ACCESS, 403).
12. Incomplete input and suspended tenant write protection.
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
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    PartyReconciliationRecord,
    PartyReconciliationStatus,
    PartyReconciliationType,
    PosCustomer,
    Supplier,
    SupplierLedgerEntry,
    SupplierPostingType,
    SupplierStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPartyReconciliationIntegration:

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
            name="Dhanmondi Branch",
        )

        # 2. Foreign Tenant & Branch
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Foreign Health {self.suffix}",
            subdomain=f"for-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Foreign Branch",
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 4. Customers for Tenant A
        self.customer_1 = PosCustomer.objects.create(
            customer_id=f"cust-1-{self.suffix}",
            tenant=self.tenant_a,
            primary_branch=self.branch_a,
            full_name="Kazi Nazrul Islam",
            phone=f"+880171{random.randint(1000000, 9999999)}",
            credit_limit_bdt=Decimal("15000.00"),
            current_due_bdt=Decimal("1500.00"),
        )

        self.customer_drift = PosCustomer.objects.create(
            customer_id=f"cust-drift-{self.suffix}",
            tenant=self.tenant_a,
            primary_branch=self.branch_a,
            full_name="Jasim Uddin",
            phone=f"+880181{random.randint(1000000, 9999999)}",
            credit_limit_bdt=Decimal("10000.00"),
            current_due_bdt=Decimal("1200.00"), # Will drift from 1500.00 ledger balance
        )

        # 5. Suppliers for Tenant A
        self.supplier_1 = Supplier.objects.create(
            supplier_id=f"sup-1-{self.suffix}",
            tenant=self.tenant_a,
            name="Beximco Pharma Depot",
            code=f"BEX-{self.suffix.upper()}",
            contact_person="Kabir Ahmed",
            phone=f"+880191{random.randint(1000000, 9999999)}",
            current_balance=Decimal("5000.00"),
            status=SupplierStatus.ACTIVE,
        )

        # 6. Foreign Customer
        self.foreign_customer = PosCustomer.objects.create(
            customer_id=f"cust-for-{self.suffix}",
            tenant=self.tenant_b,
            primary_branch=self.branch_b,
            full_name="Foreign Customer",
            phone="+8801999999999",
        )

        # 7. Users
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.finance = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@square.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Finance Officer",
            password="StrongPassword123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@square.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@square.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Suspended Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_customer_ar_balanced_cycle(self):
        """Full Customer AR cycle (due sale 5000 -> payment collection 3000 -> refund 500 = 1500) balances with 0 variance."""
        # 1. Due sale (Debit 5000)
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="INV-001",
            debit_amount_bdt=Decimal("5000.00"),
            credit_amount_bdt=Decimal("0.00"),
            balance_after_bdt=Decimal("5000.00"),
        )
        # 2. Payment collection (Credit 3000)
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.PAYMENT_COLLECTION,
            reference_number="COL-001",
            debit_amount_bdt=Decimal("0.00"),
            credit_amount_bdt=Decimal("3000.00"),
            balance_after_bdt=Decimal("2000.00"),
        )
        # 3. Sales return credit (Credit 500)
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.SALES_RETURN_CREDIT,
            reference_number="RET-001",
            debit_amount_bdt=Decimal("0.00"),
            credit_amount_bdt=Decimal("500.00"),
            balance_after_bdt=Decimal("1500.00"),
        )

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {"customer_id": self.customer_1.customer_id},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["success"] is True
        assert data["is_balanced"] is True
        assert data["status"] == PartyReconciliationStatus.BALANCED
        assert data["calculated_balance_bdt"] == "1500.00"
        assert data["recorded_balance_bdt"] == "1500.00"
        assert data["variance_bdt"] == "0.00"
        assert data["discrepancies"] == []

        # Audit record created
        assert PartyReconciliationRecord.objects.filter(
            tenant=self.tenant_a,
            party_id=self.customer_1.customer_id,
            status=PartyReconciliationStatus.BALANCED,
        ).exists()

    def test_02_customer_split_payment_and_refund_reconciliation(self):
        """Customer with multiple split payments and partial refund reconciles accurately."""
        # Due sale 10000
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="INV-SPLIT",
            debit_amount_bdt=Decimal("10000.00"),
            balance_after_bdt=Decimal("10000.00"),
        )
        # Split 1 (4000)
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.PAYMENT_COLLECTION,
            reference_number="COL-SPLIT-1",
            credit_amount_bdt=Decimal("4000.00"),
            balance_after_bdt=Decimal("6000.00"),
        )
        # Split 2 (4500)
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.PAYMENT_COLLECTION,
            reference_number="COL-SPLIT-2",
            credit_amount_bdt=Decimal("4500.00"),
            balance_after_bdt=Decimal("1500.00"),
        )
        self.customer_1.current_due_bdt = Decimal("1500.00")
        self.customer_1.save()

        self.client.force_authenticate(user=self.finance)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {"customer_id": self.customer_1.customer_id},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["is_balanced"] is True
        assert data["total_settlements_collections_bdt"] == "8500.00"
        assert data["variance_bdt"] == "0.00"

    def test_03_customer_invoice_reversal_reconciliation(self):
        """Credit sale followed by full reversal brings net calculated balance to 0.00 BDT."""
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="INV-REV",
            debit_amount_bdt=Decimal("2000.00"),
            balance_after_bdt=Decimal("2000.00"),
        )
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            customer=self.customer_1,
            entry_type=CustomerReceivableEntryType.REVERSAL,
            reference_number="REV-001",
            credit_amount_bdt=Decimal("2000.00"),
            balance_after_bdt=Decimal("0.00"),
        )
        self.customer_1.current_due_bdt = Decimal("0.00")
        self.customer_1.save()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {"customer_id": self.customer_1.customer_id},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["is_balanced"] is True
        assert data["calculated_balance_bdt"] == "0.00"
        assert data["recorded_balance_bdt"] == "0.00"

    def test_04_customer_discrepancy_detection_when_balance_drifts(self):
        """When recorded customer due drifts from ledger, reconciliation detects variance."""
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            customer=self.customer_drift,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="INV-DRIFT",
            debit_amount_bdt=Decimal("1500.00"),
            balance_after_bdt=Decimal("1500.00"),
        )
        # Recorded due is 1200.00 (drift of 300.00)
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {"customer_id": self.customer_drift.customer_id},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["is_balanced"] is False
        assert data["status"] == PartyReconciliationStatus.DISCREPANCY_DETECTED
        assert data["calculated_balance_bdt"] == "1500.00"
        assert data["recorded_balance_bdt"] == "1200.00"
        assert data["variance_bdt"] == "300.00"
        assert len(data["discrepancies"]) >= 1

    def test_05_manager_auto_adjust_blocked(self):
        """Branch Manager attempting auto_adjust=True is blocked with ERR_AUTO_ADJUST_UNAUTHORIZED."""
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            customer=self.customer_drift,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="INV-DRIFT-2",
            debit_amount_bdt=Decimal("1500.00"),
            balance_after_bdt=Decimal("1500.00"),
        )
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {
                "customer_id": self.customer_drift.customer_id,
                "auto_adjust": True,
                "adjustment_reason": "Manager unverified write-off",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.json()
        assert data["error_code"] == "ERR_AUTO_ADJUST_UNAUTHORIZED"

    def test_06_owner_auto_adjust_success(self):
        """Pharmacy Owner performing auto_adjust=True generates compensating adjustment entry."""
        CustomerReceivableLedgerEntry.objects.create(
            tenant=self.tenant_a,
            customer=self.customer_drift,
            entry_type=CustomerReceivableEntryType.DUE_SALE,
            reference_number="INV-DRIFT-3",
            debit_amount_bdt=Decimal("1500.00"),
            balance_after_bdt=Decimal("1500.00"),
        )
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {
                "customer_id": self.customer_drift.customer_id,
                "auto_adjust": True,
                "adjustment_reason": "Reconcile unrecorded manual receipt discount",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["is_balanced"] is True
        assert data["status"] == PartyReconciliationStatus.RECONCILED_WITH_ADJUSTMENT
        assert data["variance_bdt"] == "0.00"

        # Check compensating entry created in ledger
        assert CustomerReceivableLedgerEntry.objects.filter(
            tenant=self.tenant_a,
            customer=self.customer_drift,
            entry_type=CustomerReceivableEntryType.ADJUSTMENT,
        ).exists()

    def test_07_supplier_ap_balanced_cycle(self):
        """Supplier AP cycle (bill 20000 -> payment 12000 -> credit note 3000 = 5000) balances with 0 variance."""
        # 1. Bill (Credit 20000)
        SupplierLedgerEntry.objects.create(
            tenant=self.tenant_a,
            supplier=self.supplier_1,
            posting_type=SupplierPostingType.GRN_INVOICE,
            reference_number="BILL-001",
            credit_amount=Decimal("20000.00"),
            debit_amount=Decimal("0.00"),
            balance_after=Decimal("20000.00"),
        )
        # 2. Payment voucher (Debit 12000)
        SupplierLedgerEntry.objects.create(
            tenant=self.tenant_a,
            supplier=self.supplier_1,
            posting_type=SupplierPostingType.PAYMENT,
            reference_number="PAY-001",
            credit_amount=Decimal("0.00"),
            debit_amount=Decimal("12000.00"),
            balance_after=Decimal("8000.00"),
        )
        # 3. Supplier credit note (Debit 3000)
        SupplierLedgerEntry.objects.create(
            tenant=self.tenant_a,
            supplier=self.supplier_1,
            posting_type=SupplierPostingType.SUPPLIER_CREDIT_NOTE,
            reference_number="SCN-001",
            credit_amount=Decimal("0.00"),
            debit_amount=Decimal("3000.00"),
            balance_after=Decimal("5000.00"),
        )

        self.client.force_authenticate(user=self.finance)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-supplier/",
            {"supplier_id": self.supplier_1.supplier_id},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["success"] is True
        assert data["is_balanced"] is True
        assert data["status"] == PartyReconciliationStatus.BALANCED
        assert data["calculated_balance_bdt"] == "5000.00"
        assert data["recorded_balance_bdt"] == "5000.00"
        assert data["variance_bdt"] == "0.00"

    def test_08_supplier_payment_reversal_reconciliation(self):
        """Supplier payment reversal is credited back, restoring liability balance."""
        SupplierLedgerEntry.objects.create(
            tenant=self.tenant_a,
            supplier=self.supplier_1,
            posting_type=SupplierPostingType.INVOICE,
            reference_number="BILL-REV",
            credit_amount=Decimal("10000.00"),
            balance_after=Decimal("10000.00"),
        )
        SupplierLedgerEntry.objects.create(
            tenant=self.tenant_a,
            supplier=self.supplier_1,
            posting_type=SupplierPostingType.PAYMENT,
            reference_number="PAY-REV",
            debit_amount=Decimal("10000.00"),
            balance_after=Decimal("0.00"),
        )
        SupplierLedgerEntry.objects.create(
            tenant=self.tenant_a,
            supplier=self.supplier_1,
            posting_type=SupplierPostingType.PAYMENT_REVERSAL,
            reference_number="REV-PAY-001",
            credit_amount=Decimal("10000.00"),
            balance_after=Decimal("10000.00"),
        )
        self.supplier_1.current_balance = Decimal("10000.00")
        self.supplier_1.save()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-supplier/",
            {"supplier_id": self.supplier_1.supplier_id},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["is_balanced"] is True
        assert data["calculated_balance_bdt"] == "10000.00"

    def test_09_tenant_portfolio_summary(self):
        """Portfolio summary endpoint aggregates total AR and AP balances."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/v2/parties/reconciliation/portfolio-summary/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert "total_customers_count" in data
        assert "total_suppliers_count" in data
        assert "total_outstanding_ar_bdt" in data
        assert "total_outstanding_ap_bdt" in data

    def test_10_staff_roles_strictly_denied(self):
        """Cashier and staff pharmacist are strictly forbidden from reconciliation endpoints."""
        for user in [self.cashier, self.pharmacist]:
            self.client.force_authenticate(user=user)
            resp = self.client.post(
                "/api/v2/parties/reconciliation/reconcile-customer/",
                {"customer_id": self.customer_1.customer_id},
                format="json",
            )
            assert resp.status_code == status.HTTP_403_FORBIDDEN
            assert resp.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_11_cross_tenant_isolation_enforced(self):
        """Tenant A manager cannot reconcile customer belonging to Tenant B."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {"customer_id": self.foreign_customer.customer_id},
            format="json",
        )
        assert resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        if resp.status_code == status.HTTP_403_FORBIDDEN:
            assert resp.json()["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_12_incomplete_input_and_suspended_tenant(self):
        """Incomplete payload is rejected (422/400); suspended tenant account is blocked."""
        self.client.force_authenticate(user=self.manager)
        # Missing customer_id
        r_invalid = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {},
            format="json",
        )
        assert r_invalid.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert r_invalid.json()["error_code"] == "ERR_MISSING_CUSTOMER_ID"

        # Suspended tenant
        self.client.force_authenticate(user=self.suspended_user)
        r_susp = self.client.post(
            "/api/v2/parties/reconciliation/reconcile-customer/",
            {"customer_id": self.customer_1.customer_id},
            format="json",
        )
        assert r_susp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_402_PAYMENT_REQUIRED)
        assert r_susp.json()["error_code"] in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")
