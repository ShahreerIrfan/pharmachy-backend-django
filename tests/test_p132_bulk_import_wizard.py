"""
backend/tests/test_p132_bulk_import_wizard.py
Architecture Decision: ADR-0132 (P132).
Integration Tests for Bulk Medicine Catalog Import Wizard:
CSV Parsing, Dynamic Field Mapping, Duplicate Policies, Atomic Commit, and Single-Click Rollback.
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
    ImportSessionStatus,
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
    / "bulk_import_mapping_wizard.schema.json"
)


class BulkImportWizardIntegrationTests(TestCase):
    """End-to-end integration tests for P132 Bulk Import Mapping Wizard."""

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

        # 2. Pharmacy Tenant 1 (Banani Pharma) & Staff
        self.tenant1 = Tenant.objects.create(
            tenant_id=f"tn-p132-1-{self.suffix}",
            name=f"Banani Pharma {self.suffix}",
            subdomain=f"banani-p132-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p132-1-{self.suffix}",
            tenant=self.tenant1,
            name="Main Banani Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner1 = PharmacyUser.objects.create_user(
            email=f"owner1_{self.suffix}@banani.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Banani Owner",
            role=UserRole.PHARMACY_OWNER,
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

        # 3. Pharmacy Tenant 2 (Mohakhali Pharma)
        self.tenant2 = Tenant.objects.create(
            tenant_id=f"tn-p132-2-{self.suffix}",
            name=f"Mohakhali Pharma {self.suffix}",
            subdomain=f"mohakhali-p132-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.owner2 = PharmacyUser.objects.create_user(
            email=f"owner2_{self.suffix}@mohakhali.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Mohakhali Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant2.tenant_id,
        )

        # 4. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-p132-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p132-{self.suffix}",
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

    def test_01_upload_csv_generates_preview_and_auto_mapping_validating_json_schema(self):
        """P132 Acceptance: Uploading valid CSV generates preview, auto-maps columns, and conforms to JSON schema."""
        self.client.force_authenticate(user=self.owner1)
        csv_data = (
            "Trade Name,Molecule,Form,Strength,MRP,Manufacturer,Barcode\n"
            f"Napa Extend {self.suffix},Paracetamol,Tablet,665mg,2.50,Beximco Pharmaceuticals,890111{random.randint(1000, 9999)}\n"
            f"Seclo 40 {self.suffix},Omeprazole,Capsule,40mg,10.00,Square Pharmaceuticals,890222{random.randint(1000, 9999)}\n"
        )

        payload = {
            "file_name": "beximco_catalog.csv",
            "csv_content": csv_data,
            "duplicate_policy": "SKIP",
        }

        response = self.client.post("/api/v2/catalog/bulk-import/upload/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()

        # Validate against JSON schema
        sub_schema = dict(self.schema_doc["definitions"]["ImportSessionItem"])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=data, schema=sub_schema)

        self.assertEqual(data["file_name"], "beximco_catalog.csv")
        self.assertEqual(data["total_rows"], 2)
        self.assertEqual(len(data["preview_rows"]), 2)
        self.assertIn("Trade Name", data["field_mapping"])
        self.assertEqual(data["field_mapping"]["Trade Name"], "brand_name")
        self.assertEqual(data["field_mapping"]["Molecule"], "generic_name")
        self.assertEqual(data["field_mapping"]["MRP"], "mrp_unit")

    def test_02_custom_column_mapping_and_preflight_validation(self):
        """P132 Acceptance: User submits custom column mappings and validates pre-flight dry run."""
        self.client.force_authenticate(user=self.owner1)
        # CSV with unfamiliar custom headers
        csv_data = (
            "ITEM_NAME,DRUG_MOLECULE,FORM_TYPE,DOSE_POWER,SALE_PRICE,MAKER_NAME\n"
            f"Almex {self.suffix},Albendazole,Tablet,400mg,5.00,Square Pharma\n"
            f"Monas 10 {self.suffix},Montelukast,Tablet,10mg,16.00,Acme Laboratories\n"
        )
        res_upload = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "custom_headers.csv", "csv_content": csv_data},
            format="json",
        )
        self.assertEqual(res_upload.status_code, status.HTTP_201_CREATED)
        session_id = res_upload.json()["session_id"]

        # Map custom headers to canonical fields
        mapping_payload = {
            "field_mapping": {
                "ITEM_NAME": "brand_name",
                "DRUG_MOLECULE": "generic_name",
                "FORM_TYPE": "dosage_form",
                "DOSE_POWER": "strength",
                "SALE_PRICE": "mrp_unit",
                "MAKER_NAME": "manufacturer",
            },
            "duplicate_policy": "SKIP",
        }

        res_map = self.client.post(
            f"/api/v2/catalog/bulk-import/{session_id}/map-and-validate/",
            data=mapping_payload,
            format="json",
        )
        self.assertEqual(res_map.status_code, status.HTTP_200_OK)
        data = res_map.json()

        self.assertEqual(data["status"], ImportSessionStatus.VALIDATED)
        self.assertEqual(data["valid_rows"], 2)
        self.assertEqual(data["invalid_rows"], 0)
        self.assertEqual(len(data["validation_errors"]), 0)

    def test_03_atomic_commit_creates_products_and_updates_session_status(self):
        """P132 Acceptance: Committing validated session creates products transactionally in catalog."""
        self.client.force_authenticate(user=self.owner1)
        b_code = f"890999{random.randint(10000, 99999)}"
        csv_data = (
            "Brand,Generic,Dosage,Strength,MRP,Manufacturer,Barcode\n"
            f"Ciprocin 500 {self.suffix},Ciprofloxacin,Tablet,500mg,15.00,Square Pharmaceuticals,{b_code}\n"
        )
        res_upload = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "cipro_import.csv", "csv_content": csv_data},
            format="json",
        )
        session_id = res_upload.json()["session_id"]

        # Commit import
        res_commit = self.client.post(f"/api/v2/catalog/bulk-import/{session_id}/commit/", format="json")
        self.assertEqual(res_commit.status_code, status.HTTP_200_OK)
        data = res_commit.json()

        self.assertEqual(data["status"], ImportSessionStatus.COMMITTED)
        self.assertEqual(data["created_count"], 1)
        self.assertIsNotNone(data["committed_at"])

        # Verify product created in MedicineProduct
        product = MedicineProduct.objects.filter(barcode=b_code).first()
        self.assertIsNotNone(product)
        self.assertEqual(product.brand_name, f"Ciprocin 500 {self.suffix}")
        self.assertEqual(product.mrp_unit, Decimal("15.00"))

    def test_04_rollback_reverses_created_products_cleanly(self):
        """P132 Acceptance: Rollback cleanly deletes created products and updates session state."""
        self.client.force_authenticate(user=self.owner1)
        b_code = f"890888{random.randint(10000, 99999)}"
        csv_data = (
            "Brand,Generic,Dosage,Strength,MRP,Manufacturer,Barcode\n"
            f"Defective Batch {self.suffix},Chemical X,Tablet,100mg,50.00,Experimental Lab,{b_code}\n"
        )
        res_upload = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "defective_batch.csv", "csv_content": csv_data},
            format="json",
        )
        session_id = res_upload.json()["session_id"]

        # Commit first
        self.client.post(f"/api/v2/catalog/bulk-import/{session_id}/commit/", format="json")
        self.assertTrue(MedicineProduct.objects.filter(barcode=b_code).exists())

        # Now trigger rollback
        res_rollback = self.client.post(f"/api/v2/catalog/bulk-import/{session_id}/rollback/", format="json")
        self.assertEqual(res_rollback.status_code, status.HTTP_200_OK)
        data = res_rollback.json()

        self.assertEqual(data["status"], ImportSessionStatus.ROLLED_BACK)
        self.assertGreaterEqual(data["deleted_products_count"], 1)

        # Confirm product is deleted from database
        self.assertFalse(MedicineProduct.objects.filter(barcode=b_code).exists())

        # Re-attempting rollback fails
        res_repeat = self.client.post(f"/api/v2/catalog/bulk-import/{session_id}/rollback/", format="json")
        self.assertIn(res_repeat.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_05_duplicate_policy_skip_ignores_existing_records(self):
        """P132 Acceptance: SKIP policy ignores rows matching existing catalog entries without failing batch."""
        existing_barcode = f"890777{random.randint(10000, 99999)}"
        MedicineProduct.objects.create(
            product_id=f"prod-existing-{self.suffix}",
            brand_name=f"Existing Drug {self.suffix}",
            generic_name="Existing Molecule",
            dosage_form="Tablet",
            strength="10mg",
            mrp_unit=Decimal("5.00"),
            cost_unit=Decimal("4.00"),
            barcode=existing_barcode,
        )

        novel_barcode = f"890666{random.randint(10000, 99999)}"
        csv_data = (
            "Brand,Generic,Dosage,Strength,MRP,Manufacturer,Barcode\n"
            f"Duplicate Row {self.suffix},Existing Molecule,Tablet,10mg,5.00,Any Pharma,{existing_barcode}\n"
            f"Novel Drug {self.suffix},Novel Molecule,Capsule,20mg,12.00,Any Pharma,{novel_barcode}\n"
        )

        self.client.force_authenticate(user=self.owner1)
        res_upload = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "mixed_duplicates.csv", "csv_content": csv_data, "duplicate_policy": "SKIP"},
            format="json",
        )
        self.assertEqual(res_upload.status_code, status.HTTP_201_CREATED)
        session_id = res_upload.json()["session_id"]
        self.assertEqual(res_upload.json()["duplicate_rows"], 1)

        # Commit: only novel product should be created
        res_commit = self.client.post(f"/api/v2/catalog/bulk-import/{session_id}/commit/", format="json")
        self.assertEqual(res_commit.status_code, status.HTTP_200_OK)
        data = res_commit.json()
        self.assertEqual(data["created_count"], 1)
        self.assertTrue(MedicineProduct.objects.filter(barcode=novel_barcode).exists())

    def test_06_duplicate_policy_fail_on_duplicate_blocks_commit(self):
        """P132 Acceptance: FAIL_ON_DUPLICATE policy halts import if duplicate record detected."""
        dup_barcode = f"890555{random.randint(10000, 99999)}"
        MedicineProduct.objects.create(
            product_id=f"prod-dup-{self.suffix}",
            brand_name=f"Duplicate Baseline {self.suffix}",
            generic_name="Baseline Molecule",
            dosage_form="Tablet",
            strength="5mg",
            mrp_unit=Decimal("2.00"),
            cost_unit=Decimal("1.60"),
            barcode=dup_barcode,
        )

        csv_data = (
            "Brand,Generic,Dosage,Strength,MRP,Manufacturer,Barcode\n"
            f"Another Drug {self.suffix},Baseline Molecule,Tablet,5mg,2.00,Pharma Co,{dup_barcode}\n"
        )

        self.client.force_authenticate(user=self.owner1)
        res_upload = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "fail_dup.csv", "csv_content": csv_data, "duplicate_policy": "FAIL_ON_DUPLICATE"},
            format="json",
        )
        session_id = res_upload.json()["session_id"]
        data = res_upload.json()
        self.assertGreaterEqual(len(data["validation_errors"]), 1)
        self.assertEqual(data["valid_rows"], 0)

        # Commit attempt rejected
        res_commit = self.client.post(f"/api/v2/catalog/bulk-import/{session_id}/commit/", format="json")
        self.assertIn(res_commit.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_07_invalid_row_with_negative_price_or_missing_brand_rejected(self):
        """P132 Acceptance: Pre-flight validation catches negative prices and missing required fields."""
        csv_data = (
            "Brand,Generic,Dosage,Strength,MRP,Manufacturer\n"
            ",Missing Brand Generic,Tablet,500mg,10.00,Square\n"
            "Valid Drug,Valid Molecule,Tablet,20mg,-5.00,Beximco\n"
        )
        self.client.force_authenticate(user=self.owner1)
        res_upload = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "corrupt_data.csv", "csv_content": csv_data},
            format="json",
        )
        self.assertEqual(res_upload.status_code, status.HTTP_201_CREATED)
        data = res_upload.json()

        self.assertEqual(data["invalid_rows"], 2)
        self.assertEqual(data["valid_rows"], 0)
        errors = data["validation_errors"]
        error_fields = [e["field"] for e in errors]
        self.assertIn("brand_name", error_fields)
        self.assertIn("mrp_unit", error_fields)

    def test_08_cross_tenant_isolation_enforced(self):
        """P132 Acceptance: Tenant 2 owner denied mutating Tenant 1 import session."""
        self.client.force_authenticate(user=self.owner1)
        csv_data = "Brand,Generic,Dosage,Strength,MRP,Manufacturer\nDrug A,Generic A,Tablet,10mg,1.00,Pharma\n"
        res_upload = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "tenant1.csv", "csv_content": csv_data},
            format="json",
        )
        session_id = res_upload.json()["session_id"]

        # Tenant 2 owner attempts to commit Tenant 1's session
        self.client.force_authenticate(user=self.owner2)
        res_cross = self.client.post(f"/api/v2/catalog/bulk-import/{session_id}/commit/", format="json")
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)
        error_repr = json.dumps(res_cross.json())
        self.assertIn("ERR_CROSS_TENANT_ACCESS", error_repr)

    def test_09_cashier_role_denied_import_access(self):
        """P132 Acceptance: Cashier denied initiating bulk import."""
        self.client.force_authenticate(user=self.cashier1)
        res = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "cashier_attempt.csv", "csv_content": "Col1,Col2\nVal1,Val2\n"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        error_repr = json.dumps(res.json())
        self.assertIn("ERR_STAFF_ROLE_DENIED", error_repr)

    def test_10_suspended_tenant_blocked_from_import(self):
        """P132 Acceptance: Suspended pharmacy owner denied bulk import with ERR_TENANT_SUSPENDED."""
        self.client.force_authenticate(user=self.suspended_owner)
        res = self.client.post(
            "/api/v2/catalog/bulk-import/upload/",
            data={"file_name": "susp_attempt.csv", "csv_content": "Col1,Col2\nVal1,Val2\n"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        error_repr = json.dumps(res.json())
        self.assertTrue(
            "ERR_TENANT_SUSPENDED" in error_repr
            or "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED" in error_repr
        )
