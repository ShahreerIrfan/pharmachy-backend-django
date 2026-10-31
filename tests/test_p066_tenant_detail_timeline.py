"""
backend/tests/test_p066_tenant_detail_timeline.py
Architecture Decision: ADR-0005, ADR-0036, ADR-0051 & ADR-0066
Comprehensive test suite for Platform Super Admin Tenant Detail View and Audit Timeline:
- Valid scenario: Super Admin inspects a tenant and receives 7 core components:
  1. membership
  2. branches
  3. devices & active sessions
  4. plan & quota utilization
  5. subscription history
  6. support events / diagnostic incident tickets
  7. chronological audit timeline
- Patient Privacy Guarantee: Verified exclusion of patient clinical data and prescriptions.
- 404 Not Found scenario: Non-existent tenant returns HTTP 404 with TENANT_NOT_FOUND error code.
- Unauthorized scenario: Tenant Owner, Cashier, and anonymous callers receive HTTP 403/401 with PLATFORM_ACCESS_DENIED.
- Cross-tenant isolation scenario: Tenant A details do not bleed into Tenant B.
- Audit trail scenario: Inspecting a tenant records PLATFORM_TENANT_DETAIL_VIEWED entry.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import Branch, SubscriptionRecord, Tenant
from apps.identity.models import PharmacyUser, UserRole, UserSession
from apps.quality_governance.models import QualityIssueTicket


class TestP066TenantDetailTimeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p066_super_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "is_staff": True,
                "is_superuser": True,
                "phone_number": f"+88019{phone_suffix}",
            },
        )
        cls.superadmin.set_password("AdminSecurePass2026!")
        cls.superadmin.save()

        # 2. Tenant Alpha (ACTIVE, STANDARD)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"tenant-p066-a-{cls.suffix}",
            name=f"Prime Care Pharma {cls.suffix}",
            subdomain=f"prime-p066-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
            billing_email=f"billing_a_{cls.suffix}@pharmacy.bd",
        )

        cls.branch_a1 = Branch.objects.create(
            branch_id=f"{cls.tenant_a.tenant_id}-br-1",
            tenant=cls.tenant_a,
            name="Dhanmondi Main",
            license_number=f"DGDA-DHK-{random.randint(1000, 9999)}",
            is_active=True,
        )
        cls.branch_a2 = Branch.objects.create(
            branch_id=f"{cls.tenant_a.tenant_id}-br-2",
            tenant=cls.tenant_a,
            name="Gulshan Outlet",
            license_number=f"DGDA-DHK-{random.randint(1000, 9999)}",
            is_active=True,
        )

        phone_owner_a = random.randint(10000000, 99999999)
        cls.owner_a = PharmacyUser.objects.create(
            email=f"owner_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Dr. Farhan {cls.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a1.branch_id,
            phone_number=f"+88018{phone_owner_a}",
        )
        cls.owner_a.set_password("OwnerSecure2026!")
        cls.owner_a.save()
        cls.tenant_a.owner_user_id = str(cls.owner_a.user_id)
        cls.tenant_a.save()

        phone_staff_a = random.randint(10000000, 99999999)
        cls.staff_a = PharmacyUser.objects.create(
            email=f"staff_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Pharmacist Anik {cls.suffix}",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a1.branch_id,
            phone_number=f"+88017{phone_staff_a}",
        )

        # Active Subscription for Tenant Alpha
        today = timezone.now().date()
        cls.sub_a = SubscriptionRecord.objects.create(
            tenant=cls.tenant_a,
            subscription_id=f"sub-p066-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
            billing_cycle="MONTHLY",
            amount_bdt=Decimal("3500.00"),
            licensed_branches=3,
            licensed_devices=5,
            start_date=today,
            renewal_date=today + timezone.timedelta(days=30),
        )

        # Active UserSession for Tenant Alpha
        cls.session_a = UserSession.objects.create(
            user=cls.staff_a,
            tenant_id=cls.tenant_a.tenant_id,
            device_id=f"pos-dev-{cls.suffix}",
            device_name="Front Desk POS Terminal",
            client_platform="JAVAFX_DESKTOP",
            ip_address="192.168.1.50",
            is_active=True,
            last_seen_at=timezone.now(),
        )

        # Support incident ticket for Tenant Alpha
        cls.ticket_a = QualityIssueTicket.objects.create(
            issue_id=uuid.uuid4(),
            tenant_id=cls.tenant_a.tenant_id,
            title="Receipt thermal printer alignment lag",
            category="HARDWARE",
            severity="LOW",
            is_resolved=True,
        )

        # Audit timeline log for Tenant Alpha
        AuditLogEntry.objects.create(
            action="TENANT_PROVISIONED",
            resource_type="TENANT",
            resource_id=cls.tenant_a.tenant_id,
            tenant_id=cls.tenant_a.tenant_id,
            status=AuditActionStatus.SUCCESS,
            actor_email=cls.superadmin.email,
            actor_role=cls.superadmin.role,
        )

        # 3. Tenant Beta (for isolation check)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"tenant-p066-b-{cls.suffix}",
            name=f"Beta Wellness {cls.suffix}",
            subdomain=f"beta-p066-{cls.suffix}",
            plan_tier="BASIC",
            status="ACTIVE",
            billing_email=f"billing_b_{cls.suffix}@pharmacy.bd",
        )

    def setUp(self):
        self.client = APIClient()

    def test_superadmin_retrieves_tenant_detail_and_timeline_valid(self):
        """Valid scenario: Super Admin accesses /tenants/<id>/detail/ and /tenants/<id>/ verifying 7 sections."""
        self.client.force_authenticate(user=self.superadmin)

        for endpoint_path in [
            f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/detail/",
            f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/",
        ]:
            resp = self.client.get(endpoint_path)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)

            body = resp.json()
            self.assertTrue(body["success"])
            data = body["data"]

            # 1. Membership
            membership = data["membership"]
            self.assertEqual(membership["tenant_id"], self.tenant_a.tenant_id)
            self.assertEqual(membership["name"], self.tenant_a.name)
            self.assertEqual(membership["subdomain"], self.tenant_a.subdomain)
            self.assertEqual(membership["status"], "ACTIVE")
            self.assertEqual(membership["plan_tier"], "STANDARD")
            self.assertEqual(membership["total_staff_count"], 2)  # owner_a and staff_a
            self.assertIsNotNone(membership["owner"])
            self.assertEqual(membership["owner"]["email"], self.owner_a.email)

            # 2. Branches
            branches = data["branches"]
            self.assertEqual(len(branches), 2)
            branch_names = [b["name"] for b in branches]
            self.assertIn("Dhanmondi Main", branch_names)
            self.assertIn("Gulshan Outlet", branch_names)

            # 3. Devices
            devices = data["devices"]
            self.assertEqual(devices["licensed_devices"], 5)
            self.assertGreaterEqual(devices["active_devices_count"], 1)
            self.assertTrue(any(s["device_id"] == f"pos-dev-{self.suffix}" for s in devices["recent_sessions"]))

            # 4. Plan & Quotas
            plan = data["plan"]
            self.assertEqual(plan["plan_tier"], "STANDARD")
            self.assertEqual(plan["licensed_branches"], 3)
            self.assertEqual(plan["licensed_devices"], 5)
            self.assertEqual(plan["branches_used"], 2)
            self.assertGreaterEqual(plan["devices_used"], 1)
            self.assertIn("quota_utilization_pct", plan)

            # 5. Subscription History
            sub_history = data["subscription_history"]
            self.assertGreaterEqual(len(sub_history), 1)
            first_sub = sub_history[0]
            self.assertEqual(first_sub["subscription_id"], self.sub_a.subscription_id)
            self.assertEqual(first_sub["amount_bdt"], "3500.00")

            # 6. Support Events
            support_events = data["support_events"]
            self.assertGreaterEqual(len(support_events), 1)
            self.assertEqual(support_events[0]["issue_id"], str(self.ticket_a.issue_id))
            self.assertTrue(support_events[0]["is_resolved"])

            # 7. Audit Timeline
            timeline = data["audit_timeline"]
            self.assertGreaterEqual(len(timeline), 1)
            actions = [t["action"] for t in timeline]
            self.assertIn("TENANT_PROVISIONED", actions)

    def test_patient_privacy_invariants(self):
        """Privacy guarantee: Ensure zero patient health records, prescriptions, or clinical data in detail response."""
        self.client.force_authenticate(user=self.superadmin)
        url = f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/detail/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.json()["data"]
        forbidden_keys = [
            "patient_name",
            "patient_phone",
            "patient_address",
            "prescriptions",
            "dispensing_history",
            "dispensing_records",
            "clinical_notes",
        ]
        for key in forbidden_keys:
            self.assertNotIn(key, data, f"Patient clinical key '{key}' illegally leaked in tenant detail view!")

    def test_nonexistent_tenant_returns_404(self):
        """Invalid scenario: Non-existent tenant returns HTTP 404 with TENANT_NOT_FOUND error code."""
        self.client.force_authenticate(user=self.superadmin)
        url = "/api/v2/platform/console/tenants/non-existent-tenant-99999/detail/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        body = resp.json()
        self.assertIn("TENANT_NOT_FOUND", str(body))

    def test_unauthorized_store_users_denied_access(self):
        """Unauthorized scenario: Tenant Owner, Cashier, and unauthenticated callers receive HTTP 403/401."""
        url = f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/detail/"

        # 1. Tenant Owner
        self.client.force_authenticate(user=self.owner_a)
        resp_owner = self.client.get(url)
        self.assertEqual(resp_owner.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_owner.json()))

        # 2. Staff / Cashier
        self.client.force_authenticate(user=self.staff_a)
        resp_staff = self.client.get(url)
        self.assertEqual(resp_staff.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_staff.json()))

        # 3. Anonymous caller
        self.client.force_authenticate(user=None)
        resp_anon = self.client.get(url)
        self.assertIn(resp_anon.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_two_tenant_isolation(self):
        """Cross-tenant isolation: Tenant A's branches and sessions are not visible in Tenant B's detail view."""
        self.client.force_authenticate(user=self.superadmin)
        url_b = f"/api/v2/platform/console/tenants/{self.tenant_b.tenant_id}/detail/"
        resp_b = self.client.get(url_b)
        self.assertEqual(resp_b.status_code, status.HTTP_200_OK)

        data_b = resp_b.json()["data"]
        branch_ids_b = [b["branch_id"] for b in data_b["branches"]]
        self.assertNotIn(self.branch_a1.branch_id, branch_ids_b)
        self.assertNotIn(self.branch_a2.branch_id, branch_ids_b)

    def test_inspection_creates_audit_log(self):
        """Audit trail: Inspecting tenant creates an immutable PLATFORM_TENANT_DETAIL_VIEWED log."""
        self.client.force_authenticate(user=self.superadmin)
        url = f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/detail/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        audit = AuditLogEntry.objects.filter(
            action="PLATFORM_TENANT_DETAIL_VIEWED",
            resource_type="TENANT",
            resource_id=self.tenant_a.tenant_id,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.status, AuditActionStatus.SUCCESS)


if __name__ == "__main__":
    unittest.main()
