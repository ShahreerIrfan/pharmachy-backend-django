"""
backend/tests/test_p088_manual_payment.py
Architecture Decision: ADR-0013, ADR-0027, ADR-0055, ADR-0087 & ADR-0088
Phase 05: Basic/Standard/Pro Subscription & Billing (P088)

Comprehensive Integration Tests for Manual Payment Verification with Maker-Checker Approval:
1. Valid Scenario (Submission & Approval): Maker submits claim with evidence -> Distinct Checker approves -> Invoice settled to PAID -> Subscription extended -> Activation outcomes recorded.
2. Maker-Checker Invariant: Maker cannot self-approve their own claim (ERR_MAKER_CHECKER_VIOLATION).
3. Audited Rejection Flow: Checker rejects claim with mandatory reason -> Claim REJECTED -> Invoice remains payable -> Subscription unchanged.
4. Amount Mismatch Rejection: Claimed amount not matching invoice total rejected with 400 Bad Request.
5. Duplicate Pending Claim Prevention: Multiple pending claims for the same invoice blocked with 400.
6. Paid Invoice Claim Prevention: Submitting claim for an already settled invoice rejected with 400.
7. Cross-Tenant Security Barrier: Tenant B owner cannot view or cancel Tenant A's payment claims (ERR_CROSS_TENANT_VIOLATION).
8. RBAC Gate: Regular pharmacy staff cannot approve or reject claims (HTTP 403 Forbidden).
9. Maker Self-Cancellation: Maker can cancel their own pending claim before verification.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PlanTier,
    SubscriptionRecord,
    Tenant,
)
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.renewal_invoice_service import RenewalInvoiceService
from apps.identity.models import PharmacyUser, UserRole


class TestP088ManualPayment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin Checker 1
        cls.superadmin_checker_1, _ = PharmacyUser.objects.get_or_create(
            email=f"p088_checker1_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Checker One {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Platform SuperAdmin Checker 2 (for maker-checker self-approval test)
        cls.superadmin_checker_2, _ = PharmacyUser.objects.get_or_create(
            email=f"p088_checker2_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Checker Two {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 3. Provision Tenant A (Standard Plan)
        cls.subdomain_a = f"pharma-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-pay-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P088-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"maker_a_{cls.suffix}@pharmaa.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Pharmacy A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner Maker A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Main Branch A",
            branch_license=cls.license_a,
            is_trial=False,
            operator_id=str(cls.superadmin_checker_1.user_id),
        )

        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.branch_a = Branch.objects.get(tenant=cls.tenant_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)
        cls.sub_a = SubscriptionRecord.objects.get(tenant=cls.tenant_a)

        # 4. Provision Tenant B
        cls.subdomain_b = f"pharma-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-pay-b-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"maker_b_{cls.suffix}@pharmab.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Pharmacy B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner Maker B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Head Branch B",
            branch_license=f"DGDA-DL-P088-B-{cls.suffix}",
            is_trial=False,
            operator_id=str(cls.superadmin_checker_1.user_id),
        )

        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

    def setUp(self):
        self.client = APIClient()

    def test_01_valid_manual_payment_submission_and_checker_approval(self):
        """Maker submits claim with evidence; distinct checker approves; invoice settled & renewed."""
        # 1. Generate renewal invoice for Tenant A (STANDARD = 6500 + 5% VAT = 6825.00 BDT)
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_a.email,
        )
        self.assertEqual(inv.total_bdt, Decimal("6825.00"))

        # 2. Maker (Owner A) submits manual payment claim
        self.client.force_authenticate(user=self.owner_a)
        claim_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "invoice_id": inv.invoice_id,
            "payment_method": "BKASH",
            "transaction_reference": f"BKASH-TRX-{self.suffix}-01",
            "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            "claimed_amount_bdt": "6825.00",
            "evidence_file_url": "https://storage.pharmasaas.internal/evidence/bkash-slip-01.png",
            "evidence_notes": "Paid via bKash Merchant counter on Sunday",
        }
        res_submit = self.client.post("/api/v2/billing/payment-claims/submit/", claim_payload, format="json")
        self.assertEqual(res_submit.status_code, status.HTTP_201_CREATED)
        claim_data = res_submit.json()["data"]
        claim_id = claim_data["claim_id"]

        self.assertEqual(claim_data["status"], "PENDING_VERIFICATION")
        self.assertEqual(claim_data["payment_method"], "BKASH")
        self.assertEqual(claim_data["claimed_amount_bdt"], "6825.00")
        self.assertEqual(claim_data["maker_user_id"], str(self.owner_a.user_id))
        self.assertIsNone(claim_data["checker_user_id"])

        # 3. Checker (SuperAdmin 1) approves claim
        self.client.force_authenticate(user=self.superadmin_checker_1)
        res_approve = self.client.post(
            f"/api/v2/billing/payment-claims/{claim_id}/approve/",
            {"checker_notes": "Reconciled with bKash merchant statement."},
            format="json",
        )
        self.assertEqual(res_approve.status_code, status.HTTP_200_OK)
        approved_data = res_approve.json()["data"]

        self.assertEqual(approved_data["status"], "APPROVED")
        self.assertEqual(approved_data["checker_user_id"], str(self.superadmin_checker_1.user_id))
        self.assertIsNotNone(approved_data["verified_at"])

        # 4. Verify recorded activation outcomes
        outcomes = approved_data["activation_outcomes"]
        self.assertIsNotNone(outcomes)
        self.assertEqual(outcomes["invoice_id"], inv.invoice_id)
        self.assertEqual(outcomes["amount_bdt"], "6825.00")
        self.assertEqual(outcomes["new_renewal_date"], inv.period_end.isoformat())

        # 5. Verify database states: Invoice is PAID, Subscription renewal date extended
        inv.refresh_from_db()
        self.assertEqual(inv.status, "PAID")
        self.assertEqual(inv.payment_method, "BKASH")

        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.renewal_date, inv.period_end)

    def test_02_maker_checker_invariant_self_approval_prohibited(self):
        """A user who created the claim cannot self-approve their own claim."""
        # Create an invoice for Tenant A
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.superadmin_checker_1.email,
        )

        # SuperAdmin 1 acts as Maker submitting claim
        self.client.force_authenticate(user=self.superadmin_checker_1)
        claim_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "invoice_id": inv.invoice_id,
            "payment_method": "BANK_TRANSFER",
            "transaction_reference": f"EFT-TRX-{self.suffix}-SELF",
            "sender_account_number": "001122334455",
            "claimed_amount_bdt": str(inv.total_bdt),
            "evidence_file_url": "https://storage/bank-deposit.pdf",
        }
        res_submit = self.client.post("/api/v2/billing/payment-claims/submit/", claim_payload, format="json")
        self.assertEqual(res_submit.status_code, status.HTTP_201_CREATED)
        claim_id = res_submit.json()["data"]["claim_id"]

        # SuperAdmin 1 attempts to self-approve -> Blocked with 400 Maker-checker violation
        res_self_approve = self.client.post(f"/api/v2/billing/payment-claims/{claim_id}/approve/", {}, format="json")
        self.assertEqual(res_self_approve.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Maker-checker violation", res_self_approve.json()["message"])

        # Distinct SuperAdmin 2 can approve successfully
        self.client.force_authenticate(user=self.superadmin_checker_2)
        res_other_approve = self.client.post(
            f"/api/v2/billing/payment-claims/{claim_id}/approve/",
            {"checker_notes": "Approved by distinct auditor."},
            format="json",
        )
        self.assertEqual(res_other_approve.status_code, status.HTTP_200_OK)
        self.assertEqual(res_other_approve.json()["data"]["status"], "APPROVED")

    def test_03_audited_rejection_flow(self):
        """Checker rejects claim with mandatory reason; invoice remains open and unrenewed."""
        # 1. Create renewal invoice
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_a.email,
        )
        old_renewal_date = SubscriptionRecord.objects.get(tenant=self.tenant_a).renewal_date

        # 2. Maker submits claim
        self.client.force_authenticate(user=self.owner_a)
        res_submit = self.client.post(
            "/api/v2/billing/payment-claims/submit/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "invoice_id": inv.invoice_id,
                "payment_method": "NAGAD",
                "transaction_reference": f"NAGAD-TRX-{self.suffix}-REJ",
                "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "claimed_amount_bdt": str(inv.total_bdt),
                "evidence_file_url": "https://storage/nagad-slip.png",
            },
            format="json",
        )
        claim_id = res_submit.json()["data"]["claim_id"]

        # 3. Checker attempts rejection without reason -> Rejected with 400
        self.client.force_authenticate(user=self.superadmin_checker_1)
        res_bad_reject = self.client.post(f"/api/v2/billing/payment-claims/{claim_id}/reject/", {}, format="json")
        self.assertEqual(res_bad_reject.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_reject.json()["error_code"], "ERR_REJECTION_REASON_REQUIRED")

        # 4. Checker rejects with valid reason
        res_valid_reject = self.client.post(
            f"/api/v2/billing/payment-claims/{claim_id}/reject/",
            {"rejection_reason": "Transaction reference not found in Nagad daily ledger."},
            format="json",
        )
        self.assertEqual(res_valid_reject.status_code, status.HTTP_200_OK)
        rej_data = res_valid_reject.json()["data"]

        self.assertEqual(rej_data["status"], "REJECTED")
        self.assertEqual(rej_data["rejection_reason"], "Transaction reference not found in Nagad daily ledger.")

        # 5. Invoice remains in ISSUED status, subscription NOT renewed
        inv.refresh_from_db()
        self.assertEqual(inv.status, "ISSUED")

        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.renewal_date, old_renewal_date)

    def test_04_amount_mismatch_rejection(self):
        """Claimed amount not matching invoice total rejected with 400."""
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_a.email,
        )

        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "invoice_id": inv.invoice_id,
            "payment_method": "BKASH",
            "transaction_reference": f"BKASH-TRX-{self.suffix}-MISMATCH",
            "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            "claimed_amount_bdt": "5000.00",  # Incorrect amount
            "evidence_file_url": "https://storage/slip.png",
        }
        res = self.client.post("/api/v2/billing/payment-claims/submit/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not match invoice total", res.json()["message"])

    def test_05_duplicate_pending_claim_prevention(self):
        """Multiple active pending claims for the same invoice blocked."""
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_a.email,
        )

        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "invoice_id": inv.invoice_id,
            "payment_method": "BKASH",
            "transaction_reference": f"BKASH-TRX-{self.suffix}-DUP1",
            "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            "claimed_amount_bdt": str(inv.total_bdt),
            "evidence_file_url": "https://storage/slip1.png",
        }
        # First submission succeeds
        res1 = self.client.post("/api/v2/billing/payment-claims/submit/", payload, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Second submission on same open invoice fails
        payload["transaction_reference"] = f"BKASH-TRX-{self.suffix}-DUP2"
        res2 = self.client.post("/api/v2/billing/payment-claims/submit/", payload, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", res2.json()["message"])

    def test_06_paid_invoice_claim_prevention(self):
        """Submitting claim for an already paid invoice rejected with 400."""
        # Settle invoice first
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.superadmin_checker_1.email,
        )
        RenewalInvoiceService.settle_renewal_invoice(
            invoice_id=inv.invoice_id,
            payment_method="BKASH",
            payment_reference=f"SETTLED-DIRECT-{self.suffix}",
            actor_user=self.superadmin_checker_1,
        )

        # Attempt to submit claim for the settled invoice
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "invoice_id": inv.invoice_id,
            "payment_method": "BKASH",
            "transaction_reference": f"BKASH-TRX-{self.suffix}-LATE",
            "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            "claimed_amount_bdt": str(inv.total_bdt),
            "evidence_file_url": "https://storage/late.png",
        }
        res = self.client.post("/api/v2/billing/payment-claims/submit/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cannot accept manual payment claims", res.json()["message"])

    def test_07_cross_tenant_access_denied(self):
        """Owner B cannot view, cancel or approve Tenant A's payment claims (ERR_CROSS_TENANT_VIOLATION)."""
        # 1. Superadmin submits claim for Tenant A
        self.client.force_authenticate(user=self.superadmin_checker_1)
        inv_a = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.superadmin_checker_1.email,
        )
        res_submit = self.client.post(
            "/api/v2/billing/payment-claims/submit/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "invoice_id": inv_a.invoice_id,
                "payment_method": "BKASH",
                "transaction_reference": f"BKASH-TRX-{self.suffix}-ISO",
                "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "claimed_amount_bdt": str(inv_a.total_bdt),
                "evidence_file_url": "https://storage/iso.png",
            },
            format="json",
        )
        claim_id = res_submit.json()["data"]["claim_id"]

        # 2. Owner B attempts to inspect Tenant A's claim -> 403 Forbidden
        self.client.force_authenticate(user=self.owner_b)
        res_get = self.client.get(f"/api/v2/billing/payment-claims/{claim_id}/")
        self.assertEqual(res_get.status_code, status.HTTP_403_FORBIDDEN)
        err_code = res_get.json().get("error_code") or res_get.json().get("code")
        self.assertEqual(err_code, "ERR_CROSS_TENANT_VIOLATION")

        # 3. Owner B attempts to cancel Tenant A's claim -> 403 Forbidden
        res_cancel = self.client.post(f"/api/v2/billing/payment-claims/{claim_id}/cancel/", {}, format="json")
        self.assertEqual(res_cancel.status_code, status.HTTP_403_FORBIDDEN)

    def test_08_rbac_gate_tenant_user_cannot_approve(self):
        """Regular tenant owner/manager cannot approve payment claims."""
        # 1. Submit claim for Tenant B
        inv_b = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_b.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_b.email,
        )

        self.client.force_authenticate(user=self.owner_b)
        res_submit = self.client.post(
            "/api/v2/billing/payment-claims/submit/",
            {
                "tenant_id": self.tenant_b.tenant_id,
                "invoice_id": inv_b.invoice_id,
                "payment_method": "BKASH",
                "transaction_reference": f"BKASH-TRX-{self.suffix}-RBAC",
                "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "claimed_amount_bdt": str(inv_b.total_bdt),
                "evidence_file_url": "https://storage/rbac.png",
            },
            format="json",
        )
        claim_id = res_submit.json()["data"]["claim_id"]

        # 2. Owner B attempts to approve claim -> 403 Forbidden
        res_approve = self.client.post(f"/api/v2/billing/payment-claims/{claim_id}/approve/", {}, format="json")
        self.assertEqual(res_approve.status_code, status.HTTP_403_FORBIDDEN)

    def test_09_maker_self_cancellation(self):
        """Maker can cancel their own pending payment claim."""
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_b.tenant_id,
            billing_cycle="ANNUAL",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_b.email,
        )

        self.client.force_authenticate(user=self.owner_b)
        res_submit = self.client.post(
            "/api/v2/billing/payment-claims/submit/",
            {
                "tenant_id": self.tenant_b.tenant_id,
                "invoice_id": inv.invoice_id,
                "payment_method": "ROCKET",
                "transaction_reference": f"ROCKET-TRX-{self.suffix}-CANCEL",
                "sender_account_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "claimed_amount_bdt": str(inv.total_bdt),
                "evidence_file_url": "https://storage/cancel.png",
            },
            format="json",
        )
        self.assertEqual(res_submit.status_code, status.HTTP_201_CREATED)
        claim_id = res_submit.json()["data"]["claim_id"]

        # Maker cancels
        res_cancel = self.client.post(f"/api/v2/billing/payment-claims/{claim_id}/cancel/", {}, format="json")
        self.assertEqual(res_cancel.status_code, status.HTTP_200_OK)
        self.assertEqual(res_cancel.json()["data"]["status"], "CANCELLED")

        # Claim can no longer be approved
        self.client.force_authenticate(user=self.superadmin_checker_1)
        res_approve = self.client.post(f"/api/v2/billing/payment-claims/{claim_id}/approve/", {}, format="json")
        self.assertEqual(res_approve.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cannot be approved", res_approve.json()["message"])
