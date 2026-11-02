"""
Integration tests for P126: Medicine variant structure & SKUs (ADR-0126).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import json
import random
import uuid
from decimal import Decimal
from pathlib import Path

from django.test import TestCase
from jsonschema import validate
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "shared"
    / "contracts"
    / "schemas"
    / "medicine_variant_structure.schema.json"
)


class MedicineVariantStructureIntegrationTests(TestCase):
    """End-to-end integration tests for P126 Medicine Variant Structure & SKUs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Platform Superadmin User
        self.superadmin = PharmacyUser.objects.create_user(
            email=f"superadmin_{self.suffix}@platform.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Platform Superadmin",
            role=UserRole.SUPERADMIN,
            is_superuser=True,
            is_staff=True,
        )

        # 2. Pharmacy Tenant & Cashier User
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p126-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-p126-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p126-{self.suffix}",
            tenant=self.tenant,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-susp-p126-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p126-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"suspended_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 4. Canonical Parent Medicine
        self.parent_medicine = MedicineProduct.objects.create(
            product_id=f"med-napa-{self.suffix}",
            brand_name="Napa",
            generic_name="Paracetamol",
            dosage_form="TABLET",
            strength="500mg",
            strength_numeric=Decimal("500.0"),
            strength_unit="mg",
            strength_unit_family="MASS",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("1.00"),
            is_prescription_only=False,
            is_narcotic=False,
            is_antibiotic=False,
            requires_cold_chain=False,
        )

        # 5. Regulated Antibiotic Parent Medicine
        self.antibiotic_medicine = MedicineProduct.objects.create(
            product_id=f"med-moxacil-{self.suffix}",
            brand_name="Moxacil",
            generic_name="Amoxicillin Trihydrate",
            dosage_form="CAPSULE",
            strength="500mg",
            strength_numeric=Decimal("500.0"),
            strength_unit="mg",
            strength_unit_family="MASS",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("7.50"),
            cost_unit=Decimal("6.00"),
            is_prescription_only=True,
            is_narcotic=False,
            is_antibiotic=True,
            requires_cold_chain=False,
        )

    def test_01_create_variant_by_superadmin_and_validate_json_schema(self):
        """Superadmin creates commercial presentation variant and validates against JSON Schema."""
        self.client.force_authenticate(user=self.superadmin)
        variant_payload = {
            "product_id": self.parent_medicine.product_id,
            "sku": f"SKU-BEX-NAPA-500-100B-{self.suffix}",
            "variant_name": "Napa 500mg Tablet (Box of 10x10)",
            "strength": "500 mg",
            "pack_size": "Box of 100",
            "pack_size_numeric": 100,
            "route": "Oral",
            "container_type": "BLISTER_PACK",
            "barcode": f"8941100{random.randint(100000, 999999)}",
            "mrp_unit": "1.20",
            "cost_unit": "1.00",
            "mrp_pack": "120.00",
            "cost_pack": "100.00",
        }
        resp = self.client.post("/api/v2/medicines/variants/", data=variant_payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()

        # JSON Schema validation
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

        self.assertEqual(data["sku"], variant_payload["sku"])
        self.assertEqual(data["pack_size_numeric"], 100)
        self.assertEqual(data["container_type"], "BLISTER_PACK")
        self.assertEqual(data["mrp_pack"], "120.00")
        self.assertFalse(data["is_prescription_required"])

    def test_02_retrieve_variant_by_id_and_sku(self):
        """Cashier retrieves variant by ID and searches by SKU."""
        self.client.force_authenticate(user=self.superadmin)
        sku = f"SKU-BEX-NAPA-500-50B-{self.suffix}"
        resp_create = self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.parent_medicine.product_id,
                "sku": sku,
                "variant_name": "Napa 500mg Tablet (Box of 50)",
                "pack_size": "Box of 50",
                "pack_size_numeric": 50,
                "container_type": "STRIP",
                "mrp_unit": "1.20",
                "cost_unit": "1.00",
            },
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        var_id = resp_create.json()["variant_id"]

        # Cashier retrieves by ID
        self.client.force_authenticate(user=self.cashier)
        resp_get = self.client.get(f"/api/v2/medicines/variants/{var_id}/")
        self.assertEqual(resp_get.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_get.json()["sku"], sku)

        # Cashier searches by SKU
        resp_search = self.client.get(f"/api/v2/medicines/variants/?sku={sku}")
        self.assertEqual(resp_search.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_search.json()["count"], 1)

    def test_03_lookup_variant_by_barcode(self):
        """Cashier looks up variant directly by barcode scanner."""
        self.client.force_authenticate(user=self.superadmin)
        barcode = f"8941100{random.randint(100000, 999999)}"
        self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.parent_medicine.product_id,
                "sku": f"SKU-BARCODE-TEST-{self.suffix}",
                "variant_name": "Napa Barcode Test",
                "pack_size": "Box of 10",
                "pack_size_numeric": 10,
                "barcode": barcode,
                "container_type": "BLISTER_PACK",
            },
            format="json",
        )

        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get(f"/api/v2/medicines/variants/lookup-barcode/?barcode={barcode}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["barcode"], barcode)
        self.assertEqual(data["brand_name"], "Napa")

    def test_04_duplicate_sku_rejected(self):
        """Attempt to register an already existing SKU fails with ERR_DUPLICATE_SKU."""
        self.client.force_authenticate(user=self.superadmin)
        sku = f"SKU-DUP-{self.suffix}"
        self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.parent_medicine.product_id,
                "sku": sku,
                "pack_size_numeric": 10,
            },
            format="json",
        )
        # Duplicate
        resp_dup = self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.parent_medicine.product_id,
                "sku": sku,
                "pack_size_numeric": 20,
            },
            format="json",
        )
        self.assertIn(resp_dup.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertEqual(resp_dup.json().get("error_code"), "ERR_DUPLICATE_SKU")

    def test_05_duplicate_barcode_rejected(self):
        """Attempt to register an already assigned barcode fails with ERR_DUPLICATE_BARCODE."""
        self.client.force_authenticate(user=self.superadmin)
        barcode = f"8941199{random.randint(100000, 999999)}"
        self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.parent_medicine.product_id,
                "sku": f"SKU-BC-1-{self.suffix}",
                "barcode": barcode,
                "pack_size_numeric": 10,
            },
            format="json",
        )
        # Duplicate barcode
        resp_dup = self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.parent_medicine.product_id,
                "sku": f"SKU-BC-2-{self.suffix}",
                "barcode": barcode,
                "pack_size_numeric": 20,
            },
            format="json",
        )
        self.assertIn(resp_dup.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertEqual(resp_dup.json().get("error_code"), "ERR_DUPLICATE_BARCODE")

    def test_06_regulatory_classification_inherited_from_parent(self):
        """Variant strictly inherits Rx-only and antibiotic classification from parent medicine."""
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.antibiotic_medicine.product_id,
                "sku": f"SKU-MOX-500-100B-{self.suffix}",
                "variant_name": "Moxacil 500mg Capsule (Box of 100)",
                "pack_size": "Box of 100",
                "pack_size_numeric": 100,
                "container_type": "BLISTER_PACK",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertTrue(data["is_prescription_required"])
        self.assertTrue(data["is_antibiotic"])

    def test_07_tenant_owner_mutation_blocked(self):
        """Pharmacy owner cannot mutate or publish catalog variants; blocked with PLATFORM_CATALOG_OWNERSHIP_PROTECTED."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/medicines/variants/",
            data={
                "product_id": self.parent_medicine.product_id,
                "sku": f"SKU-TENANT-ATTEMPT-{self.suffix}",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json().get("error_code"), "PLATFORM_CATALOG_OWNERSHIP_PROTECTED")

    def test_08_suspended_tenant_blocked(self):
        """User belonging to suspended tenant is blocked with HTTP 403."""
        self.client.force_authenticate(user=self.suspended_user)
        self.client.handler._force_tenant = self.suspended_tenant
        resp = self.client.get(
            "/api/v2/medicines/variants/",
            HTTP_X_TENANT_ID=self.suspended_tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_09_unauthenticated_request_rejected(self):
        """Unauthenticated request returns HTTP 401."""
        resp = self.client.get("/api/v2/medicines/variants/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
