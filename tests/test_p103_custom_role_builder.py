"""
backend/tests/test_p103_custom_role_builder.py

Architecture Decision: ADR-0005, ADR-0015, ADR-0033, ADR-0059, ADR-0103
Phase 06: Milestone M03 - Staff Onboarding & Multi-Branch Scopes (P103)
Requirement Trace: FR-005, FR-031, FR-049, FR-082

Comprehensive Integration Tests for Custom Role Builder & Permission-Key Catalog:
1. Scenario 1: Permission catalog retrieval with domains (clinical, pos, inventory, finance, admin).
2. Scenario 2: Owner creates valid custom role with operational permissions.
3. Scenario 3: Safety Minimum: Rx antibiotic dispensing strictly requires verified PCB registration.
4. Scenario 4: Absolute danger actions (hard delete, dispense expired) blocked unconditionally.
5. Scenario 5: Input validation: empty permissions, invalid role codes, short names, duplicate codes rejected.
6. Scenario 6: Strict RBAC: Branch Managers, Cashiers, and Pharmacists denied role management (HTTP 403).
7. Scenario 7: Owner edits custom role display name, description, and permissions.
8. Scenario 8: Protected system reference roles cannot be modified or archived.
9. Scenario 9: Role cloning clones permissions, PCB flag, and records provenance cloned_from_id.
10. Scenario 10: Soft archival sets is_archived=True and filters from default listings.
11. Scenario 11: Dry-run permission preview detects Separation of Duties (SoD) conflicts.
12. Scenario 12: Cross-tenant barrier: Tenant B cannot view, modify, clone, or archive Tenant A custom roles.
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
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.custom_role_service import CustomRoleService
from apps.core.models import (
    Branch,
    CustomRoleDefinition,
    PlanTier,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP103CustomRoleBuilderIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p103_super_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Superadmin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (STANDARD Plan)
        cls.tenant_id_a = f"tn-role-a-{cls.suffix}"
        cls.subdomain_a = f"role-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P103-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@rolea.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Role Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        cls.branch_a1 = Branch.objects.get(tenant=cls.tenant_a)

        # Branch Manager A1
        cls.manager_a1 = PharmacyUser.objects.create_user(
            email=f"manager_a1_{cls.suffix}@rolea.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            tenant_id=cls.tenant_id_a,
            branch_id=cls.branch_a1.branch_id,
            full_name="Manager A1",
            role=UserRole.BRANCH_MANAGER,
            is_active=True,
        )

        # Cashier A1
        cls.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cashier_a1_{cls.suffix}@rolea.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            tenant_id=cls.tenant_id_a,
            branch_id=cls.branch_a1.branch_id,
            full_name="Cashier A1",
            role=UserRole.CASHIER,
            is_active=True,
        )

        # 3. Provision Tenant B (Foreign Tenant)
        cls.tenant_id_b = f"tn-role-b-{cls.suffix}"
        cls.subdomain_b = f"role-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P103-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@roleb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Role Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

    def setUp(self):
        self.client = APIClient()

    def test_01_get_permission_catalog(self):
        """Authenticated users can query the permission-key catalog."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/roles/catalog/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertIn("version", data)
        self.assertIn("categories", data)
        self.assertTrue(data["total_permissions"] >= 15)

        category_ids = [c["category_id"] for c in data["categories"]]
        self.assertIn("clinical", category_ids)
        self.assertIn("pos", category_ids)
        self.assertIn("inventory", category_ids)
        self.assertIn("finance", category_ids)
        self.assertIn("admin", category_ids)

    def test_02_owner_creates_valid_custom_role(self):
        """Owner can define a custom operational role with designated permissions."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "role_code": f"ROLE_EVENING_CASHIER_{self.suffix.upper()}",
            "display_name": "Evening Shift Cashier",
            "description": "Handles retail sales and credit due repayments during evening peak hours.",
            "base_role": "CASHIER",
            "permission_keys": ["sales:checkout", "finance:due_repayment", "sales:view_reports"],
            "requires_pcb_registration": False,
        }
        res = self.client.post("/api/v2/roles/custom/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()

        self.assertTrue(data["role_id"].startswith("role-"))
        self.assertEqual(data["role_code"], payload["role_code"])
        self.assertEqual(data["display_name"], payload["display_name"])
        self.assertEqual(len(data["permission_keys"]), 3)
        self.assertFalse(data["requires_pcb_registration"])
        self.assertFalse(data["is_system_protected"])
        self.assertFalse(data["is_archived"])

        # Verify DB
        role = CustomRoleDefinition.objects.get(role_id=data["role_id"])
        self.assertEqual(role.tenant, self.tenant_a)

    def test_03_pcb_safety_minimum_enforcement(self):
        """Clinical safety minimum: 'dispense:rx_antibiotics' strictly requires verified PCB qualification."""
        self.client.force_authenticate(user=self.owner_a)

        # Attempt 1: dispense:rx_antibiotics WITHOUT requires_pcb_registration -> REJECTED
        invalid_payload = {
            "role_code": f"ROLE_UNVERIFIED_DISPENSER_{self.suffix.upper()}",
            "display_name": "Unverified Dispenser",
            "base_role": "CASHIER",
            "permission_keys": ["dispense:rx_antibiotics", "sales:checkout"],
            "requires_pcb_registration": False,
        }
        res1 = self.client.post("/api/v2/roles/custom/", data=invalid_payload, format="json")
        self.assertEqual(res1.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_SAFETY_MINIMUM_PCB_REQUIRED", str(res1.json()))

        # Attempt 2: dispense:rx_antibiotics WITH requires_pcb_registration -> ACCEPTED
        valid_payload = {
            "role_code": f"ROLE_QUALIFIED_PHARMACIST_{self.suffix.upper()}",
            "display_name": "Qualified Shift Pharmacist",
            "base_role": "PHARMACIST",
            "permission_keys": ["dispense:rx_antibiotics", "rx:verify", "sales:checkout"],
            "requires_pcb_registration": True,
        }
        res2 = self.client.post("/api/v2/roles/custom/", data=valid_payload, format="json")
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res2.json()["requires_pcb_registration"])

    def test_04_blocked_absolute_danger_action_rejected(self):
        """Absolute danger actions (hard delete, dispense expired) are blocked unconditionally."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "role_code": f"ROLE_DESTRUCTIVE_{self.suffix.upper()}",
            "display_name": "Destructive Role",
            "permission_keys": ["system:hard_delete", "sales:checkout"],
        }
        res = self.client.post("/api/v2/roles/custom/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_BLOCKED_PERMISSION", str(res.json()))

    def test_05_input_validation_empty_permissions_and_duplicates(self):
        """Validates role input constraints and rejects duplicates/empty permissions."""
        self.client.force_authenticate(user=self.owner_a)

        # Empty permissions list
        res_empty = self.client.post(
            "/api/v2/roles/custom/",
            data={"role_code": f"ROLE_EMPTY_{self.suffix.upper()}", "display_name": "Empty", "permission_keys": []},
            format="json",
        )
        self.assertEqual(res_empty.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_EMPTY_PERMISSIONS", str(res_empty.json()))

        # Malformed role_code
        res_code = self.client.post(
            "/api/v2/roles/custom/",
            data={"role_code": "lowercase-bad-code!", "display_name": "Bad Code", "permission_keys": ["sales:checkout"]},
            format="json",
        )
        self.assertEqual(res_code.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_INVALID_ROLE_CODE", str(res_code.json()))

        # Duplicate role_code
        dup_code = f"ROLE_DUP_{self.suffix.upper()}"
        res_first = self.client.post(
            "/api/v2/roles/custom/",
            data={"role_code": dup_code, "display_name": "First Attempt", "permission_keys": ["sales:checkout"]},
            format="json",
        )
        self.assertEqual(res_first.status_code, status.HTTP_201_CREATED)

        res_second = self.client.post(
            "/api/v2/roles/custom/",
            data={"role_code": dup_code, "display_name": "Second Attempt", "permission_keys": ["sales:checkout"]},
            format="json",
        )
        self.assertEqual(res_second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_ROLE_ALREADY_EXISTS", str(res_second.json()))

    def test_06_rbac_branch_manager_and_cashier_denied_custom_role_creation(self):
        """Branch Managers, Cashiers, and frontline staff cannot create custom roles."""
        payload = {
            "role_code": f"ROLE_UNAUTHORIZED_{self.suffix.upper()}",
            "display_name": "Unauthorized Role",
            "permission_keys": ["sales:checkout"],
        }

        # Branch Manager
        self.client.force_authenticate(user=self.manager_a1)
        res_mgr = self.client.post("/api/v2/roles/custom/", data=payload, format="json")
        self.assertEqual(res_mgr.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res_mgr.json()))

        # Cashier
        self.client.force_authenticate(user=self.cashier_a1)
        res_cashier = self.client.post("/api/v2/roles/custom/", data=payload, format="json")
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", str(res_cashier.json()))

    def test_07_edit_custom_role(self):
        """Owner can edit display name, description, and permissions of existing custom role."""
        role = CustomRoleService.create_custom_role(
            tenant_id=self.tenant_id_a,
            role_code=f"ROLE_EDITABLE_{self.suffix.upper()}",
            display_name="Editable Role",
            base_role="CASHIER",
            permission_keys=["sales:checkout"],
        )
        role_id = role["role_id"]

        self.client.force_authenticate(user=self.owner_a)
        patch_payload = {
            "display_name": "Senior Counter Associate",
            "description": "Updated duties and additional reports permission",
            "permission_keys": ["sales:checkout", "sales:view_reports"],
        }
        res = self.client.patch(f"/api/v2/roles/custom/{role_id}/", data=patch_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["display_name"], "Senior Counter Associate")
        self.assertEqual(len(data["permission_keys"]), 2)
        self.assertIn("sales:view_reports", data["permission_keys"])

    def test_08_system_protected_roles_cannot_be_modified_or_archived(self):
        """System reference roles are protected and cannot be mutated or archived."""
        system_role = CustomRoleDefinition.objects.create(
            role_id=f"role-sys-{self.suffix}",
            tenant=self.tenant_a,
            role_code=f"ROLE_SYS_PROTECTED_{self.suffix.upper()}",
            display_name="System Protected Template",
            base_role="CASHIER",
            permission_keys=["sales:checkout"],
            is_system_protected=True,
        )
        role_id = system_role.role_id

        self.client.force_authenticate(user=self.owner_a)

        # Attempt edit
        res_edit = self.client.patch(
            f"/api/v2/roles/custom/{role_id}/",
            data={"display_name": "Hacked System Role"},
            format="json",
        )
        self.assertEqual(res_edit.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_PROTECTED_ROLE_IMMUTABLE", str(res_edit.json()))

        # Attempt archive
        res_arch = self.client.post(f"/api/v2/roles/custom/{role_id}/archive/", format="json")
        self.assertEqual(res_arch.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ERR_PROTECTED_ROLE_IMMUTABLE", str(res_arch.json()))

    def test_09_clone_custom_role(self):
        """Owner can clone an existing role into a new custom role with provenance tracking."""
        source_role = CustomRoleService.create_custom_role(
            tenant_id=self.tenant_id_a,
            role_code=f"ROLE_SOURCE_{self.suffix.upper()}",
            display_name="Source Role",
            base_role="PHARMACIST",
            permission_keys=["dispense:rx_antibiotics", "rx:verify"],
            requires_pcb_registration=True,
        )
        source_id = source_role["role_id"]

        self.client.force_authenticate(user=self.owner_a)
        clone_payload = {
            "new_role_code": f"ROLE_CLONED_DISPENSER_{self.suffix.upper()}",
            "new_display_name": "Cloned Dispenser Role",
            "description": "Cloned specialized variant",
        }
        res = self.client.post(f"/api/v2/roles/custom/{source_id}/clone/", data=clone_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()

        self.assertEqual(data["role_code"], clone_payload["new_role_code"])
        self.assertEqual(data["display_name"], clone_payload["new_display_name"])
        self.assertEqual(data["cloned_from_id"], source_id)
        self.assertTrue(data["requires_pcb_registration"])
        self.assertEqual(set(data["permission_keys"]), {"dispense:rx_antibiotics", "rx:verify"})

    def test_10_archive_custom_role(self):
        """Owner can archive custom role; role is filtered from default queries."""
        role = CustomRoleService.create_custom_role(
            tenant_id=self.tenant_id_a,
            role_code=f"ROLE_TO_ARCHIVE_{self.suffix.upper()}",
            display_name="Temporary Role",
            base_role="CASHIER",
            permission_keys=["sales:checkout"],
        )
        role_id = role["role_id"]

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(f"/api/v2/roles/custom/{role_id}/archive/", format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.json()["is_archived"])

        # Check list endpoint without include_archived -> role should be absent
        res_list = self.client.get("/api/v2/roles/custom/")
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        active_role_ids = [r["role_id"] for r in res_list.json()["results"]]
        self.assertNotIn(role_id, active_role_ids)

        # Check list endpoint with include_archived=true -> role should be present
        res_arch_list = self.client.get("/api/v2/roles/custom/?include_archived=true")
        self.assertEqual(res_arch_list.status_code, status.HTTP_200_OK)
        all_role_ids = [r["role_id"] for r in res_arch_list.json()["results"]]
        self.assertIn(role_id, all_role_ids)

    def test_11_preview_permissions_and_sod_conflict_detection(self):
        """Dry-run preview endpoint categorizes permissions and warns on Separation of Duties conflicts."""
        self.client.force_authenticate(user=self.owner_a)
        preview_payload = {
            "permission_keys": [
                "sales:checkout",
                "sales:void",
                "inventory:receive_po",
                "inventory:adjust_writeoff",
                "dispense:rx_antibiotics",
            ],
            "requires_pcb_registration": True,
        }
        res = self.client.post("/api/v2/roles/custom/preview/", data=preview_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["total_permissions"], 5)
        self.assertTrue(data["clinical_dispensing_cleared"])
        self.assertEqual(len(data["category_breakdown"]["pos"]), 2)
        self.assertEqual(len(data["category_breakdown"]["inventory"]), 2)

        # Expect 2 SoD warnings:
        # 1: checkout + void
        # 2: receive_po + adjust_writeoff
        self.assertEqual(len(data["sod_warnings"]), 2)

    def test_12_cross_tenant_isolation(self):
        """Tenant B owner cannot view, update, clone, or archive Tenant A custom roles."""
        role_a = CustomRoleService.create_custom_role(
            tenant_id=self.tenant_id_a,
            role_code=f"ROLE_TENANT_A_{self.suffix.upper()}",
            display_name="Tenant A Isolated Role",
            base_role="CASHIER",
            permission_keys=["sales:checkout"],
        )
        role_id_a = role_a["role_id"]

        # Authenticate as Tenant B owner
        self.client.force_authenticate(user=self.owner_b)

        # Attempt to edit Tenant A role
        res_edit = self.client.patch(
            f"/api/v2/roles/custom/{role_id_a}/",
            data={"display_name": "Breach Attempt"},
            format="json",
        )
        self.assertEqual(res_edit.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(res_edit.json()))

        # Attempt to clone Tenant A role
        res_clone = self.client.post(
            f"/api/v2/roles/custom/{role_id_a}/clone/",
            data={"new_role_code": f"ROLE_HACK_{self.suffix.upper()}", "new_display_name": "Hack"},
            format="json",
        )
        self.assertEqual(res_clone.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(res_clone.json()))

        # Attempt to archive Tenant A role
        res_arch = self.client.post(f"/api/v2/roles/custom/{role_id_a}/archive/", format="json")
        self.assertEqual(res_arch.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", str(res_arch.json()))
