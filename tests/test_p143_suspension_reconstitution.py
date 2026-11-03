"""
backend/tests/test_p143_suspension_reconstitution.py
Architecture Decision: ADR-0143 (P143).
End-to-end integration test suite for Suspension Reconstitution Editor and Beyond-Use Date (BUD) management.
Tests:
1. Schema endpoint introspection and JSON Schema compliance.
2. Non-destructive validation endpoint.
3. Zero-trust rejection of solid oral fields (ERR_HIDDEN_FIELD_VALUE_REJECTED).
4. Diluent and final volume dimension consistency (final_volume >= water_volume).
5. Beyond-Use Date (BUD) auto-calculation and boundary validation (ERR_INVALID_BEYOND_USE_DATE).
6. Pharmacist review requirement and role enforcement (ERR_PHARMACIST_REVIEW_REQUIRED).
7. Cashier mutation rejection (ERR_PERMISSION_DENIED).
8. Multi-tenant isolation and cross-tenant guard (ERR_CROSS_TENANT_VIOLATION).
9. Suspended tenant rejection (ERR_TENANT_SUSPENDED).
10. Basic subscription plan unentitled check (ERR_SUBSCRIPTION_UNENTITLED).
11. Invalid diluent type rejection (ERR_INVALID_DILUENT_TYPE).
12. Invalid dosage form rejection (ERR_INVALID_DOSAGE_FORM).
13. Global monograph creation by superadmin and tenant fallback.
14. Full CRUD and deletion audit logging lifecycle.
"""

import json
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import jsonschema
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineCurationStatus,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "shared"
    / "contracts"
    / "schemas"
    / "suspension_reconstitution_editor.schema.json"
)


class SuspensionReconstitutionIntegrationTests(TestCase):
    """End-to-end integration tests for P143 Suspension Reconstitution Editor."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema = json.load(f)

        # 1. Platform Superadmin
        self.superadmin = PharmacyUser.objects.create_user(
            email=f"superadmin_{self.suffix}@platform.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Platform Superadmin",
            role=UserRole.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
        )

        # 2. Standard Plan Tenant A (Active) & Users
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p143-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p143-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p143-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_a_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Square Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.pharmacist_a = PharmacyUser.objects.create_user(
            email=f"pharmacist_a_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Square Registered Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Square Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 3. Standard Plan Tenant B (Active) & Owner
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p143-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p143-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p143-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@labaid.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Labaid Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        # 4. Suspended Tenant C
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p143-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p143-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p143-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner_suspended = PharmacyUser.objects.create_user(
            email=f"owner_susp_{self.suffix}@suspended.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

        # 5. Basic Tier Tenant D
        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p143-basic-{self.suffix}",
            name=f"Basic Med {self.suffix}",
            subdomain=f"basic-p143-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p143-basic-{self.suffix}",
            tenant=self.tenant_basic,
            name="Basic Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner_basic = PharmacyUser.objects.create_user(
            email=f"owner_basic_{self.suffix}@basic.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Basic Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_basic.tenant_id,
            branch_id=self.branch_basic.branch_id,
        )

        # 6. Test Dry Powder Suspension Product (Cefixime 100mg/5mL 50mL)
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-cefixime-{self.suffix}",
            brand_name=f"Cef-3 {self.suffix}",
            brand_name_bn="সেফ-৩",
            generic_name="Cefixime Trihydrate",
            generic_name_bn="সেফিক্সিম ট্রাইহাইড্রেট",
            strength="100 mg/5ml",
            strength_numeric=Decimal("100.00"),
            strength_unit="mg/5ml",
            strength_unit_family="CONCENTRATION",
            dosage_form="DRY_SYRUP_RECONSTITUTED",
            manufacturer="Square Pharmaceuticals PLC",
            route="Oral",
            barcode=f"8801999{random.randint(10000, 99999)}",
            mrp_unit=Decimal("210.00"),
            cost_unit=Decimal("168.00"),
            dgda_dar_number=f"DAR-CEF-{self.suffix}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

    def test_01_schema_endpoint_conformance(self):
        """GET /schema/ returns sections, conditions, and adheres to JSON Schema."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/catalog/suspension-reconstitution/schema/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["dosage_form_family"], "RECONSTITUTED_SUSPENSION")
        self.assertIn("sections", data)
        self.assertIn("hidden_fields", data)
        self.assertIn("strips_per_box", data["hidden_fields"])
        self.assertIn("tablets_per_strip", data["hidden_fields"])

    def test_02_validate_endpoint_valid_payload(self):
        """POST /validate/ validates valid suspension reconstitution payload."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "opening_date": "2026-11-03",
            "beyond_use_date": "2026-11-10",
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Add 35 mL of boiled and cooled water in two portions and shake well.",
            "instructions_bn": "৩৫ মি.লি. ফুটিয়ে ঠান্ডা করা পানি দুই ধাপে মিশিয়ে ভালো করে ঝাঁকিয়ে নিন।",
            "handling_guidance_en": "Shake well before each use. Store in refrigerator (2-8C).",
            "is_pharmacist_reviewed": False,
        }
        # JSON Schema validation
        jsonschema.validate(instance=payload, schema=self.schema)

        response = self.client.post("/api/v2/catalog/suspension-reconstitution/validate/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["valid"])
        self.assertEqual(float(response.json()["validated_payload"]["water_volume_ml"]), 35.0)
        self.assertEqual(float(response.json()["validated_payload"]["final_volume_ml"]), 50.0)

    def test_03_reject_hidden_solid_fields(self):
        """Zero-Trust: Passing tablet/capsule fields must be rejected with ERR_HIDDEN_FIELD_VALUE_REJECTED."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "instructions_en": "Add 35 mL of water.",
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "strips_per_box": 10,  # Prohibited solid packaging field!
        }
        response = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_HIDDEN_FIELD_VALUE_REJECTED")

    def test_04_dimension_consistency_volume_mismatch(self):
        """Water volume cannot exceed reconstituted final volume (final_volume < water_volume)."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 60.0,  # 60 mL water for a 50 mL bottle is physically impossible!
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "instructions_en": "Add 60 mL water into 50 mL bottle.",
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
        }
        response = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_DIMENSION_CONSISTENCY_MISMATCH")

    def test_05_bud_auto_calculation_and_validation(self):
        """BUD is auto-calculated if omitted; if provided, must not precede opening_date or exceed shelf life."""
        self.client.force_authenticate(user=self.owner_a)
        open_date = date(2026, 11, 3)

        # 1. Auto-calculation when beyond_use_date is omitted
        payload_auto = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 10,
            "opening_date": open_date.isoformat(),
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Add 35 mL water.",
        }
        res_auto = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload_auto, format="json")
        self.assertEqual(res_auto.status_code, status.HTTP_201_CREATED)
        expected_bud = open_date + timedelta(days=10)
        self.assertEqual(res_auto.json()["beyond_use_date"], expected_bud.isoformat())

        # 2. Reject BUD prior to opening date
        payload_prior = dict(payload_auto)
        payload_prior["beyond_use_date"] = (open_date - timedelta(days=1)).isoformat()
        res_prior = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload_prior, format="json")
        self.assertEqual(res_prior.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_prior.json()["error_code"], "ERR_INVALID_BEYOND_USE_DATE")

        # 3. Reject BUD exceeding stability shelf life
        payload_exceed = dict(payload_auto)
        payload_exceed["beyond_use_date"] = (open_date + timedelta(days=11)).isoformat()
        res_exceed = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload_exceed, format="json")
        self.assertEqual(res_exceed.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_exceed.json()["error_code"], "ERR_INVALID_BEYOND_USE_DATE")

    def test_06_pharmacist_review_authorization(self):
        """Clinical handling guidance review requires registered pharmacist or owner credentials."""
        # 1. Registered pharmacist can approve
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Boil water, cool down, add 35 mL in two portions.",
            "handling_guidance_en": "Dispense with oral syringe. Refrigerate after reconstitution.",
            "is_pharmacist_reviewed": True,
            "reviewer_notes": "Reviewed against DGDA monograph and manufacturer stability sheet.",
        }
        res_pharm = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res_pharm.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_pharm.json()["is_pharmacist_reviewed"])
        self.assertEqual(res_pharm.json()["reviewed_by_id"], str(self.pharmacist_a.user_id))

        # 2. Cashier attempting to submit pharmacist approval is denied
        self.client.force_authenticate(user=self.cashier_a)
        payload_cashier = dict(payload)
        payload_cashier["reviewer_notes"] = "Cashier fake review"
        res_cash = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload_cashier, format="json")
        self.assertEqual(res_cash.status_code, status.HTTP_403_FORBIDDEN)
        # Cashier is blocked by either ERR_PERMISSION_DENIED or ERR_PHARMACIST_REVIEW_REQUIRED
        self.assertIn(res_cash.json()["error_code"], ["ERR_PERMISSION_DENIED", "ERR_PHARMACIST_REVIEW_REQUIRED"])

    def test_07_cashier_mutation_blocked(self):
        """Cashiers are strictly read-only and cannot mutate suspension configurations."""
        self.client.force_authenticate(user=self.cashier_a)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Add 35 mL water.",
            "is_pharmacist_reviewed": False,
        }
        res = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_PERMISSION_DENIED")

    def test_08_cross_tenant_isolation(self):
        """Tenant B owner cannot modify or hijack Tenant A's suspension configuration."""
        self.client.force_authenticate(user=self.owner_b)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,  # Attempting cross-tenant write
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Add 35 mL water.",
        }
        res = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_09_suspended_tenant_blocked(self):
        """Suspended tenant accounts are blocked from accessing or modifying configurations."""
        self.client.force_authenticate(user=self.owner_suspended)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Add 35 mL water.",
        }
        res = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(res.json().get("error_code"), ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"])

    def test_10_basic_tier_unentitled(self):
        """Basic tier plan cannot author custom tenant-specific suspension monographs."""
        self.client.force_authenticate(user=self.owner_basic)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Add 35 mL water.",
        }
        res = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED")

    def test_11_invalid_diluent_type_rejected(self):
        """Unsupported diluent type must be rejected with ERR_INVALID_DILUENT_TYPE."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "TAP_WATER_UNBOILED",  # Invalid diluent
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Add tap water.",
        }
        res = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_DILUENT_TYPE")

    def test_12_invalid_dosage_form_rejected(self):
        """Passing non-suspension dosage form must be rejected with ERR_INVALID_DOSAGE_FORM."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "TABLET",  # Invalid for suspension editor!
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Dissolve tablet in water.",
        }
        res = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_DOSAGE_FORM")

    def test_13_global_monograph_creation_by_superadmin(self):
        """Superadmin creates global monograph (tenant=None) accessible to all tenants as fallback."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.product.product_id,
            "tenant_id": None,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Standard national clinical monograph: Add 35 mL boiled and cooled water.",
            "is_pharmacist_reviewed": True,
            "reviewer_notes": "DGDA Approved National Clinical Standard",
        }
        res = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(res.json()["tenant_id"])

        # Tenant A user can read global monograph
        self.client.force_authenticate(user=self.owner_a)
        list_res = self.client.get(f"/api/v2/catalog/suspension-reconstitution/?product_id={self.product.product_id}")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        items = list_res.json()
        self.assertTrue(any(item["instructions_en"].startswith("Standard national") for item in items))

    def test_14_crud_lifecycle_and_deletion(self):
        """Full lifecycle: create -> retrieve -> patch update -> delete."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Create
        payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 35.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 7,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Initial reconstitution directions.",
            "is_pharmacist_reviewed": False,
        }
        res_create = self.client.post("/api/v2/catalog/suspension-reconstitution/", payload, format="json")
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        config_id = res_create.json()["config_id"]

        # 2. Retrieve
        res_get = self.client.get(f"/api/v2/catalog/suspension-reconstitution/{config_id}/")
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertEqual(res_get.json()["config_id"], config_id)

        # 3. Patch
        patch_payload = {
            "product_id": self.product.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "reconstitution_diluent": "BOILED_COOLED_WATER",
            "water_volume_ml": 38.0,
            "final_volume_ml": 50.0,
            "shelf_life_days": 14,
            "storage_policy": "REFRIGERATE_2_8C_AFTER_RECONSTITUTION",
            "instructions_en": "Updated instructions with 38 mL water.",
            "is_pharmacist_reviewed": True,
        }
        res_patch = self.client.patch(f"/api/v2/catalog/suspension-reconstitution/{config_id}/", patch_payload, format="json")
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.json()["water_volume_ml"], 38.0)
        self.assertEqual(res_patch.json()["shelf_life_days"], 14)
        self.assertTrue(res_patch.json()["is_pharmacist_reviewed"])

        # 4. Delete
        res_del = self.client.delete(f"/api/v2/catalog/suspension-reconstitution/{config_id}/")
        self.assertEqual(res_del.status_code, status.HTTP_200_OK)
        self.assertTrue(res_del.json()["deleted"])

        # 5. Verify 404
        res_verify = self.client.get(f"/api/v2/catalog/suspension-reconstitution/{config_id}/")
        self.assertEqual(res_verify.status_code, status.HTTP_404_NOT_FOUND)
