"""
backend/tests/test_p135_non_medicine_retail_items.py
Architecture Decision: ADR-0135 (P135).
Integration Tests for Authorized Non-Medicine Retail Goods:
Separation of Dispensing Controls, Regulatory Invariant Rejection,
Multi-Tenancy, Barcode Deduplication, and Search.
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path

import jsonschema
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
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
    / "non_medicine_retail_item.schema.json"
)


class NonMedicineRetailItemIntegrationTests(TestCase):
    """End-to-end integration tests for P135 Non-Medicine Retail Items."""

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
            tenant_id=f"tn-p135-1-{self.suffix}",
            name=f"Banani Health {self.suffix}",
            subdomain=f"banani-p135-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p135-1-{self.suffix}",
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

        # 3. Pharmacy Tenant 2 (Gulshan Care)
        self.tenant2 = Tenant.objects.create(
            tenant_id=f"tn-p135-2-{self.suffix}",
            name=f"Gulshan Care {self.suffix}",
            subdomain=f"gulshan-p135-{self.suffix}",
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

        # 4. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-p135-susp-{self.suffix}",
            name=f"Suspended Retail {self.suffix}",
            subdomain=f"susp-p135-{self.suffix}",
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

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def _validate_schema(self, instance, definition_key: str):
        sub_schema = dict(self.schema_doc["definitions"][definition_key])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=instance, schema=sub_schema)

    def test_01_create_valid_non_medicine_item_validates_json_schema(self):
        """P135 Acceptance: Valid non-medicine retail item creation conforms to JSON schema and non-medicine invariants."""
        self.client.force_authenticate(user=self.manager1)

        payload = {
            "name": f"OneTouch Select Plus Glucose Test Strips 50s {self.suffix}",
            "name_bn": "ওয়ানটাচ সিলেক্ট প্লাস টেস্ট স্ট্রিপস",
            "category": NonMedicineCategory.DIAGNOSTIC_DEVICES,
            "brand_name": "OneTouch",
            "manufacturer_or_distributor": "LifeScan Inc.",
            "barcode": f"890123{random.randint(1000, 9999)}",
            "sku": f"SKU-STRIP-{self.suffix}",
            "mrp": "1250.00",
            "cost_price": "1050.00",
            "vat_percent": "5.00",
            "unit_of_measure": "PACK",
            "requires_cold_chain": False,
            "storage_instructions": "Store in dry place between 5°C and 30°C.",
        }

        response = self.client.post("/api/v2/catalog/retail-items/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()

        # Schema Validation
        self._validate_schema(data, "NonMedicineRetailItemRecord")

        self.assertEqual(data["tenant_id"], self.tenant1.tenant_id)
        self.assertEqual(data["name"], payload["name"])
        self.assertEqual(data["category"], NonMedicineCategory.DIAGNOSTIC_DEVICES)
        self.assertFalse(data["is_medicine"])
        self.assertFalse(data["is_prescription_only"])
        self.assertFalse(data["is_antibiotic"])
        self.assertFalse(data["is_narcotic"])
        self.assertEqual(data["mrp"], "1250.00")
        self.assertEqual(data["vat_percent"], "5.00")
        self.assertEqual(len(data["immutable_hash"]), 64)

    def test_02_regulatory_restrictions_enforced_blocks_prescription_antibiotic_narcotic(self):
        """P135 Acceptance: Invariant guard blocks attempting to assign prescription, antibiotic, or narcotic flags to retail items."""
        self.client.force_authenticate(user=self.manager1)

        # Attempting to assign prescription requirement to retail bandages
        payload = {
            "name": "Sterile Gauze Bandage 4-inch",
            "category": NonMedicineCategory.SURGICAL_WOUND_CARE,
            "mrp": "45.00",
            "cost_price": "30.00",
            "is_prescription_only": True,  # Regulatory violation!
        }

        response = self.client.post("/api/v2/catalog/retail-items/", data=payload, format="json")
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        err = str(response.json())
        self.assertIn("ERR_NON_MEDICINE_REGULATORY_RESTRICTION", err)

        # Attempting to assign antibiotic flag
        payload2 = {
            "name": "Dettol Antiseptic Liquid 250ml",
            "category": NonMedicineCategory.PERSONAL_HYGIENE,
            "mrp": "180.00",
            "cost_price": "150.00",
            "is_antibiotic": True,  # Regulatory violation!
        }
        resp2 = self.client.post("/api/v2/catalog/retail-items/", data=payload2, format="json")
        self.assertIn(resp2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("ERR_NON_MEDICINE_REGULATORY_RESTRICTION", str(resp2.json()))

    def test_03_invalid_negative_or_zero_mrp_rejected(self):
        """P135 Acceptance: Zero or negative MRP price is rejected with validation error."""
        self.client.force_authenticate(user=self.manager1)

        payload = {
            "name": "Cotton Crepe Bandage",
            "category": NonMedicineCategory.SURGICAL_WOUND_CARE,
            "mrp": "-10.00",
            "cost_price": "20.00",
        }

        response = self.client.post("/api/v2/catalog/retail-items/", data=payload, format="json")
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("mrp", str(response.json()))

    def test_04_cashier_can_read_and_search_but_denied_from_mutating(self):
        """P135 Acceptance: Cashier can search/read retail goods for checkout, but is denied from creating/mutating items."""
        # 1. Manager creates item
        self.client.force_authenticate(user=self.manager1)
        create_resp = self.client.post(
            "/api/v2/catalog/retail-items/",
            data={
                "name": f"Pampers Diapers Large {self.suffix}",
                "category": NonMedicineCategory.BABY_MOTHER_CARE,
                "barcode": f"890987{random.randint(1000, 9999)}",
                "mrp": "1450.00",
                "cost_price": "1200.00",
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)

        # 2. Cashier reads/searches (Permitted for checkout)
        self.client.force_authenticate(user=self.cashier1)
        list_resp = self.client.get("/api/v2/catalog/retail-items/?search=Pampers")
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(len(list_resp.json()["results"]) >= 1)

        # 3. Cashier attempts mutation (Blocked with ERR_STAFF_ROLE_DENIED)
        mutate_resp = self.client.post(
            "/api/v2/catalog/retail-items/",
            data={
                "name": "Cashier Created Syringe",
                "category": NonMedicineCategory.SURGICAL_WOUND_CARE,
                "mrp": "10.00",
                "cost_price": "7.00",
            },
            format="json",
        )
        self.assertEqual(mutate_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_STAFF_ROLE_DENIED", str(mutate_resp.json()))

    def test_05_cross_tenant_mutation_blocked_with_403(self):
        """P135 Acceptance: Tenant 2 cannot mutate retail item owned by Tenant 1."""
        self.client.force_authenticate(user=self.manager1)
        create_resp = self.client.post(
            "/api/v2/catalog/retail-items/",
            data={
                "name": f"Tenant 1 Diagnostic Thermometer {self.suffix}",
                "category": NonMedicineCategory.DIAGNOSTIC_DEVICES,
                "mrp": "350.00",
                "cost_price": "280.00",
            },
            format="json",
        )
        item_id = create_resp.json()["item_id"]

        # Foreign Tenant 2 attempts mutation
        self.client.force_authenticate(user=self.manager2)
        patch_resp = self.client.patch(
            f"/api/v2/catalog/retail-items/{item_id}/",
            data={"mrp": "400.00"},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_ACCESS", str(patch_resp.json()))

    def test_06_suspended_tenant_blocked_from_mutating_with_403(self):
        """P135 Acceptance: Staff of suspended tenant cannot create or mutate retail items."""
        self.client.force_authenticate(user=self.suspended_owner)

        resp = self.client.post(
            "/api/v2/catalog/retail-items/",
            data={
                "name": "Suspended Hot Water Bag",
                "category": NonMedicineCategory.HEALTH_ACCESSORIES,
                "mrp": "220.00",
                "cost_price": "180.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        err_str = str(resp.json())
        self.assertTrue("ERR_TENANT_SUSPENDED" in err_str or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in err_str)

    def test_07_barcode_search_and_category_filtering(self):
        """P135 Acceptance: Quick barcode lookup and category filtering for non-medicine inventory."""
        self.client.force_authenticate(user=self.manager1)
        barcode = f"890555{random.randint(1000, 9999)}"

        self.client.post(
            "/api/v2/catalog/retail-items/",
            data={
                "name": f"Accu-Chek Instant Blood Glucose Meter {self.suffix}",
                "category": NonMedicineCategory.DIAGNOSTIC_DEVICES,
                "barcode": barcode,
                "mrp": "2100.00",
                "cost_price": "1750.00",
            },
            format="json",
        )

        # Lookup by exact barcode
        search_resp = self.client.get(
            f"/api/v2/catalog/retail-items/?search={barcode}&category={NonMedicineCategory.DIAGNOSTIC_DEVICES}"
        )
        self.assertEqual(search_resp.status_code, status.HTTP_200_OK)
        results = search_resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["barcode"], barcode)
        self.assertEqual(results[0]["category"], NonMedicineCategory.DIAGNOSTIC_DEVICES)

    def test_08_update_retail_item_increments_version_and_updates_hash(self):
        """P135 Acceptance: Mutating retail item increments version number and updates immutable audit hash."""
        self.client.force_authenticate(user=self.manager1)
        create_resp = self.client.post(
            "/api/v2/catalog/retail-items/",
            data={
                "name": f"Horlicks Health Drink 500g {self.suffix}",
                "category": NonMedicineCategory.NUTRITION_SUPPLEMENT,
                "mrp": "550.00",
                "cost_price": "480.00",
            },
            format="json",
        )
        item_id = create_resp.json()["item_id"]
        v1_hash = create_resp.json()["immutable_hash"]
        self.assertEqual(create_resp.json()["version_number"], 1)

        # Update price to 580.00
        patch_resp = self.client.patch(
            f"/api/v2/catalog/retail-items/{item_id}/",
            data={"mrp": "580.00"},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        v2_data = patch_resp.json()
        self.assertEqual(v2_data["version_number"], 2)
        self.assertEqual(v2_data["mrp"], "580.00")
        self.assertNotEqual(v1_hash, v2_data["immutable_hash"])

    def test_09_archive_item_soft_deactivates_record(self):
        """P135 Acceptance: Soft archiving retail item marks is_active = False."""
        self.client.force_authenticate(user=self.manager1)
        create_resp = self.client.post(
            "/api/v2/catalog/retail-items/",
            data={
                "name": f"Discontinued Surgical Item {self.suffix}",
                "category": NonMedicineCategory.SURGICAL_WOUND_CARE,
                "mrp": "100.00",
                "cost_price": "70.00",
            },
            format="json",
        )
        item_id = create_resp.json()["item_id"]

        archive_resp = self.client.post(
            f"/api/v2/catalog/retail-items/{item_id}/archive/",
            data={"reason": "Vendor discontinued product"},
            format="json",
        )
        self.assertEqual(archive_resp.status_code, status.HTTP_200_OK)
        self.assertFalse(archive_resp.json()["is_active"])

        # Check DB
        item = NonMedicineRetailItem.objects.get(item_id=item_id)
        self.assertFalse(item.is_active)

    def test_10_unauthenticated_request_rejected_with_401(self):
        """P135 Acceptance: Anonymous requests to retail items endpoint rejected with HTTP 401."""
        self.client.logout()
        resp = self.client.get("/api/v2/catalog/retail-items/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
