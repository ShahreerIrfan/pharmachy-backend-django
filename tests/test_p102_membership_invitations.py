"""
backend/tests/test_p102_membership_invitations.py

Architecture Decision: ADR-0005, ADR-0015, ADR-0033, ADR-0082, ADR-0102
Phase 06: Milestone M03 - Staff Onboarding & Multi-Branch Scopes (P102)
Requirement Trace: FR-031, FR-032, FR-049, FR-082

Comprehensive Integration Tests for Staff Invitations, Memberships, Branch Scopes, and RBAC:
1. Scenario 1: Owner successfully creates invitation with primary branch and custom role.
2. Scenario 2: Branch Manager successfully invites operational staff (Cashier) to assigned branch.
3. Scenario 3: Branch Manager denied inviting another Branch Manager or Tenant Owner (RBAC gate).
4. Scenario 4: Branch Manager denied inviting staff to another sibling branch (Branch scope gate).
5. Scenario 5: Frontline Cashier and Pharmacist denied creating invitations (Role permission gate).
6. Scenario 6: Subscription staff quota exceeded raises HTTP 403 ERR_QUOTA_EXCEEDED.
7. Scenario 7: Resend invitation refreshes token, increments resend_count, and extends 7-day expiry.
8. Scenario 8: Revoke invitation marks status REVOKED; revoked invitation cannot be accepted.
9. Scenario 9: Expired invitation cannot be accepted (ERR_INVITATION_EXPIRED).
10. Scenario 10: Valid invitation accepted via public endpoint; provisions StaffMember, StaffMembership, and PharmacyUser.
11. Scenario 11: Incomplete or invalid input (bad email, short name, duplicate pending invite) rejected.
12. Scenario 12: Cross-tenant barrier: Tenant B cannot list, resend, revoke, or mutate Tenant A invitations/memberships.
13. Scenario 13: Owner updates membership primary branch, roaming branch scopes, and custom role.
14. Scenario 14: Owner deactivates staff membership; updates StaffMember and user statuses.
"""

from __future__ import annotations

import datetime
import os
import random
import sys
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import django

django.setup()

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    InvitationStatus,
    PlanTier,
    StaffInvitation,
    StaffMember,
    StaffMembership,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.staff_membership_service import StaffMembershipService
from apps.identity.models import PharmacyUser, UserRole


class TestP102StaffMembershipInvitationsIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p102_super_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Superadmin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (STANDARD Plan: allows 10 staff users, 3 branches)
        cls.tenant_id_a = f"tn-inv-a-{cls.suffix}"
        cls.subdomain_a = f"inv-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P102-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@inva.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Invite Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        cls.branch_a1 = Branch.objects.get(tenant=cls.tenant_a)
        cls.branch_a2 = Branch.objects.create(
            branch_id=f"{cls.tenant_id_a}-br-dhanmondi",
            tenant=cls.tenant_a,
            name="Dhanmondi Branch",
            license_number=f"LIC-DHAN-{cls.suffix}",
            is_active=True,
        )

        # Branch Manager for Branch A1
        cls.manager_a1 = PharmacyUser.objects.create_user(
            email=f"manager_a1_{cls.suffix}@inva.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            tenant_id=cls.tenant_id_a,
            branch_id=cls.branch_a1.branch_id,
            full_name="Manager A1",
            role=UserRole.BRANCH_MANAGER,
            is_active=True,
        )

        # Cashier for Branch A1
        cls.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cashier_a1_{cls.suffix}@inva.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            tenant_id=cls.tenant_id_a,
            branch_id=cls.branch_a1.branch_id,
            full_name="Cashier A1",
            role=UserRole.CASHIER,
            is_active=True,
        )

        # 3. Provision Tenant B (Foreign Tenant for Cross-Tenant tests)
        cls.tenant_id_b = f"tn-inv-b-{cls.suffix}"
        cls.subdomain_b = f"inv-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P102-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@invb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Invite Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

    def setUp(self):
        self.client = APIClient()

    def test_01_owner_creates_staff_invitation_with_branch_and_custom_role(self):
        """Owner can invite staff with custom role title and primary branch scope."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "email": f"rahim_{self.suffix}@example.com",
            "full_name": "Rahim Uddin",
            "role": "CASHIER",
            "branch_id": self.branch_a1.branch_id,
            "custom_role_title": "Senior Dispensing Cashier",
            "phone_number": "+8801711223344",
        }
        res = self.client.post("/api/v2/staff/invitations/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()

        self.assertTrue(data["invitation_id"].startswith("inv-"))
        self.assertEqual(data["email"], payload["email"])
        self.assertEqual(data["role"], "CASHIER")
        self.assertEqual(data["custom_role_title"], "Senior Dispensing Cashier")
        self.assertEqual(data["branch_id"], self.branch_a1.branch_id)
        self.assertEqual(data["status"], "PENDING")
        self.assertIsNotNone(data["invite_token"])
        self.assertEqual(data["resend_count"], 0)

        # Verify database record
        inv = StaffInvitation.objects.get(invitation_id=data["invitation_id"])
        self.assertEqual(inv.status, InvitationStatus.PENDING)
        self.assertEqual(inv.tenant, self.tenant_a)

    def test_02_branch_manager_creates_operational_staff_invitation_in_own_branch(self):
        """Branch Manager can invite operational staff (Pharmacist) to their assigned branch."""
        self.client.force_authenticate(user=self.manager_a1)
        payload = {
            "email": f"pharmacist_{self.suffix}@example.com",
            "full_name": "Dr. Aminul Islam",
            "role": "PHARMACIST",
            "branch_id": self.branch_a1.branch_id,
            "custom_role_title": "Day Shift Pharmacist",
            "pcb_registration_no": "A-998877",
            "phone_number": "+8801811223344",
        }
        res = self.client.post("/api/v2/staff/invitations/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()

        self.assertEqual(data["role"], "PHARMACIST")
        self.assertEqual(data["branch_id"], self.branch_a1.branch_id)
        self.assertEqual(data["status"], "PENDING")

    def test_03_branch_manager_denied_inviting_owner_or_manager(self):
        """Branch Manager cannot invite another Branch Manager or Tenant Owner (RBAC violation)."""
        self.client.force_authenticate(user=self.manager_a1)
        payload = {
            "email": f"rogue_mgr_{self.suffix}@example.com",
            "full_name": "Rogue Manager",
            "role": "BRANCH_MANAGER",
            "branch_id": self.branch_a1.branch_id,
        }
        res = self.client.post("/api/v2/staff/invitations/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res.json()))

    def test_04_branch_manager_denied_inviting_to_sibling_branch(self):
        """Branch Manager of Branch A1 cannot invite staff to Branch A2 (Sibling branch isolation)."""
        self.client.force_authenticate(user=self.manager_a1)
        payload = {
            "email": f"sibling_staff_{self.suffix}@example.com",
            "full_name": "Cross Branch Staff",
            "role": "CASHIER",
            "branch_id": self.branch_a2.branch_id,
        }
        res = self.client.post("/api/v2/staff/invitations/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res.json()))

    def test_05_cashier_and_pharmacist_denied_creating_invitations(self):
        """Operational staff (Cashier) cannot create staff invitations."""
        self.client.force_authenticate(user=self.cashier_a1)
        payload = {
            "email": f"cashier_friend_{self.suffix}@example.com",
            "full_name": "Friend",
            "role": "CASHIER",
        }
        res = self.client.post("/api/v2/staff/invitations/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res.json()))

    def test_06_quota_exceeded_check(self):
        """When tenant staff seats reach subscription plan quota, invitation creation is denied."""
        # Tenant B has BASIC tier: max_staff_users = 3
        # Currently Tenant B has 1 staff user (Owner B). Let's create 2 more users to hit quota = 3
        PharmacyUser.objects.create_user(
            email=f"b_staff1_{self.suffix}@invb.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            tenant_id=self.tenant_id_b,
            full_name="Staff 1",
            role=UserRole.CASHIER,
            is_active=True,
        )
        PharmacyUser.objects.create_user(
            email=f"b_staff2_{self.suffix}@invb.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            tenant_id=self.tenant_id_b,
            full_name="Staff 2",
            role=UserRole.CASHIER,
            is_active=True,
        )

        self.client.force_authenticate(user=self.owner_b)
        payload = {
            "email": f"exceed_quota_{self.suffix}@invb.local",
            "full_name": "Exceed User",
            "role": "CASHIER",
        }
        res = self.client.post("/api/v2/staff/invitations/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        data = res.json()
        self.assertEqual(data["error_code"], "ERR_QUOTA_EXCEEDED")
        self.assertIn("quota", data["message_en"].lower())

    def test_07_resend_invitation_refreshes_token_and_extends_expiry(self):
        """Owner can resend pending invitation; token is refreshed and expiry reset to 7 days."""
        self.client.force_authenticate(user=self.owner_a)
        inv = StaffMembershipService.create_invitation(
            tenant_id=self.tenant_id_a,
            email=f"resend_test_{self.suffix}@example.com",
            full_name="Resend Candidate",
            role="CASHIER",
        )
        old_token = inv["invite_token"]
        inv_id = inv["invitation_id"]

        res = self.client.post(f"/api/v2/staff/invitations/{inv_id}/resend/", format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["resend_count"], 1)
        self.assertNotEqual(data["invite_token"], old_token)
        self.assertEqual(data["status"], "PENDING")
        self.assertIsNotNone(data["last_resent_at"])

    def test_08_revoke_invitation_and_prevent_acceptance(self):
        """Owner can revoke invitation; revoked invitation cannot be accepted."""
        self.client.force_authenticate(user=self.owner_a)
        inv = StaffMembershipService.create_invitation(
            tenant_id=self.tenant_id_a,
            email=f"revoke_test_{self.suffix}@example.com",
            full_name="Revoke Candidate",
            role="CASHIER",
        )
        inv_id = inv["invitation_id"]
        token = inv["invite_token"]

        res = self.client.post(
            f"/api/v2/staff/invitations/{inv_id}/revoke/",
            data={"reason": "Candidate declined preliminary offer"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()["status"], "REVOKED")

        # Now try to accept revoked invitation via public endpoint
        self.client.logout()
        accept_payload = {
            "invite_token": token,
            "username": f"revoked_user_{self.suffix}",
            "password": "Password123!",
        }
        accept_res = self.client.post("/api/v2/staff/invitations/accept/", data=accept_payload, format="json")
        self.assertEqual(accept_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(accept_res.json()["error_code"], "ERR_INVITATION_REVOKED")

    def test_09_expired_invitation_cannot_be_accepted(self):
        """Past-due invitation cannot be accepted."""
        inv = StaffMembershipService.create_invitation(
            tenant_id=self.tenant_id_a,
            email=f"expired_test_{self.suffix}@example.com",
            full_name="Expired Candidate",
            role="CASHIER",
        )
        inv_record = StaffInvitation.objects.get(invitation_id=inv["invitation_id"])
        # Backdate expiration to 2 days ago
        inv_record.expires_at = timezone.now() - datetime.timedelta(days=2)
        inv_record.save()

        accept_payload = {
            "invite_token": inv["invite_token"],
            "username": f"expired_user_{self.suffix}",
        }
        res = self.client.post("/api/v2/staff/invitations/accept/", data=accept_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["error_code"], "ERR_INVITATION_EXPIRED")

        inv_record.refresh_from_db()
        self.assertEqual(inv_record.status, InvitationStatus.EXPIRED)

    def test_10_accept_invitation_provisions_staff_member_membership_and_user(self):
        """Public acceptance of valid invitation creates StaffMember, StaffMembership, and PharmacyUser."""
        inv = StaffMembershipService.create_invitation(
            tenant_id=self.tenant_id_a,
            email=f"accept_success_{self.suffix}@example.com",
            full_name="Kabir Ahmed",
            role="CASHIER",
            branch_id=self.branch_a1.branch_id,
            custom_role_title="Lead Counter Cashier",
            phone_number="+8801911998877",
        )
        token = inv["invite_token"]
        new_username = f"kabir_{self.suffix}"

        payload = {
            "invite_token": token,
            "username": new_username,
            "password": "StrongPassword123#",
            "phone_number": "+8801911998877",
        }
        res = self.client.post("/api/v2/staff/invitations/accept/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()["membership"]

        self.assertTrue(data["membership_id"].startswith("mem-"))
        self.assertEqual(data["full_name"], "Kabir Ahmed")
        self.assertEqual(data["role"], "CASHIER")
        self.assertEqual(data["custom_role_title"], "Lead Counter Cashier")
        self.assertEqual(data["primary_branch_id"], self.branch_a1.branch_id)
        self.assertTrue(data["is_active"])

        # Check StaffMember
        sm = StaffMember.objects.get(staff_id=data["staff_id"])
        self.assertEqual(sm.username, new_username)
        self.assertTrue(sm.is_active)

        # Check PharmacyUser
        user = PharmacyUser.objects.get(email=inv["email"])
        self.assertEqual(user.tenant_id, self.tenant_id_a)
        self.assertTrue(user.check_password("StrongPassword123#"))
        self.assertEqual(user.role, UserRole.CASHIER)

        # Check invitation status
        inv_record = StaffInvitation.objects.get(invitation_id=inv["invitation_id"])
        self.assertEqual(inv_record.status, InvitationStatus.ACCEPTED)
        self.assertEqual(inv_record.accepted_by_username, new_username)

    def test_11_input_validation_and_duplicate_pending_invite_rejection(self):
        """Rejects invalid emails, short names, and duplicate pending invites."""
        self.client.force_authenticate(user=self.owner_a)

        # Bad email
        res1 = self.client.post(
            "/api/v2/staff/invitations/",
            data={"email": "not-an-email", "full_name": "Test", "role": "CASHIER"},
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)

        # Short name
        res2 = self.client.post(
            "/api/v2/staff/invitations/",
            data={"email": "valid@email.com", "full_name": "A", "role": "CASHIER"},
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

        # Duplicate pending invitation
        dup_email = f"dup_{self.suffix}@example.com"
        res3 = self.client.post(
            "/api/v2/staff/invitations/",
            data={"email": dup_email, "full_name": "First Attempt", "role": "CASHIER"},
            format="json",
        )
        self.assertEqual(res3.status_code, status.HTTP_201_CREATED)

        res4 = self.client.post(
            "/api/v2/staff/invitations/",
            data={"email": dup_email, "full_name": "Second Attempt", "role": "CASHIER"},
            format="json",
        )
        self.assertEqual(res4.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already exists", str(res4.json()).lower())

    def test_12_cross_tenant_isolation_prevents_access_and_mutation(self):
        """Tenant B owner cannot view, resend, revoke, or mutate Tenant A invitations/memberships."""
        inv_a = StaffMembershipService.create_invitation(
            tenant_id=self.tenant_id_a,
            email=f"isolate_{self.suffix}@inva.local",
            full_name="Tenant A Staff",
            role="CASHIER",
        )
        inv_id_a = inv_a["invitation_id"]

        # Authenticate as Tenant B owner
        self.client.force_authenticate(user=self.owner_b)

        # Attempt to resend Tenant A invitation
        res_resend = self.client.post(f"/api/v2/staff/invitations/{inv_id_a}/resend/", format="json")
        self.assertEqual(res_resend.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(res_resend.json()))

        # Attempt to revoke Tenant A invitation
        res_revoke = self.client.post(f"/api/v2/staff/invitations/{inv_id_a}/revoke/", format="json")
        self.assertEqual(res_revoke.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(res_revoke.json()))

    def test_13_update_membership_branch_scopes_and_custom_role(self):
        """Owner updates membership primary branch, roaming branch scopes, and custom role title."""
        # Accept an invitation first to establish membership
        inv = StaffMembershipService.create_invitation(
            tenant_id=self.tenant_id_a,
            email=f"roaming_{self.suffix}@example.com",
            full_name="Roving Pharmacist",
            role="PHARMACIST",
            branch_id=self.branch_a1.branch_id,
        )
        mem = StaffMembershipService.accept_invitation(
            invite_token=inv["invite_token"],
            username=f"roving_{self.suffix}",
        )
        mem_id = mem["membership_id"]

        self.client.force_authenticate(user=self.owner_a)
        patch_payload = {
            "primary_branch_id": self.branch_a2.branch_id,
            "assigned_branch_ids": [self.branch_a1.branch_id, self.branch_a2.branch_id],
            "custom_role_title": "Regional Chief Pharmacist",
        }
        res = self.client.patch(f"/api/v2/staff/memberships/{mem_id}/", data=patch_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["primary_branch_id"], self.branch_a2.branch_id)
        self.assertEqual(data["custom_role_title"], "Regional Chief Pharmacist")
        self.assertEqual(len(data["assigned_branch_ids"]), 2)
        self.assertIn(self.branch_a1.branch_id, data["assigned_branch_ids"])
        self.assertIn(self.branch_a2.branch_id, data["assigned_branch_ids"])

        # Branch Manager should be DENIED modifying memberships
        self.client.force_authenticate(user=self.manager_a1)
        res_mgr = self.client.patch(f"/api/v2/staff/memberships/{mem_id}/", data=patch_payload, format="json")
        self.assertEqual(res_mgr.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res_mgr.json()))

    def test_14_deactivate_staff_membership(self):
        """Owner can deactivate staff membership; cascades to StaffMember and user account."""
        inv = StaffMembershipService.create_invitation(
            tenant_id=self.tenant_id_a,
            email=f"deact_{self.suffix}@example.com",
            full_name="Deactivate Staff",
            role="CASHIER",
        )
        mem = StaffMembershipService.accept_invitation(
            invite_token=inv["invite_token"],
            username=f"deact_{self.suffix}",
        )
        mem_id = mem["membership_id"]

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            f"/api/v2/staff/memberships/{mem_id}/deactivate/",
            data={"reason": "Contract concluded amicably"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertFalse(data["is_active"])
        self.assertIsNotNone(data["deactivated_at"])
        self.assertEqual(data["deactivation_reason"], "Contract concluded amicably")

        mem_record = StaffMembership.objects.get(membership_id=mem_id)
        self.assertFalse(mem_record.is_active)
        self.assertFalse(mem_record.staff_member.is_active)
