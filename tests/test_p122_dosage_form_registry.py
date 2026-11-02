"""
Integration tests for P122: Dosage form metadata registry (ADR-0122).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path

from django.test import TestCase
from jsonschema import validate
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.dosage_form_service import DosageFormRegistryService
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "dosage_form_registry.schema.json"


class DosageFormRegistryIntegrationTests(TestCase):
    """End-to-end integration tests for P122 Dosage Form Metadata Registry."""

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
            tenant_id=f"tn-p122-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-p122-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p122-{self.suffix}",
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

        # 3. Seed canonical dosage forms
        DosageFormRegistryService.ensure_canonical_forms_seeded()

    def test_01_retrieve_dosage_forms_and_validate_json_schema(self):
        """Tenant cashier retrieves registered dosage forms and validates with JSON Schema."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/dosage-forms/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        data = resp.json()

        self.assertGreaterEqual(data["count"], 8)
        self.assertEqual(len(data["results"]), data["count"])

        # Validate with JSON schema
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

    def test_02_superadmin_creates_custom_dosage_form(self):
        """Platform Superadmin registers a new specialized dosage form."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "form_code": f"PATCH_{self.suffix.upper()}",
            "name_en": "Transdermal Patch",
            "name_bn": "ট্রান্সডার্মাল প্যাচ",
            "category": "TOPICAL",
            "default_route": "Topical",
            "is_strip_applicable": False,
            "is_volume_applicable": False,
            "is_cold_chain_applicable": False,
            "display_order": 90,
            "field_groups": [
                {
                    "group_id": "patch_specs",
                    "name_en": "Patch Matrix Dimensions",
                    "name_bn": "প্যাচ পরিমাপ",
                    "fields": ["surface_area_sqcm", "release_rate_per_hour"],
                    "required": True,
                }
            ],
        }

        resp = self.client.post("/api/v2/dosage-forms/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        data = resp.json()
        self.assertEqual(data["form_code"], f"PATCH_{self.suffix.upper()}")
        self.assertEqual(data["category"], "TOPICAL")
        self.assertEqual(data["display_order"], 90)

    def test_03_duplicate_form_code_rejected(self):
        """Attempt to register an already existing dosage form code is rejected (400 Bad Request)."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "form_code": "TABLET",  # Already canonical
            "name_en": "Tablet Duplicate",
            "name_bn": "ট্যাবলেট নকল",
        }

        resp = self.client.post("/api/v2/dosage-forms/", payload, format="json")
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertEqual(resp.json().get("error_code"), "ERR_DUPLICATE_DOSAGE_FORM")

    def test_04_missing_required_fields_rejected(self):
        """POST without required fields returns 400 Bad Request."""
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.post("/api/v2/dosage-forms/", {"display_order": 10}, format="json")
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertEqual(resp.json().get("error_code"), "ERR_INVALID_DOSAGE_FORM_INPUT")

    def test_05_tenant_user_denied_dosage_form_creation(self):
        """Tenant pharmacy owner cannot mutate dosage form registry (403 Forbidden)."""
        self.client.force_authenticate(user=self.owner)
        payload = {
            "form_code": "UNAUTHORIZED_FORM",
            "name_en": "Unauthorized",
            "name_bn": "অননুমোদিত",
        }

        resp = self.client.post("/api/v2/dosage-forms/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "PLATFORM_CATALOG_OWNERSHIP_PROTECTED")

    def test_06_filter_by_category_solid(self):
        """Filter dosage forms by category SOLID returns TABLET and CAPSULE."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/dosage-forms/?category=SOLID")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]
        self.assertTrue(all(f["category"] == "SOLID" for f in results))
        codes = [f["form_code"] for f in results]
        self.assertIn("TABLET", codes)
        self.assertIn("CAPSULE", codes)

    def test_07_filter_by_is_active(self):
        """Filter by is_active=true returns only active forms."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/dosage-forms/?is_active=true")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]
        self.assertTrue(all(f["is_active"] for f in results))

    def test_08_display_order_sequence_preserved(self):
        """Results strictly follow display_order ascending."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/dosage-forms/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]
        orders = [f["display_order"] for f in results]
        self.assertEqual(orders, sorted(orders))
