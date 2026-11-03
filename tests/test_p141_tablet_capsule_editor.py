"""
backend/tests/test_p141_tablet_capsule_editor.py
Architecture Decision: ADR-0141 (P141).
End-to-end integration test suite for Solid Oral Tablet & Capsule Dosage-Form Editor.
Tests: schema introspection, dimension consistency, loose-sale safety rules,
solid strength units, administration routes, zero-trust hidden field rejection,
RBAC, cross-tenant isolation, suspended tenants, and subscription gating.
"""

import json
import random
import uuid
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
    / "tablet_capsule_editor.schema.json"
)


class TabletCapsuleEditorIntegrationTests(TestCase):
    """End-to-end integration tests for P141 Tablet and Capsule Editor."""

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
            tenant_id=f"tn-p141-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p141-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p141-a-{self.suffix}",
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
            tenant_id=f"tn-p141-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p141-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@labaid.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Labaid Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 4. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p141-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p141-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.owner_suspended = PharmacyUser.objects.create_user(
            email=f"owner_susp_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_suspended.tenant_id,
        )

        # 5. Basic Plan Tenant (Unentitled to custom packaging overrides)
        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p141-basic-{self.suffix}",
            name=f"Basic Rural Pharma {self.suffix}",
            subdomain=f"basic-p141-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner_basic = PharmacyUser.objects.create_user(
            email=f"owner_basic_{self.suffix}@basic.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Basic Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_basic.tenant_id,
        )

        # 6. Sample Medicines (Napa 500mg Tablet & C-Vivit Effervescent Tablet)
        self.medicine_tablet = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name=f"Napa 500mg {self.suffix}",
            brand_name_bn="নাপা ৫০০ মিগ্রা",
            generic_name="Paracetamol",
            generic_name_bn="প্যারাসিটামল",
            dosage_form="TABLET",
            strength="500 mg",
            strength_numeric=Decimal("500.00"),
            strength_unit="mg",
            strength_unit_family="MASS",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            route="Oral",
            barcode=f"8801234{random.randint(10000, 99999)}",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.95"),
            dgda_dar_number=f"DAR-P141-{random.randint(10000, 99999)}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

        self.medicine_effervescent = MedicineProduct.objects.create(
            product_id=f"prod-cvivit-{self.suffix}",
            brand_name=f"C-Vivit 1000mg {self.suffix}",
            brand_name_bn="সি-ভিভিট ১০০০ মিগ্রা",
            generic_name="Ascorbic Acid",
            generic_name_bn="অ্যাসকরবিক অ্যাসিড",
            dosage_form="EFFERVESCENT_TABLET",
            strength="1000 mg",
            strength_numeric=Decimal("1000.00"),
            strength_unit="mg",
            strength_unit_family="MASS",
            manufacturer="Square Pharmaceuticals PLC",
            route="Oral",
            barcode=f"8809999{random.randint(10000, 99999)}",
            mrp_unit=Decimal("15.00"),
            cost_unit=Decimal("11.50"),
            dgda_dar_number=f"DAR-EFF-{random.randint(10000, 99999)}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

    def test_01_schema_introspection(self):
        """Schema endpoint returns complete specifications, supported forms, and hidden fields."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/catalog/tablet-capsule/schema/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.data
        self.assertEqual(data["dosage_form_family"], "SOLID_ORAL")
        form_codes = [f["code"] for f in data["supported_dosage_forms"]]
        self.assertIn("TABLET", form_codes)
        self.assertIn("CAPSULE", form_codes)
        self.assertIn("EFFERVESCENT_TABLET", form_codes)
        self.assertIn("CHEWABLE_TABLET", form_codes)
        self.assertIn("SUBLINGUAL_TABLET", form_codes)

        self.assertIn("volume_ml", data["hidden_fields"])
        self.assertIn("concentration", data["hidden_fields"])
        self.assertEqual(data["dimension_consistency_formula"], "box_size == strips_per_box * tablets_per_strip")

    def test_02_validate_endpoint_contract(self):
        """Validate endpoint verifies payloads against JSON Schema and business rules without persisting."""
        self.client.force_authenticate(user=self.owner_a)

        valid_payload = {
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
            "is_scored": True,
            "coating_type": "FILM_COATED",
        }
        # JSON Schema validation
        jsonschema.validate(instance=valid_payload, schema=self.schema)

        res = self.client.post("/api/v2/catalog/tablet-capsule/validate/", data=valid_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data["valid"])
        self.assertEqual(res.data["validated_payload"]["box_size"], 100)

    def test_03_create_and_retrieve_tablet_configuration(self):
        """Superadmin creates canonical tablet packaging config, verifies calculation and retrieval."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
            "is_scored": True,
            "coating_type": "FILM_COATED",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        config_id = res.data["config_id"]
        self.assertEqual(res.data["box_size"], 100)
        self.assertTrue(res.data["loose_sale_eligible"])

        # Retrieve
        get_res = self.client.get(f"/api/v2/catalog/tablet-capsule/{config_id}/")
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)
        self.assertEqual(get_res.data["product_id"], self.medicine_tablet.product_id)
        self.assertEqual(get_res.data["tablets_per_strip"], 10)
        self.assertEqual(get_res.data["strips_per_box"], 10)

    def test_04_dimension_consistency_mismatch_rejected(self):
        """If provided box_size differs from tablets_per_strip * strips_per_box, request is rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 150,  # 150 != 10 * 10 = 100!
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_DIMENSION_CONSISTENCY_MISMATCH")

    def test_05_omitted_box_size_auto_calculated(self):
        """When box_size is omitted, service auto-calculates tablets_per_strip * strips_per_box."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 15,
            "strips_per_box": 20,
            # box_size omitted
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["box_size"], 300)  # 15 * 20 = 300

    def test_06_business_rule_effervescent_loose_sale_blocked(self):
        """Effervescent tablets strictly prohibit loose sales due to atmospheric moisture degradation."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_effervescent.product_id,
            "dosage_form": "EFFERVESCENT_TABLET",
            "dose_strength": 1000.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 2,
            "box_size": 20,
            "loose_sale_eligible": True,  # Prohibited for effervescent tablets!
            "route": "Effervescent",
            "storage_policy": "PROTECT_FROM_MOISTURE_LIGHT",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_LOOSE_SALE_NOT_PERMITTED")

        # Now submit with loose_sale_eligible=False -> Succeeds!
        payload["loose_sale_eligible"] = False
        res_ok = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res_ok.status_code, status.HTTP_201_CREATED)
        self.assertFalse(res_ok.data["loose_sale_eligible"])

    def test_07_invalid_administration_route_rejected(self):
        """Injectable or topical administration routes on solid tablets are strictly rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Intravenous",  # Incompatible with tablet!
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_INVALID_ROUTE_FOR_DOSAGE_FORM")

    def test_08_invalid_strength_unit_rejected(self):
        """Volumetric liquid units (mL, %) are rejected for solid oral tablets."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "ml",  # Invalid unit for solid tablet!
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_INVALID_STRENGTH_UNIT_FOR_SOLID_FORM")

    def test_09_zero_trust_hidden_field_injection_rejected(self):
        """Injection of hidden liquid/parenteral fields (volume_ml) is strictly rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
            "volume_ml": 100.0,  # Hidden field injection!
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_HIDDEN_FIELD_VALUE_REJECTED")

    def test_10_rbac_cashier_denied_mutation(self):
        """Cashiers have read-only access to tablet configs; mutation attempts return HTTP 403."""
        # 1. Read access -> Allowed
        self.client.force_authenticate(user=self.cashier_a)
        get_res = self.client.get("/api/v2/catalog/tablet-capsule/")
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)

        # 2. Write attempt -> Denied
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        post_res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(post_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(post_res.data.get("error_code"), "ERR_PERMISSION_DENIED")

    def test_11_cross_tenant_isolation_enforced(self):
        """Tenant A owner cannot create or mutate configuration for Tenant B."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_b.tenant_id,  # Cross-tenant injection attempt!
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get("error_code"), "ERR_CROSS_TENANT_VIOLATION")

    def test_12_suspended_tenant_blocked(self):
        """Suspended tenant is blocked from creating or modifying configurations."""
        self.client.force_authenticate(user=self.owner_suspended)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        data = res.json() if hasattr(res, "json") and callable(res.json) else getattr(res, "data", {})
        self.assertIn("SUSPENDED", str(data))

    def test_13_basic_plan_subscription_unentitled(self):
        """Basic tier pharmacy cannot create custom multi-tier packaging overrides."""
        self.client.force_authenticate(user=self.owner_basic)
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get("error_code"), "ERR_SUBSCRIPTION_UNENTITLED")

    def test_14_partial_update_and_deletion(self):
        """Authorized tenant owner can partially update packaging or remove custom config."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Create tenant override
        payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": True,
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        res = self.client.post("/api/v2/catalog/tablet-capsule/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        config_id = res.data["config_id"]

        # 2. Update loose sale eligibility
        patch_payload = {
            "product_id": self.medicine_tablet.product_id,
            "dosage_form": "TABLET",
            "dose_strength": 500.0,
            "strength_unit": "mg",
            "tablets_per_strip": 10,
            "strips_per_box": 10,
            "box_size": 100,
            "loose_sale_eligible": False,  # Updated!
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_DRY",
        }
        patch_res = self.client.patch(f"/api/v2/catalog/tablet-capsule/{config_id}/", data=patch_payload, format="json")
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.assertFalse(patch_res.data["loose_sale_eligible"])

        # 3. Delete config
        del_res = self.client.delete(f"/api/v2/catalog/tablet-capsule/{config_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_200_OK)
        self.assertTrue(del_res.data["deleted"])

        # 4. Verify gone
        verify_res = self.client.get(f"/api/v2/catalog/tablet-capsule/{config_id}/")
        self.assertEqual(verify_res.status_code, status.HTTP_404_NOT_FOUND)
