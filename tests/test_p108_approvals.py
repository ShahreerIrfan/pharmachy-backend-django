"""
Backend Integration Tests for P108 Manager and Supervisor Approvals (Maker-Checker).
Architecture Decision: ADR-0108.
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Verifies:
- Dual-Control Segregation (Maker != Checker)
- High Discounts, Sale Refunds, Stock Write-offs, Sensitive Expenses, Clinical Escalations
- Role-specific authority gates (Cashier, Staff Pharmacist, Finance Officer, Branch Manager, Owner)
- Branch assignment restrictions
- Approval token generation and verification
- Cancellation workflows and audit trails
- Cross-tenant security barriers and suspended subscription blocks
"""

import random
import uuid

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    ApprovalAuditEvent,
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole


class MakerCheckerApprovalIntegrationTests(TestCase):
    """Integration test suite verifying P108 Maker-Checker workflows and invariants."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Primary Tenant & Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p108-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-p108-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name=f"Dhanmondi Branch {self.suffix}",
            license_number=f"DGDA-DL-A-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
            is_main_branch=True,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant,
            name=f"Gulshan Branch {self.suffix}",
            license_number=f"DGDA-DL-B-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
            is_main_branch=False,
        )

        # 2. Foreign Tenant for Isolation Tests
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"tn-foreign-{self.suffix}",
            name=f"Foreign Pharma {self.suffix}",
            subdomain=f"for-p108-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-foreign-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Store",
            license_number=f"DGDA-DL-FOR-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 3. Users in Primary Tenant
        # Owner
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )
        # Branch Manager A
        self.manager_a = PharmacyUser.objects.create_user(
            email=f"mgr_a_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        # Branch Manager B
        self.manager_b = PharmacyUser.objects.create_user(
            email=f"mgr_b_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Manager Gulshan",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        # Staff Pharmacist
        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Staff Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        # Finance Officer
        self.finance = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Finance Officer",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        # Frontline Cashier
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Frontline Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. Foreign User
        self.foreign_user = PharmacyUser.objects.create_user(
            email=f"f_user_{self.suffix}@foreign.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Foreign Manager",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

    def test_01_submit_high_discount_request_by_cashier(self):
        """Cashier successfully submits a HIGH_DISCOUNT approval request (MAKER)."""
        self.client.force_authenticate(user=self.cashier)

        res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "15% Chronic Refill Discount",
            "reason": "Cancer patient requesting compassionate discount for monthly cycle.",
            "amount_bdt": "1500.00",
            "discount_percent": "15.00",
            "urgency": "URGENT",
            "payload": {"invoice_ref": "INV-DHK-9901"},
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        data = res.json()
        self.assertTrue(data["request_id"].startswith("appr-"))
        self.assertEqual(data["status"], "PENDING")
        self.assertEqual(data["maker_username"], self.cashier.email)
        self.assertIsNone(data["checker_username"])
        self.assertEqual(data["amount_bdt"], 1500.0)
        self.assertEqual(data["discount_percent"], 15.0)

        # Verify audit log created
        audits = ApprovalAuditEvent.objects.filter(approval_request_id=data["request_id"])
        self.assertEqual(audits.count(), 1)
        self.assertEqual(audits.first().action, "SUBMITTED")

    def test_02_maker_checker_self_approval_strictly_prohibited(self):
        """Dual-Control Invariant: User who submitted request cannot approve their own request."""
        # Manager creates a stock write-off request as Maker
        self.client.force_authenticate(user=self.manager_a)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "STOCK_WRITE_OFF",
            "title": "Damaged Ampoules Box",
            "reason": "Accidental drop during unloading; 10 ampoules shattered.",
            "amount_bdt": "450.00",
        }, format="json")
        self.assertEqual(create_res.status_code, status.HTTP_201_CREATED)
        req_id = create_res.json()["request_id"]

        # Manager attempts to self-approve
        approve_res = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {
            "decision_notes": "Self approved as branch manager",
        }, format="json")

        self.assertEqual(approve_res.status_code, status.HTTP_403_FORBIDDEN)
        data = approve_res.json()
        self.assertEqual(data["error_code"], "ERR_CANNOT_APPROVE_OWN_REQUEST")
        self.assertTrue("মেকার-চেকার লঙ্ঘন" in data["message_bn"])

    def test_03_branch_manager_approves_discount_request_generates_token(self):
        """Checker (Branch Manager) approves cashier's request; token is generated."""
        # 1. Cashier creates request
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "12% Corporate Card Discount",
            "reason": "Corporate employee presented valid ID card.",
            "amount_bdt": "600.00",
            "discount_percent": "12.00",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # 2. Branch Manager A approves
        self.client.force_authenticate(user=self.manager_a)
        approve_res = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {
            "decision_notes": "Verified corporate agreement terms. Approved.",
        }, format="json")

        self.assertEqual(approve_res.status_code, status.HTTP_200_OK)
        data = approve_res.json()
        self.assertEqual(data["status"], "APPROVED")
        self.assertEqual(data["checker_username"], self.manager_a.email)
        self.assertIsNotNone(data["approval_token"])
        self.assertTrue(data["approval_token"].startswith("tok_appr_"))

        # Verify audit log has APPROVED event
        audits = ApprovalAuditEvent.objects.filter(approval_request_id=req_id)
        self.assertEqual(audits.count(), 2)
        self.assertEqual(audits.filter(action="APPROVED").count(), 1)

    def test_04_verify_token_endpoint(self):
        """Verifies approval token endpoint returns authorization parameters."""
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "REFUND",
            "title": "Counter Void Refund",
            "reason": "Customer changed mind before leaving dispensary.",
            "amount_bdt": "220.00",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # Manager approves
        self.client.force_authenticate(user=self.manager_a)
        app_res = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {
            "decision_notes": "Cash returned to customer. Approved.",
        }, format="json")
        token = app_res.json()["approval_token"]

        # Verify token via API
        verify_res = self.client.post("/api/v2/approvals/verify-token/", {
            "approval_token": token,
        }, format="json")
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        v_data = verify_res.json()
        self.assertTrue(v_data["valid"])
        self.assertEqual(v_data["request_id"], req_id)
        self.assertEqual(v_data["approval_type"], "REFUND")
        self.assertEqual(v_data["amount_bdt"], 220.0)

        # Invalid token test
        bad_res = self.client.post("/api/v2/approvals/verify-token/", {
            "approval_token": "non-existent-token",
        }, format="json")
        self.assertEqual(bad_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(bad_res.json()["valid"])

    def test_05_checker_rejects_refund_with_mandatory_reason(self):
        """Checker rejects request with explanation; status becomes REJECTED."""
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "REFUND",
            "title": "Suspicious Return Without Receipt",
            "reason": "Customer brought open syrup bottle without any invoice.",
            "amount_bdt": "150.00",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # Attempt rejection without reason -> blocked
        self.client.force_authenticate(user=self.manager_a)
        empty_rej = self.client.post(f"/api/v2/approvals/{req_id}/reject/", {
            "rejection_reason": "No",
        }, format="json")
        self.assertEqual(empty_rej.status_code, status.HTTP_400_BAD_REQUEST)

        # Valid rejection
        rej_res = self.client.post(f"/api/v2/approvals/{req_id}/reject/", {
            "rejection_reason": "Policy strictly forbids open liquid medicine refunds without purchase receipt.",
        }, format="json")
        self.assertEqual(rej_res.status_code, status.HTTP_200_OK)
        data = rej_res.json()
        self.assertEqual(data["status"], "REJECTED")
        self.assertEqual(data["checker_username"], self.manager_a.email)
        self.assertIsNone(data["approval_token"])

    def test_06_maker_cancels_pending_request(self):
        """Maker can cancel their own pending request before checker takes action."""
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "Accidental Discount Request",
            "reason": "Mistyped discount rate; will submit correct request.",
            "amount_bdt": "100.00",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # Cashier cancels
        cancel_res = self.client.post(f"/api/v2/approvals/{req_id}/cancel/", {}, format="json")
        self.assertEqual(cancel_res.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_res.json()["status"], "CANCELLED")

        # Other staff cannot cancel someone else's request
        create_res_2 = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "Second Request",
            "reason": "Valid reason for test.",
        }, format="json")
        req_id_2 = create_res_2.json()["request_id"]

        self.client.force_authenticate(user=self.pharmacist)
        unauth_cancel = self.client.post(f"/api/v2/approvals/{req_id_2}/cancel/", {}, format="json")
        self.assertEqual(unauth_cancel.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(unauth_cancel.json()["error_code"], "ERR_CANNOT_CANCEL_OTHERS_REQUEST")

    def test_07_cannot_cancel_or_approve_already_decided_request(self):
        """Transitioning an already approved or rejected request is rejected."""
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "Routine Discount",
            "reason": "Standard concession.",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # Approve
        self.client.force_authenticate(user=self.manager_a)
        self.client.post(f"/api/v2/approvals/{req_id}/approve/", {"decision_notes": "OK"}, format="json")

        # Try to re-approve
        re_approve = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {"decision_notes": "Duplicate"}, format="json")
        self.assertEqual(re_approve.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(re_approve.json()["error_code"], "ERR_REQUEST_NOT_PENDING")

        # Try to reject approved
        try_rej = self.client.post(f"/api/v2/approvals/{req_id}/reject/", {"rejection_reason": "Late rejection"}, format="json")
        self.assertEqual(try_rej.status_code, status.HTTP_400_BAD_REQUEST)

    def test_08_clinical_escalation_requires_pharmacist_or_manager(self):
        """Clinical escalation requires licensed Supervising Pharmacist, Manager, or Owner."""
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "CLINICAL_ESCALATION",
            "title": "Severe Drug-Drug Interaction",
            "reason": "Prescription contains Simvastatin + Clarithromycin (CYP3A4 inhibition).",
            "urgency": "CRITICAL",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # Cashier peer tries to approve -> rejected (no clinical authority)
        peer_cashier = PharmacyUser.objects.create_user(
            email=f"peer_cashier_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Peer Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.client.force_authenticate(user=peer_cashier)
        bad_app = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {"decision_notes": "Looks ok"}, format="json")
        self.assertEqual(bad_app.status_code, status.HTTP_403_FORBIDDEN)

        # Licensed Staff Pharmacist approves -> succeeds
        self.client.force_authenticate(user=self.pharmacist)
        good_app = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {
            "decision_notes": "Prescriber consented to temporary statin pause during 7-day macrolide antibiotic course.",
        }, format="json")
        self.assertEqual(good_app.status_code, status.HTTP_200_OK)
        self.assertEqual(good_app.json()["status"], "APPROVED")

    def test_09_sensitive_expense_approval_by_finance_officer(self):
        """Finance Officer can approve sensitive operational expenses."""
        self.client.force_authenticate(user=self.manager_a)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "SENSITIVE_EXPENSE",
            "title": "AC Emergency Repair Compressor",
            "reason": "Dispensary cold room AC broke down; emergency replacement needed to protect insulin.",
            "amount_bdt": "18500.00",
            "urgency": "CRITICAL",
        }, format="json")
        req_id = create_res.json()["request_id"]

        self.client.force_authenticate(user=self.finance)
        fin_app = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {
            "decision_notes": "Authorized emergency petty cash disbursement from central store account.",
        }, format="json")
        self.assertEqual(fin_app.status_code, status.HTTP_200_OK)
        self.assertEqual(fin_app.json()["status"], "APPROVED")
        self.assertEqual(fin_app.json()["checker_username"], self.finance.email)

    def test_10_branch_manager_cross_branch_violation(self):
        """Branch Manager of Branch B is blocked from approving requests for Branch A."""
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "Branch A Discount",
            "reason": "Dhanmondi counter discount request.",
            "amount_bdt": "500.00",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # Manager B (Gulshan) tries to approve Dhanmondi request
        self.client.force_authenticate(user=self.manager_b)
        cross_res = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {
            "decision_notes": "Attempting cross-branch approval",
        }, format="json")
        self.assertEqual(cross_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(cross_res.json()["error_code"], "ERR_CROSS_BRANCH_VIOLATION")

    def test_11_cross_tenant_isolation_barrier(self):
        """User from foreign tenant cannot view or approve requests of another tenant."""
        self.client.force_authenticate(user=self.cashier)
        create_res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "Popular Pharma Request",
            "reason": "Confidential patient pricing request.",
            "amount_bdt": "300.00",
        }, format="json")
        req_id = create_res.json()["request_id"]

        # Foreign user tries to retrieve or approve
        self.client.force_authenticate(user=self.foreign_user)
        get_res = self.client.get(f"/api/v2/approvals/{req_id}/")
        self.assertEqual(get_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(get_res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

        app_res = self.client.post(f"/api/v2/approvals/{req_id}/approve/", {"decision_notes": "Hack"}, format="json")
        self.assertEqual(app_res.status_code, status.HTTP_403_FORBIDDEN)

    def test_12_suspended_tenant_subscription_blocks_mutations(self):
        """When tenant subscription is suspended, new approval submissions are rejected."""
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        self.client.force_authenticate(user=self.cashier)
        res = self.client.post("/api/v2/approvals/", {
            "branch_id": self.branch_a.branch_id,
            "approval_type": "HIGH_DISCOUNT",
            "title": "Suspended Tenant Request",
            "reason": "Should be blocked.",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(res.json()["error_code"], ["ERR_SUBSCRIPTION_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"])
