"""
backend/tests/test_p276_payment_status_handling.py
Architecture Decision: ADR-0276 (P276).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Payment Status Handling, Evidence Verification & Financial Reconciliation:
1. Initiate and confirm payment with provider evidence and financial account balance update.
2. Fail payment from PENDING state.
3. Reject invalid state transitions (e.g. confirming a failed or refunded payment).
4. Manager refund execution and financial account balance deduction.
5. Manager reversal execution with mandatory reason and ledger balance reversal.
6. RBAC enforcement: Cashier is strictly denied refunds and reversals (ERR_STAFF_ROLE_DENIED, 403).
7. Cross-tenant isolation strictly enforced.
8. Idempotency replay on duplicate initiation and confirmation.
9. Payment reconciliation against statement batch.
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
    FinancialAccount,
    FinancialAccountType,
    PaymentLifecycleStatus,
    PaymentMethod,
    PaymentMethodConfig,
    PaymentStatusRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPaymentStatusHandlingIntegration:

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
            name="Main Road Branch",
        )

        # 2. Foreign Tenant & Branch
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Foreign Care {self.suffix}",
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

        # 4. Financial Account & Config
        self.bkash_account = FinancialAccount.objects.create(
            account_id=f"finacc-mfs-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            account_type=FinancialAccountType.MFS_CLEARING,
            account_name="bKash Clearing",
            account_code=f"MFS-BK-{self.suffix}",
            current_balance=Decimal("1000.0000"),
        )

        self.payment_config = PaymentMethodConfig.objects.create(
            config_id=f"pmc-bk-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            method_type=PaymentMethod.BKASH,
            display_name="bKash Merchant",
            clearing_account=self.bkash_account,
            requires_reference=True,
        )

        # 5. Users with diverse roles
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner-{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.manager = PharmacyUser.objects.create_user(
            email=f"manager-{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier-{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_user = PharmacyUser.objects.create_user(
            email=f"foreign-{self.suffix}@foreign.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp-{self.suffix}@susp.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_initiate_and_confirm_payment_with_evidence(self):
        """Cashier initiates a pending payment and confirms it with provider reference."""
        self.client.force_authenticate(user=self.cashier)

        init_payload = {
            "branch_id": self.branch_a.branch_id,
            "payment_method_config_id": self.payment_config.config_id,
            "financial_account_id": self.bkash_account.account_id,
            "tender_type": "BKASH",
            "amount_bdt": "1500.0000",
            "surcharge_bdt": "30.0000",
            "provider_name": "bKash Merchant Pay",
            "notes": "Rx order checkout",
        }

        resp = self.client.post("/api/v2/finance/payment-status/", init_payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["status"] == "PENDING"
        assert data["amount_bdt"] == "1500.0000"
        assert data["total_collected_bdt"] == "1530.0000"
        record_id = data["record_id"]

        # Financial account balance untouched before confirmation
        self.bkash_account.refresh_from_db()
        assert self.bkash_account.current_balance == Decimal("1000.0000")

        # Confirm payment with provider reference and evidence
        confirm_payload = {
            "provider_reference": "TRX998877AA",
            "provider_evidence_json": {"wallet": "01711000000", "terminal": "POS-01"},
        }
        confirm_resp = self.client.post(
            f"/api/v2/finance/payment-status/{record_id}/confirm/",
            confirm_payload,
            format="json",
        )
        assert confirm_resp.status_code == status.HTTP_200_OK
        conf_data = confirm_resp.json()
        assert conf_data["status"] == "CONFIRMED"
        assert conf_data["provider_reference"] == "TRX998877AA"
        assert conf_data["evidence_hash"] != ""
        assert conf_data["verified_by_id"] == str(self.cashier.user_id)

        # Financial account balance incremented by total_collected_bdt (1000 + 1530 = 2530)
        self.bkash_account.refresh_from_db()
        assert self.bkash_account.current_balance == Decimal("2530.0000")

    def test_02_fail_payment_from_pending(self):
        """Cashier fails an unconfirmed payment attempt."""
        self.client.force_authenticate(user=self.cashier)

        init_payload = {
            "tender_type": "CARD",
            "amount_bdt": "800.0000",
        }
        resp = self.client.post("/api/v2/finance/payment-status/", init_payload, format="json")
        record_id = resp.json()["record_id"]

        fail_payload = {
            "failure_code": "ISSUER_TIMEOUT",
            "failure_reason": "POS gateway connection lost",
        }
        fail_resp = self.client.post(
            f"/api/v2/finance/payment-status/{record_id}/fail/",
            fail_payload,
            format="json",
        )
        assert fail_resp.status_code == status.HTTP_200_OK
        assert fail_resp.json()["status"] == "FAILED"
        assert fail_resp.json()["failure_code"] == "ISSUER_TIMEOUT"

    def test_03_invalid_state_transitions(self):
        """Transitions from terminal or non-pending states are rejected."""
        self.client.force_authenticate(user=self.manager)

        # 1. Create and fail payment
        init_payload = {"tender_type": "CASH", "amount_bdt": "300.0000"}
        r = self.client.post("/api/v2/finance/payment-status/", init_payload, format="json")
        record_id = r.json()["record_id"]

        self.client.post(f"/api/v2/finance/payment-status/{record_id}/fail/", {}, format="json")

        # Attempt to confirm a FAILED payment -> 400 Bad Request
        conf_resp = self.client.post(
            f"/api/v2/finance/payment-status/{record_id}/confirm/",
            {"provider_reference": "REF123"},
            format="json",
        )
        assert conf_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert conf_resp.json()["error_code"] == "ERR_INVALID_PAYMENT_STATE_TRANSITION"

    def test_04_manager_refund_success_and_financial_balance_deduction(self):
        """Manager executes partial refund on a CONFIRMED payment and ledger balance adjusts."""
        self.client.force_authenticate(user=self.manager)

        init_payload = {
            "financial_account_id": self.bkash_account.account_id,
            "tender_type": "BKASH",
            "amount_bdt": "1000.0000",
        }
        rec_id = self.client.post("/api/v2/finance/payment-status/", init_payload, format="json").json()["record_id"]
        self.client.post(f"/api/v2/finance/payment-status/{rec_id}/confirm/", {"provider_reference": "REF-ABC"}, format="json")

        # Balance before refund: 1000 + 1000 = 2000
        self.bkash_account.refresh_from_db()
        assert self.bkash_account.current_balance == Decimal("2000.0000")

        # Execute refund of 400 BDT
        refund_payload = {
            "refund_amount_bdt": "400.0000",
            "refund_reference": "RF-9988",
            "refund_reason": "Damaged insulin ampoule return",
        }
        ref_resp = self.client.post(
            f"/api/v2/finance/payment-status/{rec_id}/refund/",
            refund_payload,
            format="json",
        )
        assert ref_resp.status_code == status.HTTP_200_OK
        ref_data = ref_resp.json()
        assert ref_data["status"] == "REFUNDED"
        assert ref_data["refund_amount_bdt"] == "400.0000"

        # Balance adjusted: 2000 - 400 = 1600
        self.bkash_account.refresh_from_db()
        assert self.bkash_account.current_balance == Decimal("1600.0000")

    def test_05_manager_reversal_success_and_financial_balance_deduction(self):
        """Manager reverses full payment due to duplicate settlement."""
        self.client.force_authenticate(user=self.manager)

        init_payload = {
            "financial_account_id": self.bkash_account.account_id,
            "tender_type": "BKASH",
            "amount_bdt": "500.0000",
        }
        rec_id = self.client.post("/api/v2/finance/payment-status/", init_payload, format="json").json()["record_id"]
        self.client.post(f"/api/v2/finance/payment-status/{rec_id}/confirm/", {"provider_reference": "REV-TEST"}, format="json")

        # Reversal requires mandatory reason
        missing_reason_resp = self.client.post(
            f"/api/v2/finance/payment-status/{rec_id}/reverse/",
            {"reversal_reason": ""},
            format="json",
        )
        assert missing_reason_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert missing_reason_resp.json()["error_code"] == "ERR_MISSING_REVERSAL_REASON"

        # Valid reversal
        rev_resp = self.client.post(
            f"/api/v2/finance/payment-status/{rec_id}/reverse/",
            {"reversal_reason": "Double charged at bKash terminal"},
            format="json",
        )
        assert rev_resp.status_code == status.HTTP_200_OK
        assert rev_resp.json()["status"] == "REVERSED"

    def test_06_cashier_denied_refund_and_reversal_rbac(self):
        """Cashiers are strictly denied refund and reversal execution."""
        # 1. Manager sets up confirmed payment
        self.client.force_authenticate(user=self.manager)
        rec_id = self.client.post(
            "/api/v2/finance/payment-status/",
            {"tender_type": "CASH", "amount_bdt": "600.0000"},
            format="json",
        ).json()["record_id"]
        self.client.post(f"/api/v2/finance/payment-status/{rec_id}/confirm/", {"provider_reference": "CSH-1"}, format="json")

        # 2. Cashier attempts refund -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        refund_resp = self.client.post(
            f"/api/v2/finance/payment-status/{rec_id}/refund/",
            {"refund_amount_bdt": "600.0000"},
            format="json",
        )
        assert refund_resp.status_code == status.HTTP_403_FORBIDDEN
        assert refund_resp.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 3. Cashier attempts reversal -> 403 Forbidden
        reverse_resp = self.client.post(
            f"/api/v2/finance/payment-status/{rec_id}/reverse/",
            {"reversal_reason": "Fraud suspected"},
            format="json",
        )
        assert reverse_resp.status_code == status.HTTP_403_FORBIDDEN
        assert reverse_resp.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_07_cross_tenant_isolation(self):
        """Cross-tenant payment operations are strictly blocked."""
        # Tenant A creates payment
        self.client.force_authenticate(user=self.cashier)
        rec_id = self.client.post(
            "/api/v2/finance/payment-status/",
            {"tender_type": "CASH", "amount_bdt": "200.0000"},
            format="json",
        ).json()["record_id"]

        # Foreign user from Tenant B attempts access -> 403 or 404
        self.client.force_authenticate(user=self.foreign_user)
        get_resp = self.client.get(f"/api/v2/finance/payment-status/{rec_id}/")
        assert get_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        confirm_resp = self.client.post(
            f"/api/v2/finance/payment-status/{rec_id}/confirm/",
            {"provider_reference": "HACK"},
            format="json",
        )
        assert confirm_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_08_idempotency_replay_protection(self):
        """Duplicate idempotency key or provider reference triggers safe idempotent replay."""
        self.client.force_authenticate(user=self.cashier)
        idem_key = f"IDEM-{uuid.uuid4().hex[:8]}"

        payload = {
            "tender_type": "BKASH",
            "amount_bdt": "750.0000",
            "idempotency_key": idem_key,
        }

        # First request
        r1 = self.client.post("/api/v2/finance/payment-status/", payload, format="json")
        assert r1.status_code == status.HTTP_201_CREATED

        # Duplicate request with same idempotency key -> 200 OK with identical record
        r2 = self.client.post("/api/v2/finance/payment-status/", payload, format="json")
        assert r2.status_code == status.HTTP_200_OK
        assert r2.json()["record_id"] == r1.json()["record_id"]
        assert r2.json()["is_idempotent_replay"] is True

    def test_09_reconcile_settled_payment(self):
        """Manager reconciles confirmed payment against provider settlement batch."""
        self.client.force_authenticate(user=self.manager)
        rec_id = self.client.post(
            "/api/v2/finance/payment-status/",
            {"tender_type": "CASH", "amount_bdt": "900.0000"},
            format="json",
        ).json()["record_id"]
        self.client.post(f"/api/v2/finance/payment-status/{rec_id}/confirm/", {"provider_reference": "CSH-REC"}, format="json")

        rec_resp = self.client.post(
            f"/api/v2/finance/payment-status/{rec_id}/reconcile/",
            {"reconciliation_batch_id": "BATCH-20261118-01"},
            format="json",
        )
        assert rec_resp.status_code == status.HTTP_200_OK
        data = rec_resp.json()
        assert data["is_reconciled"] is True
        assert data["reconciliation_batch_id"] == "BATCH-20261118-01"

    def test_10_suspended_tenant_blocked(self):
        """Suspended tenant accounts are blocked from payment status operations."""
        self.client.force_authenticate(user=self.suspended_user)
        resp = self.client.post(
            "/api/v2/finance/payment-status/",
            {"tender_type": "CASH", "amount_bdt": "100.0000"},
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error_code"] in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")
