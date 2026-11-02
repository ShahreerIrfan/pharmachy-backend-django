"""
Integration tests for P118: Staff audit timeline with actor and device attribution (ADR-0118).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path

from django.test import TestCase
from jsonschema import validate
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.staff_audit_timeline_service import StaffAuditTimelineService
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "staff_audit_timeline.schema.json"


class StaffAuditTimelineIntegrationTests(TestCase):
    """End-to-end integration tests for P118 Staff Audit Timeline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Primary Tenant & Branches
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p118-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-p118-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Users in Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@lazz.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Owner A",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.manager_a1 = PharmacyUser.objects.create_user(
            email=f"mgr1_{self.suffix}@lazz.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@lazz.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier Rahim",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 3. Foreign Tenant & User
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p118-b-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-p118-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Foreign Owner B",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 4. Pre-seed Audit Log Entries for Tenant A
        # Event 1: Role Change
        StaffAuditTimelineService.record_staff_audit_event(
            tenant_id=self.tenant_a.tenant_id,
            event_category="ROLE_CHANGE",
            action="ROLE_ELEVATED",
            actor_id=str(self.owner_a.user_id),
            actor_role="PHARMACY_OWNER",
            actor_email=self.owner_a.email,
            branch_id=self.branch_a1.branch_id,
            device_id="DEV-POS-01",
            ip_address="192.168.1.10",
            target_staff_id=str(self.cashier_a1.user_id),
            target_staff_name=self.cashier_a1.full_name,
            status="SUCCESS",
            summary_en="Promoted cashier to senior billing staff",
            summary_bn="ক্যাশিয়ারকে সিনিয়র পদে উন্নীত করা হয়েছে",
        )

        # Event 2: Permission Denial
        StaffAuditTimelineService.record_staff_audit_event(
            tenant_id=self.tenant_a.tenant_id,
            event_category="PERMISSION_DENIAL",
            action="DRAWER_ASSIGNMENT_DENIED",
            actor_id=str(self.cashier_a1.user_id),
            actor_role="CASHIER",
            actor_email=self.cashier_a1.email,
            branch_id=self.branch_a1.branch_id,
            device_id="DEV-POS-02",
            ip_address="192.168.1.12",
            status="DENIED",
            summary_en="Cashier attempted unauthorized drawer assignment",
            summary_bn="ক্যাশিয়ার অননুমোদিত ড্রয়ার বরাদ্দের চেষ্টা করেছেন",
        )

        # Event 3: Data Export
        StaffAuditTimelineService.record_staff_audit_event(
            tenant_id=self.tenant_a.tenant_id,
            event_category="DATA_EXPORT",
            action="SALES_LEDGER_EXPORTED",
            actor_id=str(self.manager_a1.user_id),
            actor_role="BRANCH_MANAGER",
            actor_email=self.manager_a1.email,
            branch_id=self.branch_a1.branch_id,
            device_id="DEV-DESK-01",
            ip_address="192.168.1.20",
            status="SUCCESS",
            summary_en="Exported monthly branch sales ledger CSV",
            summary_bn="মাসিক বিক্রয় লেজার এক্সপোর্ট সম্পন্ন",
        )

        # Event 4: Refund in Branch A2
        StaffAuditTimelineService.record_staff_audit_event(
            tenant_id=self.tenant_a.tenant_id,
            event_category="REFUND",
            action="REFUND_ISSUED",
            actor_id=str(self.owner_a.user_id),
            actor_role="PHARMACY_OWNER",
            actor_email=self.owner_a.email,
            branch_id=self.branch_a2.branch_id,
            device_id="DEV-POS-99",
            ip_address="192.168.2.5",
            status="SUCCESS",
            summary_en="Refund BDT 1,200 authorized for damaged medicine",
            summary_bn="ক্ষতিগ্রস্ত ওষুধের জন্য ১,২০০ টাকা রিফান্ড মঞ্জুর",
        )

        # Event 5: Offline Sync Event
        StaffAuditTimelineService.record_staff_audit_event(
            tenant_id=self.tenant_a.tenant_id,
            event_category="OFFLINE_EVENT",
            action="OFFLINE_INVOICES_REPLAYED",
            actor_id=str(self.cashier_a1.user_id),
            actor_role="CASHIER",
            actor_email=self.cashier_a1.email,
            branch_id=self.branch_a1.branch_id,
            device_id="DEV-POS-01",
            ip_address="192.168.1.10",
            status="SUCCESS",
            summary_en="Replayed 8 offline queued sales invoices",
            summary_bn="৮টি অফলাইন বিক্রয় চালান সিঙ্ক সম্পন্ন",
        )

    def test_01_owner_retrieves_staff_timeline_and_validates_json_schema(self):
        """Owner retrieves timeline across all branches and validates with JSON schema."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.get("/api/v2/staff/audit-timeline/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        data = resp.json()
        self.assertGreaterEqual(data["count"], 5)
        self.assertEqual(len(data["results"]), data["count"])

        # Check attribution on first result
        first = data["results"][0]
        self.assertIn("timeline_id", first)
        self.assertIn("timestamp", first)
        self.assertIn("event_category", first)
        self.assertIn("actor_id", first)
        self.assertIn("actor_role", first)
        self.assertIn("summary_en", first)
        self.assertIn("summary_bn", first)

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

    def test_02_record_new_timeline_event_via_api(self):
        """Manager records a managerial approval event via POST and queries it."""
        self.client.force_authenticate(user=self.manager_a1)
        post_resp = self.client.post(
            "/api/v2/staff/audit-timeline/",
            {
                "event_category": "APPROVAL",
                "action": "DISCOUNT_APPROVED",
                "target_staff_id": str(self.cashier_a1.user_id),
                "target_staff_name": self.cashier_a1.full_name,
                "summary_en": "Special patient discount of 15% approved",
                "summary_bn": "রোগীর জন্য ১৫% বিশেষ ছাড় অনুমোদন করা হয়েছে",
            },
            format="json",
        )
        self.assertEqual(post_resp.status_code, status.HTTP_201_CREATED, post_resp.data)
        created_data = post_resp.json()
        self.assertEqual(created_data["action"], "DISCOUNT_APPROVED")
        self.assertEqual(created_data["event_category"], "APPROVAL")

        # Query back and verify
        get_resp = self.client.get("/api/v2/staff/audit-timeline/?event_category=APPROVAL")
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(get_resp.json()["count"], 1)
        self.assertEqual(get_resp.json()["results"][0]["action"], "DISCOUNT_APPROVED")

    def test_03_invalid_event_category_rejected(self):
        """Querying with an invalid event_category returns 400 Bad Request."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.get("/api/v2/staff/audit-timeline/?event_category=INVALID_CATEGORY")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error_code"], "ERR_INVALID_EVENT_CATEGORY")

    def test_04_missing_required_fields_on_create_rejected(self):
        """POST without action or event_category returns 400 Bad Request."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/staff/audit-timeline/",
            {"summary_en": "Missing fields"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error_code"], "ERR_INVALID_TIMELINE_INPUT")

    def test_05_cashier_role_denied_access(self):
        """Cashier role is unauthorized to view staff audit timeline (403 Forbidden)."""
        self.client.force_authenticate(user=self.cashier_a1)
        resp = self.client.get("/api/v2/staff/audit-timeline/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_STAFF_ROLE_DENIED")

    def test_06_branch_manager_scoped_to_own_branch(self):
        """Branch Manager can view own branch events, but foreign branch is rejected."""
        self.client.force_authenticate(user=self.manager_a1)

        # 1. Own branch query succeeds
        resp1 = self.client.get(f"/api/v2/staff/audit-timeline/?branch_id={self.branch_a1.branch_id}")
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        for item in resp1.json()["results"]:
            self.assertEqual(item["branch_id"], self.branch_a1.branch_id)

        # 2. Foreign branch query forbidden
        resp2 = self.client.get(f"/api/v2/staff/audit-timeline/?branch_id={self.branch_a2.branch_id}")
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp2.json()["error_code"], "ERR_CROSS_BRANCH_VIOLATION")

    def test_07_cross_tenant_access_forbidden(self):
        """Foreign tenant user cannot inspect Tenant A audit timeline."""
        self.client.force_authenticate(user=self.owner_b)
        resp = self.client.get(f"/api/v2/staff/audit-timeline/?tenant_id={self.tenant_a.tenant_id}")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_08_suspended_tenant_read_access_blocked(self):
        """Suspended tenant subscription blocks staff audit timeline read access."""
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.get("/api/v2/staff/audit-timeline/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED_READ_BLOCKED")

    def test_09_filter_by_device_id_and_category(self):
        """Filter timeline strictly by device_id attribution and event_category."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.get("/api/v2/staff/audit-timeline/?device_id=DEV-POS-01&event_category=OFFLINE_EVENT")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["device_id"], "DEV-POS-01")
        self.assertEqual(data["results"][0]["action"], "OFFLINE_INVOICES_REPLAYED")
