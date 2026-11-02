"""
Integration tests for P124: Server-driven medicine editor schema (ADR-0124).
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

from apps.core.medicine_editor_schema_service import MedicineEditorSchemaService
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
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
    / "medicine_editor_schema.schema.json"
)


class SchemaDrivenMedicineFormIntegrationTests(TestCase):
    """End-to-end integration tests for P124 Server-Driven Medicine Editor Schema & Validation."""

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
            tenant_id=f"tn-p124-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-p124-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p124-{self.suffix}",
            tenant=self.tenant,
            name="Main Dhanmondi Branch",
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
            tenant_id=f"tn-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p124-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"suspended_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 4. Seed canonical schema
        MedicineEditorSchemaService.ensure_canonical_schema_seeded()

    def test_01_retrieve_active_schema_and_validate_json_schema(self):
        """Cashier retrieves active editor schema and validates against JSON Schema contract."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/medicines/editor-schema/active/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Validate against JSON Schema
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

        self.assertEqual(data["schema_version"], "v1.0")
        self.assertTrue(data["is_active"])
        self.assertTrue(len(data["sections"]) >= 4)
        self.assertTrue(len(data["conditional_rules"]) >= 3)

    def test_02_validate_valid_tablet_payload(self):
        """Valid tablet form payload with strips and units passes schema validation."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Napa Extra",
            "generic_name": "Paracetamol + Caffeine",
            "dosage_form": "TABLET",
            "manufacturer": "Beximco Pharmaceuticals Ltd.",
            "strength": "500mg+65mg",
            "strips_per_box": 20,
            "units_per_strip": 10,
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/validate/",
            data={"payload": payload},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data["is_valid"])
        self.assertEqual(data["schema_version"], "v1.0")

    def test_03_validate_valid_syrup_payload(self):
        """Valid liquid syrup payload with volume_ml passes schema validation."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Tusca Syrup",
            "generic_name": "Dextromethorphan + Pseudoephedrine",
            "dosage_form": "SYRUP",
            "manufacturer": "Square Pharmaceuticals Ltd.",
            "strength": "10mg/5mL",
            "volume_ml": 100,
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/validate/",
            data={"payload": payload},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data["is_valid"])

    def test_04_incomplete_base_payload_rejected(self):
        """Missing generic_name fails with ERR_SCHEMA_VALIDATION_FAILED."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Napa Extra",
            "dosage_form": "TABLET",
            "manufacturer": "Beximco",
            "strength": "500mg",
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/validate/",
            data={"payload": payload},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_SCHEMA_VALIDATION_FAILED")
        self.assertIn("generic_name", data.get("missing_fields", []))

    def test_05_missing_conditional_tablet_strip_counts_rejected(self):
        """Solid tablet missing strips_per_box fails with ERR_CONDITIONAL_FIELD_REQUIRED."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Ace Plus",
            "generic_name": "Paracetamol + Caffeine",
            "dosage_form": "TABLET",
            "manufacturer": "Square Pharmaceuticals Ltd.",
            "strength": "500mg+65mg",
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/validate/",
            data={"payload": payload},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_CONDITIONAL_FIELD_REQUIRED")
        self.assertEqual(data.get("field"), "strips_per_box")

    def test_06_inverted_cold_chain_temperature_range_rejected(self):
        """Cold chain product with inverted temperatures fails with ERR_INVALID_TEMPERATURE_RANGE."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Insulin Glargine",
            "generic_name": "Insulin Glargine",
            "dosage_form": "INJECTION",
            "manufacturer": "Novo Nordisk",
            "strength": "100 IU/mL",
            "is_cold_chain": True,
            "min_temperature_celsius": 10.0,
            "max_temperature_celsius": 4.0,  # min > max!
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/validate/",
            data={"payload": payload},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_INVALID_TEMPERATURE_RANGE")

    def test_07_tenant_staff_cannot_create_or_mutate_schema(self):
        """Tenant owner attempting to POST new schema is blocked with PLATFORM_CATALOG_OWNERSHIP_PROTECTED."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/",
            data={"schema_version": "v2.0", "title_en": "Tenant Custom Schema"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        data = resp.json()
        self.assertEqual(data.get("error_code"), "PLATFORM_CATALOG_OWNERSHIP_PROTECTED")

    def test_08_superadmin_can_publish_new_schema_version(self):
        """Platform Superadmin successfully publishes a new schema version."""
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/",
            data={
                "schema_version": "v2.0",
                "title_en": "Enhanced 2026 Editor Schema",
                "title_bn": "উন্নত ২০২৬ ঔষধ বিবরণী ফর্ম",
                "description": "Updated schema with pediatric dosage indicators.",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertEqual(data["schema_version"], "v2.0")

    def test_09_suspended_tenant_blocked(self):
        """User from a suspended tenant is blocked from accessing editor schemas."""
        self.client.force_authenticate(user=self.suspended_user)
        # Manually attach suspended tenant to simulate tenant middleware
        self.client.handler._force_tenant = self.suspended_tenant
        resp = self.client.get(
            "/api/v2/medicines/editor-schema/active/",
            HTTP_X_TENANT_ID=self.suspended_tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_10_unauthenticated_request_rejected(self):
        """Unauthenticated request is rejected with HTTP 401."""
        resp = self.client.get("/api/v2/medicines/editor-schema/active/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
