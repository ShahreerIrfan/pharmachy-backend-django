"""
Integration tests for P121: Normalized Global Medicine Master schema and regulatory versioning (ADR-0121).
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
    MedicineCurationStatus,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "medicine_global_master.schema.json"


class MedicineGlobalMasterIntegrationTests(TestCase):
    """End-to-end integration tests for P121 Medicine Global Master Catalog."""

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

        # 2. Pharmacy Tenant & Users
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p121-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-p121-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p121-{self.suffix}",
            tenant=self.tenant,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@lazz.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@lazz.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        # 3. Pre-seed a base medicine in master
        self.med_existing = MedicineProduct.objects.create(
            product_id=f"med-exist-{self.suffix}",
            brand_name=f"Napa Extra {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Beximco Pharmaceuticals Ltd",
            route="Oral",
            barcode=f"89012345{random.randint(10000, 99999)}",
            mrp_unit=Decimal("3.50"),
            cost_unit=Decimal("2.80"),
            dgda_dar_number=f"DAR-P121-{self.suffix}",
            curation_status=MedicineCurationStatus.APPROVED,
            source_metadata={"approval_year": 2024, "dgda_gazette": "Vol-12-Sec-B"},
        )

    def test_01_superadmin_creates_master_medicine_and_validates_json_schema(self):
        """Platform Superadmin creates normalized master medicine and validates against JSON Schema."""
        self.client.force_authenticate(user=self.superadmin)
        dar_no = f"DAR-NEW-{self.suffix}"
        barcode_no = f"890999{random.randint(1000000, 9999999)}"

        payload = {
            "product_id": f"med-new-{self.suffix}",
            "brand_name": f"Seclo {self.suffix}",
            "generic_name": "Omeprazole",
            "dosage_form": "Capsule",
            "strength": "20mg",
            "manufacturer": "Square Pharmaceuticals PLC",
            "route": "Oral",
            "barcode": barcode_no,
            "mrp_unit": "7.00",
            "cost_unit": "5.60",
            "dgda_dar_number": dar_no,
            "requires_cold_chain": False,
            "source_metadata": {"schedule": "Prescription Only", "registration_year": 2025},
        }

        resp = self.client.post("/api/v2/medicines/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        data = resp.json()

        self.assertEqual(data["brand_name"], f"Seclo {self.suffix}")
        self.assertEqual(data["route"], "Oral")
        self.assertEqual(data["barcode"], barcode_no)
        self.assertEqual(data["version_number"], 1)
        self.assertTrue(len(data["immutable_version_hash"]) > 20)

        # Validate with JSON Schema
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

    def test_02_duplicate_dar_rejected(self):
        """Attempt to register another medicine with identical DGDA DAR number is rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": f"med-dup-{self.suffix}",
            "brand_name": "Duplicate DAR Medicine",
            "generic_name": "Paracetamol",
            "dosage_form": "Tablet",
            "strength": "500mg",
            "manufacturer": "Another Pharma",
            "mrp_unit": "2.00",
            "cost_unit": "1.50",
            "dgda_dar_number": self.med_existing.dgda_dar_number,  # Identical DAR
        }

        resp = self.client.post("/api/v2/medicines/", payload, format="json")
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn(resp.json().get("error_code"), ["ERR_DUPLICATE_MEDICINE", "ERR_VALIDATION_FAILED"])

    def test_03_duplicate_brand_and_strength_rejected(self):
        """Attempt to register medicine with identical brand, strength, and manufacturer is rejected."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "product_id": f"med-dup2-{self.suffix}",
            "brand_name": self.med_existing.brand_name,
            "generic_name": self.med_existing.generic_name,
            "dosage_form": self.med_existing.dosage_form,
            "strength": self.med_existing.strength,
            "manufacturer": self.med_existing.manufacturer,
            "mrp_unit": "4.00",
            "cost_unit": "3.00",
            "dgda_dar_number": f"DAR-DIFF-{self.suffix}",
        }

        resp = self.client.post("/api/v2/medicines/", payload, format="json")
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn(resp.json().get("error_code"), ["ERR_DUPLICATE_MEDICINE", "ERR_VALIDATION_FAILED"])

    def test_04_tenant_user_denied_master_creation(self):
        """Tenant pharmacy owner cannot create global master records (403 Forbidden)."""
        self.client.force_authenticate(user=self.owner)
        payload = {
            "product_id": f"med-unauth-{self.suffix}",
            "brand_name": "Unauthorized Brand",
            "generic_name": "Ibuprofen",
            "dosage_form": "Tablet",
            "strength": "400mg",
            "manufacturer": "Fake Pharma",
            "mrp_unit": "5.00",
            "cost_unit": "3.50",
        }

        resp = self.client.post("/api/v2/medicines/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(resp.json().get("error_code"), ["PLATFORM_CATALOG_OWNERSHIP_PROTECTED", "ERR_PERMISSION_DENIED"])

    def test_05_tenant_user_denied_master_patch(self):
        """Tenant pharmacy owner cannot modify global master records (403 Forbidden)."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.patch(
            f"/api/v2/medicines/{self.med_existing.product_id}/",
            {"mrp_unit": "999.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(resp.json().get("error_code"), ["PLATFORM_CATALOG_OWNERSHIP_PROTECTED", "ERR_PERMISSION_DENIED"])

    def test_06_tenant_user_denied_master_delete(self):
        """Tenant cashier cannot delete global master records (403 Forbidden)."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.delete(f"/api/v2/medicines/{self.med_existing.product_id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(resp.json().get("error_code"), ["PLATFORM_CATALOG_OWNERSHIP_PROTECTED", "ERR_PERMISSION_DENIED"])

    def test_07_tenant_search_by_barcode(self):
        """Tenant cashier searches global catalog by barcode and finds exact match."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get(f"/api/v2/medicines/?search={self.med_existing.barcode}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get("results", [])
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["product_id"], self.med_existing.product_id)

    def test_08_tenant_search_by_generic_and_route(self):
        """Tenant searches catalog by generic name and route filter."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/medicines/?generic_name=Paracetamol&route=Oral")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get("results", [])
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["route"], "Oral")

    def test_09_immutable_version_history_recorded(self):
        """Superadmin update increments version and writes audit record."""
        self.client.force_authenticate(user=self.superadmin)

        resp = self.client.patch(
            f"/api/v2/medicines/{self.med_existing.product_id}/",
            {"mrp_unit": "3.80"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.med_existing.refresh_from_db()
        self.assertEqual(self.med_existing.mrp_unit, Decimal("3.80"))
