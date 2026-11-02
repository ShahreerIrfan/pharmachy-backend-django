"""
backend/tests/test_p131_medicine_form_template.py
Architecture Decision: ADR-0131 (P131).
Integration Tests for Medicine Form Template API, Layout Rules,
Allowed Values, and Platform RBAC & Tenant Subscription Safeguards.
"""

import json
import random
import uuid
from pathlib import Path

import jsonschema
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.dosage_form_service import DosageFormRegistryService
from apps.core.medicine_form_template_service import MedicineFormTemplateService
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
    / "medicine_form_template.schema.json"
)


class MedicineFormTemplateIntegrationTests(TestCase):
    """End-to-end integration tests for P131 Medicine Form Template API."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        DosageFormRegistryService.ensure_canonical_forms_seeded()
        MedicineFormTemplateService.ensure_canonical_templates_seeded()

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

        # 2. Active Tenant (Dhanmondi Pharma) & Owner / Cashier
        self.active_tenant = Tenant.objects.create(
            tenant_id=f"tn-p131-act-{self.suffix}",
            name=f"Dhanmondi Pharma {self.suffix}",
            subdomain=f"dhanmondi-p131-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch1 = Branch.objects.create(
            branch_id=f"br-p131-1-{self.suffix}",
            tenant=self.active_tenant,
            name="Main Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@dhanmondi.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Dhanmondi Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.active_tenant.tenant_id,
            branch_id=self.branch1.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@dhanmondi.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Dhanmondi Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.active_tenant.tenant_id,
            branch_id=self.branch1.branch_id,
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"tn-p131-susp-{self.suffix}",
            name=f"Suspended Pharmacy {self.suffix}",
            subdomain=f"suspended-p131-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.suspended_owner = PharmacyUser.objects.create_user(
            email=f"susp_owner_{self.suffix}@suspended.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            self.schema_doc = json.load(f)

    def test_01_authenticated_user_fetches_default_tablet_template_and_validates_json_schema(self):
        """P131 Acceptance: Authenticated pharmacist queries TABLET template adhering to JSON schema contract."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get("/api/v2/catalog/templates/TABLET/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Validate JSON Schema
        sub_schema = dict(self.schema_doc["definitions"]["MedicineFormTemplateItem"])
        sub_schema["definitions"] = self.schema_doc["definitions"]
        jsonschema.validate(instance=data, schema=sub_schema)

        self.assertEqual(data["dosage_form_code"], "TABLET")
        self.assertEqual(data["template_code"], "TABLET_STANDARD")
        self.assertEqual(data["template_version"], "v1.0")
        self.assertTrue(data["is_active"])
        self.assertTrue(data["is_default"])
        self.assertEqual(data["layout_rules"]["grid_columns"], 12)

        # Verify allowed values
        allowed = data["allowed_values"]
        self.assertIn("mg", allowed["strength_units"])
        self.assertIn("STRIP", allowed["container_types"])
        self.assertIn("Oral", allowed["administration_routes"])

        # Verify 64-char SHA-256 hash
        self.assertEqual(len(data["immutable_version_hash"]), 64)

    def test_02_fetch_syrup_liquid_template_with_volume_and_concentration(self):
        """P131 Acceptance: SYRUP template includes liquid volume and bottle concentration parameters."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get("/api/v2/catalog/templates/SYRUP/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["dosage_form_code"], "SYRUP")
        self.assertEqual(data["template_code"], "SYRUP_LIQUID")

        allowed = data["allowed_values"]
        self.assertIn("mg/5mL", allowed["strength_units"])
        self.assertIn("PET_BOTTLE", allowed["container_types"])
        self.assertIn("GLASS_BOTTLE", allowed["container_types"])

        # Check section has volume_ml
        sections = data["layout_rules"]["sections"]
        section_ids = [s["section_id"] for s in sections]
        self.assertIn("liquid_volume_spec", section_ids)

    def test_03_fetch_injection_sterile_template_with_cold_chain_thresholds(self):
        """P131 Acceptance: INJECTION template publishes cold-chain parameters and vial/ampoule containers."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get("/api/v2/catalog/templates/INJECTION/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["dosage_form_code"], "INJECTION")
        allowed = data["allowed_values"]
        self.assertIn("VIAL", allowed["container_types"])
        self.assertIn("AMPOULE", allowed["container_types"])
        self.assertIn("COLD_CHAIN_2_8C", allowed["storage_conditions"])
        self.assertIn("Intravenous (IV)", allowed["administration_routes"])

    def test_04_by_dosage_form_action_endpoint(self):
        """P131 Acceptance: by-dosage-form action returns matching template."""
        self.client.force_authenticate(user=self.cashier)
        response = self.client.get("/api/v2/catalog/templates/by-dosage-form/?dosage_form=TABLET")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["dosage_form_code"], "TABLET")

        # Missing dosage_form param returns 400
        res_missing = self.client.get("/api/v2/catalog/templates/by-dosage-form/")
        self.assertIn(res_missing.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_05_allowed_values_dedicated_endpoint(self):
        """P131 Acceptance: allowed-values endpoint returns enum sets for frontend dropdowns."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get("/api/v2/catalog/templates/allowed-values/?dosage_form=TABLET")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["dosage_form"], "TABLET")
        self.assertIn("allowed_values", data)
        self.assertIn("strength_units", data["allowed_values"])
        self.assertIn("mg", data["allowed_values"]["strength_units"])

    def test_06_superadmin_creates_custom_template_successfully(self):
        """P131 Acceptance: Platform Superadmin creates new dosage-form template."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "dosage_form_code": "CAPSULE",
            "template_code": f"CAPSULE_EXTENDED_{self.suffix}",
            "template_version": "v1.0",
            "title_en": "Extended Capsule Form Template",
            "title_bn": "এক্সটেন্ডেড ক্যাপসুল ফরম টেমপ্লেট",
            "description": "Custom capsule template with delayed release options.",
            "is_default": False,
            "layout_rules": {
                "grid_columns": 12,
                "sections": [
                    {
                        "section_id": "basic_spec",
                        "title_en": "Basic Details",
                        "title_bn": "প্রাথমিক বিবরণ",
                        "display_order": 1,
                        "fields": [
                            {
                                "field_key": "brand_name",
                                "widget_type": "text_input",
                                "column_span": 6,
                                "label_en": "Brand Name",
                                "label_bn": "ব্র্যান্ডের নাম",
                                "is_required": True,
                            }
                        ],
                    }
                ],
            },
            "allowed_values": {
                "strength_units": ["mg", "mcg"],
                "administration_routes": ["Oral"],
                "container_types": ["BLISTER_PACK", "ALU_ALU"],
                "storage_conditions": ["ROOM_TEMPERATURE"],
                "dispensing_units": ["Capsule"],
            },
        }

        response = self.client.post("/api/v2/catalog/templates/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["dosage_form_code"], "CAPSULE")
        self.assertEqual(data["template_code"], f"CAPSULE_EXTENDED_{self.suffix.upper()}")
        self.assertEqual(len(data["immutable_version_hash"]), 64)

    def test_07_tenant_owner_denied_creating_template(self):
        """P131 Acceptance: Pharmacy Owner denied mutating templates (Platform Superadmin only)."""
        self.client.force_authenticate(user=self.owner)
        payload = {
            "dosage_form_code": "TABLET",
            "template_code": "TENANT_ATTEMPT",
            "template_version": "v1.0",
            "title_en": "Unauthorized Template",
            "title_bn": "অননুমোদিত টেমপ্লেট",
            "layout_rules": {"grid_columns": 12, "sections": []},
            "allowed_values": {"strength_units": ["mg"]},
        }

        response = self.client.post("/api/v2/catalog/templates/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        error_repr = json.dumps(response.json())
        self.assertIn("PLATFORM_CATALOG_OWNERSHIP_PROTECTED", error_repr)

    def test_08_suspended_tenant_blocked_from_fetching_templates(self):
        """P131 Acceptance: Suspended tenant staff blocked with ERR_TENANT_SUSPENDED."""
        self.client.force_authenticate(user=self.suspended_owner)
        response = self.client.get("/api/v2/catalog/templates/TABLET/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        error_repr = json.dumps(response.json())
        self.assertIn("ERR_TENANT_SUSPENDED", error_repr)

    def test_09_unknown_dosage_form_returns_404(self):
        """P131 Acceptance: Querying non-existent dosage form returns 404."""
        self.client.force_authenticate(user=self.owner)
        response = self.client.get("/api/v2/catalog/templates/UNKNOWN_FORM_999/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        data = response.json()
        self.assertEqual(data["error_code"], "ERR_TEMPLATE_NOT_FOUND")

    def test_10_unauthenticated_request_rejected(self):
        """P131 Acceptance: Anonymous request rejected with 401 Unauthorized."""
        response = self.client.get("/api/v2/catalog/templates/TABLET/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
