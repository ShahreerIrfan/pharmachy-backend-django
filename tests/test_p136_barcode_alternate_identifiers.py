"""
backend/tests/test_p136_barcode_alternate_identifiers.py
Architecture Decision: ADR-0136 (P136).
Integration Tests for Barcode & Alternate Identifiers:
Primary, Secondary, and Alternate GTIN/EAN/Store Codes across Packaging Tiers,
Contextual Uniqueness, Scan Prioritization Resolution, RBAC, and Audit Immutability.
"""

from __future__ import annotations

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
    NonMedicineCategory,
    NonMedicineRetailItem,
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
    / "barcode_identifier.schema.json"
)


class BarcodeAlternateIdentifierIntegrationTests(TestCase):
    """End-to-end integration tests for P136 Barcode & Alternate Identifiers."""

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
            tenant_id=f"tn-p136-1-{self.suffix}",
            name=f"Banani Health {self.suffix}",
            subdomain=f"banani-p136-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p136-1-{self.suffix}",
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
            tenant_id=f"tn-p136-2-{self.suffix}",
            name=f"Gulshan Care {self.suffix}",
            subdomain=f"gulshan-p136-{self.suffix}",
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

        # 4. Suspended Tenant & Owner
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-p136-susp-{self.suffix}",
            name=f"Suspended Pharmacy {self.suffix}",
            subdomain=f"susp-p136-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_owner = PharmacyUser.objects.create_user(
            email=f"susp_owner_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 5. Shared Medicine Products
        self.product1 = MedicineProduct.objects.create(
            product_id=f"med-p136-1-{self.suffix}",
            brand_name=f"Napa Extra 500mg {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("2.00"),
            curation_status=MedicineCurationStatus.APPROVED,
        )
        self.product2 = MedicineProduct.objects.create(
            product_id=f"med-p136-2-{self.suffix}",
            brand_name=f"Ace Plus 500mg {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Square Pharmaceuticals PLC",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("2.00"),
            curation_status=MedicineCurationStatus.APPROVED,
        )

        # 6. Retail Item
        self.retail_item1 = NonMedicineRetailItem.objects.create(
            item_id=f"ret-p136-{self.suffix}",
            tenant=self.tenant1,
            tenant_id_str=self.tenant1.tenant_id,
            name=f"Omron Digital Thermometer {self.suffix}",
            category=NonMedicineCategory.DIAGNOSTIC_DEVICES,
            mrp=Decimal("350.00"),
            cost_price=Decimal("280.00"),
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def _validate_schema(self, instance, definition_key: str):
        sub_schema = dict(self.schema_doc["definitions"][definition_key])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=instance, schema=sub_schema)

    def test_01_register_primary_and_box_barcodes_validates_json_schema(self):
        """P136 Acceptance: Registering primary EAN-13 and secondary box GTIN-14 validates JSON Schema contract."""
        self.client.force_authenticate(user=self.manager1)

        primary_barcode = f"890100{random.randint(1000000, 9999999)}"
        payload_unit = {
            "product_id": self.product1.product_id,
            "barcode_value": primary_barcode,
            "identifier_type": "PRIMARY_EAN13_GTIN",
            "packaging_level": "UNIT",
            "priority_order": 1,
            "pack_quantity_multiplier": 1,
            "description": "Primary retail unit barcode",
        }

        resp_unit = self.client.post("/api/v2/catalog/barcodes/", data=payload_unit, format="json")
        self.assertEqual(resp_unit.status_code, status.HTTP_201_CREATED)
        unit_data = resp_unit.json()

        # Schema Validation
        self._validate_schema(unit_data, "BarcodeIdentifierRecord")
        self.assertEqual(unit_data["barcode_value"], primary_barcode)
        self.assertEqual(unit_data["packaging_level"], "UNIT")
        self.assertEqual(unit_data["priority_order"], 1)
        self.assertEqual(unit_data["pack_quantity_multiplier"], 1)
        self.assertEqual(len(unit_data["immutable_hash"]), 64)

        # Register outer trade box GTIN-14
        box_barcode = f"1890100{random.randint(1000000, 9999999)}"
        payload_box = {
            "product_id": self.product1.product_id,
            "barcode_value": box_barcode,
            "identifier_type": "SECONDARY_BOX_GTIN14",
            "packaging_level": "BOX",
            "priority_order": 3,
            "pack_quantity_multiplier": 100,
            "description": "Outer trade box GTIN-14 (10 strips x 10 tablets)",
        }

        resp_box = self.client.post("/api/v2/catalog/barcodes/", data=payload_box, format="json")
        self.assertEqual(resp_box.status_code, status.HTTP_201_CREATED)
        box_data = resp_box.json()

        self._validate_schema(box_data, "BarcodeIdentifierRecord")
        self.assertEqual(box_data["barcode_value"], box_barcode)
        self.assertEqual(box_data["packaging_level"], "BOX")
        self.assertEqual(box_data["priority_order"], 3)
        self.assertEqual(box_data["pack_quantity_multiplier"], 100)

    def test_02_contextual_uniqueness_blocks_duplicate_barcode_for_different_product(self):
        """P136 Acceptance: Re-assigning an already active barcode to another product triggers ERR_BARCODE_ALREADY_ASSIGNED."""
        self.client.force_authenticate(user=self.manager1)
        barcode = f"890200{random.randint(1000000, 9999999)}"

        payload1 = {
            "product_id": self.product1.product_id,
            "barcode_value": barcode,
            "identifier_type": "PRIMARY_EAN13_GTIN",
            "packaging_level": "UNIT",
        }
        resp1 = self.client.post("/api/v2/catalog/barcodes/", data=payload1, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        # Attempt to register same barcode to product2 in same tenant
        payload2 = {
            "product_id": self.product2.product_id,
            "barcode_value": barcode,
            "identifier_type": "PRIMARY_EAN13_GTIN",
            "packaging_level": "UNIT",
        }
        resp2 = self.client.post("/api/v2/catalog/barcodes/", data=payload2, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_400_BAD_REQUEST)
        err = str(resp2.json())
        self.assertIn("ERR_BARCODE_ALREADY_ASSIGNED", err)

    def test_03_scan_prioritization_resolves_highest_priority_identifier(self):
        """P136 Acceptance: Lookup endpoint resolves matched record with highest priority."""
        self.client.force_authenticate(user=self.manager1)
        scan_code = f"890300{random.randint(1000000, 9999999)}"

        payload = {
            "product_id": self.product1.product_id,
            "barcode_value": scan_code,
            "identifier_type": "PRIMARY_EAN13_GTIN",
            "packaging_level": "UNIT",
            "priority_order": 1,
            "pack_quantity_multiplier": 1,
        }
        resp_create = self.client.post("/api/v2/catalog/barcodes/", data=payload, format="json")
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)

        # Lookup by scan code
        resp_lookup = self.client.get(f"/api/v2/catalog/barcodes/lookup/?code={scan_code}")
        self.assertEqual(resp_lookup.status_code, status.HTTP_200_OK)
        lookup_data = resp_lookup.json()

        self.assertTrue(lookup_data["matched"])
        self.assertEqual(lookup_data["barcode_value"], scan_code)
        self.assertEqual(lookup_data["resolved_packaging_level"], "UNIT")
        self.assertEqual(lookup_data["pack_quantity_multiplier"], 1)
        self.assertEqual(lookup_data["identifier_record"]["product_id"], self.product1.product_id)

    def test_04_scan_box_barcode_resolves_pack_quantity_multiplier(self):
        """P136 Acceptance: Scanning outer trade box barcode resolves pack quantity multiplier and packaging level."""
        self.client.force_authenticate(user=self.manager1)
        box_code = f"1890400{random.randint(1000000, 9999999)}"

        payload = {
            "product_id": self.product1.product_id,
            "barcode_value": box_code,
            "identifier_type": "SECONDARY_BOX_GTIN14",
            "packaging_level": "BOX",
            "priority_order": 3,
            "pack_quantity_multiplier": 100,
        }
        resp_create = self.client.post("/api/v2/catalog/barcodes/", data=payload, format="json")
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)

        resp_lookup = self.client.get(f"/api/v2/catalog/barcodes/lookup/?code={box_code}")
        self.assertEqual(resp_lookup.status_code, status.HTTP_200_OK)
        lookup_data = resp_lookup.json()

        self.assertTrue(lookup_data["matched"])
        self.assertEqual(lookup_data["resolved_packaging_level"], "BOX")
        self.assertEqual(lookup_data["pack_quantity_multiplier"], 100)
        self.assertEqual(lookup_data["identifier_record"]["target_name"], self.product1.brand_name)

    def test_05_cashier_can_lookup_for_checkout_but_denied_from_registering(self):
        """P136 Acceptance: Cashiers can scan/lookup barcodes at checkout, but cannot mutate catalog barcodes."""
        # 1. Manager registers barcode
        self.client.force_authenticate(user=self.manager1)
        code = f"890500{random.randint(1000000, 9999999)}"
        self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product1.product_id,
                "barcode_value": code,
                "identifier_type": "PRIMARY_EAN13_GTIN",
                "packaging_level": "UNIT",
            },
            format="json",
        )

        # 2. Cashier lookups barcode -> Allowed
        self.client.force_authenticate(user=self.cashier1)
        resp_lookup = self.client.get(f"/api/v2/catalog/barcodes/lookup/?code={code}")
        self.assertEqual(resp_lookup.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_lookup.json()["matched"])

        # 3. Cashier attempts to register a barcode -> Forbidden
        resp_create = self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product2.product_id,
                "barcode_value": f"890501{random.randint(1000000, 9999999)}",
                "identifier_type": "PRIMARY_EAN13_GTIN",
                "packaging_level": "UNIT",
            },
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_STAFF_ROLE_DENIED", str(resp_create.json()))

    def test_06_cross_tenant_barcode_mutation_blocked_with_403(self):
        """P136 Acceptance: Staff from Tenant 2 cannot alter or archive barcodes belonging to Tenant 1."""
        self.client.force_authenticate(user=self.manager1)
        code = f"890600{random.randint(1000000, 9999999)}"
        resp = self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product1.product_id,
                "barcode_value": code,
                "identifier_type": "INTERNAL_STORE_BARCODE",
                "packaging_level": "UNIT",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        ident_id = resp.json()["identifier_id"]

        # Manager 2 attempts to change priority
        self.client.force_authenticate(user=self.manager2)
        resp_pri = self.client.post(
            f"/api/v2/catalog/barcodes/{ident_id}/priority/",
            data={"priority_order": 2},
            format="json",
        )
        self.assertEqual(resp_pri.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_ACCESS", str(resp_pri.json()))

        # Manager 2 attempts to archive
        resp_arc = self.client.post(
            f"/api/v2/catalog/barcodes/{ident_id}/archive/",
            data={"reason": "Malicious attempt"},
            format="json",
        )
        self.assertEqual(resp_arc.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_ACCESS", str(resp_arc.json()))

    def test_07_suspended_tenant_blocked_with_403(self):
        """P136 Acceptance: Suspended tenant cannot register barcode identifiers."""
        self.client.force_authenticate(user=self.suspended_owner)
        resp = self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product1.product_id,
                "barcode_value": f"890700{random.randint(1000000, 9999999)}",
                "identifier_type": "PRIMARY_EAN13_GTIN",
                "packaging_level": "UNIT",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        err = str(resp.json())
        self.assertTrue(
            "ERR_TENANT_SUSPENDED" in err or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in err
        )

    def test_08_update_priority_order_recomputes_hash_and_increments_version(self):
        """P136 Acceptance: Updating priority order recomputes SHA-256 immutable hash and increments version."""
        self.client.force_authenticate(user=self.manager1)
        code = f"890800{random.randint(1000000, 9999999)}"
        resp_create = self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product1.product_id,
                "barcode_value": code,
                "identifier_type": "INTERNAL_STORE_BARCODE",
                "packaging_level": "STRIP",
                "priority_order": 4,
            },
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        data1 = resp_create.json()
        ident_id = data1["identifier_id"]
        v1 = data1["version_number"]
        hash1 = data1["immutable_hash"]

        # Update priority to 1
        resp_update = self.client.post(
            f"/api/v2/catalog/barcodes/{ident_id}/priority/",
            data={"priority_order": 1},
            format="json",
        )
        self.assertEqual(resp_update.status_code, status.HTTP_200_OK)
        data2 = resp_update.json()

        self.assertEqual(data2["priority_order"], 1)
        self.assertEqual(data2["version_number"], v1 + 1)
        self.assertNotEqual(data2["immutable_hash"], hash1)
        self.assertEqual(len(data2["immutable_hash"]), 64)

    def test_09_archive_barcode_deactivates_and_frees_up_code(self):
        """P136 Acceptance: Soft archiving a barcode deactivates it and allows subsequent assignment without conflict."""
        self.client.force_authenticate(user=self.manager1)
        code = f"890900{random.randint(1000000, 9999999)}"

        resp_create = self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product1.product_id,
                "barcode_value": code,
                "identifier_type": "PRIMARY_EAN13_GTIN",
                "packaging_level": "UNIT",
            },
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        ident_id = resp_create.json()["identifier_id"]

        # Archive barcode
        resp_archive = self.client.post(
            f"/api/v2/catalog/barcodes/{ident_id}/archive/",
            data={"reason": "Packaging redesign by manufacturer"},
            format="json",
        )
        self.assertEqual(resp_archive.status_code, status.HTTP_200_OK)
        self.assertFalse(resp_archive.json()["is_active"])

        # Re-assigning same barcode to product2 now succeeds because previous is inactive
        resp_reassign = self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product2.product_id,
                "barcode_value": code,
                "identifier_type": "PRIMARY_EAN13_GTIN",
                "packaging_level": "UNIT",
            },
            format="json",
        )
        self.assertEqual(resp_reassign.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp_reassign.json()["is_active"])

    def test_10_unauthenticated_request_rejected_with_401(self):
        """P136 Acceptance: Unauthenticated access to catalog barcode endpoints is rejected with HTTP 401."""
        self.client.force_authenticate(user=None)

        resp_list = self.client.get("/api/v2/catalog/barcodes/")
        self.assertEqual(resp_list.status_code, status.HTTP_401_UNAUTHORIZED)

        resp_create = self.client.post(
            "/api/v2/catalog/barcodes/",
            data={
                "product_id": self.product1.product_id,
                "barcode_value": "890999000111",
                "identifier_type": "PRIMARY_EAN13_GTIN",
                "packaging_level": "UNIT",
            },
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_401_UNAUTHORIZED)
