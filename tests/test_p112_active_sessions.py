"""
Integration tests for P112: Active sessions UI, multi-device visibility,
and instant online revocation (ADR-0045, ADR-0112).
Tests cover:
- Multi-device active session listing with user/branch enrichment
- Role-based scoping (Pharmacy Owner vs Branch Manager vs Cashier)
- Cross-branch and cross-tenant isolation barriers
- Instant online blocking: immediate rejection of revoked tokens with ERR_SESSION_REVOKED
- Single session revocation and bulk logout-all workflows
- Invalid UUID and non-existent session error handling
- Suspended tenant subscription guards
- JSON schema contract compliance
"""

import json
import random
import uuid
from pathlib import Path

from django.test import Client, TestCase
from jsonschema import validate
from rest_framework import status

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    BranchStaffMembership,
    PlanTier,
    StaffMember,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole, UserSession

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "active_sessions.schema.json"


class ActiveSessionsIntegrationTests(TestCase):
    """End-to-end integration tests for P112 Active Sessions UI & Instant Revocation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Provision Tenant A (Active, Pro Tier)
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p112-a-{self.suffix}",
            name=f"Lazz Pharma Dhaka {self.suffix}",
            subdomain=f"lazz-a-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status="ACTIVE",
        )

        # 2. Provision Tenant B (Foreign Tenant)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p112-b-{self.suffix}",
            name=f"Popular Pharma B {self.suffix}",
            subdomain=f"pop-b-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status="ACTIVE",
        )

        # 3. Provision Branches for Tenant A
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Dhanmondi Branch {self.suffix}",
            license_number=f"DGDA-DL-A1-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
            is_main_branch=True,
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name=f"Gulshan Branch {self.suffix}",
            license_number=f"DGDA-DL-A2-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 4. Provision Branch for Tenant B
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name=f"Chittagong Branch {self.suffix}",
            license_number=f"DGDA-DL-B-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 5. Provision Users for Tenant A
        self.password = "StrongPass2026!#"

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Pharmacy Owner A",
            password=self.password,
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        self.manager_a1 = PharmacyUser.objects.create_user(
            email=f"mgr1_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Branch Manager Dhanmondi",
            password=self.password,
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.staff_mgr = StaffMember.objects.create(
            staff_id=f"stf-mgr-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            username=self.manager_a1.email,
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            mobile_number=f"01700{random.randint(100000, 999999)}",
            is_active=True,
        )
        BranchStaffMembership.objects.create(
            membership_id=f"bsm-mgr-{self.suffix}",
            tenant=self.tenant_a,
            staff_member=self.staff_mgr,
            branch=self.branch_a1,
            role=UserRole.BRANCH_MANAGER,
            is_active=True,
        )

        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"csh1_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Cashier Dhanmondi",
            password=self.password,
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"csh2_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Cashier Gulshan",
            password=self.password,
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )

        # 6. Provision User for Tenant B
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Foreign Owner B",
            password=self.password,
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

    def _login_and_get_session(self, user: PharmacyUser, device_id: str, platform: str = "DESKTOP_POS"):
        """Helper to simulate login, creating UserSession and JWT tokens."""
        res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": user.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID=device_id,
            HTTP_X_DEVICE_NAME=f"Device-{device_id}",
            HTTP_X_CLIENT_PLATFORM=platform,
        )
        self.assertEqual(res.status_code, 200, res.json())
        data = res.json()
        session = UserSession.objects.filter(user=user, device_id=device_id, is_active=True).first()
        return data["access"], data["refresh"], session

    # -------------------------------------------------------------------------
    # 1. Multi-Device Active Sessions Listing & Enriched Metadata
    # -------------------------------------------------------------------------

    def test_owner_lists_active_sessions_across_branches(self):
        """Pharmacy Owner can view active sessions across all branches in their tenant with enriched fields."""
        access_owner, _, sess_owner = self._login_and_get_session(self.owner_a, "dev-owner-mac", "WEB_ADMIN")
        _, _, sess_csh1 = self._login_and_get_session(self.cashier_a1, "dev-csh1-pos", "DESKTOP_POS")
        _, _, sess_csh2 = self._login_and_get_session(self.cashier_a2, "dev-csh2-pos", "DESKTOP_POS")

        resp = self.client.get(
            "/api/v2/sessions/active/",
            HTTP_AUTHORIZATION=f"Bearer {access_owner}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.json())
        data = resp.json()
        self.assertEqual(data["count"], 3)

        session_ids = [s["session_id"] for s in data["results"]]
        self.assertIn(str(sess_owner.session_id), session_ids)
        self.assertIn(str(sess_csh1.session_id), session_ids)
        self.assertIn(str(sess_csh2.session_id), session_ids)

        # Check enrichment
        first = data["results"][0]
        self.assertIn("user_email", first)
        self.assertIn("user_role", first)
        self.assertIn("device_name", first)
        self.assertIn("client_platform", first)
        self.assertIn("is_current", first)

    # -------------------------------------------------------------------------
    # 2. Branch Manager Scoping
    # -------------------------------------------------------------------------

    def test_branch_manager_scoped_to_assigned_branch(self):
        """Branch Manager only sees active sessions for staff in their assigned branch."""
        access_mgr, _, _ = self._login_and_get_session(self.manager_a1, "dev-mgr-pos", "DESKTOP_POS")
        _, _, sess_csh1 = self._login_and_get_session(self.cashier_a1, "dev-csh1-pos", "DESKTOP_POS")
        _, _, sess_csh2 = self._login_and_get_session(self.cashier_a2, "dev-csh2-pos", "DESKTOP_POS")

        resp = self.client.get(
            "/api/v2/sessions/active/",
            HTTP_AUTHORIZATION=f"Bearer {access_mgr}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]

        # Should only see Manager and Cashier 1 (Dhanmondi), NOT Cashier 2 (Gulshan)
        session_ids = [s["session_id"] for s in results]
        self.assertIn(str(sess_csh1.session_id), session_ids)
        self.assertNotIn(str(sess_csh2.session_id), session_ids)

        # Explicit cross-branch query parameter returns 403
        cross_resp = self.client.get(
            f"/api/v2/sessions/active/?branch_id={self.branch_a2.branch_id}",
            HTTP_AUTHORIZATION=f"Bearer {access_mgr}",
        )
        self.assertEqual(cross_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(cross_resp.json().get("error_code"), "ERR_CROSS_BRANCH_VIOLATION")

    # -------------------------------------------------------------------------
    # 3. Cashier Self-Only Scoping
    # -------------------------------------------------------------------------

    def test_cashier_strictly_restricted_to_self_sessions(self):
        """Cashier can only list their own active sessions. Cross-user queries are forbidden."""
        access_csh1, _, sess_csh1 = self._login_and_get_session(self.cashier_a1, "dev-csh1-pos")
        _, _, sess_csh2 = self._login_and_get_session(self.cashier_a2, "dev-csh2-pos")

        resp = self.client.get(
            "/api/v2/sessions/active/",
            HTTP_AUTHORIZATION=f"Bearer {access_csh1}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["session_id"], str(sess_csh1.session_id))

        # Trying to query with another user_id returns 403
        cross_user_resp = self.client.get(
            f"/api/v2/sessions/active/?user_id={self.cashier_a2.user_id}",
            HTTP_AUTHORIZATION=f"Bearer {access_csh1}",
        )
        self.assertEqual(cross_user_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(cross_user_resp.json().get("error_code"), "ERR_PERMISSION_DENIED")

    # -------------------------------------------------------------------------
    # 4. Instant Online Revocation & Immediate API Invalidation
    # -------------------------------------------------------------------------

    def test_revoked_session_immediately_stops_online_access(self):
        """
        Evidence that revoked access stops immediately online:
        1. Login cashier terminal, obtain JWT access token.
        2. Authenticated request succeeds (200).
        3. Manager/Owner revokes cashier session.
        4. Next request using that access token is IMMEDIATELY BLOCKED (401 ERR_SESSION_REVOKED).
        """
        access_csh, _, sess_csh = self._login_and_get_session(self.cashier_a1, "dev-csh-online")
        access_owner, _, _ = self._login_and_get_session(self.owner_a, "dev-owner-mac")

        # 1. Cashier calls active sessions endpoint -> 200 OK
        valid_call = self.client.get(
            "/api/v2/sessions/active/",
            HTTP_AUTHORIZATION=f"Bearer {access_csh}",
        )
        self.assertEqual(valid_call.status_code, status.HTTP_200_OK)

        # 2. Owner revokes cashier session
        revoke_call = self.client.post(
            f"/api/v2/sessions/active/{sess_csh.session_id}/revoke/",
            data={"reason": "Security lockout triggered by owner"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_owner}",
        )
        self.assertEqual(revoke_call.status_code, status.HTTP_200_OK)
        self.assertTrue(revoke_call.json()["revoked"])

        # 3. Subsequent request with cashier token MUST BE IMMEDIATELY BLOCKED (401 ERR_SESSION_REVOKED)
        blocked_call = self.client.get(
            "/api/v2/sessions/active/",
            HTTP_AUTHORIZATION=f"Bearer {access_csh}",
        )
        self.assertEqual(blocked_call.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(blocked_call.json().get("error_code"), "ERR_SESSION_REVOKED")

    # -------------------------------------------------------------------------
    # 5. Cross-Tenant Isolation Barrier
    # -------------------------------------------------------------------------

    def test_cross_tenant_session_revocation_denied(self):
        """User from Tenant B cannot view or revoke sessions from Tenant A."""
        _, _, sess_a = self._login_and_get_session(self.owner_a, "dev-tenant-a-mac")
        access_b, _, _ = self._login_and_get_session(self.owner_b, "dev-tenant-b-mac")

        # Attempt to revoke cross-tenant session
        cross_revoke = self.client.post(
            f"/api/v2/sessions/active/{sess_a.session_id}/revoke/",
            data={"reason": "Cross-tenant intrusion attempt"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_b}",
        )
        self.assertEqual(cross_revoke.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(cross_revoke.json().get("error_code"), "ERR_CROSS_TENANT_VIOLATION")

    # -------------------------------------------------------------------------
    # 6. Cashier Denied Foreign Revocation
    # -------------------------------------------------------------------------

    def test_cashier_denied_revocation_of_other_user(self):
        """Cashier attempting to revoke owner or manager session is denied with ERR_PERMISSION_DENIED."""
        _, _, sess_owner = self._login_and_get_session(self.owner_a, "dev-owner-mac")
        access_csh, _, _ = self._login_and_get_session(self.cashier_a1, "dev-csh1-pos")

        rogue_revoke = self.client.post(
            f"/api/v2/sessions/active/{sess_owner.session_id}/revoke/",
            data={"reason": "Rogue cashier trying to revoke owner"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_csh}",
        )
        self.assertEqual(rogue_revoke.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(rogue_revoke.json().get("error_code"), "ERR_PERMISSION_DENIED")

    # -------------------------------------------------------------------------
    # 7. Invalid & Non-Existent Input Handling
    # -------------------------------------------------------------------------

    def test_invalid_session_inputs(self):
        """Malformed UUID returns 400; non-existent UUID returns 404."""
        access_owner, _, _ = self._login_and_get_session(self.owner_a, "dev-owner-mac")

        # Malformed UUID
        bad_id = self.client.post(
            "/api/v2/sessions/active/not-a-valid-uuid/revoke/",
            data={},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_owner}",
        )
        self.assertEqual(bad_id.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(bad_id.json().get("error_code"), "INVALID_SESSION_ID")

        # Non-existent UUID
        non_existent = str(uuid.uuid4())
        not_found = self.client.post(
            f"/api/v2/sessions/active/{non_existent}/revoke/",
            data={},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_owner}",
        )
        self.assertEqual(not_found.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(not_found.json().get("error_code"), "SESSION_NOT_FOUND")

    # -------------------------------------------------------------------------
    # 8. Bulk Revocation (Revoke-All)
    # -------------------------------------------------------------------------

    def test_bulk_revoke_user_sessions(self):
        """Owner can bulk-terminate all active sessions for a compromised staff member."""
        access_owner, _, _ = self._login_and_get_session(self.owner_a, "dev-owner-mac")
        self._login_and_get_session(self.cashier_a1, "dev-pos-1")
        self._login_and_get_session(self.cashier_a1, "dev-pos-2")

        active_count = UserSession.objects.filter(user=self.cashier_a1, is_active=True).count()
        self.assertEqual(active_count, 2)

        bulk_resp = self.client.post(
            "/api/v2/sessions/active/revoke-all/",
            data={"target_user_id": str(self.cashier_a1.user_id), "reason": "Compromised device batch"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_owner}",
        )
        self.assertEqual(bulk_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(bulk_resp.json()["revoked_count"], 2)

        remaining = UserSession.objects.filter(user=self.cashier_a1, is_active=True).count()
        self.assertEqual(remaining, 0)

    # -------------------------------------------------------------------------
    # 9. Suspended Tenant Subscription Guard
    # -------------------------------------------------------------------------

    def test_suspended_tenant_subscription_blocks_mutations(self):
        """Suspended tenant subscription blocks session revocation mutations."""
        access_owner, _, sess_owner = self._login_and_get_session(self.owner_a, "dev-owner-mac")

        self.tenant_a.status = "SUSPENDED"
        self.tenant_a.save()

        resp = self.client.post(
            f"/api/v2/sessions/active/{sess_owner.session_id}/revoke/",
            data={"reason": "Should be blocked due to suspension"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_owner}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp_data = resp.json()
        self.assertEqual(resp_data.get("error_code"), "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    # -------------------------------------------------------------------------
    # 10. Contract Schema Compliance
    # -------------------------------------------------------------------------

    def test_serialized_session_conforms_to_json_schema(self):
        """Serialized session output conforms strictly to active_sessions.schema.json."""
        access_owner, _, _ = self._login_and_get_session(self.owner_a, "dev-owner-mac", "WEB_ADMIN")

        resp = self.client.get(
            "/api/v2/sessions/active/",
            HTTP_AUTHORIZATION=f"Bearer {access_owner}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        first_session = resp.json()["results"][0]

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)

        # Validate against JSON schema
        validate(instance=first_session, schema=schema)

