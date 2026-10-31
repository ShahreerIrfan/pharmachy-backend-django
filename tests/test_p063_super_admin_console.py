"""
backend/tests/test_p063_super_admin_console.py
Architecture Decision: ADR-0005, ADR-0059 & ADR-0063
Comprehensive test suite for Platform Super Admin Separate Console and Boundary Isolation:
- Valid scenario: Super Admin accesses metrics, tenant directory, deep tenant inspection, and platform actions with audit logging.
- Invalid scenario: Inspection of non-existent tenant returns 404, invalid action / short reason returns 400/422.
- Unauthorized scenario: Tenant Owner, Cashier, and unauthenticated callers receive HTTP 403/401 with PLATFORM_ACCESS_DENIED.
- Two-tenant isolation & privilege separation: Tenant users never receive platform console routes in navigation or API responses.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import Branch, SubscriptionRecord, Tenant
from apps.core.navigation import NavGroup, NavigationRouter, PlanTier
from apps.identity.models import PharmacyUser, UserRole


class TestP063SuperAdminConsole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p063_super_{cls.suffix}@platform.internal",
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

        # 2. Tenant Alpha
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"tenant-p063-a-{cls.suffix}",
            name=f"Alpha Care Pharma {cls.suffix}",
            subdomain=f"alpha-p063-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
            billing_email=f"alpha_{cls.suffix}@pharmacy.bd",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"{cls.tenant_a.tenant_id}-br-1",
            tenant=cls.tenant_a,
            name="Dhanmondi Branch",
            is_active=True,
        )
        phone_owner_a = random.randint(10000000, 99999999)
        cls.owner_a = PharmacyUser.objects.create(
            email=f"owner_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Dr. Owner Alpha {cls.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            phone_number=f"+88018{phone_owner_a}",
        )
        cls.owner_a.set_password("OwnerPassword2026!")
        cls.owner_a.save()
        cls.tenant_a.owner_user_id = str(cls.owner_a.user_id)
        cls.tenant_a.save()

        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        SubscriptionRecord.objects.create(
            subscription_id=f"sub-p063-{cls.suffix}",
            tenant=cls.tenant_a,
            plan_tier="STANDARD",
            status="ACTIVE",
            amount_bdt=Decimal("2500.00"),
            billing_cycle="MONTHLY",
            licensed_branches=5,
            licensed_devices=10,
            start_date=timezone.now().date(),
            renewal_date=(timezone.now() + timedelta(days=365)).date(),
        )

        # 3. Tenant Beta
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"tenant-p063-b-{cls.suffix}",
            name=f"Beta Wellness Pharma {cls.suffix}",
            subdomain=f"beta-p063-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
            billing_email=f"beta_{cls.suffix}@pharmacy.bd",
        )
        phone_cashier_b = random.randint(10000000, 99999999)
        cls.cashier_b = PharmacyUser.objects.create(
            email=f"cashier_b_{cls.suffix}@pharmacy.bd",
            full_name=f"Cashier Beta {cls.suffix}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_b.tenant_id,
            phone_number=f"+88017{phone_cashier_b}",
        )
        cls.cashier_b.set_password("CashierPassword2026!")
        cls.cashier_b.save()

    def setUp(self):
        self.client = APIClient()

    def test_superadmin_metrics_retrieval_and_audit(self):
        """Valid scenario: Super Admin retrieves platform metrics, verifies structure and audit log."""
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.get("/api/v2/platform/console/metrics/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.json()
        self.assertTrue(data["success"])
        metrics = data["data"]
        self.assertGreaterEqual(metrics["total_tenants"], 2)
        self.assertEqual(metrics["system_status"], "HEALTHY")
        self.assertIn("subscription_tier_distribution", metrics)
        self.assertIn("STANDARD", metrics["subscription_tier_distribution"])

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(
            action="PLATFORM_METRICS_VIEWED",
            resource_id="global_metrics",
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.status, "SUCCESS")

    def test_superadmin_tenant_directory_and_deep_inspection(self):
        """Valid scenario: Super Admin lists directory and inspects Tenant Alpha diagnostics."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Directory Listing
        dir_resp = self.client.get(f"/api/v2/platform/console/tenants/?search={self.tenant_a.subdomain}")
        self.assertEqual(dir_resp.status_code, status.HTTP_200_OK)
        dir_data = dir_resp.json()["data"]
        self.assertGreaterEqual(dir_data["total"], 1)
        found_item = next((item for item in dir_data["items"] if item["tenant_id"] == self.tenant_a.tenant_id), None)
        self.assertIsNotNone(found_item)
        self.assertEqual(found_item["plan_tier"], "STANDARD")
        self.assertEqual(found_item["branch_count"], 1)

        # 2. Deep Inspection
        inspect_resp = self.client.get(f"/api/v2/platform/console/tenants/{self.tenant_a.tenant_id}/")
        self.assertEqual(inspect_resp.status_code, status.HTTP_200_OK)
        inspect_data = inspect_resp.json()["data"]
        self.assertEqual(inspect_data["tenant_id"], self.tenant_a.tenant_id)
        self.assertEqual(len(inspect_data["branches"]), 1)
        self.assertIsNotNone(inspect_data["owner"])
        self.assertEqual(inspect_data["owner"]["email"], self.owner_a.email)
        self.assertIsNotNone(inspect_data["subscription"])

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(
            action="PLATFORM_TENANT_INSPECTED",
            resource_id=self.tenant_a.tenant_id,
        ).first()
        self.assertIsNotNone(audit)

    def test_superadmin_platform_administrative_action(self):
        """Valid scenario: Super Admin triggers broadcast announcement with valid reason and audit log."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "action_type": "BROADCAST_ANNOUNCEMENT",
            "reason": "Scheduled core infrastructure upgrade at 03:00 UTC",
            "parameters": {"title": "Maintenance Window", "duration_minutes": 15},
        }
        resp = self.client.post("/api/v2/platform/console/actions/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        res_data = resp.json()["data"]
        self.assertEqual(res_data["action_type"], "BROADCAST_ANNOUNCEMENT")
        self.assertEqual(res_data["status"], "EXECUTED")

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(action="PLATFORM_ACTION_BROADCAST_ANNOUNCEMENT").first()
        self.assertIsNotNone(audit)

    def test_invalid_tenant_inspection_and_action_rejected(self):
        """Invalid scenarios: Nonexistent tenant returns 404, invalid action returns 400/422."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Nonexistent Tenant
        bad_resp = self.client.get("/api/v2/platform/console/tenants/nonexistent-tenant-999/")
        self.assertEqual(bad_resp.status_code, status.HTTP_404_NOT_FOUND)

        # 2. Unsupported Action Type
        bad_action_resp = self.client.post(
            "/api/v2/platform/console/actions/",
            {"action_type": "UNSUPPORTED_ACTION", "reason": "Testing invalid action"},
            format="json",
        )
        self.assertIn(bad_action_resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 3. Action with reason too short
        short_reason_resp = self.client.post(
            "/api/v2/platform/console/actions/",
            {"action_type": "BROADCAST_ANNOUNCEMENT", "reason": "no"},
            format="json",
        )
        self.assertIn(
            short_reason_resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        )

    def test_unauthorized_tenant_users_denied_platform_console(self):
        """Unauthorized scenario: Tenant Owner and Cashier receive HTTP 403 Forbidden."""
        # 1. Tenant Owner attempts to access platform metrics
        self.client.force_authenticate(user=self.owner_a)
        resp_owner = self.client.get("/api/v2/platform/console/metrics/")
        self.assertEqual(resp_owner.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_owner.json()))

        # 2. Cashier attempts to access tenant directory
        self.client.force_authenticate(user=self.cashier_b)
        resp_cashier = self.client.get("/api/v2/platform/console/tenants/")
        self.assertEqual(resp_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_cashier.json()))

        # 3. Anonymous caller
        self.client.force_authenticate(user=None)
        resp_anon = self.client.get("/api/v2/platform/console/metrics/")
        self.assertIn(resp_anon.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_ui_navigation_privilege_separation(self):
        """Two-tenant UI isolation: Tenant Owner and Cashier never see NavGroup.PLATFORM routes in sidebar."""
        nav_router = NavigationRouter()

        # Tenant Owner sidebar
        owner_nav = nav_router.get_sidebar_destinations(user_role="TENANT_OWNER", user_plan=PlanTier.STANDARD)
        self.assertEqual(len(owner_nav[NavGroup.PLATFORM]), 0, "Tenant Owner must never see platform routes")

        # Cashier sidebar
        cashier_nav = nav_router.get_sidebar_destinations(user_role="CASHIER", user_plan=PlanTier.BASIC)
        self.assertEqual(len(cashier_nav[NavGroup.PLATFORM]), 0, "Cashier must never see platform routes")

        # Super Admin sidebar
        super_nav = nav_router.get_sidebar_destinations(user_role="SUPERADMIN", user_plan=PlanTier.PRO)
        self.assertGreaterEqual(len(super_nav[NavGroup.PLATFORM]), 2, "Super Admin must see platform routes")


if __name__ == "__main__":
    unittest.main()
