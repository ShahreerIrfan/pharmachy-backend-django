"""
backend/tests/test_p157_dynamic_forms_javafx.py
Architecture Decision: ADR-0157 (P157).
Integration test suite for Schema-Driven Dynamic Medicine Forms and State-Preserving Switching.
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
class TestDynamicMedicineFormsIntegration:
    """Integration test suite for P157 Dynamic Forms & Dosage Switching."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p157-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"popular-p157-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p157-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Active Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p157-b-{self.suffix}",
            name=f"Ibn Sina Meds {self.suffix}",
            subdomain=f"ibnsina-p157-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p157-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Mirpur Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p157-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p157-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p157-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Users
        self.user_pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Dr. Pharmacist {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Cashier {self.suffix}",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_tenant_b = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@ibnsina.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Ibn Sina Owner {self.suffix}",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"susp_pharma_{self.suffix}@suspended.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name=f"Suspended User {self.suffix}",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

    def test_canonical_groups_retrieval_for_various_dosage_forms(self):
        """1. Canonical schema retrieval exposes formulation-specific parameters."""
        self.client.force_authenticate(user=self.user_pharmacist)

        for form in ["TABLET_CAPSULE", "INHALER", "DROPS", "TOPICAL_CREAM", "SACHET_POWDER", "SUPPOSITORY"]:
            resp = self.client.get(f"/api/v2/catalog/layout/groups/canonical/?dosage_form_family={form}")
            assert resp.status_code == status.HTTP_200_OK
            assert resp.data["dosage_form_family"] == form
            groups = resp.data["groups"]
            assert len(groups) >= 4
            group_codes = [g["group_code"] for g in groups]
            assert "GENERAL_IDENTIFICATION" in group_codes
            assert "FINANCIAL_PRICING" in group_codes

    def test_switch_dosage_form_retains_common_fields_and_caches_specifics(self):
        """2. Valid scenario: Server-side dosage form switching retains common fields and caches specific fields."""
        self.client.force_authenticate(user=self.user_pharmacist)

        payload = {
            "previous_dosage_form": "TABLET_CAPSULE",
            "new_dosage_form": "INHALER",
            "current_values": {
                "brand_name": "Ventolin Evohaler",
                "brand_name_bn": "ভেনটোলিন ইভোহেলার",
                "generic_name": "Salbutamol",
                "manufacturer": "GSK Bangladesh",
                "dar_number": "123-456-789",
                "cost_unit": "180.00",
                "mrp_unit": "225.00",
                "pieces_per_strip": 10,
                "strips_per_pack": 2,
                "total_pieces_per_pack": 20,
            },
            "retained_form_cache": {},
        }

        resp = self.client.post("/api/v2/catalog/layout/groups/switch-dosage-form/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK

        data = resp.data
        assert data["previous_dosage_form"] == "TABLET_CAPSULE"
        assert data["new_dosage_form"] == "INHALER"

        # Common fields retained
        values = data["form_values"]
        assert values["brand_name"] == "Ventolin Evohaler"
        assert values["generic_name"] == "Salbutamol"
        assert values["cost_unit"] == "180.00"
        assert values["mrp_unit"] == "225.00"

        # Form specific fields from previous form moved to cache
        assert "pieces_per_strip" not in values
        cache = data["retained_form_cache"]
        assert "TABLET_CAPSULE" in cache
        assert cache["TABLET_CAPSULE"]["pieces_per_strip"] == 10

        # Now simulate switching back to TABLET_CAPSULE with the cached state
        switch_back_payload = {
            "previous_dosage_form": "INHALER",
            "new_dosage_form": "TABLET_CAPSULE",
            "current_values": {
                **values,
                "dose_per_actuation": "100mcg",
                "actuations_per_device": 200,
            },
            "retained_form_cache": cache,
        }

        resp_back = self.client.post("/api/v2/catalog/layout/groups/switch-dosage-form/", switch_back_payload, format="json")
        assert resp_back.status_code == status.HTTP_200_OK

        back_values = resp_back.data["form_values"]
        assert back_values["brand_name"] == "Ventolin Evohaler"
        # Restored tablet fields
        assert back_values["pieces_per_strip"] == 10
        assert back_values["strips_per_pack"] == 2
        assert back_values["total_pieces_per_pack"] == 20

        # Inhaler fields now in cache
        assert "INHALER" in resp_back.data["retained_form_cache"]
        assert resp_back.data["retained_form_cache"]["INHALER"]["dose_per_actuation"] == "100mcg"

    def test_validate_inline_detects_missing_mandatory_and_pricing_errors(self):
        """3. Invalid/incomplete input: Missing mandatory fields and pricing errors."""
        self.client.force_authenticate(user=self.user_pharmacist)

        # Missing brand name, negative cost, MRP below cost
        payload = {
            "dosage_form_family": "INHALER",
            "form_values": {
                "brand_name": "",
                "cost_unit": "-10.00",
                "mrp_unit": "-5.00",
            },
        }

        resp = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_valid"] is False
        assert data["error_count"] >= 3

        error_codes = [err["error_code"] for err in data["inline_errors"]]
        assert "ERR_MANDATORY_FIELD_MISSING" in error_codes
        assert "ERR_NEGATIVE_COST" in error_codes
        assert "ERR_INVALID_MRP" in error_codes

    def test_validate_inline_detects_dimension_mismatch(self):
        """4. Packaging dimension mismatch detection."""
        self.client.force_authenticate(user=self.user_pharmacist)

        payload = {
            "dosage_form_family": "TABLET_CAPSULE",
            "form_values": {
                "brand_name": "Napa Extra",
                "generic_name": "Paracetamol",
                "pieces_per_strip": 10,
                "strips_per_pack": 10,
                "total_pieces_per_pack": 80,  # 10 * 10 != 80
            },
        }

        resp = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        error_codes = [err["error_code"] for err in data["inline_errors"]]
        assert "ERR_DIMENSION_MISMATCH" in error_codes

    def test_validate_inline_detects_temperature_inversion(self):
        """5. Temperature range inversion detection."""
        self.client.force_authenticate(user=self.user_pharmacist)

        payload = {
            "dosage_form_family": "DROPS",
            "form_values": {
                "brand_name": "Ciprofloxacin Eye Drops",
                "generic_name": "Ciprofloxacin",
                "storage_temperature_celsius_min": 28.0,
                "storage_temperature_celsius_max": 25.0,
            },
        }

        resp = self.client.post("/api/v2/catalog/layout/groups/validate-inline/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        error_codes = [err["error_code"] for err in data["inline_errors"]]
        assert "ERR_INVALID_TEMPERATURE_RANGE" in error_codes

    def test_cashier_denied_layout_mutation(self):
        """6. Unauthorized access: Cashier is restricted from mutating dynamic layout configurations."""
        self.client.force_authenticate(user=self.user_cashier)

        payload = {
            "group_code": "GENERAL_IDENTIFICATION",
            "dosage_form_family": "ALL",
            "title_en": "Hacked Title",
            "title_bn": "হ্যাক করা শিরোনাম",
            "display_order": 1,
            "display_density": "COMFORTABLE",
            "fields": [],
        }

        resp = self.client.post("/api/v2/catalog/layout/groups/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_suspended_tenant_blocked(self):
        """7. Suspended tenant blocked from dynamic form resolution."""
        self.client.force_authenticate(user=self.user_suspended)

        resp = self.client.get("/api/v2/catalog/layout/groups/?dosage_form_family=TABLET_CAPSULE")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data["error_code"] == "ERR_TENANT_SUSPENDED"

