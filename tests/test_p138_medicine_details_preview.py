"""
backend/tests/test_p138_medicine_details_preview.py
Architecture Decision: ADR-0138 (P138).
Integration Tests for Unified Medicine Details Preview:
Form, Strength Taxonomy, Route of Administration, Packaging Hierarchy,
Rx / AWaRe Status, Cold Chain Storage Conditions, Commercial Variants,
Tenant Overrides, Recall Alerts, and RBAC / Cost Privacy.
"""

from __future__ import annotations

import json
import random
import uuid
from decimal import Decimal
from pathlib import Path

import jsonschema
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BarcodeAlternateIdentifier,
    BarcodeIdentifierType,
    BarcodePackagingLevel,
    Branch,
    BranchLifecycleStatus,
    CatalogueLifecycleNotice,
    CatalogueLifecycleStatus,
    ContainerType,
    MedicineCurationStatus,
    MedicineProduct,
    MedicineVariant,
    PlanTier,
    RecallScope,
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
    / "medicine_details_preview.schema.json"
)


class MedicineDetailsPreviewIntegrationTests(TestCase):
    """End-to-end integration tests for P138 Unified Medicine Details Preview."""

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

        # 2. Pharmacy Tenant 1 (Banani Health) & Staff
        self.tenant1 = Tenant.objects.create(
            tenant_id=f"tn-p138-1-{self.suffix}",
            name=f"Banani Health {self.suffix}",
            subdomain=f"banani-p138-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p138-1-{self.suffix}",
            tenant=self.tenant1,
            name="Main Banani Dispensary",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.manager1 = PharmacyUser.objects.create_user(
            email=f"manager1_{self.suffix}@banani.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Banani Branch Manager",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )
        self.cashier1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@banani.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Banani Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )

        # 3. Pharmacy Tenant 2 (Gulshan Care) & Staff
        self.tenant2 = Tenant.objects.create(
            tenant_id=f"tn-p138-2-{self.suffix}",
            name=f"Gulshan Care {self.suffix}",
            subdomain=f"gulshan-p138-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.manager2 = PharmacyUser.objects.create_user(
            email=f"manager2_{self.suffix}@gulshan.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Gulshan Manager",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant2.tenant_id,
        )

        # 4. Standard Oral OTC Medicine
        self.product_otc = MedicineProduct.objects.create(
            product_id=f"med-p138-otc-{self.suffix}",
            brand_name=f"Napa 500mg {self.suffix}",
            brand_name_bn="নাপা ৫০০ মি.গ্রা.",
            generic_name="Paracetamol",
            generic_name_bn="প্যারাসিটামল",
            dosage_form="Tablet",
            strength="500 mg",
            strength_numeric=Decimal("500.0000"),
            strength_unit="mg",
            strength_unit_family="MASS",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            route="Oral",
            barcode=f"890100{random.randint(1000000, 9999999)}",
            dgda_dar_number=f"DAR-P138-{random.randint(100, 999)}",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.95"),
            requires_cold_chain=False,
            is_prescription_only=False,
            is_narcotic=False,
            is_antibiotic=False,
            curation_status=MedicineCurationStatus.APPROVED,
        )

        # 5. Cold Chain Prescription Injection
        self.product_cold = MedicineProduct.objects.create(
            product_id=f"med-p138-cold-{self.suffix}",
            brand_name=f"Glaritus 100 IU Cartridge {self.suffix}",
            brand_name_bn="গ্ল্যারিটাস ১০০ আইইউ",
            generic_name="Insulin Glargine",
            generic_name_bn="ইনসুলিন গ্লার্জিন",
            dosage_form="Injection",
            strength="100 IU/mL",
            strength_numeric=Decimal("100.0000"),
            strength_unit="IU/mL",
            strength_unit_family="BIOLOGICAL",
            manufacturer="Wockhardt Ltd.",
            route="Subcutaneous",
            barcode=f"890200{random.randint(1000000, 9999999)}",
            dgda_dar_number="DAR-INS-901",
            mrp_unit=Decimal("450.00"),
            cost_unit=Decimal("380.00"),
            requires_cold_chain=True,
            min_temp_celsius=Decimal("2.00"),
            max_temp_celsius=Decimal("8.00"),
            is_prescription_only=True,
            is_narcotic=False,
            is_antibiotic=False,
            curation_status=MedicineCurationStatus.APPROVED,
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def _validate_schema(self, instance, definition_key: str):
        sub_schema = dict(self.schema_doc["definitions"][definition_key])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=instance, schema=sub_schema)

    def test_01_retrieve_complete_medicine_preview_validates_json_schema(self):
        """P138 Acceptance: Retrieving unified medicine preview validates JSON schema and clinical attributes."""
        self.client.force_authenticate(user=self.manager1)

        resp = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Schema Validation
        self._validate_schema(data, "MedicineDetailsPreviewRecord")

        self.assertEqual(data["product_id"], self.product_otc.product_id)
        self.assertEqual(data["brand_name"], self.product_otc.brand_name)
        self.assertEqual(data["dosage_form"], "Tablet")
        self.assertEqual(data["strength"], "500 mg")
        self.assertEqual(data["route"], "Oral")
        self.assertEqual(data["mrp_unit"], "1.20")
        self.assertEqual(data["effective_mrp_unit"], "1.20")
        self.assertFalse(data["is_prescription_only"])
        self.assertFalse(data["is_narcotic"])
        self.assertFalse(data["is_antibiotic"])
        self.assertFalse(data["requires_cold_chain"])
        self.assertEqual(len(data["packaging_hierarchy"]), 3)
        self.assertEqual(len(data["immutable_preview_hash"]), 64)

    def test_02_cold_chain_medicine_preview_displays_temp_range_and_warning(self):
        """P138 Acceptance: Cold chain injection displays temperature bounds (2-8 C) and refrigerator instructions."""
        self.client.force_authenticate(user=self.manager1)

        resp = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_cold.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        self.assertTrue(data["requires_cold_chain"])
        self.assertEqual(data["min_temp_celsius"], 2.0)
        self.assertEqual(data["max_temp_celsius"], 8.0)
        self.assertTrue(data["is_prescription_only"])
        self.assertIn("refrigerator", data["storage_instructions"].lower())
        self.assertIn("রেফ্রিজারেটরে", data["storage_instructions_bn"])

    def test_03_prescription_and_antimicrobial_classifications_accurately_represented(self):
        """P138 Acceptance: Antibiotic POM product shows prescription required and antimicrobial classification."""
        product_ab = MedicineProduct.objects.create(
            product_id=f"med-p138-ab-{self.suffix}",
            brand_name=f"Cef-3 200mg {self.suffix}",
            generic_name="Cefixime",
            dosage_form="Capsule",
            strength="200 mg",
            manufacturer="Square Pharmaceuticals PLC",
            route="Oral",
            mrp_unit=Decimal("35.00"),
            cost_unit=Decimal("28.00"),
            is_prescription_only=True,
            is_antibiotic=True,
            curation_status=MedicineCurationStatus.APPROVED,
        )

        self.client.force_authenticate(user=self.manager1)
        resp = self.client.get(f"/api/v2/catalog/medicine-previews/{product_ab.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        self.assertTrue(data["is_prescription_only"])
        self.assertTrue(data["is_antibiotic"])
        self.assertEqual(data["rx_classification_label"], "Prescription-Only Medicine (POM)")
        self.assertIn("ACCESS", data["antimicrobial_classification"])

    def test_04_tenant_custom_override_and_shelf_location_reflected_in_preview(self):
        """P138 Acceptance: Store-level price overrides and shelf locations appear in tenant's preview."""
        TenantMedicineConfig.objects.create(
            config_id=f"cfg-p138-{self.suffix}",
            tenant=self.tenant1,
            product=self.product_otc,
            local_brand_name="Fast Action Napa",
            custom_mrp_unit=Decimal("1.50"),
            shelf_location="Aisle 2 - Shelf B3",
            is_visible=True,
        )

        # Tenant 1 queries preview -> sees override
        self.client.force_authenticate(user=self.manager1)
        resp = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        self.assertTrue(data["has_tenant_override"])
        self.assertEqual(data["effective_mrp_unit"], "1.50")
        self.assertEqual(data["local_brand_name"], "Fast Action Napa")
        self.assertEqual(data["shelf_location"], "Aisle 2 - Shelf B3")

        # Tenant 2 queries preview -> sees master price, no Tenant 1 override
        self.client.force_authenticate(user=self.manager2)
        resp2 = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        data2 = resp2.json()

        self.assertFalse(data2["has_tenant_override"])
        self.assertEqual(data2["effective_mrp_unit"], "1.20")
        self.assertEqual(data2["shelf_location"], "")

    def test_05_variants_and_packaging_hierarchy_correctly_mapped(self):
        """P138 Acceptance: Associated commercial variants and packaging tiers are mapped into preview."""
        # Add variant
        variant = MedicineVariant.objects.create(
            variant_id=f"var-p138-{self.suffix}",
            product=self.product_otc,
            sku=f"SKU-NAPA-500-100B-{self.suffix}",
            variant_name="Napa 500mg Tablet - Box of 10x10",
            strength="500 mg",
            pack_size="Box of 100",
            pack_size_numeric=100,
            container_type=ContainerType.BLISTER_PACK,
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.95"),
            mrp_pack=Decimal("120.00"),
            cost_pack=Decimal("95.00"),
        )
        # Add alternate box barcode
        BarcodeAlternateIdentifier.objects.create(
            identifier_id=f"bar-p138-{self.suffix}",
            tenant_id_str="platform",
            product=self.product_otc,
            barcode_value=f"1890100{random.randint(1000000, 9999999)}",
            identifier_type=BarcodeIdentifierType.SECONDARY_BOX_GTIN14,
            packaging_level=BarcodePackagingLevel.BOX,
            priority_order=3,
            pack_quantity_multiplier=100,
        )

        self.client.force_authenticate(user=self.manager1)
        resp = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Check variants
        self.assertEqual(len(data["variants"]), 1)
        self.assertEqual(data["variants"][0]["sku"], variant.sku)
        self.assertEqual(data["variants"][0]["mrp_pack"], "120.00")

        # Check packaging hierarchy
        box_tier = next((t for t in data["packaging_hierarchy"] if t["packaging_level"] == "BOX"), None)
        self.assertNotNull = self.assertIsNotNone(box_tier)
        self.assertEqual(box_tier["multiplier"], 100)

    def test_06_active_recall_notice_populates_recall_warning_and_blocks_dispense(self):
        """P138 Acceptance: Active safety recall triggers recall alert and marks dispensing as blocked."""
        CatalogueLifecycleNotice.objects.create(
            notice_id=f"rec-p138-{self.suffix}",
            tenant_id_str="platform",
            product=self.product_otc,
            lifecycle_status=CatalogueLifecycleStatus.RECALLED,
            recall_scope=RecallScope.NATIONWIDE_ALL_BATCHES,
            regulatory_authority="DGDA",
            notice_number="DGDA-URGENT-RC-99",
            reason="Contamination detected in manufacturing solvent.",
            effective_date=timezone.now(),
            is_active=True,
        )

        self.client.force_authenticate(user=self.manager1)
        resp = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        self.assertTrue(data["is_recalled"])
        self.assertTrue(data["is_dispensing_blocked"])
        self.assertIsNotNone(data["active_recall_notice"])
        self.assertEqual(data["active_recall_notice"]["notice_number"], "DGDA-URGENT-RC-99")

    def test_07_cashier_permitted_to_preview_but_wholesale_cost_is_masked(self):
        """P138 Acceptance: Cashier can preview medicine details, but wholesale acquisition cost is masked."""
        # 1. Cashier requests preview
        self.client.force_authenticate(user=self.cashier1)
        resp_cashier = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp_cashier.status_code, status.HTTP_200_OK)
        data_cashier = resp_cashier.json()

        self.assertIsNone(data_cashier["cost_unit"])
        self.assertEqual(data_cashier["mrp_unit"], "1.20")

        # 2. Branch Manager requests preview -> cost_unit visible
        self.client.force_authenticate(user=self.manager1)
        resp_mgr = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp_mgr.status_code, status.HTTP_200_OK)
        data_mgr = resp_mgr.json()

        self.assertEqual(data_mgr["cost_unit"], "0.95")

    def test_08_cross_tenant_preview_override_tampering_blocked_with_403(self):
        """P138 Acceptance: User from Tenant 2 attempting to view Tenant 1's store overrides is rejected with 403."""
        self.client.force_authenticate(user=self.manager2)

        resp = self.client.get(
            f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/?tenant_id={self.tenant1.tenant_id}"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        err = str(resp.json())
        self.assertTrue("ERR_CROSS_TENANT_ACCESS" in err or "ERR_CROSS_TENANT_VIOLATION" in err)

    def test_09_non_existent_medicine_id_returns_404(self):
        """P138 Acceptance: Non-existent product ID returns HTTP 404 Not Found."""
        self.client.force_authenticate(user=self.manager1)

        resp = self.client.get("/api/v2/catalog/medicine-previews/med-non-existent-9999/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_10_unauthenticated_request_rejected_with_401(self):
        """P138 Acceptance: Unauthenticated requests are rejected with HTTP 401 Unauthorized."""
        self.client.force_authenticate(user=None)

        resp = self.client.get(f"/api/v2/catalog/medicine-previews/{self.product_otc.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
