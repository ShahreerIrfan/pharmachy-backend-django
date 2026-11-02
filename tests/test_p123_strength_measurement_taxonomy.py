"""
Integration tests for P123: Strength measurement taxonomy (ADR-0123).
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
    / "strength_measurement_taxonomy.schema.json"
)


class StrengthMeasurementTaxonomyIntegrationTests(TestCase):
    """End-to-end integration tests for P123 Strength Measurement Taxonomy and Conversion Guard."""

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
            tenant_id=f"tn-p123-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"sqr-p123-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p123-{self.suffix}",
            tenant=self.tenant,
            name="Main Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

    def test_01_retrieve_strength_taxonomy_catalog(self):
        """Authenticated cashier retrieves canonical physical measurement taxonomy catalog."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/medicines/strength-taxonomy/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn("families", data)
        self.assertIn("families_count", data)
        self.assertTrue(data["families_count"] >= 5)

        families = data["families"]
        self.assertIn("MASS", families)
        self.assertIn("VOLUME", families)
        self.assertIn("PERCENTAGE", families)
        self.assertIn("CONCENTRATION", families)
        self.assertIn("BIOLOGICAL", families)

    def test_02_convert_strength_within_same_family_mass(self):
        """Valid intra-family conversion in MASS family conforms to JSON Schema."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.post(
            "/api/v2/medicines/strength-taxonomy/convert/",
            data={"value": 1, "from_unit": "g", "to_unit": "mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Validate with JSON Schema contract
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

        self.assertEqual(data["from_unit"], "g")
        self.assertEqual(data["to_unit"], "mg")
        self.assertEqual(data["from_family"], "MASS")
        self.assertEqual(data["to_family"], "MASS")
        self.assertTrue(data["is_compatible"])
        self.assertEqual(data["converted_value"], "1000")
        self.assertEqual(data["formatted_result"], "1000 mg")

        # Reverse conversion (500 mg -> 0.5 g)
        resp_rev = self.client.post(
            "/api/v2/medicines/strength-taxonomy/convert/",
            data={"value": 500, "from_unit": "mg", "to_unit": "g"},
            format="json",
        )
        self.assertEqual(resp_rev.status_code, status.HTTP_200_OK)
        data_rev = resp_rev.json()
        self.assertEqual(data_rev["converted_value"], "0.5")

    def test_03_convert_strength_within_same_family_volume(self):
        """Valid intra-family conversion in VOLUME family (L to mL)."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.post(
            "/api/v2/medicines/strength-taxonomy/convert/",
            data={"value": 2.5, "from_unit": "L", "to_unit": "ml"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["from_family"], "VOLUME")
        self.assertEqual(data["to_family"], "VOLUME")
        self.assertEqual(data["converted_value"], "2500")
        self.assertEqual(data["formatted_result"], "2500 ml")

    def test_04_cross_family_conversion_strictly_prevented(self):
        """Cross-family conversion (MASS to VOLUME: mg to mL) is strictly rejected with ERR_INCOMPATIBLE_UNIT_CONVERSION."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.post(
            "/api/v2/medicines/strength-taxonomy/convert/",
            data={"value": 500, "from_unit": "mg", "to_unit": "ml"},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_INCOMPATIBLE_UNIT_CONVERSION")
        self.assertIn("strictly forbidden", data.get("message_en", ""))
        self.assertIn("নিষিদ্ধ", data.get("message_bn", ""))

    def test_05_cross_family_mass_to_percentage_prevented(self):
        """Cross-family conversion (MASS to PERCENTAGE: mg to %) is strictly rejected."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.post(
            "/api/v2/medicines/strength-taxonomy/convert/",
            data={"value": 100, "from_unit": "mg", "to_unit": "%"},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_INCOMPATIBLE_UNIT_CONVERSION")

    def test_06_parse_strength_strings(self):
        """Parse raw strength strings into normalized numeric value, unit, and family."""
        self.client.force_authenticate(user=self.cashier)

        # Standard mass
        resp1 = self.client.post(
            "/api/v2/medicines/strength-taxonomy/parse/",
            data={"strength": "500 mg"},
            format="json",
        )
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        d1 = resp1.json()
        self.assertEqual(d1["numeric_value"], "500")
        self.assertEqual(d1["unit"], "mg")
        self.assertEqual(d1["family"], "MASS")
        self.assertFalse(d1["is_composite"])

        # Liquid concentration
        resp2 = self.client.post(
            "/api/v2/medicines/strength-taxonomy/parse/",
            data={"strength": "250mg/5mL"},
            format="json",
        )
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        d2 = resp2.json()
        self.assertEqual(d2["numeric_value"], "250")
        self.assertEqual(d2["unit"], "mg/5ml")
        self.assertEqual(d2["family"], "CONCENTRATION")

        # Percentage
        resp3 = self.client.post(
            "/api/v2/medicines/strength-taxonomy/parse/",
            data={"strength": "0.5%"},
            format="json",
        )
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        d3 = resp3.json()
        self.assertEqual(d3["numeric_value"], "0.5")
        self.assertEqual(d3["unit"], "%")
        self.assertEqual(d3["family"], "PERCENTAGE")

    def test_07_parse_composite_strength_fallback(self):
        """Parse composite strength string (e.g. 500mg+65mg) with is_composite flag."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.post(
            "/api/v2/medicines/strength-taxonomy/parse/",
            data={"strength": "500mg+65mg"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["numeric_value"], "500")
        self.assertEqual(data["unit"], "mg")
        self.assertTrue(data["is_composite"])

    def test_08_medicine_product_persists_normalized_strength_fields(self):
        """MedicineProduct model properly persists normalized numeric strength, unit, and family."""
        prod = MedicineProduct.objects.create(
            product_id=f"med-tax-{self.suffix}",
            brand_name=f"Napa Extend {self.suffix}",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="665mg",
            strength_numeric=Decimal("665.0"),
            strength_unit="mg",
            strength_unit_family="MASS",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("2.00"),
        )
        saved = MedicineProduct.objects.get(product_id=prod.product_id)
        self.assertEqual(saved.strength_numeric, Decimal("665.0000"))
        self.assertEqual(saved.strength_unit, "mg")
        self.assertEqual(saved.strength_unit_family, "MASS")

    def test_09_unauthenticated_request_rejected(self):
        """Unauthenticated call to strength taxonomy endpoint is rejected with HTTP 401."""
        resp = self.client.get("/api/v2/medicines/strength-taxonomy/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
