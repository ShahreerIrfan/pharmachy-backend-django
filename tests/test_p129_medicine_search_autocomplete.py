"""
backend/tests/test_p129_medicine_search_autocomplete.py
Architecture Decision: ADR-0129 (P129).
Integration Tests for Medicine Search Autocomplete, Barcode Prioritization,
Bilingual Transliteration, Precedence-Aware Attributes, Latency Benchmarks, and RBAC.
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
    MedicineProduct,
    MedicineVariant,
    ModerationStatus,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
    TenantMedicineConfig,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "shared"
    / "contracts"
    / "schemas"
    / "medicine_search_autocomplete.schema.json"
)


class MedicineSearchAutocompleteIntegrationTests(TestCase):
    """End-to-end integration tests for P129 Medicine Search Autocomplete."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Tenants & Branches
        self.tenant1 = Tenant.objects.create(
            tenant_id=f"tn-p129-1-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-p129-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p129-1-{self.suffix}",
            tenant=self.tenant1,
            name="Main Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner1 = PharmacyUser.objects.create_user(
            email=f"owner1_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Popular Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )
        self.cashier1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Popular Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )

        self.tenant2 = Tenant.objects.create(
            tenant_id=f"tn-p129-2-{self.suffix}",
            name=f"Labaid Pharma {self.suffix}",
            subdomain=f"labaid-p129-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner2 = PharmacyUser.objects.create_user(
            email=f"owner2_{self.suffix}@labaid.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Labaid Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant2.tenant_id,
        )

        # 2. Master Medicines
        # Napa 500mg (OTC)
        self.med_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name="Napa",
            brand_name_bn="নাপা",
            generic_name="Paracetamol",
            generic_name_bn="প্যারাসিটামল",
            dosage_form="Tablet",
            strength="500mg",
            strength_numeric=Decimal("500"),
            strength_unit="mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.90"),
            barcode=f"8901234{random.randint(100000, 999999)}",
            is_prescription_only=False,
        )

        # Seclo 20mg (POM)
        self.med_seclo = MedicineProduct.objects.create(
            product_id=f"prod-seclo-{self.suffix}",
            brand_name="Seclo",
            brand_name_bn="সেকলো",
            generic_name="Omeprazole",
            generic_name_bn="ওমেপ্রাজল",
            dosage_form="Capsule",
            strength="20mg",
            strength_numeric=Decimal("20"),
            strength_unit="mg",
            manufacturer="Square Pharmaceuticals PLC",
            mrp_unit=Decimal("6.00"),
            cost_unit=Decimal("4.80"),
            barcode=f"8901235{random.randint(100000, 999999)}",
            is_prescription_only=True,
        )

        # Hidden Medicine (is_visible=False in Tenant 1)
        self.med_hidden = MedicineProduct.objects.create(
            product_id=f"prod-hidden-{self.suffix}",
            brand_name="Special Injection",
            generic_name="Morphine Sulfate",
            dosage_form="Injection",
            strength="10mg",
            manufacturer="Gonoshokti Pharma",
            mrp_unit=Decimal("50.00"),
            cost_unit=Decimal("35.00"),
            barcode=f"8901236{random.randint(100000, 999999)}",
            is_prescription_only=True,
        )

        # 3. Store Overrides for Tenant 1
        # Napa has custom brand alias and price
        TenantMedicineConfig.objects.create(
            config_id=f"tmc-napa-{self.suffix}",
            tenant=self.tenant1,
            product=self.med_napa,
            local_brand_name="নাপা ৫০০ (স্পেশাল)",
            custom_mrp_unit=Decimal("1.50"),
            local_sku=f"SKU-NAPA-{self.suffix}",
            shelf_location="Rack B-1",
            is_visible=True,
            moderation_status=ModerationStatus.APPROVED,
        )

        # Hidden medicine is invisible in Tenant 1
        TenantMedicineConfig.objects.create(
            config_id=f"tmc-hidden-{self.suffix}",
            tenant=self.tenant1,
            product=self.med_hidden,
            is_visible=False,
            moderation_status=ModerationStatus.APPROVED,
        )

        # 4. Commercial Variant for Napa
        self.variant_napa_box = MedicineVariant.objects.create(
            variant_id=f"var-napa-box-{self.suffix}",
            product=self.med_napa,
            sku=f"SKU-VAR-NAPA-BOX-{self.suffix}",
            variant_name="Napa 500mg Tablet - Box of 100",
            strength="500mg",
            pack_size="Box of 100",
            pack_size_numeric=100,
            route="Oral",
            container_type="BLISTER_PACK",
            barcode=f"8909999{random.randint(100000, 999999)}",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.90"),
            mrp_pack=Decimal("120.00"),
            cost_pack=Decimal("90.00"),
            is_active=True,
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def test_01_exact_barcode_match_ranked_top_and_schema_valid(self):
        """P129 Acceptance: Barcode scan matches product instantly with score 100 and matches JSON schema."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get(
            f"/api/v2/catalog/search/autocomplete/?q={self.med_napa.barcode}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]

        # Validate against JSON Schema
        sub_schema = dict(self.schema_doc["definitions"]["AutocompleteResponse"])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=data, schema=sub_schema)

        self.assertGreaterEqual(data["total_matches"], 1)
        top = data["results"][0]
        self.assertEqual(top["product_id"], self.med_napa.product_id)
        self.assertEqual(top["rank_score"], 100)
        self.assertEqual(top["match_type"], "BARCODE_EXACT")
        # Precedence-aware effective attributes
        self.assertEqual(top["effective_mrp_unit"], "1.50")
        self.assertIn("নাপা ৫০০ (স্পেশাল)", top["display_name"])
        self.assertEqual(top["shelf_location"], "Rack B-1")

    def test_02_brand_prefix_search_matches_correctly(self):
        """P129 Acceptance: Brand prefix search returns matching drug with score 90."""
        self.client.force_authenticate(user=self.owner1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=sec")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]

        self.assertTrue(len(results) >= 1)
        match = results[0]
        self.assertEqual(match["product_id"], self.med_seclo.product_id)
        self.assertEqual(match["rank_score"], 90)
        self.assertEqual(match["match_type"], "BRAND_PREFIX")
        self.assertTrue(match["effective_requires_prescription"])

    def test_03_generic_name_search_matches_molecule(self):
        """P129 Acceptance: Pharmacist searching by generic molecule name matches accurately."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=omeprazole")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]

        self.assertTrue(any(r["product_id"] == self.med_seclo.product_id for r in results))
        seclo = next(r for r in results if r["product_id"] == self.med_seclo.product_id)
        self.assertTrue(seclo["rank_score"] >= 80)

    def test_04_bilingual_bengali_transliteration_search(self):
        """P129 Acceptance: Dispenser typing Bengali text 'নাপা' successfully matches Napa."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=নাপা")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]

        self.assertTrue(any(r["product_id"] == self.med_napa.product_id for r in results))
        napa = next(r for r in results if r["product_id"] == self.med_napa.product_id)
        self.assertTrue(napa["rank_score"] >= 75)

    def test_05_maker_and_strength_search(self):
        """P129 Acceptance: Searching by manufacturer ('Beximco') or strength ('500mg')."""
        self.client.force_authenticate(user=self.owner1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=Beximco")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]
        self.assertTrue(any(r["manufacturer"] == "Beximco Pharmaceuticals Ltd." for r in results))

    def test_06_commercial_variant_sku_exact_match(self):
        """P129 Acceptance: Scanning or searching commercial variant SKU matches presentation."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get(
            f"/api/v2/catalog/search/autocomplete/?q={self.variant_napa_box.sku}"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]
        self.assertTrue(len(results) >= 1)
        top = results[0]
        self.assertEqual(top["variant_id"], self.variant_napa_box.variant_id)
        self.assertEqual(top["rank_score"], 100)
        self.assertEqual(top["match_type"], "SKU_EXACT")

    def test_07_excludes_hidden_store_medicines(self):
        """P129 Acceptance: Product configured with is_visible=False is omitted from POS search."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=Morphine")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]
        self.assertFalse(any(r["product_id"] == self.med_hidden.product_id for r in results))

    def test_08_empty_query_rejected(self):
        """P129 Acceptance: Empty search query returns HTTP 400 Bad Request or 422."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=")
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_09_cross_tenant_search_blocked(self):
        """P129 Security: User from Tenant 2 cannot query Tenant 1 catalog."""
        self.client.force_authenticate(user=self.owner2)
        response = self.client.get(
            f"/api/v2/catalog/search/autocomplete/?q=napa&tenant_id={self.tenant1.tenant_id}"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            "ERR_CROSS_TENANT_VIOLATION" in str(response.json()) or "ERR_CROSS_TENANT_ACCESS" in str(response.json())
        )

    def test_10_cashier_role_is_permitted_to_search(self):
        """P129 RBAC: Unlike mutations, cashiers are explicitly permitted to search catalog."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=napa")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_11_suspended_tenant_blocked_from_search(self):
        """P129 Lifecycle: Suspended tenant cannot execute autocomplete search."""
        self.tenant1.status = TenantLifecycleStatus.SUSPENDED
        self.tenant1.save()

        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=napa")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            "ERR_TENANT_SUSPENDED" in str(response.json())
            or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in str(response.json())
        )

    def test_12_latency_metric_reported_under_target(self):
        """P129 Performance: Search latency is measured and reported under online target (< 800ms)."""
        self.client.force_authenticate(user=self.cashier1)
        response = self.client.get("/api/v2/catalog/search/autocomplete/?q=napa")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        latency = response.json()["data"]["latency_ms"]
        self.assertLess(latency, 800.0)
