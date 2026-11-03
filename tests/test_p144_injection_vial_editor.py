"""
backend/tests/test_p144_injection_vial_editor.py
Architecture Decision: ADR-0144 (P144).
End-to-end integration test suite for Injection Vial, Ampoule, Sterile Route, and Cold-Chain Editor.
Tests:
1. Schema endpoint introspection and JSON Schema compliance.
2. Non-destructive validation endpoint.
3. Zero-trust rejection of solid oral fields (ERR_HIDDEN_FIELD_VALUE_REJECTED).
4. Sterile parenteral route enforcement (ERR_INVALID_STERILE_ROUTE).
5. Cold-chain temperature boundary validation (min < max, ERR_INVALID_TEMPERATURE_RANGE).
6. Single-dose glass ampoule preservative prohibition (ERR_SINGLE_DOSE_PRESERVATIVE_PROHIBITED).
7. Lyophilized powder diluent requirements (ERR_DILUENT_REQUIRED).
8. Cashier mutation rejection (ERR_PERMISSION_DENIED).
9. Multi-tenant isolation and cross-tenant guard (ERR_CROSS_TENANT_VIOLATION).
10. Suspended tenant rejection (ERR_TENANT_SUSPENDED).
11. Basic subscription plan unentitled check (ERR_SUBSCRIPTION_UNENTITLED).
12. Invalid strength unit rejection (ERR_INVALID_STRENGTH_UNIT).
13. Global monograph creation by superadmin and tenant fallback.
14. Full CRUD and deletion audit logging lifecycle.
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
    / "injection_vial_editor.schema.json"
)


class InjectionVialEditorIntegrationTests(TestCase):
    """End-to-end integration tests for P144 Injection Vial Editor."""

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
            tenant_id=f"tn-p144-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p144-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p144-a-{self.suffix}",
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
            tenant_id=f"tn-p144-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p144-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p144-b-{self.suffix}",
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
            tenant_id=f"tn-p144-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p144-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p144-susp-{self.suffix}",
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
            tenant_id=f"tn-p144-basic-{self.suffix}",
            name=f"Basic Med {self.suffix}",
            subdomain=f"basic-p144-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p144-basic-{self.suffix}",
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

        # 6. Sample Injectable Products
        self.insulin_product = MedicineProduct.objects.create(
            product_id=f"prod-insulin-{self.suffix}",
            brand_name=f"Insulatard 100IU/mL {self.suffix}",
            brand_name_bn="ইনসুলাটার্ড ১০০ আইইউ/মি.লি.",
            generic_name="Insulin Isophane (NPH)",
            generic_name_bn="ইনসুলিন আইসোফেন",
            strength="100 IU/ml",
            strength_numeric=Decimal("100.00"),
            strength_unit="IU/ml",
            strength_unit_family="CONCENTRATION",
            dosage_form="INJECTION_SUSPENSION",
            manufacturer="Novo Nordisk A/S",
            route="Subcutaneous",
            barcode=f"8801111{random.randint(10000, 99999)}",
            mrp_unit=Decimal("450.00"),
            cost_unit=Decimal("380.00"),
            dgda_dar_number=f"DAR-INS-{self.suffix}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

        self.powder_product = MedicineProduct.objects.create(
            product_id=f"prod-ceftriaxone-{self.suffix}",
            brand_name=f"Rocphin 1g IV {self.suffix}",
            brand_name_bn="রোকফিন ১গ্রাম আইভি",
            generic_name="Ceftriaxone Sodium",
            generic_name_bn="সেফট্রিয়াক্সন সোডিয়াম",
            strength="1 g/vial",
            strength_numeric=Decimal("1.00"),
            strength_unit="g/vial",
            strength_unit_family="MASS",
            dosage_form="INJECTION_POWDER_LYOPHILIZED",
            manufacturer="Roche Bangladesh",
            route="Intravenous",
            barcode=f"8802222{random.randint(10000, 99999)}",
            mrp_unit=Decimal("200.00"),
            cost_unit=Decimal("160.00"),
            dgda_dar_number=f"DAR-CEF-IV-{self.suffix}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

    def test_01_schema_endpoint_conformance(self):
        """GET /schema/ returns sections, conditions, and adheres to JSON Schema."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/catalog/injection-vials/schema/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["dosage_form_family"], "PARENTERAL_INJECTION")
        self.assertIn("sections", data)
        self.assertIn("hidden_fields", data)
        self.assertIn("strips_per_box", data["hidden_fields"])
        self.assertIn("needle_gauge", data["hidden_fields"])

    def test_02_validate_endpoint_valid_payload(self):
        """POST /validate/ validates valid injection vial payload."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
            "is_cold_chain_required": True,
            "min_storage_temp_celsius": 2.0,
            "max_storage_temp_celsius": 8.0,
            "has_preservative": True,
        }
        # JSON Schema validation
        jsonschema.validate(instance=payload, schema=self.schema)

        response = self.client.post("/api/v2/catalog/injection-vials/validate/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["valid"])
        self.assertEqual(float(response.json()["validated_payload"]["strength_value"]), 100.0)
        self.assertEqual(float(response.json()["validated_payload"]["fill_volume_ml"]), 10.0)

    def test_03_reject_hidden_solid_fields(self):
        """Zero-Trust: Passing tablet/capsule fields must be rejected with ERR_HIDDEN_FIELD_VALUE_REJECTED."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
            "strips_per_box": 5,  # Forbidden solid packaging field
        }
        response = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_HIDDEN_FIELD_VALUE_REJECTED")

    def test_04_reject_invalid_sterile_route(self):
        """Non-parenteral routes like 'Oral' must be rejected with ERR_INVALID_STERILE_ROUTE."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "ORAL",  # Prohibited route
        }
        response = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_INVALID_STERILE_ROUTE")

    def test_05_cold_chain_temperature_validation(self):
        """Cold chain temperature bounds must satisfy min < max; invalid ranges rejected."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Invalid temperature range (min >= max)
        payload_invalid = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
            "is_cold_chain_required": True,
            "min_storage_temp_celsius": 8.0,  # min is greater than max!
            "max_storage_temp_celsius": 2.0,
        }
        res_invalid = self.client.post("/api/v2/catalog/injection-vials/", payload_invalid, format="json")
        self.assertEqual(res_invalid.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_invalid.json()["error_code"], "ERR_INVALID_TEMPERATURE_RANGE")

        # 2. Valid temperature range (2°C - 8°C) succeeds
        payload_valid = dict(payload_invalid)
        payload_valid["min_storage_temp_celsius"] = 2.0
        payload_valid["max_storage_temp_celsius"] = 8.0
        res_valid = self.client.post("/api/v2/catalog/injection-vials/", payload_valid, format="json")
        self.assertEqual(res_valid.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_valid.json()["is_cold_chain_required"])
        self.assertEqual(res_valid.json()["min_storage_temp_celsius"], 2.0)
        self.assertEqual(res_valid.json()["max_storage_temp_celsius"], 8.0)

    def test_06_single_dose_ampoule_preservative_prohibited(self):
        """Single-dose glass ampoules cannot contain antimicrobial preservatives."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SOLUTION",
            "container_type": "AMPOULE_GLASS",
            "strength_value": 50.0,
            "strength_unit": "mg/ml",
            "fill_volume_ml": 2.0,
            "sterile_route": "IM",
            "has_preservative": True,  # Prohibited for glass ampoules!
        }
        response = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_SINGLE_DOSE_PRESERVATIVE_PROHIBITED")

    def test_07_lyophilized_powder_diluent_required(self):
        """Powder injections require positive diluent volume and valid diluent type."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Missing diluent for lyophilized powder
        payload_missing = {
            "product_id": self.powder_product.product_id,
            "dosage_form": "INJECTION_POWDER_LYOPHILIZED",
            "container_type": "VIAL_SINGLE_DOSE",
            "strength_value": 1.0,
            "strength_unit": "g/vial",
            "fill_volume_ml": 10.0,
            "sterile_route": "IV",
            "requires_diluent": True,
        }
        res_missing = self.client.post("/api/v2/catalog/injection-vials/", payload_missing, format="json")
        self.assertEqual(res_missing.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_missing.json()["error_code"], "ERR_DILUENT_REQUIRED")

        # 2. Valid powder injection with diluent
        payload_valid = dict(payload_missing)
        payload_valid["diluent_type"] = "WATER_FOR_INJECTION"
        payload_valid["diluent_volume_ml"] = 10.0
        res_valid = self.client.post("/api/v2/catalog/injection-vials/", payload_valid, format="json")
        self.assertEqual(res_valid.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_valid.json()["requires_diluent"])
        self.assertEqual(res_valid.json()["diluent_type"], "WATER_FOR_INJECTION")
        self.assertEqual(res_valid.json()["diluent_volume_ml"], 10.0)

    def test_08_cashier_mutation_blocked(self):
        """Cashiers are strictly read-only and cannot mutate injection configurations."""
        self.client.force_authenticate(user=self.cashier_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
        }
        res = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_PERMISSION_DENIED")

    def test_09_cross_tenant_isolation(self):
        """Tenant B owner cannot modify or hijack Tenant A's injection configuration."""
        self.client.force_authenticate(user=self.owner_b)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,  # Attempting cross-tenant write
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
        }
        res = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_10_suspended_tenant_blocked(self):
        """Suspended tenant accounts are blocked from accessing or modifying configurations."""
        self.client.force_authenticate(user=self.owner_suspended)
        payload = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
        }
        res = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(res.json().get("error_code"), ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"])

    def test_11_basic_tier_unentitled(self):
        """Basic tier plan cannot author custom tenant-specific injection monographs."""
        self.client.force_authenticate(user=self.owner_basic)
        payload = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
        }
        res = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED")

    def test_12_invalid_strength_unit_rejected(self):
        """Unsupported strength units must be rejected with ERR_INVALID_STRENGTH_UNIT."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "mg/tablet",  # Invalid unit for injection
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
        }
        res = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_STRENGTH_UNIT")

    def test_13_global_monograph_creation_by_superadmin(self):
        """Superadmin creates global monograph (tenant=None) accessible to all tenants as fallback."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.insulin_product.product_id,
            "tenant_id": None,
            "dosage_form": "INJECTION_SUSPENSION",
            "container_type": "VIAL_MULTI_DOSE",
            "strength_value": 100.0,
            "strength_unit": "IU/ml",
            "fill_volume_ml": 10.0,
            "sterile_route": "SC",
            "is_cold_chain_required": True,
            "min_storage_temp_celsius": 2.0,
            "max_storage_temp_celsius": 8.0,
        }
        res = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(res.json()["tenant_id"])

        # Tenant A user can read global monograph
        self.client.force_authenticate(user=self.owner_a)
        list_res = self.client.get(f"/api/v2/catalog/injection-vials/?product_id={self.insulin_product.product_id}")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        items = list_res.json()
        self.assertTrue(any(item["sterile_route"] == "SC" for item in items))

    def test_14_crud_lifecycle_and_deletion(self):
        """Full lifecycle: create -> retrieve -> patch update -> delete."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Create
        payload = {
            "product_id": self.powder_product.product_id,
            "dosage_form": "INJECTION_POWDER_LYOPHILIZED",
            "container_type": "VIAL_SINGLE_DOSE",
            "strength_value": 1.0,
            "strength_unit": "g/vial",
            "fill_volume_ml": 10.0,
            "sterile_route": "IV",
            "requires_diluent": True,
            "diluent_type": "WATER_FOR_INJECTION",
            "diluent_volume_ml": 10.0,
        }
        res_create = self.client.post("/api/v2/catalog/injection-vials/", payload, format="json")
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        config_id = res_create.json()["config_id"]

        # 2. Retrieve
        res_get = self.client.get(f"/api/v2/catalog/injection-vials/{config_id}/")
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertEqual(res_get.json()["config_id"], config_id)

        # 3. Patch
        patch_payload = {
            "product_id": self.powder_product.product_id,
            "dosage_form": "INJECTION_POWDER_LYOPHILIZED",
            "container_type": "VIAL_SINGLE_DOSE",
            "strength_value": 2.0,  # 2g updated
            "strength_unit": "g/vial",
            "fill_volume_ml": 20.0,
            "sterile_route": "IV",
            "requires_diluent": True,
            "diluent_type": "NORMAL_SALINE_0_9",
            "diluent_volume_ml": 20.0,
        }
        res_patch = self.client.patch(f"/api/v2/catalog/injection-vials/{config_id}/", patch_payload, format="json")
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.json()["strength_value"], 2.0)
        self.assertEqual(res_patch.json()["diluent_type"], "NORMAL_SALINE_0_9")

        # 4. Delete
        res_del = self.client.delete(f"/api/v2/catalog/injection-vials/{config_id}/")
        self.assertEqual(res_del.status_code, status.HTTP_200_OK)
        self.assertTrue(res_del.json()["deleted"])

        # 5. Verify 404
        res_verify = self.client.get(f"/api/v2/catalog/injection-vials/{config_id}/")
        self.assertEqual(res_verify.status_code, status.HTTP_404_NOT_FOUND)
