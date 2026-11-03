"""
backend/tests/test_p152_dynamic_field_group_layout.py
Architecture Decision: ADR-0152 (P152).
Integration test suite for Dynamic Field Group Layout & Keyboard Accessible Flow.
"""

import random
import uuid

import pytest
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    DynamicFieldGroupLayout,
    FieldGroupCode,
    LayoutDisplayDensity,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestDynamicFieldGroupLayoutIntegration:
    """Integration test suite for P152 Dynamic Field Group Layout."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p152-a-{self.suffix}",
            name=f"Square Healthcare {self.suffix}",
            subdomain=f"square-p152-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p152-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p152-b-{self.suffix}",
            name=f"Labaid Pharma {self.suffix}",
            subdomain=f"labaid-p152-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p152-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p152-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"suspended-p152-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p152-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Users
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharma_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_tenant_b = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@labaid.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Labaid Owner {self.suffix}",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@suspended.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Susp Owner {self.suffix}",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

    def test_canonical_groups_inhaler(self):
        """Validates canonical layout endpoint for INHALER dosage form."""
        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/layout/groups/canonical/?dosage_form_family=INHALER")
        assert res.status_code == 200
        data = res.json()
        assert data["dosage_form_family"] == "INHALER"
        groups = data["groups"]
        assert len(groups) == 6

        # Check group codes and keyboard mnemonics
        codes = [g["group_code"] for g in groups]
        assert "GENERAL_IDENTIFICATION" in codes
        assert "FORM_SPECIFIC_PARAMETERS" in codes
        assert "PACKAGING_DIMENSIONS" in codes
        assert "STORAGE_AND_COLD_CHAIN" in codes

        gen_group = next(g for g in groups if g["group_code"] == "GENERAL_IDENTIFICATION")
        assert gen_group["keyboard_shortcut_mnemonic"] == "G"
        assert gen_group["fields"][0]["field_name"] == "brand_name"
        assert gen_group["fields"][0]["keyboard_mnemonic"] == "B"

        form_group = next(g for g in groups if g["group_code"] == "FORM_SPECIFIC_PARAMETERS")
        field_names = [f["field_name"] for f in form_group["fields"]]
        assert "dose_per_actuation" in field_names
        assert "actuations_per_device" in field_names

    def test_canonical_groups_needle(self):
        """Validates canonical layout endpoint for NEEDLE returns needle gauge & length."""
        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.get("/api/v2/catalog/layout/groups/canonical/?dosage_form_family=NEEDLE")
        assert res.status_code == 200
        data = res.json()
        form_group = next(g for g in data["groups"] if g["group_code"] == "FORM_SPECIFIC_PARAMETERS")
        field_names = [f["field_name"] for f in form_group["fields"]]
        assert "needle_gauge" in field_names
        assert "needle_length_mm" in field_names

    def test_dynamic_group_visibility_evaluation(self):
        """Verifies conditional visibility of formulation group based on parent dosage_form."""
        self.client.force_authenticate(user=self.user_pharmacist)

        # 1. When dosage_form=INHALER, FORM_SPECIFIC_PARAMETERS is visible
        res_inhaler = self.client.get("/api/v2/catalog/layout/groups/?dosage_form_family=INHALER&dosage_form=INHALER")
        assert res_inhaler.status_code == 200
        groups_inhaler = res_inhaler.json()["groups"]
        form_grp_inhaler = next(g for g in groups_inhaler if g["group_code"] == "FORM_SPECIFIC_PARAMETERS")
        assert form_grp_inhaler["is_visible"] is True

        # 2. When dosage_form=TABLET, FORM_SPECIFIC_PARAMETERS is hidden
        res_tablet = self.client.get("/api/v2/catalog/layout/groups/?dosage_form_family=INHALER&dosage_form=TABLET")
        assert res_tablet.status_code == 200
        groups_tablet = res_tablet.json()["groups"]
        form_grp_tablet = next(g for g in groups_tablet if g["group_code"] == "FORM_SPECIFIC_PARAMETERS")
        assert form_grp_tablet["is_visible"] is False

    def test_validate_inline_errors_missing_mandatory(self):
        """Aggregates inline errors for missing mandatory fields with tab_index mapping."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "dosage_form_family": "INHALER",
            "form_values": {
                "dosage_form": "INHALER",
                "brand_name": "",  # Missing mandatory
                "generic_name": "",  # Missing mandatory
            },
        }
        res = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["is_valid"] is False
        assert data["error_count"] >= 2
        field_errors = [e["field_name"] for e in data["inline_errors"]]
        assert "brand_name" in field_errors
        assert "generic_name" in field_errors

    def test_validate_inline_errors_dimension_mismatch(self):
        """Returns inline error for dimension mismatch (pieces_per_strip * strips_per_pack != total)."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "dosage_form_family": "ALL",
            "form_values": {
                "brand_name": "Napa 500mg",
                "generic_name": "Paracetamol",
                "dosage_form": "TABLET",
                "pieces_per_strip": 10,
                "strips_per_pack": 5,
                "total_pieces_per_pack": 60,  # 10 * 5 = 50 != 60
            },
        }
        res = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["is_valid"] is False
        dim_error = next(e for e in data["inline_errors"] if e["error_code"] == "ERR_DIMENSION_MISMATCH")
        assert dim_error["field_name"] == "total_pieces_per_pack"
        assert dim_error["tab_index"] == 12

    def test_validate_inline_errors_temperature_range(self):
        """Returns inline error when minimum storage temp exceeds maximum."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "dosage_form_family": "ALL",
            "form_values": {
                "brand_name": "Insulin Glargine",
                "generic_name": "Insulin",
                "dosage_form": "INJECTION",
                "storage_temperature_celsius_min": 30.0,
                "storage_temperature_celsius_max": 25.0,  # Min > Max
            },
        }
        res = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["is_valid"] is False
        temp_error = next(e for e in data["inline_errors"] if e["error_code"] == "ERR_INVALID_TEMPERATURE_RANGE")
        assert temp_error["field_name"] == "storage_temperature_celsius_min"

    def test_create_custom_layout_group_by_pharmacist(self):
        """Pharmacist customizes layout density and title for tenant branch."""
        self.client.force_authenticate(user=self.user_pharmacist)
        payload = {
            "group_code": "PACKAGING_DIMENSIONS",
            "dosage_form_family": "ALL",
            "display_order": 3,
            "title_en": "Custom Packaging Multipliers",
            "title_bn": "কাস্টম প্যাকেজিং অনুপাত",
            "display_density": "COMPACT",
            "is_collapsible": True,
            "default_collapsed": False,
            "keyboard_shortcut_mnemonic": "P",
        }
        res = self.client.post("/api/v2/catalog/layout/groups/", payload, format="json")
        assert res.status_code == 201, res.data
        data = res.json()
        assert data["title_en"] == "Custom Packaging Multipliers"
        assert data["display_density"] == "COMPACT"

        # Verify DB persistence
        layout = DynamicFieldGroupLayout.objects.get(pk=data["layout_id"])
        assert layout.title_en == "Custom Packaging Multipliers"
        assert layout.tenant == self.tenant_a

    def test_cashier_write_forbidden(self):
        """Cashier role is restricted from modifying layout configurations."""
        self.client.force_authenticate(user=self.user_cashier)
        payload = {
            "group_code": "GENERAL_IDENTIFICATION",
            "title_en": "Cashier Attempt",
            "title_bn": "ক্যাশিয়ার পরিবর্তন",
        }
        res = self.client.post("/api/v2/catalog/layout/groups/", payload, format="json")
        assert res.status_code == 403
        assert res.json().get("error_code") == "ERR_ROLE_UNAUTHORIZED"

    def test_cross_tenant_isolation(self):
        """User from Tenant B cannot modify Tenant A custom layout configuration."""
        layout_a = DynamicFieldGroupLayout.objects.create(
            tenant=self.tenant_a,
            group_code=FieldGroupCode.STORAGE_AND_COLD_CHAIN,
            display_order=4,
            title_en="Square Cold Chain Layout",
            title_bn="স্কয়ার কোল্ড চেইন লেআউট",
            display_density=LayoutDisplayDensity.COMFORTABLE,
        )

        self.client.force_authenticate(user=self.user_tenant_b)
        # Attempt PATCH
        res = self.client.patch(
            f"/api/v2/catalog/layout/groups/{layout_a.layout_id}/",
            {"title_en": "Labaid Override"},
            format="json",
        )
        assert res.status_code == 403
        assert res.json().get("error_code") == "ERR_CROSS_TENANT_ACCESS_DENIED"

    def test_suspended_tenant_mutation_blocked(self):
        """Suspended tenant user attempting mutation gets blocked with HTTP 403."""
        self.client.force_authenticate(user=self.user_suspended)
        payload = {
            "group_code": "GENERAL_IDENTIFICATION",
            "title_en": "Suspended Layout",
            "title_bn": "স্থগিত লেআউট",
        }
        res = self.client.post("/api/v2/catalog/layout/groups/", payload, format="json")
        assert res.status_code == 403
        assert res.json().get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
        ]

    def test_partial_update_custom_layout(self):
        """PATCH updates fields on an existing dynamic layout configuration."""
        layout = DynamicFieldGroupLayout.objects.create(
            tenant=self.tenant_a,
            group_code=FieldGroupCode.FINANCIAL_PRICING,
            display_order=6,
            title_en="Pricing Controls",
            title_bn="মূল্য নির্ধারণ",
            display_density=LayoutDisplayDensity.COMFORTABLE,
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.patch(
            f"/api/v2/catalog/layout/groups/{layout.layout_id}/",
            {"display_density": "DENSE", "keyboard_shortcut_mnemonic": "M"},
            format="json",
        )
        assert res.status_code == 200
        assert res.json()["display_density"] == "DENSE"
        assert res.json()["keyboard_shortcut_mnemonic"] == "M"

        layout.refresh_from_db()
        assert layout.display_density == LayoutDisplayDensity.DENSE

    def test_archive_custom_layout(self):
        """DELETE archives custom layout configuration by setting is_active=False."""
        layout = DynamicFieldGroupLayout.objects.create(
            tenant=self.tenant_a,
            group_code=FieldGroupCode.REGULATORY_AND_DISPENSING,
            display_order=5,
            title_en="Regulatory Controls",
            title_bn="নিয়ন্ত্রক ব্যবস্থা",
        )

        self.client.force_authenticate(user=self.user_pharmacist)
        res = self.client.delete(f"/api/v2/catalog/layout/groups/{layout.layout_id}/")
        assert res.status_code == 200
        assert res.json()["status"] == "ARCHIVED"

        layout.refresh_from_db()
        assert layout.is_active is False
