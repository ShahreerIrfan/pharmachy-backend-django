"""
Integration tests for P116: Branch operating settings, receipt formats,
hours, storage zones, low-stock thresholds, and device policies (ADR-0116).
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
    PlanTier,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "branch_operating_settings.schema.json"


class BranchOperatingSettingsIntegrationTests(TestCase):
    """End-to-end integration tests for P116 Branch Operating Settings."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.suffix = uuid.uuid4().hex[:8]
        self.password = "StrongPassword123!"

        # 1. Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p116-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-p116-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status="ACTIVE",
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

        # 2. Users for Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Pharmacy Owner A",
            password=self.password,
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.manager_a1 = PharmacyUser.objects.create_user(
            email=f"mgr1_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Manager Dhanmondi",
            password=self.password,
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
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

        # 3. Inactive Staff User
        self.user_inactive = PharmacyUser.objects.create_user(
            email=f"inactive_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Inactive Staff",
            password=self.password,
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.user_inactive.is_active = False
        self.user_inactive.save()

        # 4. Tenant B (Foreign)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p116-b-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-p116-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status="ACTIVE",
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@square.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Foreign Owner B",
            password=self.password,
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

    def _login(self, user):
        resp = self.client.post(
            "/api/v2/auth/login/",
            data={"identifier": user.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="test-device",
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        return resp.json()["access"]

    def test_retrieve_branch_settings_and_schema_validation(self):
        """Retrieves default branch operating settings matching JSON Schema."""
        token = self._login(self.owner_a)
        resp = self.client.get(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["branch_id"], self.branch_a1.branch_id)
        self.assertIn("receipt_format", data)
        self.assertIn("operating_hours", data)
        self.assertIn("storage_zones", data)
        self.assertIn("device_policies", data)

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

    def test_owner_updates_branch_operating_settings(self):
        """Pharmacy Owner updates receipt format and operating hours."""
        token = self._login(self.owner_a)
        new_receipt = {
            "paper_width": "58mm",
            "header_text": "Lazz Pharma 24/7 Superstore",
            "footer_text": "Thank you for trusting our healthcare service.",
            "show_tax_breakdown": True,
            "show_pharmacist_signature": False,
            "language": "BN",
        }
        resp = self.client.patch(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            data={"receipt_format": new_receipt},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["receipt_format"]["header_text"], "Lazz Pharma 24/7 Superstore")
        self.assertEqual(data["receipt_format"]["paper_width"], "58mm")

    def test_branch_manager_updates_assigned_branch(self):
        """Branch manager updates device policies for their own branch."""
        token = self._login(self.manager_a1)
        resp = self.client.patch(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            data={"device_policies": {"max_active_pos_terminals": 8, "allow_offline_pos_sales": True, "max_offline_hours": 48, "require_cash_drawer_kick_on_cash_sale": True}},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["device_policies"]["max_active_pos_terminals"], 8)

    def test_branch_manager_cannot_update_foreign_branch(self):
        """Branch manager cannot update operating settings for another branch."""
        token = self._login(self.manager_a1)
        resp = self.client.patch(
            f"/api/v2/branches/operating-settings/{self.branch_a2.branch_id}/",
            data={"low_stock_thresholds": {"default_min_strips": 50, "default_critical_strips": 10, "auto_reorder_enabled": True}},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_CROSS_BRANCH_VIOLATION")

    def test_cashier_can_read_but_cannot_mutate_settings(self):
        """Cashier has read-only access for POS configuration; mutations are rejected."""
        token = self._login(self.cashier_a1)

        # Read allowed
        read_resp = self.client.get(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(read_resp.status_code, status.HTTP_200_OK)

        # Write rejected
        write_resp = self.client.patch(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            data={"tax_defaults": {"default_vat_rate": "15.00"}},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(write_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(write_resp.json()["error_code"], "ERR_PERMISSION_DENIED")

    def test_cross_tenant_access_denied(self):
        """Tenant B owner cannot access Tenant A branch settings."""
        token_b = self._login(self.owner_b)
        resp = self.client.get(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            HTTP_AUTHORIZATION=f"Bearer {token_b}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_suspended_tenant_subscription_blocks_mutations(self):
        """Suspended tenant subscription blocks settings update."""
        token = self._login(self.owner_a)
        self.tenant_a.status = "SUSPENDED"
        self.tenant_a.save()

        resp = self.client.patch(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            data={"device_policies": {"max_active_pos_terminals": 2}},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    def test_inactive_staff_role_denied(self):
        """Deactivated staff account cannot access branch operating settings."""
        self.user_inactive.is_active = True
        self.user_inactive.save()
        token = self._login(self.user_inactive)
        self.user_inactive.is_active = False
        self.user_inactive.save()

        resp = self.client.get(
            f"/api/v2/branches/operating-settings/{self.branch_a1.branch_id}/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

