"""
backend/tests/test_p065_tenant_search_filters.py
Architecture Decision: ADR-0005, ADR-0036, ADR-0051 & ADR-0065
Comprehensive test suite for Platform Super Admin Tenant Search, Filtering, Status Chips,
and Patient Privacy Boundaries:
- Valid scenario: Super Admin searches tenants by keyword and status filter, receives pagination,
  bilingual status chips (EN/BN), and approved administrative actions.
- Patient Privacy Guarantee: Verified exclusion of patient clinical data and prescriptions.
- Invalid scenario: Invalid status filter, invalid plan tier, page_size > 100, or bad date format returns HTTP 400/422.
- Unauthorized scenario: Tenant Owner, Cashier, and anonymous callers receive HTTP 403/401 with PLATFORM_ACCESS_DENIED.
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
from apps.core.models import Branch, Tenant
from apps.identity.models import PharmacyUser, UserRole


class TestP065TenantSearchFilters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p065_super_{cls.suffix}@platform.internal",
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

        # 2. Tenant Alpha (ACTIVE)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"tenant-p065-a-{cls.suffix}",
            name=f"Delta Care Pharma {cls.suffix}",
            subdomain=f"delta-p065-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
            billing_email=f"delta_{cls.suffix}@pharmacy.bd",
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

        # 3. Tenant Beta (TRIAL)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"tenant-p065-b-{cls.suffix}",
            name=f"Delta Wellness Center {cls.suffix}",
            subdomain=f"wellness-p065-{cls.suffix}",
            plan_tier="PRO",
            status="TRIAL",
            billing_email=f"wellness_{cls.suffix}@pharmacy.bd",
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

        # 4. Tenant Gamma (SUSPENDED)
        cls.tenant_c = Tenant.objects.create(
            tenant_id=f"tenant-p065-c-{cls.suffix}",
            name=f"Apex MediStore {cls.suffix}",
            subdomain=f"apex-p065-{cls.suffix}",
            plan_tier="BASIC",
            status="SUSPENDED",
            billing_email=f"apex_{cls.suffix}@pharmacy.bd",
        )

    def setUp(self):
        self.client = APIClient()

    def test_superadmin_search_and_filter_valid_with_chips_and_actions(self):
        """Valid scenario: Super Admin searches 'Delta' with status=ACTIVE, receives status chips and approved actions."""
        self.client.force_authenticate(user=self.superadmin)
        url = f"/api/v2/platform/console/tenants/?search=Delta%20Care%20Pharma%20{self.suffix}&status=ACTIVE&page=1&page_size=10"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.json()
        self.assertTrue(data["success"])
        res_data = data["data"]
        self.assertGreaterEqual(res_data["total"], 1)
        self.assertEqual(res_data["page"], 1)
        self.assertEqual(res_data["page_size"], 10)
        self.assertIn("total_pages", res_data)

        # Inspect Tenant Item
        items = res_data["items"]
        found = next((i for i in items if i["tenant_id"] == self.tenant_a.tenant_id), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "ACTIVE")
        self.assertEqual(found["plan_tier"], "STANDARD")

        # Status Chip verification
        chip = found["status_chip"]
        self.assertEqual(chip["tone"], "success")
        self.assertEqual(chip["label_en"], "Active")
        self.assertEqual(chip["label_bn"], "সক্রিয়")

        # Approved Actions verification
        actions = found["approved_actions"]
        self.assertIn("SUSPEND", actions)
        self.assertIn("ARCHIVE", actions)

        # Patient Privacy Verification: Ensure zero clinical or patient PII
        for forbidden in ["patient_name", "prescriptions", "patient_phone", "dispensing_history"]:
            self.assertNotIn(forbidden, found, f"Forbidden patient key '{forbidden}' found in tenant payload!")

        # Audit Log verification
        audit = AuditLogEntry.objects.filter(
            action="PLATFORM_TENANT_SEARCH",
            resource_type="TENANT_DIRECTORY",
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.status, "SUCCESS")

    def test_invalid_filter_inputs_rejected(self):
        """Invalid scenarios: Invalid status, plan_tier, page_size > 100, or invalid date format returns HTTP 400/422."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Invalid status
        resp_status = self.client.get("/api/v2/platform/console/tenants/?status=INVALID_STATUS")
        self.assertIn(resp_status.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("status", str(resp_status.json()))

        # 2. Invalid plan tier
        resp_plan = self.client.get("/api/v2/platform/console/tenants/?plan_tier=ULTRA_MAX")
        self.assertIn(resp_plan.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("plan_tier", str(resp_plan.json()))

        # 3. Excessive page_size (> 100)
        resp_size = self.client.get("/api/v2/platform/console/tenants/?page_size=500")
        self.assertIn(resp_size.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("page_size", str(resp_size.json()))

        # 4. Invalid date format
        resp_date = self.client.get("/api/v2/platform/console/tenants/?created_after=not-a-valid-date")
        self.assertIn(resp_date.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("created_after", str(resp_date.json()))

    def test_unauthorized_tenant_users_denied_search(self):
        """Unauthorized scenario: Tenant Owner, Cashier, and anonymous callers receive HTTP 403/401."""
        # Tenant Owner
        self.client.force_authenticate(user=self.owner_a)
        resp_owner = self.client.get("/api/v2/platform/console/tenants/")
        self.assertEqual(resp_owner.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_owner.json()))

        # Cashier
        self.client.force_authenticate(user=self.cashier_b)
        resp_cashier = self.client.get("/api/v2/platform/console/tenants/")
        self.assertEqual(resp_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_cashier.json()))

        # Anonymous caller
        self.client.force_authenticate(user=None)
        resp_anon = self.client.get("/api/v2/platform/console/tenants/")
        self.assertIn(resp_anon.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


if __name__ == "__main__":
    unittest.main()
