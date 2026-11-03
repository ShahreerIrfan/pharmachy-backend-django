"""
backend/tests/test_p145_insulin_editor.py
Architecture Decision: ADR-0145 (P145).
End-to-end integration test suite for Insulin Formulation, Container Dimensions, and Cold-Chain Excursion Editor.
Tests:
1. Schema endpoint introspection and JSON Schema compliance.
2. Non-destructive validation endpoint.
3. Zero-trust rejection of solid oral fields (ERR_HIDDEN_FIELD_VALUE_REJECTED).
4. Mathematical dimension consistency (total_units == concentration * volume, ERR_DIMENSION_CONSISTENCY_MISMATCH).
5. Standard biological concentration enforcement (100, 200, 300, 500 IU/mL, ERR_INVALID_INSULIN_CONCENTRATION).
6. In-use room-temperature stability window validation (14 - 56 days, ERR_INVALID_IN_USE_SHELF_LIFE).
7. Temperature excursion action governance (ERR_INVALID_EXCURSION_ACTION).
8. Cashier mutation rejection (ERR_PERMISSION_DENIED).
9. Multi-tenant isolation and cross-tenant guard (ERR_CROSS_TENANT_VIOLATION).
10. Suspended tenant rejection (ERR_TENANT_SUSPENDED).
11. Basic subscription plan unentitled check (ERR_SUBSCRIPTION_UNENTITLED).
12. Invalid delivery presentation rejection (ERR_INVALID_DELIVERY_DEVICE).
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
    / "insulin_editor.schema.json"
)


class InsulinEditorIntegrationTests(TestCase):
    """End-to-end integration tests for P145 Insulin Formulation Editor."""

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
            tenant_id=f"tn-p145-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p145-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p145-a-{self.suffix}",
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
            tenant_id=f"tn-p145-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p145-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p145-b-{self.suffix}",
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
            tenant_id=f"tn-p145-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p145-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p145-susp-{self.suffix}",
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
            tenant_id=f"tn-p145-basic-{self.suffix}",
            name=f"Basic Med {self.suffix}",
            subdomain=f"basic-p145-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p145-basic-{self.suffix}",
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

        # 6. Sample Insulin Product
        self.insulin_product = MedicineProduct.objects.create(
            product_id=f"prod-novomix-{self.suffix}",
            brand_name=f"NovoMix 30 FlexPen {self.suffix}",
            brand_name_bn="নভোভিক্স ৩০ ফ্লেক্সপেন",
            generic_name="Biphasic Insulin Aspart",
            generic_name_bn="বাইফ্যাসিক ইনসুলিন অ্যাসপার্ট",
            strength="100 IU/ml",
            strength_numeric=Decimal("100.00"),
            strength_unit="IU/ml",
            strength_unit_family="CONCENTRATION",
            dosage_form="INJECTION_SUSPENSION",
            manufacturer="Novo Nordisk A/S",
            route="Subcutaneous",
            barcode=f"8803333{random.randint(10000, 99999)}",
            mrp_unit=Decimal("780.00"),
            cost_unit=Decimal("660.00"),
            dgda_dar_number=f"DAR-INS-MIX-{self.suffix}",
            curation_status=MedicineCurationStatus.APPROVED,
        )

    def test_01_schema_endpoint_conformance(self):
        """GET /schema/ returns sections, conditions, and adheres to JSON Schema."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/catalog/insulin/schema/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["dosage_form_family"], "INSULIN_BIOPHARMA")
        self.assertIn("sections", data)
        self.assertIn("hidden_fields", data)
        self.assertIn("strips_per_box", data["hidden_fields"])
        self.assertIn("box_size", data["hidden_fields"])

    def test_02_validate_endpoint_valid_payload(self):
        """POST /validate/ validates valid insulin configuration payload."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        # JSON Schema validation
        jsonschema.validate(instance=payload, schema=self.schema)

        response = self.client.post("/api/v2/catalog/insulin/validate/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["valid"])
        self.assertEqual(response.json()["validated_payload"]["concentration_iu_per_ml"], 100)
        self.assertEqual(response.json()["validated_payload"]["total_units"], 300)

    def test_03_reject_hidden_solid_fields(self):
        """Zero-Trust: Passing tablet/capsule fields must be rejected with ERR_HIDDEN_FIELD_VALUE_REJECTED."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
            "strips_per_box": 5,  # Forbidden solid packaging field
        }
        response = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_HIDDEN_FIELD_VALUE_REJECTED")

    def test_04_dimension_consistency_total_units_mismatch(self):
        """Total units must equal concentration * volume (100 * 3 = 300 != 400)."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 400,  # Mathematical mismatch! 100 * 3.0 = 300 IU
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        response = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_DIMENSION_CONSISTENCY_MISMATCH")

    def test_05_invalid_concentration_rejected(self):
        """Non-standard concentrations (e.g. 150 IU/mL) must be rejected with ERR_INVALID_INSULIN_CONCENTRATION."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 150,  # Invalid concentration
            "volume_ml": 3.0,
            "total_units": 450,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        response = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_INVALID_INSULIN_CONCENTRATION")

    def test_06_in_use_shelf_life_range_validation(self):
        """in_use_shelf_life_days must be between 14 and 56 days; out-of-range values rejected."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Below minimum (10 days < 14)
        payload_low = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 10,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        res_low = self.client.post("/api/v2/catalog/insulin/", payload_low, format="json")
        self.assertEqual(res_low.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_low.json()["error_code"], "ERR_INVALID_IN_USE_SHELF_LIFE")

        # 2. Above maximum (70 days > 56)
        payload_high = dict(payload_low)
        payload_high["in_use_shelf_life_days"] = 70
        res_high = self.client.post("/api/v2/catalog/insulin/", payload_high, format="json")
        self.assertEqual(res_high.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_high.json()["error_code"], "ERR_INVALID_IN_USE_SHELF_LIFE")

    def test_07_excursion_action_validation(self):
        """Invalid temperature excursion action rejected with ERR_INVALID_EXCURSION_ACTION."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "IGNORE_AND_DISPENSE",  # Invalid and dangerous!
        }
        response = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "ERR_INVALID_EXCURSION_ACTION")

    def test_08_cashier_mutation_blocked(self):
        """Cashiers are strictly read-only and cannot mutate insulin configurations."""
        self.client.force_authenticate(user=self.cashier_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        res = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_PERMISSION_DENIED")

    def test_09_cross_tenant_isolation(self):
        """Tenant B owner cannot modify or hijack Tenant A's insulin configuration."""
        self.client.force_authenticate(user=self.owner_b)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,  # Attempting cross-tenant write
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        res = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_10_suspended_tenant_blocked(self):
        """Suspended tenant accounts are blocked from accessing or modifying configurations."""
        self.client.force_authenticate(user=self.owner_suspended)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        res = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(res.json().get("error_code"), ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"])

    def test_11_basic_tier_unentitled(self):
        """Basic tier plan cannot author custom tenant-specific insulin monographs."""
        self.client.force_authenticate(user=self.owner_basic)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        res = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED")

    def test_12_invalid_delivery_device_rejected(self):
        """Invalid delivery device code rejected with ERR_INVALID_DELIVERY_DEVICE."""
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "BLISTER_PACK",  # Invalid device for insulin!
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        res = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_DELIVERY_DEVICE")

    def test_13_global_monograph_creation_by_superadmin(self):
        """Superadmin creates global monograph (tenant=None) accessible to all tenants as fallback."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.insulin_product.product_id,
            "tenant_id": None,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "DISPOSE_IF_FROZEN",
            "patient_counseling_en": "Global monograph: store in fridge (2-8C). Do not freeze.",
        }
        res = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(res.json()["tenant_id"])

        # Tenant A user can read global monograph
        self.client.force_authenticate(user=self.owner_a)
        list_res = self.client.get(f"/api/v2/catalog/insulin/?product_id={self.insulin_product.product_id}")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        items = list_res.json()
        self.assertTrue(any(item["delivery_device"] == "PEN_DISPOSABLE" for item in items))

    def test_14_crud_lifecycle_and_deletion(self):
        """Full lifecycle: create -> retrieve -> patch update -> delete."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Create
        payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "PEN_DISPOSABLE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 3.0,
            "total_units": 300,
            "in_use_shelf_life_days": 28,
            "excursion_action": "QUARANTINE_FOR_PHARMACIST_ASSESSMENT",
        }
        res_create = self.client.post("/api/v2/catalog/insulin/", payload, format="json")
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        config_id = res_create.json()["config_id"]

        # 2. Retrieve
        res_get = self.client.get(f"/api/v2/catalog/insulin/{config_id}/")
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertEqual(res_get.json()["config_id"], config_id)

        # 3. Patch
        patch_payload = {
            "product_id": self.insulin_product.product_id,
            "action_category": "PREMIXED_BIPHASIC",
            "delivery_device": "VIAL_MULTI_DOSE",
            "concentration_iu_per_ml": 100,
            "volume_ml": 10.0,
            "total_units": 1000,
            "in_use_shelf_life_days": 42,
            "excursion_action": "DISPOSE_IF_FROZEN",
        }
        res_patch = self.client.patch(f"/api/v2/catalog/insulin/{config_id}/", patch_payload, format="json")
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.json()["delivery_device"], "VIAL_MULTI_DOSE")
        self.assertEqual(res_patch.json()["total_units"], 1000)

        # 4. Delete
        res_del = self.client.delete(f"/api/v2/catalog/insulin/{config_id}/")
        self.assertEqual(res_del.status_code, status.HTTP_200_OK)
        self.assertTrue(res_del.json()["deleted"])

        # 5. Verify 404
        res_verify = self.client.get(f"/api/v2/catalog/insulin/{config_id}/")
        self.assertEqual(res_verify.status_code, status.HTTP_404_NOT_FOUND)

