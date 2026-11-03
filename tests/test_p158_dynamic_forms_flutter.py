"""
backend/tests/test_p158_dynamic_forms_flutter.py
Architecture Decision: ADR-0158 (P158).
Integration test suite for Flutter Dynamic Medicine Forms & Backend Validation Parity.
"""

import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestFlutterDynamicFormsIntegration:
    """Integration test suite for P158 Flutter Dynamic Forms & Contract Parity."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p158-{self.suffix}",
            name=f"Evercare Pharma {self.suffix}",
            subdomain=f"evercare-p158-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-p158-{self.suffix}",
            tenant=self.tenant,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p158-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"susp-p158-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p158-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Users
        self.user_manager = PharmacyUser.objects.create_user(
            email=f"manager_{self.suffix}@evercare.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Manager {self.suffix}",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@evercare.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@suspended.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Suspended Staff {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

    def test_flutter_canonical_layout_parity_with_desktop(self):
        """1. Verifies Flutter client receives identical canonical group structures as JavaFX Desktop."""
        self.client.force_authenticate(user=self.user_manager)

        resp = self.client.get("/api/v2/catalog/layout/groups/canonical/?dosage_form_family=TABLET_CAPSULE")
        assert resp.status_code == status.HTTP_200_OK

        groups = resp.data.get("groups", [])
        group_codes = [g["group_code"] for g in groups]
        assert "GENERAL_IDENTIFICATION" in group_codes
        assert "PACKAGING_DIMENSIONS" in group_codes
        assert "STORAGE_AND_COLD_CHAIN" in group_codes
        assert "FINANCIAL_PRICING" in group_codes

        # Check field keys match Flutter client expectation
        general_group = next(g for g in groups if g["group_code"] == "GENERAL_IDENTIFICATION")
        field_names = [f["field_name"] for f in general_group["fields"]]
        assert "brand_name" in field_names
        assert "generic_name" in field_names
        assert "dosage_form" in field_names

    def test_flutter_valid_dosage_switch_and_validation(self):
        """2. Valid scenario: Flutter client switches dosage form, preserves common fields, and validates."""
        self.client.force_authenticate(user=self.user_manager)

        # Switch TABLET_CAPSULE -> INHALER
        payload_switch = {
            "previous_dosage_form": "TABLET_CAPSULE",
            "new_dosage_form": "INHALER",
            "current_values": {
                "brand_name": "Seretide Evohaler",
                "generic_name": "Salmeterol + Fluticasone",
                "dar_number": "002-334-556",
                "cost_unit": "450.00",
                "mrp_unit": "550.00",
                "storage_condition": "CONTROLLED_ROOM_TEMPERATURE",
                "pieces_per_strip": 10,
                "strips_per_pack": 3,
                "total_pieces_per_pack": 30,
            },
            "retained_form_cache": {},
        }
        resp_switch = self.client.post("/api/v2/catalog/layout/groups/switch-dosage-form/", payload_switch, format="json")
        assert resp_switch.status_code == status.HTTP_200_OK

        # Validate form after switch with inhaler specifics
        switched_values = resp_switch.data["form_values"]
        switched_values["dose_per_actuation"] = "125mcg"
        switched_values["actuations_per_device"] = 120

        payload_validate = {
            "dosage_form_family": "INHALER",
            "form_values": switched_values,
        }
        resp_val = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload_validate, format="json")
        assert resp_val.status_code == status.HTTP_200_OK
        assert resp_val.data["is_valid"] is True
        assert resp_val.data["error_count"] == 0

    def test_flutter_invalid_scenario_triggers_canonical_inline_errors(self):
        """3. Invalid/incomplete input: Missing brand name, negative cost, and packaging dimension error."""
        self.client.force_authenticate(user=self.user_manager)

        payload = {
            "dosage_form_family": "TABLET_CAPSULE",
            "form_values": {
                "brand_name": "",  # Missing
                "generic_name": "Cefixime",
                "dar_number": "112-334-556",
                "cost_unit": "-15.00",  # Negative
                "mrp_unit": "30.00",
                "pieces_per_strip": 7,
                "strips_per_pack": 2,
                "total_pieces_per_pack": 20,  # 7 * 2 != 20
            },
        }

        resp = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["is_valid"] is False

        err_codes = [err["error_code"] for err in resp.data["inline_errors"]]
        assert "ERR_MANDATORY_FIELD_MISSING" in err_codes
        assert "ERR_NEGATIVE_COST" in err_codes
        assert "ERR_DIMENSION_MISMATCH" in err_codes

    def test_flutter_unauthorized_cashier_denied_layout_mutation(self):
        """4. Unauthorized access: Cashier role blocked from mutating dynamic form layouts."""
        self.client.force_authenticate(user=self.user_cashier)

        payload = {
            "group_code": "FINANCIAL_PRICING",
            "dosage_form_family": "ALL",
            "title_en": "Cashier Pricing Override",
            "title_bn": "অননুমোদিত মূল্য",
            "display_order": 1,
            "display_density": "COMFORTABLE",
            "fields": [],
        }

        resp = self.client.post("/api/v2/catalog/layout/groups/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_flutter_suspended_tenant_blocked(self):
        """5. Cross-tenant security: Suspended tenant blocked from dynamic form resolution."""
        self.client.force_authenticate(user=self.user_suspended)

        resp = self.client.get("/api/v2/catalog/layout/groups/?dosage_form_family=INHALER")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data["error_code"] == "ERR_TENANT_SUSPENDED"

