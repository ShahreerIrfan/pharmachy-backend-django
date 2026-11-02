"""
Integration tests for P119: Role migration & grants versioning (ADR-0119).
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

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    CustomRoleDefinition,
    PlanTier,
    StaffRole,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "role_migration_grants.schema.json"


class RoleMigrationGrantsIntegrationTests(TestCase):
    """End-to-end integration tests for P119 Role Migration & Grants."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Primary Tenant A (Downgraded to BASIC)
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p119-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-p119-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
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
        self.manager_a = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@lazz.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@lazz.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier Rahim",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 3. Foreign Tenant B & User
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p119-b-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-p119-{self.suffix}",
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

        # 4. Custom Role with mixed permissions (some unentitled on BASIC, some forbidden)
        self.role_1 = CustomRoleDefinition.objects.create(
            role_id=f"role-1-{self.suffix}",
            tenant=self.tenant_a,
            role_code="SR_BILLING",
            display_name="Senior Billing Specialist",
            base_role=StaffRole.CASHIER,
            permission_keys=[
                "sales:checkout",
                "inventory:view_stock",
                "inventory:transfer",            # unentitled in BASIC (requires PRO)
                "finance:executive_dashboard",   # unentitled in BASIC (requires PRO)
                "stock:dispense_expired",        # FORBIDDEN
            ],
            grant_version=1,
        )

    def test_01_owner_migrates_role_on_plan_downgrade_and_validates_json_schema(self):
        """Owner migrates custom role on plan downgrade to Basic, pruning unentitled and forbidden keys."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {
                "role_id": self.role_1.role_id,
                "reason": "PLAN_DOWNGRADE",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        data = resp.json()

        self.assertEqual(data["role_id"], self.role_1.role_id)
        self.assertEqual(data["previous_version"], 1)
        self.assertEqual(data["new_version"], 2)
        self.assertFalse(data["is_dry_run"])

        # Check retained vs removed
        self.assertIn("sales:checkout", data["retained_permissions"])
        self.assertIn("inventory:view_stock", data["retained_permissions"])
        self.assertIn("inventory:transfer", data["removed_permissions"])
        self.assertIn("finance:executive_dashboard", data["removed_permissions"])
        self.assertIn("stock:dispense_expired", data["forbidden_grants_blocked"])

        # Verify DB mutation
        self.role_1.refresh_from_db()
        self.assertEqual(self.role_1.grant_version, 2)
        self.assertEqual(set(self.role_1.permission_keys), {"sales:checkout", "inventory:view_stock"})

        # Validate with JSON schema
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

    def test_02_forbidden_permissions_stripped_and_never_restored(self):
        """Forbidden permissions are permanently blocked and cannot be retained."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {
                "role_id": self.role_1.role_id,
                "reason": "COMPLIANCE_PRUNING",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn("stock:dispense_expired", data["forbidden_grants_blocked"])
        self.assertNotIn("stock:dispense_expired", data["retained_permissions"])

    def test_03_dry_run_simulation_does_not_mutate_state(self):
        """Dry-run endpoint returns simulated version increment without mutating database record."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/roles/migrations/dry-run/",
            {
                "role_id": self.role_1.role_id,
                "reason": "TEMPLATE_EVOLUTION",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data["is_dry_run"])
        self.assertEqual(data["new_version"], 2)

        # Database state remains unchanged
        self.role_1.refresh_from_db()
        self.assertEqual(self.role_1.grant_version, 1)
        self.assertEqual(len(self.role_1.permission_keys), 5)

    def test_04_missing_role_id_rejected(self):
        """POST without role_id returns 400 Bad Request."""
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {"reason": "PLAN_DOWNGRADE"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error_code"], "ERR_INVALID_MIGRATION_INPUT")

    def test_05_cashier_role_denied_access(self):
        """Cashier role cannot execute role migration (403 Forbidden)."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {"role_id": self.role_1.role_id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_STAFF_ROLE_DENIED")

    def test_06_branch_manager_role_denied_access(self):
        """Branch Manager role cannot execute role migration (403 Forbidden)."""
        self.client.force_authenticate(user=self.manager_a)
        resp = self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {"role_id": self.role_1.role_id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_STAFF_ROLE_DENIED")

    def test_07_cross_tenant_migration_forbidden(self):
        """Foreign tenant user cannot migrate Tenant A role (403 Forbidden)."""
        self.client.force_authenticate(user=self.owner_b)
        resp = self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {"role_id": self.role_1.role_id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_08_suspended_tenant_migration_blocked(self):
        """Suspended tenant subscription blocks role grant mutations."""
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {"role_id": self.role_1.role_id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    def test_09_list_migration_history_for_role(self):
        """Listing migration history returns persisted migration audit records."""
        self.client.force_authenticate(user=self.owner_a)

        # Execute migration
        self.client.post(
            "/api/v2/roles/migrations/migrate/",
            {"role_id": self.role_1.role_id, "reason": "PLAN_DOWNGRADE"},
            format="json",
        )

        resp = self.client.get(f"/api/v2/roles/migrations/?role_id={self.role_1.role_id}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["role_id"], self.role_1.role_id)
        self.assertEqual(data["results"][0]["new_version"], 2)
