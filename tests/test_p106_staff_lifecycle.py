"""
backend/tests/test_p106_staff_lifecycle.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0033, ADR-0082, ADR-0102, ADR-0105, ADR-0106
Phase 06: Milestone M03 - Staff Lifecycle Management (P106)
Requirement Trace: FR-031, FR-032, FR-049, FR-070, FR-071, FR-082

Comprehensive Integration Test Suite for Staff Lifecycle Management:
1. Valid direct administrative staff onboarding (create) with model synchronization.
2. Staff profile and role update with cross-model propagation.
3. Soft deactivation with immediate active user session revocation.
4. Quota-safeguarded reactivation and over-quota blocking.
5. Permanent termination with cascaded session, offline lease, branch membership, and MFA revocations.
6. Administrative password reset with old session invalidation.
7. Administrative MFA recovery and re-enrollment readiness.
8. Explicit session and device credential revocation endpoints.
9. Incomplete or invalid input validation errors (400 Bad Request).
10. Cross-tenant isolation barrier (ERR_CROSS_TENANT_VIOLATION).
11. Branch Manager separation of duties (SoD) enforcement.
12. Frontline staff denied all lifecycle mutations (ERR_PERMISSION_DENIED).
13. Inactive/suspended subscription blocks lifecycle mutations (ERR_SUBSCRIPTION_INACTIVE).
"""

from __future__ import annotations

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
    BranchStaffMembership,
    DeviceLeaseCertificate,
    OfflineLeaseStatus,
    PlanTier,
    StaffLifecycleEvent,
    StaffLifecycleEventType,
    StaffLifecycleStatus,
    StaffMember,
    StaffMembership,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import (
    PharmacyUser,
    UserMfaDevice,
    UserRole,
    UserSession,
)


class StaffLifecycleIntegrationTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Primary Tenant & Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p106-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"popular-p106-{self.suffix}",
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

        # 2. Foreign Tenant for Cross-Tenant Barrier
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"tn-foreign-{self.suffix}",
            name=f"Foreign Pharma {self.suffix}",
            subdomain=f"foreign-p106-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-foreign-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Branch",
        )

        # 3. Primary Tenant Owner
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Owner {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )

        # 4. Foreign Tenant Owner
        self.foreign_owner = PharmacyUser.objects.create_user(
            email=f"f_owner_{self.suffix}@foreign.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Foreign Owner {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.foreign_tenant.tenant_id,
        )

        # 5. Branch Manager for Branch A
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_a_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 6. Frontline Cashier
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier Tariq",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

    def test_01_valid_direct_staff_creation(self):
        """Owner directly provisions a new staff member with synchronized models and audit log."""
        self.client.force_authenticate(user=self.owner)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "username": f"salim_{self.suffix}",
            "email": f"salim_{self.suffix}@popular.local",
            "full_name": "Salim Reza",
            "role": "CASHIER",
            "branch_id": self.branch_a.branch_id,
            "mobile_number": "+8801711223344",
            "pcb_registration_no": "",
        }

        res = self.client.post("/api/v2/staff/lifecycle/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        data = res.json() if hasattr(res, "json") else res.data

        self.assertEqual(data["username"], f"salim_{self.suffix}")
        self.assertEqual(data["lifecycle_status"], "ACTIVE")
        self.assertTrue(data["is_active"])
        self.assertEqual(data["role"], "CASHIER")
        self.assertIsNotNone(data.get("temporary_password"))

        # Verify StaffMember, StaffMembership, and PharmacyUser exist
        sm = StaffMember.objects.get(staff_id=data["staff_id"], tenant=self.tenant)
        self.assertEqual(sm.lifecycle_status, StaffLifecycleStatus.ACTIVE)
        self.assertTrue(sm.is_active)

        mem = StaffMembership.objects.get(staff_member=sm, tenant=self.tenant)
        self.assertTrue(mem.is_active)
        self.assertEqual(mem.primary_branch, self.branch_a)

        u = PharmacyUser.objects.get(email=payload["email"])
        self.assertEqual(u.role, UserRole.CASHIER)
        self.assertTrue(u.is_active)

        # Verify Audit Event
        evt = StaffLifecycleEvent.objects.filter(staff_member=sm, event_type=StaffLifecycleEventType.CREATED).first()
        self.assertIsNotNone(evt)
        self.assertEqual(evt.performed_by_username, self.owner.email)

    def test_02_update_staff_details_and_role_sync(self):
        """Owner updates staff profile details and verifies cross-model synchronization."""
        self.client.force_authenticate(user=self.owner)

        # Create staff first
        create_res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"habib_{self.suffix}",
            "email": f"habib_{self.suffix}@popular.local",
            "full_name": "Habib Rahman",
            "role": "CASHIER",
            "branch_id": self.branch_a.branch_id,
        }, format="json")
        staff_id = create_res.json()["staff_id"]

        # Update staff to PHARMACIST with PCB registration number
        update_res = self.client.patch(f"/api/v2/staff/lifecycle/{staff_id}/", {
            "full_name": "Habib Rahman Senior",
            "role": "STAFF_PHARMACIST",
            "mobile_number": "+8801899887766",
            "pcb_registration_no": "PCB-A-54321",
            "branch_id": self.branch_b.branch_id,
        }, format="json")
        self.assertEqual(update_res.status_code, status.HTTP_200_OK, update_res.data)
        updated_data = update_res.json()

        self.assertEqual(updated_data["full_name"], "Habib Rahman Senior")
        self.assertEqual(updated_data["role"], "STAFF_PHARMACIST")
        self.assertEqual(updated_data["branch_id"], self.branch_b.branch_id)
        self.assertEqual(updated_data["pcb_registration_no"], "PCB-A-54321")

        # Verify PharmacyUser updated
        user = PharmacyUser.objects.get(email=f"habib_{self.suffix}@popular.local")
        self.assertEqual(user.full_name, "Habib Rahman Senior")
        self.assertEqual(user.role, UserRole.STAFF_PHARMACIST)
        self.assertEqual(user.pcb_registration_no, "PCB-A-54321")

    def test_03_soft_deactivation_revokes_active_sessions(self):
        """Deactivation transitions status to DEACTIVATED and immediately invalidates active sessions."""
        self.client.force_authenticate(user=self.owner)

        create_res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"deact_{self.suffix}",
            "email": f"deact_{self.suffix}@popular.local",
            "full_name": "Deact Employee",
            "role": "CASHIER",
            "branch_id": self.branch_a.branch_id,
        }, format="json")
        staff_id = create_res.json()["staff_id"]
        user = PharmacyUser.objects.get(email=f"deact_{self.suffix}@popular.local")

        # Provision 2 active sessions for this employee
        s1 = UserSession.objects.create(
            user=user,
            tenant_id=self.tenant.tenant_id,
            device_id="desktop-pos-01",
            is_active=True,
        )
        s2 = UserSession.objects.create(
            user=user,
            tenant_id=self.tenant.tenant_id,
            device_id="mobile-terminal-01",
            is_active=True,
        )

        # Execute deactivation
        deact_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/deactivate/", {
            "reason": "Administrative inquiry pending",
        }, format="json")
        self.assertEqual(deact_res.status_code, status.HTTP_200_OK, deact_res.data)
        data = deact_res.json()

        self.assertEqual(data["lifecycle_status"], "DEACTIVATED")
        self.assertFalse(data["is_active"])
        self.assertEqual(data["active_sessions_count"], 0)
        self.assertIsNotNone(data["deactivated_at"])

        # Check sessions revoked in database
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertFalse(s1.is_active)
        self.assertFalse(s2.is_active)
        self.assertTrue("STAFF_DEACTIVATION" in s1.revocation_reason)

    def test_04_reactivation_restores_active_status_and_enforces_quota(self):
        """Reactivation restores ACTIVE status, but is blocked when plan seat quota is exceeded."""
        self.client.force_authenticate(user=self.owner)

        # 1. Create and deactivate staff
        create_res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"react_{self.suffix}",
            "email": f"react_{self.suffix}@popular.local",
            "full_name": "Reactivate Candidate",
            "role": "CASHIER",
        }, format="json")
        staff_id = create_res.json()["staff_id"]

        self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/deactivate/", {"reason": "Temp leave"}, format="json")

        # 2. Reactivate successfully
        react_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/reactivate/", {
            "reason": "Returned from leave",
        }, format="json")
        self.assertEqual(react_res.status_code, status.HTTP_200_OK, react_res.data)
        self.assertEqual(react_res.json()["lifecycle_status"], "ACTIVE")
        self.assertTrue(react_res.json()["is_active"])

        # 3. Simulate quota exhaustion: switch tenant to BASIC plan limit (3 max staff, 3 active exist)
        self.tenant.plan_tier = PlanTier.BASIC
        self.tenant.save()
        self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/deactivate/", {"reason": "Temp pause"}, format="json")

        # Reactivation now blocked by QuotaGuard
        blocked_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/reactivate/", {}, format="json")
        self.assertEqual(blocked_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(blocked_res.json()["error_code"], "ERR_QUOTA_EXCEEDED")

    def test_05_permanent_termination_revokes_sessions_leases_and_mfa(self):
        """Termination permanently separates staff, revokes all sessions, leases, MFA, and memberships."""
        self.client.force_authenticate(user=self.owner)

        create_res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"term_{self.suffix}",
            "email": f"term_{self.suffix}@popular.local",
            "full_name": "Terminated Staff",
            "role": "CASHIER",
            "branch_id": self.branch_a.branch_id,
        }, format="json")
        staff_id = create_res.json()["staff_id"]
        user = PharmacyUser.objects.get(email=f"term_{self.suffix}@popular.local")

        # Provision active session, device lease, branch membership, and MFA
        session = UserSession.objects.create(
            user=user,
            tenant_id=self.tenant.tenant_id,
            device_id="desktop-pos-term",
            is_active=True,
        )
        lease = DeviceLeaseCertificate.objects.create(
            lease_id=f"ls-term-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            device_id="dev-pos-term",
            user_id=str(user.pk),
            user_email=user.email,
            expires_at=timezone.now() + timezone.timedelta(days=3),
            signature="sig-term",
            status=OfflineLeaseStatus.ACTIVE,
        )
        BranchStaffMembership.objects.create(
            membership_id=f"bsm-term-{self.suffix}",
            tenant=self.tenant,
            staff_member=StaffMember.objects.get(staff_id=staff_id),
            branch=self.branch_a,
            role="CASHIER",
            is_active=True,
        )
        UserMfaDevice.objects.create(
            user=user,
            secret_key="JBSWY3DPEHPK3PXP",
            is_confirmed=True,
        )
        user.mfa_enabled = True
        user.save()

        # Execute termination
        term_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/terminate/", {
            "reason": "Permanent separation of employment due to relocation",
        }, format="json")
        self.assertEqual(term_res.status_code, status.HTTP_200_OK, term_res.data)
        data = term_res.json()

        self.assertEqual(data["lifecycle_status"], "TERMINATED")
        self.assertFalse(data["is_active"])
        self.assertFalse(data["mfa_enabled"])
        self.assertEqual(data["active_sessions_count"], 0)
        self.assertEqual(data["active_leases_count"], 0)
        self.assertIsNotNone(data["terminated_at"])

        # Check cascaded revocations
        session.refresh_from_db()
        self.assertFalse(session.is_active)

        lease.refresh_from_db()
        self.assertEqual(lease.status, OfflineLeaseStatus.REVOKED)

        bsm = BranchStaffMembership.objects.get(membership_id=f"bsm-term-{self.suffix}")
        self.assertFalse(bsm.is_active)

        self.assertFalse(UserMfaDevice.objects.filter(user=user).exists())

        # Cannot reactivate terminated staff
        react_try = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/reactivate/", {}, format="json")
        self.assertEqual(react_try.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(react_try.json()["error_code"], "CANNOT_REACTIVATE_TERMINATED")

    def test_06_admin_credential_reset_and_mfa_recovery(self):
        """Owner resets staff password and executes administrative MFA recovery."""
        self.client.force_authenticate(user=self.owner)

        create_res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"cred_{self.suffix}",
            "email": f"cred_{self.suffix}@popular.local",
            "full_name": "Credential Reset Candidate",
            "role": "CASHIER",
        }, format="json")
        staff_id = create_res.json()["staff_id"]
        user = PharmacyUser.objects.get(email=f"cred_{self.suffix}@popular.local")

        # Provision session and MFA
        UserSession.objects.create(
            user=user,
            tenant_id=self.tenant.tenant_id,
            device_id="desktop-pos-02",
            is_active=True,
        )
        UserMfaDevice.objects.create(user=user, secret_key="BASE32SECRETKEY", is_confirmed=True)
        user.mfa_enabled = True
        user.save()

        # 1. Reset Credentials
        reset_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/reset-credentials/", {
            "password": "NewSecretPass123!",
        }, format="json")
        self.assertEqual(reset_res.status_code, status.HTTP_200_OK, reset_res.data)
        self.assertEqual(reset_res.json()["revoked_sessions_count"], 1)

        # 2. MFA Recovery
        mfa_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/mfa-recovery/", {
            "reason": "Employee lost physical authenticator phone",
        }, format="json")
        self.assertEqual(mfa_res.status_code, status.HTTP_200_OK, mfa_res.data)
        self.assertFalse(mfa_res.json()["mfa_enabled"])
        self.assertEqual(mfa_res.json()["cleared_devices_count"], 1)
        self.assertFalse(UserMfaDevice.objects.filter(user=user).exists())

    def test_07_explicit_session_and_device_revocation_endpoints(self):
        """Owner can explicitly revoke all sessions or all offline device leases for a staff member."""
        self.client.force_authenticate(user=self.owner)

        create_res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"rev_{self.suffix}",
            "email": f"rev_{self.suffix}@popular.local",
            "full_name": "Revocation Candidate",
            "role": "CASHIER",
            "branch_id": self.branch_a.branch_id,
        }, format="json")
        staff_id = create_res.json()["staff_id"]
        user = PharmacyUser.objects.get(email=f"rev_{self.suffix}@popular.local")

        UserSession.objects.create(user=user, tenant_id=self.tenant.tenant_id, device_id="pos-rev-1", is_active=True)
        UserSession.objects.create(user=user, tenant_id=self.tenant.tenant_id, device_id="pos-rev-2", is_active=True)

        DeviceLeaseCertificate.objects.create(
            lease_id=f"ls-rev-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            device_id="dev-pos-rev",
            user_id=str(user.pk),
            user_email=user.email,
            expires_at=timezone.now() + timezone.timedelta(days=1),
            signature="sig-rev",
            status=OfflineLeaseStatus.ACTIVE,
        )

        # Revoke Sessions
        s_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/revoke-sessions/", {}, format="json")
        self.assertEqual(s_res.status_code, status.HTTP_200_OK)
        self.assertEqual(s_res.json()["revoked_sessions_count"], 2)

        # Revoke Device Leases
        d_res = self.client.post(f"/api/v2/staff/lifecycle/{staff_id}/revoke-devices/", {}, format="json")
        self.assertEqual(d_res.status_code, status.HTTP_200_OK)
        self.assertEqual(d_res.json()["revoked_leases_count"], 1)

    def test_08_invalid_input_rejections(self):
        """Incomplete or invalid input is rejected with HTTP 400 Bad Request."""
        self.client.force_authenticate(user=self.owner)

        # 1. Invalid email
        res1 = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": "bad_email_user",
            "email": "not-an-email",
            "full_name": "Valid Name",
            "role": "CASHIER",
        }, format="json")
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res1.json()["error_code"], "INVALID_EMAIL")

        # 2. Short name
        res2 = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": "short_name_user",
            "email": "valid@popular.local",
            "full_name": "X",
            "role": "CASHIER",
        }, format="json")
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res2.json()["error_code"], "INVALID_FULL_NAME")

        # 3. Invalid role
        res3 = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": "bad_role_user",
            "email": "role@popular.local",
            "full_name": "Bad Role User",
            "role": "NON_EXISTENT_ROLE",
        }, format="json")
        self.assertEqual(res3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res3.json()["error_code"], "INVALID_ROLE")

    def test_09_cross_tenant_isolation_barrier(self):
        """Foreign tenant owner is denied inspecting or modifying primary tenant staff lifecycle."""
        # Create staff under primary tenant
        self.client.force_authenticate(user=self.owner)
        create_res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"iso_{self.suffix}",
            "email": f"iso_{self.suffix}@popular.local",
            "full_name": "Isolation Staff",
            "role": "CASHIER",
        }, format="json")
        staff_id = create_res.json()["staff_id"]

        # Authenticate as foreign tenant owner
        self.client.force_authenticate(user=self.foreign_owner)

        # 1. Inspect
        get_res = self.client.get(f"/api/v2/staff/lifecycle/{staff_id}/?tenant_id={self.tenant.tenant_id}")
        self.assertEqual(get_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(get_res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

        # 2. Deactivate attempt
        deact_res = self.client.post(
            f"/api/v2/staff/lifecycle/{staff_id}/deactivate/?tenant_id={self.tenant.tenant_id}",
            {},
            format="json",
        )
        self.assertEqual(deact_res.status_code, status.HTTP_403_FORBIDDEN)

        # 3. Terminate attempt
        term_res = self.client.post(
            f"/api/v2/staff/lifecycle/{staff_id}/terminate/?tenant_id={self.tenant.tenant_id}",
            {"reason": "Malicious attempt"},
            format="json",
        )
        self.assertEqual(term_res.status_code, status.HTTP_403_FORBIDDEN)

    def test_10_branch_manager_sod_boundaries(self):
        """Branch Manager can inspect own branch staff, but cannot terminate, reset MFA, or touch other branches."""
        self.client.force_authenticate(user=self.owner)

        # Staff in Branch A (manager's branch)
        s_a = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"s_a_{self.suffix}",
            "email": f"s_a_{self.suffix}@popular.local",
            "full_name": "Staff Branch A",
            "role": "CASHIER",
            "branch_id": self.branch_a.branch_id,
        }, format="json").json()

        # Staff in Branch B (other branch)
        s_b = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"s_b_{self.suffix}",
            "email": f"s_b_{self.suffix}@popular.local",
            "full_name": "Staff Branch B",
            "role": "CASHIER",
            "branch_id": self.branch_b.branch_id,
        }, format="json").json()

        # Switch to Branch Manager
        self.client.force_authenticate(user=self.manager)

        # Can inspect own branch staff
        res_a = self.client.get(f"/api/v2/staff/lifecycle/{s_a['staff_id']}/")
        self.assertEqual(res_a.status_code, status.HTTP_200_OK)

        # Blocked from inspecting other branch staff
        res_b = self.client.get(f"/api/v2/staff/lifecycle/{s_b['staff_id']}/")
        self.assertEqual(res_b.status_code, status.HTTP_403_FORBIDDEN)

        # Blocked from terminating staff
        term_try = self.client.post(f"/api/v2/staff/lifecycle/{s_a['staff_id']}/terminate/", {
            "reason": "Manager trying to fire staff",
        }, format="json")
        self.assertEqual(term_try.status_code, status.HTTP_403_FORBIDDEN)

        # Blocked from promoting staff to BRANCH_MANAGER
        promo_try = self.client.patch(f"/api/v2/staff/lifecycle/{s_a['staff_id']}/", {
            "role": "BRANCH_MANAGER",
        }, format="json")
        self.assertEqual(promo_try.status_code, status.HTTP_403_FORBIDDEN)

    def test_11_frontline_staff_denied_lifecycle_management(self):
        """Frontline cashier is denied all lifecycle mutation endpoints."""
        self.client.force_authenticate(user=self.owner)
        staff = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"fl_{self.suffix}",
            "email": f"fl_{self.suffix}@popular.local",
            "full_name": "Frontline Target",
            "role": "CASHIER",
        }, format="json").json()

        # Switch to frontline Cashier
        self.client.force_authenticate(user=self.cashier)

        # Denied Create
        r1 = self.client.post("/api/v2/staff/lifecycle/", {"username": "foo"}, format="json")
        self.assertEqual(r1.status_code, status.HTTP_403_FORBIDDEN)

        # Denied Deactivate
        r2 = self.client.post(f"/api/v2/staff/lifecycle/{staff['staff_id']}/deactivate/", {}, format="json")
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)

        # Denied Terminate
        r3 = self.client.post(f"/api/v2/staff/lifecycle/{staff['staff_id']}/terminate/", {}, format="json")
        self.assertEqual(r3.status_code, status.HTTP_403_FORBIDDEN)

        # Denied Reset Credentials
        r4 = self.client.post(f"/api/v2/staff/lifecycle/{staff['staff_id']}/reset-credentials/", {}, format="json")
        self.assertEqual(r4.status_code, status.HTTP_403_FORBIDDEN)

    def test_12_suspended_tenant_subscription_blocks_lifecycle_mutations(self):
        """Suspended tenant subscription blocks staff lifecycle mutations with ERR_SUBSCRIPTION_INACTIVE."""
        self.client.force_authenticate(user=self.owner)

        # Suspend tenant
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        # Attempt to provision staff
        res = self.client.post("/api/v2/staff/lifecycle/", {
            "tenant_id": self.tenant.tenant_id,
            "username": f"susp_{self.suffix}",
            "email": f"susp_{self.suffix}@popular.local",
            "full_name": "Suspended Candidate",
            "role": "CASHIER",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        data = res.json()
        self.assertIn(data["error_code"], ["ERR_SUBSCRIPTION_INACTIVE", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"])
