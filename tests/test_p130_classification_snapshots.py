"""
backend/tests/test_p130_classification_snapshots.py
Architecture Decision: ADR-0130 (P130).
Integration Tests for Controlled Classification Snapshots, Recalls,
Point-in-Time Regulatory Audits, and Master Medicine Synchronization.
"""

import json
import random
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import jsonschema
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineClassificationSnapshot,
    MedicineCurationStatus,
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
    / "controlled_classification_snapshots.schema.json"
)


class ControlledClassificationSnapshotIntegrationTests(TestCase):
    """End-to-end integration tests for P130 Controlled Classification Snapshots."""

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

        # 2. Pharmacy Tenant 1 (Uttara Pharmacy) & Staff Users
        self.tenant1 = Tenant.objects.create(
            tenant_id=f"tn-p130-1-{self.suffix}",
            name=f"Uttara Pharmacy {self.suffix}",
            subdomain=f"uttara-p130-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p130-1-{self.suffix}",
            tenant=self.tenant1,
            name="Main Uttara Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner1 = PharmacyUser.objects.create_user(
            email=f"owner1_{self.suffix}@uttara.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Uttara Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )
        self.cashier1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@uttara.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Uttara Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant1.tenant_id,
            branch_id=self.branch1.branch_id,
        )

        # 3. Master Medicines
        self.med_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name="Napa 500mg",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="500mg",
            strength_numeric=Decimal("500"),
            strength_unit="mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.90"),
            barcode=f"8901234{random.randint(100000, 999999)}",
            is_prescription_only=False,
            is_narcotic=False,
            is_antibiotic=False,
        )

        self.med_cipro = MedicineProduct.objects.create(
            product_id=f"prod-cipro-{self.suffix}",
            brand_name="Ciprocin 500mg",
            generic_name="Ciprofloxacin",
            dosage_form="Tablet",
            strength="500mg",
            strength_numeric=Decimal("500"),
            strength_unit="mg",
            manufacturer="Square Pharmaceuticals PLC",
            mrp_unit=Decimal("15.00"),
            cost_unit=Decimal("12.00"),
            barcode=f"8901235{random.randint(100000, 999999)}",
            is_prescription_only=True,
            is_narcotic=False,
            is_antibiotic=False,
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def test_01_superadmin_creates_classification_snapshot_and_validates_json_schema(self):
        """P130 Acceptance: Platform Superadmin creates classification snapshot conforming to JSON schema."""
        self.client.force_authenticate(user=self.superadmin)
        now_iso = timezone.now().isoformat()
        payload = {
            "product_id": self.med_napa.product_id,
            "classification_type": "OTC",
            "regulatory_source": "DGDA Over-The-Counter Drug Schedule 2024 Gazette",
            "effective_from": now_iso,
            "review_notes": "Statutory scheduled review confirming OTC general sales classification.",
        }

        response = self.client.post("/api/v2/catalog/classifications/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()

        # Validate against JSON Schema ClassificationSnapshotItem
        sub_schema = dict(self.schema_doc["definitions"]["ClassificationSnapshotItem"])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=data, schema=sub_schema)

        self.assertEqual(data["product_id"], self.med_napa.product_id)
        self.assertEqual(data["version_number"], 1)
        self.assertEqual(data["classification_type"], "OTC")
        self.assertTrue(data["is_current"])
        self.assertFalse(data["is_dispensing_blocked"])
        self.assertIsNone(data["effective_to"])
        self.assertEqual(len(data["immutable_hash"]), 64)
        self.assertEqual(data["reviewer_email"], self.superadmin.email)

    def test_02_superseding_snapshot_sets_effective_to_and_increments_version(self):
        """P130 Acceptance: Successive snapshot increments version, updates effective_to and syncs master flags."""
        self.client.force_authenticate(user=self.superadmin)
        t1 = timezone.now() - timedelta(days=30)
        t2 = timezone.now()

        # Snapshot 1: OTC
        p1 = {
            "product_id": self.med_napa.product_id,
            "classification_type": "OTC",
            "regulatory_source": "Initial 2024 Schedule",
            "effective_from": t1.isoformat(),
            "review_notes": "Initial OTC status.",
        }
        r1 = self.client.post("/api/v2/catalog/classifications/", data=p1, format="json")
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        snap1_id = r1.json()["snapshot_id"]

        # Snapshot 2: Reclassified to Prescription-Only (POM)
        p2 = {
            "product_id": self.med_napa.product_id,
            "classification_type": "PRESCRIPTION_ONLY",
            "regulatory_source": "DGDA Directive 2026/Reclassification",
            "effective_from": t2.isoformat(),
            "review_notes": "Reclassified to prescription-only based on updated adverse event study.",
        }
        r2 = self.client.post("/api/v2/catalog/classifications/", data=p2, format="json")
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        data2 = r2.json()

        self.assertEqual(data2["version_number"], 2)
        self.assertEqual(data2["classification_type"], "PRESCRIPTION_ONLY")
        self.assertTrue(data2["is_current"])

        # Check snapshot 1 in DB
        snap1 = MedicineClassificationSnapshot.objects.get(snapshot_id=snap1_id)
        self.assertFalse(snap1.is_current)
        self.assertIsNotNone(snap1.effective_to)

        # Check master medicine flag updated
        self.med_napa.refresh_from_db()
        self.assertTrue(self.med_napa.is_prescription_only)

    def test_03_recalled_snapshot_updates_master_status_and_blocks_dispensing(self):
        """P130 Acceptance: Recall snapshot updates master status to RECALLED and blocks POS dispensing."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.med_napa.product_id,
            "classification_type": "RECALLED",
            "regulatory_source": "DGDA Urgent Statutory Recall Notice 88/2026",
            "effective_from": timezone.now().isoformat(),
            "review_notes": "Safety recall due to detected contaminant lot report.",
        }

        response = self.client.post("/api/v2/catalog/classifications/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()

        self.assertTrue(data["is_dispensing_blocked"])
        self.assertEqual(data["classification_type"], "RECALLED")

        # Verify master medicine curation_status
        self.med_napa.refresh_from_db()
        self.assertEqual(self.med_napa.curation_status, MedicineCurationStatus.RECALLED)

        # Query check-recall endpoint as cashier
        self.client.force_authenticate(user=self.cashier1)
        check_res = self.client.get(f"/api/v2/catalog/classifications/check-recall/?product_id={self.med_napa.product_id}")
        self.assertEqual(check_res.status_code, status.HTTP_200_OK)
        check_data = check_res.json()
        self.assertTrue(check_data["is_dispensing_blocked"])
        self.assertIn("ERR_PRODUCT_RECALLED_SALE_BLOCKED", check_data["block_reason"])

    def test_04_point_in_time_historical_resolution(self):
        """P130 Acceptance: Temporal point-in-time query accurately retrieves historical classification."""
        self.client.force_authenticate(user=self.superadmin)
        t_jan = timezone.make_aware(datetime(2026, 1, 1, 0, 0, 0))
        t_jun = timezone.make_aware(datetime(2026, 6, 1, 0, 0, 0))

        # Snapshot 1: OTC in Jan 2026
        self.client.post(
            "/api/v2/catalog/classifications/",
            data={
                "product_id": self.med_napa.product_id,
                "classification_type": "OTC",
                "regulatory_source": "Schedule 2026-A",
                "effective_from": t_jan.isoformat(),
                "review_notes": "OTC status from Jan 2026.",
            },
            format="json",
        )

        # Snapshot 2: POM from Jun 2026
        self.client.post(
            "/api/v2/catalog/classifications/",
            data={
                "product_id": self.med_napa.product_id,
                "classification_type": "PRESCRIPTION_ONLY",
                "regulatory_source": "Schedule 2026-B",
                "effective_from": t_jun.isoformat(),
                "review_notes": "POM status from Jun 2026.",
            },
            format="json",
        )

        # Query point-in-time: March 15, 2026 (should resolve to OTC version 1)
        t_march = timezone.make_aware(datetime(2026, 3, 15, 12, 0, 0))
        res_march = self.client.get(
            f"/api/v2/catalog/classifications/point-in-time/?product_id={self.med_napa.product_id}&target_date={t_march.isoformat()}"
        )
        self.assertEqual(res_march.status_code, status.HTTP_200_OK)
        march_snap = res_march.json()["snapshot"]
        self.assertIsNotNone(march_snap)
        self.assertEqual(march_snap["version_number"], 1)
        self.assertEqual(march_snap["classification_type"], "OTC")

        # Query point-in-time: August 1, 2026 (should resolve to POM version 2)
        t_august = timezone.make_aware(datetime(2026, 8, 1, 12, 0, 0))
        res_aug = self.client.get(
            f"/api/v2/catalog/classifications/point-in-time/?product_id={self.med_napa.product_id}&target_date={t_august.isoformat()}"
        )
        self.assertEqual(res_aug.status_code, status.HTTP_200_OK)
        aug_snap = res_aug.json()["snapshot"]
        self.assertIsNotNone(aug_snap)
        self.assertEqual(aug_snap["version_number"], 2)
        self.assertEqual(aug_snap["classification_type"], "PRESCRIPTION_ONLY")

    def test_05_antimicrobial_aware_snapshot_syncs_antibiotic_flag(self):
        """P130 Acceptance: WHO AWaRe classification sets is_antibiotic and is_prescription_only on master."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": self.med_cipro.product_id,
            "classification_type": "ANTIMICROBIAL_WATCH",
            "regulatory_source": "DGDA Antimicrobial Stewardship Guidelines 2026",
            "effective_from": timezone.now().isoformat(),
            "review_notes": "Designated Watch Group Fluoroquinolone.",
        }

        response = self.client.post("/api/v2/catalog/classifications/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.med_cipro.refresh_from_db()
        self.assertTrue(self.med_cipro.is_antibiotic)
        self.assertTrue(self.med_cipro.is_prescription_only)

    def test_06_tenant_owner_cannot_mutate_snapshots(self):
        """P130 Acceptance: Pharmacy Owner denied snapshot creation (Platform Superadmins only)."""
        self.client.force_authenticate(user=self.owner1)
        payload = {
            "product_id": self.med_napa.product_id,
            "classification_type": "OTC",
            "regulatory_source": "Tenant Custom Attempt",
            "effective_from": timezone.now().isoformat(),
            "review_notes": "Attempting unauthorized tenant mutation.",
        }

        response = self.client.post("/api/v2/catalog/classifications/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        data = response.json()
        error_repr = json.dumps(data)
        self.assertIn("PLATFORM_CATALOG_OWNERSHIP_PROTECTED", error_repr)

    def test_07_cashier_role_denied_mutation(self):
        """P130 Acceptance: Cashier denied snapshot creation."""
        self.client.force_authenticate(user=self.cashier1)
        payload = {
            "product_id": self.med_napa.product_id,
            "classification_type": "OTC",
            "regulatory_source": "Cashier Attempt",
            "effective_from": timezone.now().isoformat(),
            "review_notes": "Cashier cannot create snapshots.",
        }

        response = self.client.post("/api/v2/catalog/classifications/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_08_invalid_input_rejected_with_400_or_422(self):
        """P130 Acceptance: Invalid classification type or short notes rejected."""
        self.client.force_authenticate(user=self.superadmin)
        # Invalid classification type
        p1 = {
            "product_id": self.med_napa.product_id,
            "classification_type": "NON_EXISTENT_CLASS",
            "regulatory_source": "DGDA Source",
            "effective_from": timezone.now().isoformat(),
            "review_notes": "Valid review notes.",
        }
        r1 = self.client.post("/api/v2/catalog/classifications/", data=p1, format="json")
        self.assertIn(r1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Short review notes (< 5 chars)
        p2 = {
            "product_id": self.med_napa.product_id,
            "classification_type": "OTC",
            "regulatory_source": "DGDA Source",
            "effective_from": timezone.now().isoformat(),
            "review_notes": "abc",
        }
        r2 = self.client.post("/api/v2/catalog/classifications/", data=p2, format="json")
        self.assertIn(r2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_09_unauthenticated_request_rejected(self):
        """P130 Acceptance: Unauthenticated requests rejected with 401 Unauthorized."""
        response = self.client.get(f"/api/v2/catalog/classifications/current/?product_id={self.med_napa.product_id}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_10_immutable_hash_and_history_schema_contract(self):
        """P130 Acceptance: Snapshot history adheres to SnapshotHistoryResponse schema contract."""
        self.client.force_authenticate(user=self.superadmin)
        self.client.post(
            "/api/v2/catalog/classifications/",
            data={
                "product_id": self.med_napa.product_id,
                "classification_type": "OTC",
                "regulatory_source": "DGDA Gazette 2026/01",
                "effective_from": timezone.now().isoformat(),
                "review_notes": "Verified OTC status.",
            },
            format="json",
        )

        # Query history
        res = self.client.get(f"/api/v2/catalog/classifications/history/?product_id={self.med_napa.product_id}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        # Validate against SnapshotHistoryResponse JSON Schema
        sub_schema = dict(self.schema_doc["definitions"]["SnapshotHistoryResponse"])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=data, schema=sub_schema)

        self.assertEqual(data["product_id"], self.med_napa.product_id)
        self.assertGreaterEqual(data["total_snapshots"], 1)
        self.assertIsNotNone(data["current_snapshot"])
        self.assertEqual(len(data["snapshots"]), data["total_snapshots"])
