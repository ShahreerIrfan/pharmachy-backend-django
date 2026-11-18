"""
backend/tests/test_p277_customer_due_collection_mobile.py
Architecture Decision: ADR-0277 (P277).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Mobile Owner Customer Due Review, Collection, Digital Receipts & Audits:
1. Owner reviews outstanding dues list and summary KPI metrics.
2. Successful mobile due collection with digital receipt and ledger synchronization.
3. Full settlement of due record (status -> SETTLED).
4. Overpayment prevention (ERR_DUE_OVERPAYMENT, 400).
5. Duplicate settlement prevention on already settled due (ERR_DUE_ALREADY_SETTLED, 400).
6. RBAC enforcement: Cashiers and staff pharmacists strictly denied (ERR_STAFF_ROLE_DENIED, 403).
7. Cross-tenant isolation strictly enforced.
8. Offline collection refusal (ERR_OFFLINE_UNAVAILABLE, 400).
9. Idempotency replay protection on duplicate submissions.
10. Suspended tenant account write protection.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueInvoiceAllocation,
    CustomerDueRecord,
    DueStatus,
    FinancialAccount,
    FinancialAccountType,
    PaymentMethod,
    PosCustomer,
    SaleInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestMobileCustomerDueCollectionIntegration:

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

        # 4. Financial Account
        self.bkash_account = FinancialAccount.objects.create(
            account_id=f"finacc-bk-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            account_type=FinancialAccountType.MFS_CLEARING,
            account_name="Owner bKash Settlement",
            account_code=f"MFS-BK-{self.suffix}",
            current_balance=Decimal("5000.0000"),
        )

        # 5. Customers
        self.customer_a = PosCustomer.objects.create(
            customer_id=f"cust-a-{self.suffix}",
            tenant=self.tenant_a,
            full_name="Rafiqul Islam",
            phone=f"+880171{random.randint(1000000, 9999999)}",
            current_due_bdt=Decimal("2500.00"),
        )

        # 6. Customer Due Records
        self.due_1 = CustomerDueRecord.objects.create(
            due_id=f"DUE-01-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_a,
            customer_name=self.customer_a.full_name,
            customer_phone=self.customer_a.phone,
            total_due_bdt=Decimal("1500.00"),
            paid_amount_bdt=Decimal("0.00"),
            remaining_due_bdt=Decimal("1500.00"),
            status=DueStatus.OUTSTANDING,
            notes="Credit sale for antibiotics",
        )

        self.due_2 = CustomerDueRecord.objects.create(
            due_id=f"DUE-02-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            customer=self.customer_a,
            customer_name=self.customer_a.full_name,
            customer_phone=self.customer_a.phone,
            total_due_bdt=Decimal("1000.00"),
            paid_amount_bdt=Decimal("0.00"),
            remaining_due_bdt=Decimal("1000.00"),
            status=DueStatus.OUTSTANDING,
            notes="Credit sale for chronic medicines",
        )

        # Foreign due for Tenant B
        self.foreign_due = CustomerDueRecord.objects.create(
            due_id=f"DUE-FOR-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            customer_name="Foreign Customer",
            customer_phone="+8801819999999",
            total_due_bdt=Decimal("500.00"),
            remaining_due_bdt=Decimal("500.00"),
            status=DueStatus.OUTSTANDING,
        )

        # 7. Users
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@popular.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@popular.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.foreign_user = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@foreign.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Foreign Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@susp.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_owner_review_outstanding_dues_and_summary(self):
        """Owner reviews open customer dues list and checks summary KPIs."""
        self.client.force_authenticate(user=self.owner)

        # 1. List dues
        list_resp = self.client.get("/api/v2/mobile/due-collections/")
        assert list_resp.status_code == status.HTTP_200_OK
        data = list_resp.json()
        assert data["count"] == 2
        assert len(data["dues"]) == 2

        # 2. Get summary
        summary_resp = self.client.get("/api/v2/mobile/due-collections/summary/")
        assert summary_resp.status_code == status.HTTP_200_OK
        sum_data = summary_resp.json()
        assert sum_data["total_outstanding_due_bdt"] == "2500.00"
        assert sum_data["total_due_accounts_count"] == 2

    def test_02_successful_mobile_due_collection_and_receipt(self):
        """Owner records partial due collection via bKash and receives digital receipt."""
        self.client.force_authenticate(user=self.owner)

        payload = {
            "due_id": self.due_1.due_id,
            "amount_bdt": "600.00",
            "tender_type": "BKASH",
            "gateway_reference": "TRX998877AA",
            "financial_account_id": self.bkash_account.account_id,
            "notes": "Collected at clinic visit",
        }

        resp = self.client.post("/api/v2/mobile/due-collections/collect/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        receipt = resp.json()
        assert receipt["success"] is True
        assert receipt["amount_collected_bdt"] == "600.00"
        assert receipt["remaining_due_bdt"] == "900.00"
        assert receipt["due_status"] == "PARTIAL"
        assert receipt["receipt_number"].startswith("REC-DUE-")

        # Verify CustomerDueRecord updated
        self.due_1.refresh_from_db()
        assert self.due_1.paid_amount_bdt == Decimal("600.00")
        assert self.due_1.remaining_due_bdt == Decimal("900.00")
        assert self.due_1.status == DueStatus.PARTIAL

        # Verify Customer balance reduced: 2500 - 600 = 1900
        self.customer_a.refresh_from_db()
        assert self.customer_a.current_due_bdt == Decimal("1900.00")

        # Verify FinancialAccount credited: 5000 + 600 = 5600
        self.bkash_account.refresh_from_db()
        assert self.bkash_account.current_balance == Decimal("5600.0000")

        # Verify CustomerDueCollection record and allocation
        col = CustomerDueCollection.objects.get(collection_id=receipt["collection_id"])
        assert col.amount_bdt == Decimal("600.00")
        assert col.verification_channel == "MOBILE_OWNER_APP"

        alloc = CustomerDueInvoiceAllocation.objects.get(collection=col)
        assert alloc.allocated_amount_bdt == Decimal("600.00")
        assert alloc.previous_remaining_due_bdt == Decimal("1500.00")
        assert alloc.new_remaining_due_bdt == Decimal("900.00")

    def test_03_full_due_settlement(self):
        """Owner collects full remaining due and marks due status SETTLED."""
        self.client.force_authenticate(user=self.owner)

        payload = {
            "due_id": self.due_2.due_id,
            "amount_bdt": "1000.00",
            "tender_type": "CASH",
        }

        resp = self.client.post("/api/v2/mobile/due-collections/collect/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        receipt = resp.json()
        assert receipt["remaining_due_bdt"] == "0.00"
        assert receipt["due_status"] == "SETTLED"

        self.due_2.refresh_from_db()
        assert self.due_2.remaining_due_bdt == Decimal("0.00")
        assert self.due_2.status == DueStatus.SETTLED

    def test_04_overpayment_prohibition(self):
        """Collecting an amount exceeding remaining due is strictly rejected with ERR_DUE_OVERPAYMENT."""
        self.client.force_authenticate(user=self.owner)

        payload = {
            "due_id": self.due_1.due_id,
            "amount_bdt": "1600.00",  # remaining is 1500.00
            "tender_type": "CASH",
        }

        resp = self.client.post("/api/v2/mobile/due-collections/collect/", payload, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error_code"] == "ERR_DUE_OVERPAYMENT"

    def test_05_duplicate_settlement_rejection(self):
        """Attempting to collect on an already settled due is rejected with ERR_DUE_ALREADY_SETTLED."""
        self.client.force_authenticate(user=self.owner)

        # 1. Settle due_2
        self.client.post(
            "/api/v2/mobile/due-collections/collect/",
            {"due_id": self.due_2.due_id, "amount_bdt": "1000.00", "tender_type": "CASH"},
            format="json",
        )

        # 2. Attempt second collection -> 400 Bad Request
        resp = self.client.post(
            "/api/v2/mobile/due-collections/collect/",
            {"due_id": self.due_2.due_id, "amount_bdt": "100.00", "tender_type": "CASH"},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error_code"] == "ERR_DUE_ALREADY_SETTLED"

    def test_06_cashier_and_pharmacist_denied_rbac(self):
        """Cashier and Staff Pharmacist are denied from mobile owner due review & collection."""
        for user in [self.cashier, self.pharmacist]:
            self.client.force_authenticate(user=user)

            # Denied on list
            list_resp = self.client.get("/api/v2/mobile/due-collections/")
            assert list_resp.status_code == status.HTTP_403_FORBIDDEN
            assert list_resp.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

            # Denied on collect
            collect_resp = self.client.post(
                "/api/v2/mobile/due-collections/collect/",
                {"due_id": self.due_1.due_id, "amount_bdt": "200.00", "tender_type": "CASH"},
                format="json",
            )
            assert collect_resp.status_code == status.HTTP_403_FORBIDDEN
            assert collect_resp.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_07_cross_tenant_isolation(self):
        """Foreign user from Tenant B cannot view or collect Tenant A's due."""
        self.client.force_authenticate(user=self.foreign_user)

        # Foreign user tries to collect Tenant A's due -> 403 or 404
        resp = self.client.post(
            "/api/v2/mobile/due-collections/collect/",
            {"due_id": self.due_1.due_id, "amount_bdt": "100.00", "tender_type": "CASH"},
            format="json",
        )
        assert resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_08_offline_collection_rejection(self):
        """Collections flagged with is_offline are rejected to guarantee live AR ledger state."""
        self.client.force_authenticate(user=self.owner)

        payload = {
            "due_id": self.due_1.due_id,
            "amount_bdt": "200.00",
            "tender_type": "CASH",
            "is_offline": True,
        }

        resp = self.client.post("/api/v2/mobile/due-collections/collect/", payload, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error_code"] == "ERR_OFFLINE_UNAVAILABLE"

    def test_09_idempotency_replay(self):
        """Duplicate idempotency key triggers safe idempotent replay without double collection."""
        self.client.force_authenticate(user=self.owner)
        idem_key = f"IDEM-MOB-{uuid.uuid4().hex[:8]}"

        payload = {
            "due_id": self.due_1.due_id,
            "amount_bdt": "300.00",
            "tender_type": "CASH",
            "idempotency_key": idem_key,
        }

        # First request
        r1 = self.client.post("/api/v2/mobile/due-collections/collect/", payload, format="json")
        assert r1.status_code == status.HTTP_201_CREATED

        # Duplicate request
        r2 = self.client.post("/api/v2/mobile/due-collections/collect/", payload, format="json")
        assert r2.status_code == status.HTTP_200_OK
        assert r2.json()["collection_id"] == r1.json()["collection_id"]
        assert r2.json()["is_idempotent_replay"] is True

    def test_10_suspended_tenant_blocked(self):
        """Suspended tenant accounts are blocked from mobile due collection operations."""
        self.client.force_authenticate(user=self.suspended_user)

        resp = self.client.get("/api/v2/mobile/due-collections/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error_code"] in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")
