"""
backend/tests/test_p142_syrup_liquid_editor.py
Architecture Decision: ADR-0142 (P142).
End-to-end integration test suite for Oral Liquid, Syrup, and Suspension Dosage-Form Editor.
Tests: schema introspection, concentration dimensions, bottle volume validation,
reconstitution water requirements, zero-trust solid field rejection,
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
    / "syrup_liquid_editor.schema.json"
)


class SyrupLiquidEditorIntegrationTests(TestCase):
    """End-to-end integration tests for P142 Syrup and Liquid Oral Editor."""

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
            tenant_id=f"tn-p142-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p142-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p142-a-{self.suffix}",
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
            tenant_id=f"tn-p142-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p142-{self.suffix}",
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
            tenant_id=f"tn-p142-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p142-{self.suffix}",
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
            tenant_id=f"tn-p142-basic-{self.suffix}",
            name=f"Basic Rural Pharma {self.suffix}",
            subdomain=f"basic-p142-{self.suffix}",
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

        # 6. Sample Medicines (Napa Syrup 60mL & Moxacil Dry Syrup 100mL)
        self.medicine_syrup = MedicineProduct.objects.create(
            product_id=f"prod-napa-syr-{self.suffix}",
            brand_name=f"Napa Syrup 60mL {self.suffix}",
            brand_name_bn="নাপা সিরাপ ৬০ মি.লি.",
            generic_name="Paracetamol",
            generic_name_bn="প্যারাসিটামল",
            dosage_form="SYRUP",
            strength="120 mg/5ml",
            strength_numeric=Decimal("120.00"),
            strength_unit="mg/5ml",
            strength_unit_family="CONCENTRATION",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            route="Oral",
            barcode=f"8801234{random.randint(10000, 99999)}",
            mrp_unit=Decimal("35.00"),
            cost_unit=Decimal("28.00"),
            dgda_dar_number=f"DAR-SYR-{random.randint(10000, 99999)}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

        self.medicine_dry_syrup = MedicineProduct.objects.create(
            product_id=f"prod-moxacil-dry-{self.suffix}",
            brand_name=f"Moxacil Dry Syrup 100mL {self.suffix}",
            brand_name_bn="মোক্সাসিল ড্রাই সিরাপ ১০০ মি.লি.",
            generic_name="Amoxicillin Trihydrate",
            generic_name_bn="অ্যামোক্সিসিলিন ট্রাইহাইড্রেট",
            dosage_form="DRY_SYRUP_RECONSTITUTED",
            strength="125 mg/5ml",
            strength_numeric=Decimal("125.00"),
            strength_unit="mg/5ml",
            strength_unit_family="CONCENTRATION",
            manufacturer="Square Pharmaceuticals PLC",
            route="Oral",
            barcode=f"8809876{random.randint(10000, 99999)}",
            mrp_unit=Decimal("70.00"),
            cost_unit=Decimal("56.00"),
            dgda_dar_number=f"DAR-DRY-{random.randint(10000, 99999)}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

    def test_01_schema_introspection(self):
        """Schema endpoint returns complete specifications, supported forms, and hidden fields."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/catalog/syrup-liquid/schema/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        data = res.data
        self.assertEqual(data["dosage_form_family"], "ORAL_LIQUID")
        form_codes = [f["code"] for f in data["supported_dosage_forms"]]
        self.assertIn("SYRUP", form_codes)
        self.assertIn("SUSPENSION", form_codes)
        self.assertIn("ORAL_SOLUTION", form_codes)
        self.assertIn("DRY_SYRUP_RECONSTITUTED", form_codes)

        self.assertIn("tablets_per_strip", data["hidden_fields"])
        self.assertIn("strips_per_box", data["hidden_fields"])
        self.assertIn("loose_sale_eligible", data["hidden_fields"])

    def test_02_validate_endpoint_contract(self):
        """Validate endpoint verifies payloads against JSON Schema without persisting."""
        self.client.force_authenticate(user=self.owner_a)

        valid_payload = {
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
            "measuring_device": "MEASURING_CUP",
            "is_reconstitution_required": False,
            "flavor": "Strawberry",
        }
        jsonschema.validate(instance=valid_payload, schema=self.schema)

        res = self.client.post("/api/v2/catalog/syrup-liquid/validate/", data=valid_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data["valid"])
        self.assertEqual(float(res.data["validated_payload"]["bottle_volume_ml"]), 60.0)

    def test_03_create_and_retrieve_syrup_configuration(self):
        """Superadmin creates canonical syrup configuration, verifies persistence and retrieval."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
            "measuring_device": "MEASURING_CUP",
            "flavor": "Strawberry",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        config_id = res.data["config_id"]
        self.assertEqual(res.data["bottle_volume_ml"], 60.0)
        self.assertEqual(res.data["sale_unit"], "Bottle")

        get_res = self.client.get(f"/api/v2/catalog/syrup-liquid/{config_id}/")
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)
        self.assertEqual(get_res.data["product_id"], self.medicine_syrup.product_id)
        self.assertEqual(get_res.data["concentration_unit"], "mg/5ml")

    def test_04_invalid_strength_dimension_rejected(self):
        """Standalone solid mass units without volume denominator are strictly rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 500.0,
            "concentration_unit": "mg",  # Invalid dimension for liquid!
            "bottle_volume_ml": 100.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_INVALID_STRENGTH_DIMENSION_FOR_LIQUID")

    def test_05_zero_trust_hidden_field_injection_rejected(self):
        """Solid tablet packaging multipliers passed in syrup payload are strictly rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
            "tablets_per_strip": 10,  # Prohibited solid packaging field!
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_HIDDEN_FIELD_VALUE_REJECTED")

    def test_06_invalid_bottle_volume_rejected(self):
        """Bottle volume <= 0 is rejected with ERR_INVALID_BOTTLE_VOLUME."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 0.0,  # Invalid volume!
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_INVALID_BOTTLE_VOLUME")

    def test_07_invalid_sale_unit_rejected(self):
        """Sale units like Tablet or Strip are rejected for oral liquids."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Strip",  # Invalid sale unit for syrup!
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_INVALID_SALE_UNIT_FOR_LIQUID")

    def test_08_invalid_route_rejected(self):
        """Injectable or topical routes on oral liquids are rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Intravenous",  # Incompatible route!
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_INVALID_ROUTE_FOR_DOSAGE_FORM")

    def test_09_dry_syrup_reconstitution_water_mandatory(self):
        """Dry syrup requires reconstitution_water_ml > 0."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.medicine_dry_syrup.product_id,
            "dosage_form": "DRY_SYRUP_RECONSTITUTED",
            "concentration_value": 125.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 100.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "REFRIGERATE_AFTER_RECONSTITUTION_2_8C",
            "is_reconstitution_required": True,
            # reconstitution_water_ml omitted!
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.data.get("error_code"), "ERR_RECONSTITUTION_WATER_REQUIRED")

        # Now provide valid reconstitution_water_ml -> Succeeds!
        payload["reconstitution_water_ml"] = 75.0
        payload["shelf_life_days_after_reconstitution"] = 7
        res_ok = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res_ok.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_ok.data["reconstitution_water_ml"], 75.0)
        self.assertEqual(res_ok.data["shelf_life_days_after_reconstitution"], 7)

    def test_10_rbac_cashier_denied_mutation(self):
        """Cashiers have read-only access to syrup configs; mutations return HTTP 403."""
        self.client.force_authenticate(user=self.cashier_a)
        get_res = self.client.get("/api/v2/catalog/syrup-liquid/")
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)

        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        post_res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(post_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(post_res.data.get("error_code"), "ERR_PERMISSION_DENIED")

    def test_11_cross_tenant_isolation_enforced(self):
        """Tenant A owner cannot create or mutate configuration for Tenant B."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_b.tenant_id,  # Cross-tenant injection!
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get("error_code"), "ERR_CROSS_TENANT_VIOLATION")

    def test_12_suspended_tenant_blocked(self):
        """Suspended tenant is blocked from modifying syrup configurations."""
        self.client.force_authenticate(user=self.owner_suspended)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        data = res.json() if hasattr(res, "json") and callable(res.json) else getattr(res, "data", {})
        self.assertIn("SUSPENDED", str(data))

    def test_13_basic_plan_subscription_unentitled(self):
        """Basic plan pharmacy cannot create custom syrup packaging overrides."""
        self.client.force_authenticate(user=self.owner_basic)
        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.data.get("error_code"), "ERR_SUBSCRIPTION_UNENTITLED")

    def test_14_partial_update_and_deletion(self):
        """Authorized tenant owner can partially update packaging or remove custom config."""
        self.client.force_authenticate(user=self.owner_a)

        payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 60.0,
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
            "flavor": "Mango",
        }
        res = self.client.post("/api/v2/catalog/syrup-liquid/", data=payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        config_id = res.data["config_id"]

        # Update flavor
        patch_payload = {
            "product_id": self.medicine_syrup.product_id,
            "dosage_form": "SYRUP",
            "concentration_value": 120.0,
            "concentration_unit": "mg/5ml",
            "bottle_volume_ml": 100.0,  # Updated volume
            "sale_unit": "Bottle",
            "route": "Oral",
            "storage_policy": "ROOM_TEMPERATURE_BELOW_30C",
            "flavor": "Orange",
        }
        patch_res = self.client.patch(f"/api/v2/catalog/syrup-liquid/{config_id}/", data=patch_payload, format="json")
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_res.data["flavor"], "Orange")
        self.assertEqual(patch_res.data["bottle_volume_ml"], 100.0)

        # Delete
        del_res = self.client.delete(f"/api/v2/catalog/syrup-liquid/{config_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_200_OK)
        self.assertTrue(del_res.data["deleted"])

        # Verify 404
        verify_res = self.client.get(f"/api/v2/catalog/syrup-liquid/{config_id}/")
        self.assertEqual(verify_res.status_code, status.HTTP_404_NOT_FOUND)
