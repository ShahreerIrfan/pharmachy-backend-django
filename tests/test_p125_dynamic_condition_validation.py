"""
Integration tests for P125: Dynamic condition validation & zero-trust hidden field guard (ADR-0125).
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
    / "dynamic_condition_validation.schema.json"
)


class DynamicConditionValidationIntegrationTests(TestCase):
    """End-to-end integration tests for P125 Dynamic Condition Evaluation and Zero-Trust Guard."""

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
            tenant_id=f"tn-p125-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sqr-p125-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p125-{self.suffix}",
            tenant=self.tenant,
            name="Main Branch",
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

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-susp-p125-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p125-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"suspended_{self.suffix}@susp.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        MedicineEditorSchemaService.ensure_canonical_schema_seeded()

    def test_01_evaluate_valid_tablet_and_validate_json_schema(self):
        """Valid tablet resolves active strips fields, hidden volume fields, and validates against JSON Schema."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Napa Extra",
            "generic_name": "Paracetamol + Caffeine",
            "dosage_form": "TABLET",
            "manufacturer": "Beximco Pharmaceuticals Ltd.",
            "strength": "500mg+65mg",
            "strips_per_box": 20,
            "units_per_strip": 10,
            "is_cold_chain": False,
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": payload, "strict_hidden_check": True},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()

        # Contract validation with JSON Schema
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

        self.assertTrue(data["is_valid"])
        self.assertIn("strips_per_box", data["active_fields"])
        self.assertIn("volume_ml", data["hidden_fields"])
        self.assertIn("min_temperature_celsius", data["hidden_fields"])
        self.assertIsNone(data["sanitized_payload"]["volume_ml"])

    def test_02_zero_trust_hidden_field_injection_strictly_rejected(self):
        """Zero-Trust Guard: Injection of strips_per_box on SYRUP is strictly rejected with ERR_HIDDEN_FIELD_VALUE_REJECTED."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Tusca Syrup",
            "generic_name": "Dextromethorphan",
            "dosage_form": "SYRUP",
            "manufacturer": "Square Pharmaceuticals Ltd.",
            "strength": "10mg/5mL",
            "volume_ml": 100,
            "strips_per_box": 10,  # Malicious / stale hidden field injection!
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": payload, "strict_hidden_check": True},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_HIDDEN_FIELD_VALUE_REJECTED")
        self.assertEqual(data.get("field"), "strips_per_box")
        self.assertIn("inactive/hidden", data.get("message_en", ""))
        self.assertIn("নিষিদ্ধ", data.get("message_bn", ""))

    def test_03_zero_trust_hidden_field_pruning_in_sanitize_mode(self):
        """Zero-Trust Guard: Non-strict sanitization prunes injected hidden fields without persisting corruption."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Tusca Syrup",
            "generic_name": "Dextromethorphan",
            "dosage_form": "SYRUP",
            "manufacturer": "Square Pharmaceuticals Ltd.",
            "strength": "10mg/5mL",
            "volume_ml": 100,
            "strips_per_box": 10,  # Hidden field
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": payload, "strict_hidden_check": False},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertTrue(data["is_valid"])
        self.assertIn("strips_per_box", data["pruned_hidden_fields"])
        self.assertIsNone(data["sanitized_payload"]["strips_per_box"])

    def test_04_ambient_medicine_rejects_hidden_cold_chain_temperatures(self):
        """Ambient medicine with is_cold_chain=False strictly rejects submitted temperature fields."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Napa 500",
            "generic_name": "Paracetamol",
            "dosage_form": "TABLET",
            "manufacturer": "Beximco",
            "strength": "500mg",
            "strips_per_box": 20,
            "units_per_strip": 10,
            "is_cold_chain": False,
            "min_temperature_celsius": 2.0,  # Inactive hidden field
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": payload, "strict_hidden_check": True},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_HIDDEN_FIELD_VALUE_REJECTED")
        self.assertEqual(data.get("field"), "min_temperature_celsius")

    def test_05_type_safety_negative_integer_rejected(self):
        """Type safety: Negative integer for strips_per_box is rejected with ERR_INVALID_NUMBER_RANGE."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Ace Plus",
            "generic_name": "Paracetamol",
            "dosage_form": "TABLET",
            "manufacturer": "Square",
            "strength": "500mg",
            "strips_per_box": -5,  # Invalid range
            "units_per_strip": 10,
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": payload, "strict_hidden_check": True},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_INVALID_NUMBER_RANGE")
        self.assertEqual(data.get("field"), "strips_per_box")

    def test_06_type_safety_invalid_select_option_rejected(self):
        """Type safety: Non-existent dosage form option rejected with ERR_INVALID_SELECT_OPTION."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Ace Plus",
            "generic_name": "Paracetamol",
            "dosage_form": "INVALID_UNAPPROVED_FORM",
            "manufacturer": "Square",
            "strength": "500mg",
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": payload, "strict_hidden_check": True},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_INVALID_SELECT_OPTION")
        self.assertEqual(data.get("field"), "dosage_form")

    def test_07_type_safety_decimal_positive_check(self):
        """Type safety: Invalid decimal format for volume_ml is rejected with ERR_INVALID_DECIMAL."""
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "brand_name": "Tusca Syrup",
            "generic_name": "Dextromethorphan",
            "dosage_form": "SYRUP",
            "manufacturer": "Square",
            "strength": "10mg/5mL",
            "volume_ml": "invalid-non-numeric",
        }
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": payload, "strict_hidden_check": True},
            format="json",
        )
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_INVALID_DECIMAL")
        self.assertEqual(data.get("field"), "volume_ml")

    def test_08_suspended_tenant_blocked(self):
        """User from a suspended tenant is blocked with HTTP 403."""
        self.client.force_authenticate(user=self.suspended_user)
        self.client.handler._force_tenant = self.suspended_tenant
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": {"brand_name": "Test"}},
            HTTP_X_TENANT_ID=self.suspended_tenant.tenant_id,
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_09_unauthenticated_request_rejected(self):
        """Unauthenticated request is rejected with HTTP 401."""
        resp = self.client.post(
            "/api/v2/medicines/editor-schema/evaluate-conditions/",
            data={"payload": {}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
