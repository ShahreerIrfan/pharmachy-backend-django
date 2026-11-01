"""
backend/tests/test_p076_system_announcements.py

Architecture Decision: ADR-0076
Integration Tests for Platform System Announcements & Read Receipts:
1. SuperAdmin broadcast creation and publishing
2. Tenant-specific targeting isolation
3. Role-specific targeting isolation
4. Delivery receipt auto-generation and user acknowledgement
5. Input validation error guardrails
6. Privilege separation (tenant cannot create platform announcements)
7. Audit log verification
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
from apps.core.models import (
    Branch,
    SystemAnnouncement,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP076SystemAnnouncements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p076_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{phone_suffix}",
                "is_active": True,
            },
        )

        # 2. Tenant A
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-ann-a-{cls.suffix}",
            name=f"Announcement Tenant A {cls.suffix}",
            subdomain=f"ann-a-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"b-ann-a-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Branch A",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p076_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"p076_cashier_a_{cls.suffix}@pharmacy.com",
            full_name=f"Cashier A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )

        # 3. Tenant B
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-ann-b-{cls.suffix}",
            name=f"Announcement Tenant B {cls.suffix}",
            subdomain=f"ann-b-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
        )
        cls.branch_b = Branch.objects.create(
            branch_id=f"b-ann-b-{cls.suffix}",
            tenant=cls.tenant_b,
            name="Main Branch B",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p076_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_b.tenant_id,
            branch_id=cls.branch_b.branch_id,
            is_active=True,
        )

    def setUp(self):
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(user=self.superadmin)

        self.client_owner_a = APIClient()
        self.client_owner_a.force_authenticate(user=self.owner_a)

        self.client_cashier_a = APIClient()
        self.client_cashier_a.force_authenticate(user=self.cashier_a)

        self.client_owner_b = APIClient()
        self.client_owner_b.force_authenticate(user=self.owner_b)

    def test_01_superadmin_create_and_publish_announcement(self):
        res_create = self.client_admin.post(
            "/api/v2/platform/announcements/",
            {
                "title": "Scheduled Server Upgrade",
                "message": "Routine cloud server upgrade this weekend from 2am to 4am.",
                "priority": "WARNING",
                "target_type": "ALL",
            },
            format="json",
        )
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        ann_id = res_create.data["data"]["announcement_id"]
        self.assertTrue(ann_id.startswith("ann-"))
        self.assertEqual(res_create.data["data"]["status"], "DRAFT")

        # Publish announcement
        res_pub = self.client_admin.post(
            f"/api/v2/platform/announcements/{ann_id}/publish/",
            format="json",
        )
        self.assertEqual(res_pub.status_code, status.HTTP_200_OK)
        self.assertEqual(res_pub.data["data"]["status"], "PUBLISHED")
        self.assertIsNotNone(res_pub.data["data"]["published_at"])

    def test_02_tenant_targeting_isolation(self):
        # Create announcement targeted ONLY to Tenant A
        res_create = self.client_admin.post(
            "/api/v2/platform/announcements/",
            {
                "title": "Targeted DGDA Alert for Tenant A",
                "message": "Specific inspection reminder for your registered branch.",
                "priority": "CRITICAL",
                "target_type": "TENANT_LIST",
                "target_tenants": [self.tenant_a.tenant_id],
            },
            format="json",
        )
        ann_id = res_create.data["data"]["announcement_id"]
        self.client_admin.post(f"/api/v2/platform/announcements/{ann_id}/publish/")

        # Tenant A owner receives announcement
        res_a = self.client_owner_a.get("/api/v2/tenants/announcements/")
        self.assertEqual(res_a.status_code, status.HTTP_200_OK)
        ids_a = [item["announcement_id"] for item in res_a.data["data"]]
        self.assertIn(ann_id, ids_a)

        # Tenant B owner MUST NOT receive announcement (Tenant isolation)
        res_b = self.client_owner_b.get("/api/v2/tenants/announcements/")
        self.assertEqual(res_b.status_code, status.HTTP_200_OK)
        ids_b = [item["announcement_id"] for item in res_b.data["data"]]
        self.assertNotIn(ann_id, ids_b)

    def test_03_role_targeting_isolation(self):
        # Create announcement targeted ONLY to Pharmacy Owners
        res_create = self.client_admin.post(
            "/api/v2/platform/announcements/",
            {
                "title": "Annual License Renewal Reminder",
                "message": "Pharmacy owners must review their trade licenses this month.",
                "priority": "INFO",
                "target_type": "ROLE_LIST",
                "target_roles": ["PHARMACY_OWNER"],
            },
            format="json",
        )
        ann_id = res_create.data["data"]["announcement_id"]
        self.client_admin.post(f"/api/v2/platform/announcements/{ann_id}/publish/")

        # Owner A receives announcement
        res_owner = self.client_owner_a.get("/api/v2/tenants/announcements/")
        ids_owner = [item["announcement_id"] for item in res_owner.data["data"]]
        self.assertIn(ann_id, ids_owner)

        # Cashier A MUST NOT receive announcement
        res_cashier = self.client_cashier_a.get("/api/v2/tenants/announcements/")
        ids_cashier = [item["announcement_id"] for item in res_cashier.data["data"]]
        self.assertNotIn(ann_id, ids_cashier)

    def test_04_read_receipt_and_acknowledgement(self):
        # Create and publish broadcast
        res_create = self.client_admin.post(
            "/api/v2/platform/announcements/",
            {
                "title": "New DGDA Guideline Available",
                "message": "Please review new guideline document attached in portal.",
                "priority": "INFO",
                "target_type": "ALL",
            },
            format="json",
        )
        ann_id = res_create.data["data"]["announcement_id"]
        self.client_admin.post(f"/api/v2/platform/announcements/{ann_id}/publish/")

        # Owner A fetches announcements -> delivery receipt auto-created
        res_feed = self.client_owner_a.get("/api/v2/tenants/announcements/")
        item = next(i for i in res_feed.data["data"] if i["announcement_id"] == ann_id)
        self.assertFalse(item["is_read"])
        self.assertIsNone(item["read_at"])

        # Owner A acknowledges
        res_ack = self.client_owner_a.post(
            f"/api/v2/tenants/announcements/{ann_id}/acknowledge/",
            format="json",
        )
        self.assertEqual(res_ack.status_code, status.HTTP_200_OK)
        self.assertTrue(res_ack.data["data"]["acknowledged"])
        self.assertIsNotNone(res_ack.data["data"]["read_at"])

        # SuperAdmin views announcement stats
        res_stats = self.client_admin.get(f"/api/v2/platform/announcements/{ann_id}/stats/")
        self.assertEqual(res_stats.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(res_stats.data["data"]["total_delivered"], 1)
        self.assertGreaterEqual(res_stats.data["data"]["total_read"], 1)

    def test_05_input_validation_errors(self):
        # Short title (< 3 chars)
        res_title = self.client_admin.post(
            "/api/v2/platform/announcements/",
            {"title": "ab", "message": "Valid message text"},
            format="json",
        )
        self.assertIn(res_title.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Invalid priority
        res_pri = self.client_admin.post(
            "/api/v2/platform/announcements/",
            {"title": "Valid Title", "message": "Valid message text", "priority": "SUPER_HIGH"},
            format="json",
        )
        self.assertIn(res_pri.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_06_rbac_separation(self):
        # Pharmacy Owner cannot create platform announcement
        res_owner = self.client_owner_a.post(
            "/api/v2/platform/announcements/",
            {"title": "Unauthorized Announcement", "message": "Trying to bypass SuperAdmin"},
            format="json",
        )
        self.assertEqual(res_owner.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(res_owner.data))

    def test_07_audit_logging(self):
        logs = AuditLogEntry.objects.filter(action="ANNOUNCEMENT_CREATED")
        self.assertTrue(logs.exists())

    @classmethod
    def tearDownClass(cls):
        try:
            Tenant.objects.filter(tenant_id__in=[cls.tenant_a.tenant_id, cls.tenant_b.tenant_id]).delete()
            SystemAnnouncement.objects.filter(title__contains=cls.suffix).delete()
        except Exception:
            pass
        super().tearDownClass()


if __name__ == "__main__":
    unittest.main()
