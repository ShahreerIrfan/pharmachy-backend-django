"""
backend/tests/test_p067_tenant_suspension_and_restore.py
Architecture Decision: ADR-0005, ADR-0061 & ADR-0067
Comprehensive test suite for P067 Tenant Suspension, Restoration, Reasoned Lifecycle Changes,
Consistent Login, Client Lease Revocation, and Pending-Sync Policies:
- 1. Valid suspension scenario: Super Admin suspends active tenant with reason and reason_code.
     Verifies status=SUSPENDED, session revocation, and immutable audit logging.
- 2. Invalid inputs rejected: Short reason, invalid reason_code, non-existent tenant, or redundant suspension.
- 3. Unauthorized callers rejected: Pharmacy Owner and Cashier receive 403 Forbidden with ROLE_PERMISSION_DENIED.
- 4. Valid restore scenario: Super Admin restores suspended tenant with documented justification.
     Verifies status=ACTIVE, restoration_reason, cleared suspended_at, and audit logging.
- 5. Consistent Login Policy: Operational Cashier receives 403 TENANT_SUSPENDED; Pharmacy Owner logs in with notice.
- 6. Pending-Sync Policy: Pre-suspension transactions drained safely (Zero Data Loss); post-suspension rejected.
"""

from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import timedelta
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import Branch, Tenant, TenantLifecycleStatus
from apps.core.platform_console import PlatformConsoleService
from apps.identity.models import PharmacyUser, UserRole, UserSession


class TestP067TenantSuspensionAndRestore(TestCase):
    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:6]
        phone_suffix_1 = random.randint(10000000, 99999999)
        phone_suffix_2 = random.randint(10000000, 99999999)
        phone_suffix_3 = random.randint(10000000, 99999999)

        # 1. Super Admin
        self.superadmin = PharmacyUser.objects.create(
            email=f"p067_super_{self.suffix}@platform.internal",
            full_name=f"Platform Super Admin {self.suffix}",
            role=UserRole.SUPERADMIN,
            is_staff=True,
            is_superuser=True,
            phone_number=f"+88019{phone_suffix_1}",
        )
        self.superadmin.set_password("AdminSecurePass2026!")
        self.superadmin.save()

        # 2. Tenant Alpha (ACTIVE)
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tenant-p067-a-{self.suffix}",
            name=f"Popular Pharma Care {self.suffix}",
            subdomain=f"popular-p067-{self.suffix}",
            plan_tier="STANDARD",
            status=TenantLifecycleStatus.ACTIVE,
            billing_email=f"billing_a_{self.suffix}@pharmacy.bd",
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"branch-p067-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Dhanmondi Branch",
            license_number=f"DGDA-DH-{self.suffix}",
            is_active=True,
        )

        # 3. Tenant Owner (Alpha)
        self.owner_a = PharmacyUser.objects.create(
            email=f"owner_{self.suffix}@popularpharma.bd",
            full_name=f"Owner Alpha {self.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            phone_number=f"+88018{phone_suffix_2}",
        )
        self.owner_a.set_password("OwnerSecurePass2026!")
        self.owner_a.save()
        self.tenant_a.owner_user_id = str(self.owner_a.user_id)
        self.tenant_a.save()

        # 4. Cashier (Alpha)
        self.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_{self.suffix}@popularpharma.bd",
            full_name=f"Cashier Alpha {self.suffix}",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            phone_number=f"+88017{phone_suffix_3}",
        )
        self.cashier_a.set_password("CashierSecurePass2026!")
        self.cashier_a.save()

        # 5. Create active session for Cashier
        self.session_a = UserSession.objects.create(
            user=self.cashier_a,
            tenant_id=self.tenant_a.tenant_id,
            device_id=f"pos-term-{self.suffix}",
            device_name="POS Counter 1",
            client_platform="DESKTOP_POS",
            is_active=True,
        )

        self.client = APIClient()

    def test_superadmin_suspends_tenant_valid_scenario(self):
        """Super Admin suspends an active tenant with reason and reason_code."""
        self.client.force_authenticate(user=self.superadmin)
        url = f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/suspend/"
        payload = {
            "reason": "Outstanding subscription balance overdue by 30 days after grace period.",
            "reason_code": "NON_PAYMENT",
        }
        response = self.client.post(url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("status"), "SUSPENDED")
        self.assertEqual(data.get("reason_code"), "NON_PAYMENT")
        self.assertEqual(data.get("lease_status"), "REVOKED")

        # Database state verification
        self.tenant_a.refresh_from_db()
        self.assertEqual(self.tenant_a.status, TenantLifecycleStatus.SUSPENDED)
        self.assertIsNotNone(self.tenant_a.suspended_at)
        self.assertEqual(self.tenant_a.suspension_reason_code, "NON_PAYMENT")

        # Active session must be revoked
        self.session_a.refresh_from_db()
        self.assertFalse(self.session_a.is_active)

        # Audit log entry verification
        audit = AuditLogEntry.objects.filter(
            action="TENANT_SUSPENDED",
            resource_id=self.tenant_a.tenant_id,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.metadata_json.get("reason_code"), "NON_PAYMENT")

    def test_invalid_suspension_inputs_rejected(self):
        """Rejects short reason, invalid reason_code, and redundant suspension."""
        self.client.force_authenticate(user=self.superadmin)
        url = f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/suspend/"

        # 1. Reason too short (< 5 chars)
        res_short = self.client.post(url, data={"reason": "bad", "reason_code": "NON_PAYMENT"}, format="json")
        self.assertIn(res_short.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 2. Invalid reason code
        res_code = self.client.post(url, data={"reason": "Valid reason here", "reason_code": "UNKNOWN_REASON"}, format="json")
        self.assertIn(res_code.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 3. Non-existent tenant
        res_not_found = self.client.post(
            "/api/v2/platform/console/tenants/non-existent-tenant-999/suspend/",
            data={"reason": "Valid reason here", "reason_code": "ADMIN_MANUAL"},
            format="json",
        )
        self.assertIn(res_not_found.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 4. Suspending already suspended tenant
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.suspended_at = timezone.now()
        self.tenant_a.save()

        res_already = self.client.post(url, data={"reason": "Attempting second suspend", "reason_code": "ADMIN_MANUAL"}, format="json")
        self.assertIn(res_already.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_unauthorized_store_roles_denied_suspension(self):
        """Pharmacy Owner and Cashier are strictly denied from performing suspensions (403)."""
        url = f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/suspend/"
        payload = {"reason": "Unauthorized attempt", "reason_code": "ADMIN_MANUAL"}

        # Pharmacy Owner denied
        self.client.force_authenticate(user=self.owner_a)
        res_owner = self.client.post(url, data=payload, format="json")
        self.assertEqual(res_owner.status_code, status.HTTP_403_FORBIDDEN)

        # Cashier denied
        self.client.force_authenticate(user=self.cashier_a)
        res_cashier = self.client.post(url, data=payload, format="json")
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)

    def test_superadmin_restores_tenant_valid_scenario(self):
        """Super Admin restores a suspended tenant with documented justification."""
        # Setup tenant as suspended
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.suspended_at = timezone.now()
        self.tenant_a.suspension_reason = "Delinquent invoice"
        self.tenant_a.suspension_reason_code = "NON_PAYMENT"
        self.tenant_a.save()

        self.client.force_authenticate(user=self.superadmin)
        url = f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/restore/"
        payload = {
            "reason": "Payment verified via Sonali Bank slip #SB-98124 and confirmed by accounts.",
        }
        response = self.client.post(url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("status"), "ACTIVE")
        self.assertEqual(data.get("lease_status"), "ACTIVE")

        # Database state verification
        self.tenant_a.refresh_from_db()
        self.assertEqual(self.tenant_a.status, TenantLifecycleStatus.ACTIVE)
        self.assertIsNone(self.tenant_a.suspended_at)
        self.assertIsNotNone(self.tenant_a.restored_at)
        self.assertIn("Sonali Bank", self.tenant_a.restoration_reason)

        # Audit log verification
        audit = AuditLogEntry.objects.filter(
            action="TENANT_RESTORED",
            resource_id=self.tenant_a.tenant_id,
        ).first()
        self.assertIsNotNone(audit)

    def test_consistent_login_policy_when_suspended(self):
        """Cashier login blocked with 403; Pharmacy Owner login allowed with suspension notice."""
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.suspended_at = timezone.now()
        self.tenant_a.suspension_reason = "Unresolved DGDA license compliance query"
        self.tenant_a.suspension_reason_code = "COMPLIANCE_BREACH"
        self.tenant_a.save()

        # 1. Cashier attempts login -> 403 Forbidden
        login_url = "/api/v2/auth/login/"
        cashier_payload = {
            "identifier": self.cashier_a.email,
            "password": "CashierSecurePass2026!",
            "device_id": "pos-cashier-test",
        }
        res_cashier = self.client.post(login_url, data=cashier_payload, format="json")
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)
        cashier_data = res_cashier.json()
        self.assertEqual(cashier_data.get("error_code"), "TENANT_SUSPENDED")

        # 2. Pharmacy Owner attempts login -> 200 OK with tenant_suspended: True
        owner_payload = {
            "identifier": self.owner_a.email,
            "password": "OwnerSecurePass2026!",
            "device_id": "owner-mobile-test",
        }
        res_owner = self.client.post(login_url, data=owner_payload, format="json")
        self.assertEqual(res_owner.status_code, status.HTTP_200_OK)
        owner_data = res_owner.json()
        self.assertTrue(owner_data.get("tenant_suspended"))
        self.assertEqual(owner_data.get("suspension_reason_code"), "COMPLIANCE_BREACH")

    def test_pending_sync_policy_evaluation(self):
        """Pre-suspension transactions drained safely; post-suspension transactions rejected."""
        now = timezone.now()
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.suspended_at = now
        self.tenant_a.save()

        pre_time = now - timedelta(minutes=20)
        post_time = now + timedelta(minutes=20)

        # Pre-suspension transaction: DRAIN_PRE_SUSPENSION (Zero Data Loss invariant)
        pre_res = PlatformConsoleService.evaluate_pending_sync_policy(
            tenant_id=self.tenant_a.tenant_id,
            transaction_timestamp=pre_time,
        )
        self.assertEqual(pre_res.get("policy"), "DRAIN_PRE_SUSPENSION")
        self.assertFalse(pre_res.get("quarantine"))

        # Post-suspension transaction: REJECT_POST_SUSPENSION
        post_res = PlatformConsoleService.evaluate_pending_sync_policy(
            tenant_id=self.tenant_a.tenant_id,
            transaction_timestamp=post_time,
        )
        self.assertEqual(post_res.get("policy"), "REJECT_POST_SUSPENSION")
        self.assertTrue(post_res.get("quarantine"))
        self.assertEqual(post_res.get("error_code"), "TENANT_SUSPENDED_SYNC_REJECTED")
