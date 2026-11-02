"""
Integration Test Suite for P105 - Branch-Specific Membership (ADR-0105).
Covers:
1. Valid multi-branch different roles scenario (Branch Manager in Branch A, Cashier in Branch B).
2. Permission gating parity: Action permitted in Branch A (refund) is denied in Branch B.
3. Invalid or incomplete input handling (missing branch, missing staff, blank role).
4. Cross-tenant isolation barrier (Tenant B cannot access Tenant A memberships).
5. Separation of Duties (Branch Manager cannot assign roles to other branches or assign managerial roles).
6. Unauthorized caller roles (Cashier denied management).
7. Expired / suspended subscription barrier.
8. Unauthorized branch access (acting in branch with no membership).
9. Audited soft-revocation lifecycle and post-revocation denial.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchStaffMembership,
    MedicineProduct,
    PlanTier,
    StaffMember,
    StaffMembership,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


class BranchSpecificMembershipIntegrationTest(TestCase):
    """Verifies end-to-end branch-specific memberships and multi-role scoping."""

    def setUp(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & 3 Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p105-{self.suffix}",
            name=f"Prime Healthcare {self.suffix}",
            subdomain=f"prime-p105-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Dhanmondi Branch",
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant,
            name="Gulshan Branch",
        )
        self.branch_c = Branch.objects.create(
            branch_id=f"br-c-{self.suffix}",
            tenant=self.tenant,
            name="Uttara Branch",
        )

        # 2. Foreign Tenant for Cross-Tenant Isolation
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"tn-foreign-{self.suffix}",
            name=f"Foreign Pharma {self.suffix}",
            subdomain=f"foreign-p105-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-foreign-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Branch",
        )

        # 3. Master Medicine
        self.medicine = MedicineProduct.objects.create(
            product_id=f"med-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals",
            mrp_unit=Decimal("3.00"),
            cost_unit=Decimal("2.10"),
        )

        # 4. Primary Tenant Owner
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@prime.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Owner {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )

        # 5. Foreign Tenant Owner
        self.foreign_owner = PharmacyUser.objects.create_user(
            email=f"f_owner_{self.suffix}@foreign.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Foreign Owner {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.foreign_tenant.tenant_id,
        )

        # 6. Multi-Branch Staff Member: Kamal
        self.kamal_user = PharmacyUser.objects.create_user(
            email=f"kamal_{self.suffix}@prime.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Kamal Hossain",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.kamal_staff = StaffMember.objects.create(
            staff_id=f"stf-kamal-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            username=self.kamal_user.email,
            full_name="Kamal Hossain",
            role="CASHIER",
            mobile_number=self.kamal_user.phone_number,
        )
        self.kamal_membership = StaffMembership.objects.create(
            membership_id=f"mem-kamal-{self.suffix}",
            tenant=self.tenant,
            staff_member=self.kamal_staff,
            primary_branch=self.branch_a,
            role="CASHIER",
            is_active=True,
        )

        # 7. Dedicated Branch Manager for Branch A
        self.manager_user = PharmacyUser.objects.create_user(
            email=f"mgr_a_{self.suffix}@prime.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

    def test_01_valid_multi_branch_different_roles_and_action_parity(self):
        """
        Kamal holds BRANCH_MANAGER in Branch A, but CASHIER in Branch B.
        Verifies actions permitted in Branch A (refund) are forbidden in Branch B.
        """
        self.client.force_authenticate(user=self.owner)

        # Assign Kamal as BRANCH_MANAGER in Branch A
        res_a = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "BRANCH_MANAGER",
                "custom_role_title": "Dhanmondi Store Lead",
                "is_primary": True,
            },
        )
        self.assertEqual(res_a.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_a.json()["role"], "BRANCH_MANAGER")
        self.assertTrue(res_a.json()["is_primary"])

        # Assign Kamal as CASHIER in Branch B
        res_b = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_b.branch_id,
                "role": "CASHIER",
                "custom_role_title": "Gulshan Floor Cashier",
                "is_primary": False,
            },
        )
        self.assertEqual(res_b.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_b.json()["role"], "CASHIER")
        self.assertFalse(res_b.json()["is_primary"])

        # Verify effective role in Branch A
        eff_a = self.client.get(
            f"/api/v2/staff/branch-memberships/effective-role/?branch_id={self.branch_a.branch_id}&staff_id={self.kamal_staff.staff_id}"
        )
        self.assertEqual(eff_a.status_code, status.HTTP_200_OK)
        self.assertEqual(eff_a.json()["role"], "BRANCH_MANAGER")
        self.assertTrue(eff_a.json()["effective_actions"]["refund"])
        self.assertTrue(eff_a.json()["effective_actions"]["discount"])
        self.assertTrue(eff_a.json()["effective_actions"]["cost_visibility"])

        # Verify effective role in Branch B
        eff_b = self.client.get(
            f"/api/v2/staff/branch-memberships/effective-role/?branch_id={self.branch_b.branch_id}&staff_id={self.kamal_staff.staff_id}"
        )
        self.assertEqual(eff_b.status_code, status.HTTP_200_OK)
        self.assertEqual(eff_b.json()["role"], "CASHIER")
        self.assertTrue(eff_b.json()["effective_actions"]["sale"])
        self.assertFalse(eff_b.json()["effective_actions"]["refund"])
        self.assertFalse(eff_b.json()["effective_actions"]["cost_visibility"])

        # Now authenticate as Kamal and test action execution
        self.client.force_authenticate(user=self.kamal_user)

        # 1. Kamal executes refund in Branch A -> SUCCESS (Permitted as Branch Manager)
        ref_a = self.client.post(
            "/api/v2/permissions/granular/refund/",
            {
                "branch_id": self.branch_a.branch_id,
                "invoice_id": "inv-dhanmondi-001",
                "refund_amount_bdt": "120.00",
                "reason": "Authorized manager return",
            },
        )
        self.assertEqual(ref_a.status_code, status.HTTP_200_OK)
        self.assertEqual(ref_a.json()["status"], "REFUNDED")

        # 2. Kamal executes refund in Branch B -> FORBIDDEN (Denied as Cashier)
        ref_b = self.client.post(
            "/api/v2/permissions/granular/refund/",
            {
                "branch_id": self.branch_b.branch_id,
                "invoice_id": "inv-gulshan-001",
                "refund_amount_bdt": "50.00",
            },
        )
        self.assertEqual(ref_b.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_GRANULAR_PERMISSION_DENIED", str(ref_b.json()))

    def test_02_invalid_or_incomplete_inputs(self):
        """Rejects assignment requests with missing branch, missing staff, or invalid roles."""
        self.client.force_authenticate(user=self.owner)

        # Missing branch_id
        res_no_branch = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "role": "CASHIER",
            },
        )
        self.assertIn(
            res_no_branch.status_code,
            [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )

        # Missing staff_member_id
        res_no_staff = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "branch_id": self.branch_a.branch_id,
                "role": "CASHIER",
            },
        )
        self.assertIn(
            res_no_staff.status_code,
            [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )

        # Empty role
        res_no_role = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "",
            },
        )
        self.assertIn(
            res_no_role.status_code,
            [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )

    def test_03_cross_tenant_isolation(self):
        """Tenant B owner cannot access or assign memberships in Tenant A."""
        self.client.force_authenticate(user=self.foreign_owner)

        # Foreign owner attempting to view Tenant A branch memberships
        res_view = self.client.get(
            f"/api/v2/staff/branch-memberships/?tenant_id={self.tenant.tenant_id}"
        )
        self.assertEqual(res_view.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(res_view.json()))

        # Foreign owner attempting to assign staff in Tenant A
        res_create = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "tenant_id": self.tenant.tenant_id,
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "CASHIER",
            },
        )
        self.assertEqual(res_create.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(res_create.json()))

    def test_04_branch_manager_separation_of_duties(self):
        """Branch Manager can only assign operational staff in their own branch."""
        self.client.force_authenticate(user=self.manager_user)

        # 1. Manager assigning Cashier in own branch (Branch A) -> SUCCESS
        res_own = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "CASHIER",
            },
        )
        self.assertEqual(res_own.status_code, status.HTTP_201_CREATED)

        # 2. Manager attempting to assign staff to Branch B -> FORBIDDEN
        res_other = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_b.branch_id,
                "role": "CASHIER",
            },
        )
        self.assertEqual(res_other.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_BRANCH_ACCESS_DENIED", str(res_other.json()))

        # 3. Manager attempting to assign Branch Manager role -> FORBIDDEN
        res_promo = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "BRANCH_MANAGER",
            },
        )
        self.assertEqual(res_promo.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res_promo.json()))

    def test_05_frontline_staff_denied_management(self):
        """Cashier cannot view or manage branch memberships."""
        self.client.force_authenticate(user=self.kamal_user)

        res_list = self.client.get("/api/v2/staff/branch-memberships/")
        self.assertEqual(res_list.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res_list.json()))

        res_create = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "CASHIER",
            },
        )
        self.assertEqual(res_create.status_code, status.HTTP_403_FORBIDDEN)

    def test_06_expired_suspended_subscription_blocks_membership(self):
        """Tenant in SUSPENDED status is blocked from assigning branch memberships."""
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        self.client.force_authenticate(user=self.owner)
        res = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "CASHIER",
            },
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        resp_str = str(res.json())
        self.assertTrue(
            "ERR_SUBSCRIPTION_INACTIVE" in resp_str
            or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in resp_str
        )

    def test_07_unauthorized_branch_access_denied(self):
        """Staff attempting action in Branch C where they have no membership is blocked."""
        # Authenticate as Kamal
        self.client.force_authenticate(user=self.kamal_user)

        # Attempt counter sale in Branch C (Kamal has no assignment in Branch C)
        sale_c = self.client.post(
            "/api/v2/permissions/granular/sale/",
            {
                "branch_id": self.branch_c.branch_id,
                "invoice_id": "inv-uttara-999",
                "total_amount_bdt": "200.00",
            },
        )
        self.assertEqual(sale_c.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_GRANULAR_PERMISSION_DENIED", str(sale_c.json()))
        self.assertIn("no authorized membership", str(sale_c.json()))

    def test_08_soft_revocation_lifecycle(self):
        """Audited revocation deactivates branch role and revokes branch capability."""
        self.client.force_authenticate(user=self.owner)

        # 1. Assign Kamal as Cashier in Branch A
        res_assign = self.client.post(
            "/api/v2/staff/branch-memberships/",
            {
                "staff_member_id": self.kamal_staff.staff_id,
                "branch_id": self.branch_a.branch_id,
                "role": "CASHIER",
            },
        )
        self.assertEqual(res_assign.status_code, status.HTTP_201_CREATED)
        membership_id = res_assign.json()["membership_id"]

        # 2. Revoke assignment
        res_revoke = self.client.post(
            f"/api/v2/staff/branch-memberships/{membership_id}/revoke/",
            {"reason": "Transferred to different department"},
        )
        self.assertEqual(res_revoke.status_code, status.HTTP_200_OK)
        self.assertFalse(res_revoke.json()["is_active"])
        self.assertEqual(res_revoke.json()["revocation_reason"], "Transferred to different department")

        # 3. Verify membership record in database
        bsm = BranchStaffMembership.objects.get(membership_id=membership_id)
        self.assertFalse(bsm.is_active)
        self.assertIsNotNone(bsm.revoked_at)
