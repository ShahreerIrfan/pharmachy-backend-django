"""
backend/tests/test_p148_topical_editor.py
Architecture Decision: ADR-0148 (P148).
Test suite for Cream, Ointment, Gel, and Lotion Topical Formulation Editor.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
    TopicalContainerType,
    TopicalFormConfig,
    TopicalFormulationType,
    TopicalRoute,
    TopicalStorageCondition,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestTopicalEditorIntegration:
    """Integration test suite for P148 Cream/Ointment Editor."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p148-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p148-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p148-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p148-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p148-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p148-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p148-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p148-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p148-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p148-basic-{self.suffix}",
            name=f"Basic Health {self.suffix}",
            subdomain=f"basic-p148-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p148-basic-{self.suffix}",
            tenant=self.tenant_basic,
            name="Basic Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Users
        self.superadmin = PharmacyUser.objects.create_superuser(
            email=f"superadmin_{self.suffix}@dgda.gov.bd",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Platform SuperAdmin",
            password="Password123!",
            role=UserRole.SUPERADMIN,
        )
        self.pharmacist_a = PharmacyUser.objects.create_user(
            email=f"pharmacist_a_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Square Registered Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@labaid.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Labaid Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Square Cashier",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.pharma_suspended = PharmacyUser.objects.create_user(
            email=f"pharma_susp_{self.suffix}@suspended.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )
        self.pharma_basic = PharmacyUser.objects.create_user(
            email=f"pharma_basic_{self.suffix}@basic.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Basic Pharmacist",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_basic.tenant_id,
            branch_id=self.branch_basic.branch_id,
        )

        # 3. Medicine Product
        self.product = MedicineProduct.objects.create(
            product_id=f"med-mupirocin-{self.suffix}",
            brand_name="Bactroban Ointment",
            generic_name="Mupirocin 2%",
            dosage_form="OINTMENT",
            strength="2% w/w",
            mrp_unit=Decimal("180.00"),
            cost_unit=Decimal("140.00"),
        )

        self.valid_payload = {
            "product_id": self.product.product_id,
            "tenant_id": self.tenant_a.tenant_id,
            "formulation_type": TopicalFormulationType.OINTMENT,
            "administration_route": TopicalRoute.TOPICAL_CUTANEOUS,
            "container_type": TopicalContainerType.TUBE_ALUMINUM,
            "net_weight_grams": "15.00",
            "concentration_value": "2.0000",
            "concentration_unit": "PERCENT_W_W",
            "is_sterile": False,
            "applicator_included": False,
            "opened_shelf_life_days": 28,
            "storage_condition": TopicalStorageCondition.ROOM_15_25,
            "is_active": True,
        }

    def test_get_schema_success(self):
        """1. Schema returns supported formulations, routes, containers, and forbidden fields."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.get("/api/v2/catalog/topical/schema/")
        assert response.status_code == 200
        data = response.json()
        assert data["dosage_form_family"] == "TOPICAL_SEMISOLID_FLUID"
        assert "strips_per_box" in data["forbidden_fields"]
        assert any(f["code"] == "OINTMENT" for f in data["supported_formulations"])
        assert any(r["code"] == "TOPICAL_CUTANEOUS" for r in data["supported_routes"])
        assert any(c["code"] == "TUBE_ALUMINUM" for c in data["supported_containers"])

    def test_validate_valid_payload(self):
        """2. Valid topical ointment payload passes non-destructive validation."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/topical/validate/", self.valid_payload, format="json")
        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert "আক্রান্ত ত্বকে" in response.json()["validated_payload"]["patient_instructions_bn"]

    def test_validate_rejects_solid_oral_attributes(self):
        """3. Zero-trust isolation against solid oral packaging fields."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["strips_per_box"] = 10
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_validate_rejects_needle_gauge(self):
        """4. Prohibits injectable needle gauge attributes."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["needle_gauge"] = "25G"
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_validate_rejects_invalid_formulation_type(self):
        """5. Invalid formulation type is rejected."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["formulation_type"] = "INJECTION_SOLUTION"
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_FORMULATION_TYPE"

    def test_validate_rejects_invalid_topical_route(self):
        """6. Rejects non-topical route (e.g. ORAL_SWALLOW)."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["administration_route"] = "ORAL_SWALLOW"
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_TOPICAL_ROUTE"

    def test_validate_rejects_invalid_container_type(self):
        """7. Rejects invalid container type."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["container_type"] = "AMPOULE"
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_CONTAINER_TYPE"

    def test_validate_rejects_non_positive_net_weight(self):
        """8. Rejects net weight <= 0."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["net_weight_grams"] = "-15.00"
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_NET_WEIGHT"

    def test_validate_rejects_non_positive_concentration(self):
        """9. Rejects concentration value <= 0."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["concentration_value"] = "0.0000"
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_CONCENTRATION"

    def test_validate_enforces_ophthalmic_sterility_mandate(self):
        """10. Ophthalmic eye ointments must be declared sterile."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["administration_route"] = TopicalRoute.TOPICAL_OPHTHALMIC
        payload["is_sterile"] = False
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_OPHTHALMIC_STERILITY_MANDATORY"

    def test_validate_rejects_ophthalmic_shelf_life_over_28_days(self):
        """11. Sterile ophthalmic eye ointments cannot exceed 28 days opened shelf life."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["administration_route"] = TopicalRoute.TOPICAL_OPHTHALMIC
        payload["is_sterile"] = True
        payload["opened_shelf_life_days"] = 60
        response = self.client.post("/api/v2/catalog/topical/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_OPENED_SHELF_LIFE"

    def test_create_topical_config_pharmacist_success(self):
        """12. Pharmacist authors topical ointment configuration with DB persistence."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/topical/", self.valid_payload, format="json")
        assert response.status_code == 201
        data = response.json()
        assert data["formulation_type"] == TopicalFormulationType.OINTMENT
        assert data["net_weight_grams"] == "15.00"
        assert data["container_type"] == TopicalContainerType.TUBE_ALUMINUM

        # DB persistence check
        cfg = TopicalFormConfig.objects.get(config_id=data["config_id"])
        assert cfg.product_id == self.product.product_id
        assert cfg.tenant_id == self.tenant_a.tenant_id
        assert cfg.net_weight_grams == Decimal("15.00")

    def test_cashier_cannot_create_or_update(self):
        """13. Cashier mutation is blocked with ERR_PERMISSION_DENIED."""
        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.post("/api/v2/catalog/topical/", self.valid_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_PERMISSION_DENIED"

    def test_cross_tenant_violation_blocked(self):
        """14. Staff cannot modify topical monograph configuration for another tenant."""
        self.client.force_authenticate(user=self.pharmacist_a)
        cross_payload = dict(self.valid_payload)
        cross_payload["tenant_id"] = self.tenant_b.tenant_id
        response = self.client.post("/api/v2/catalog/topical/", cross_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_CROSS_TENANT_VIOLATION"

    def test_suspended_tenant_write_blocked(self):
        """15. Suspended tenant cannot author or update configurations."""
        self.client.force_authenticate(user=self.pharma_suspended)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_suspended.tenant_id
        response = self.client.post("/api/v2/catalog/topical/", payload, format="json")
        assert response.status_code == 403
        assert response.json().get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_basic_plan_cannot_author_custom_monograph(self):
        """16. Basic plan tenant is gated from custom monograph authoring."""
        self.client.force_authenticate(user=self.pharma_basic)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_basic.tenant_id
        response = self.client.post("/api/v2/catalog/topical/", payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_SUBSCRIPTION_UNENTITLED"

    def test_partial_update_patch_success(self):
        """17. Pharmacist can update net weight via PATCH."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/topical/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        patch_data = {"net_weight_grams": "30.00"}
        res_patch = self.client.patch(f"/api/v2/catalog/topical/{config_id}/", patch_data, format="json")
        assert res_patch.status_code == 200
        assert res_patch.json()["net_weight_grams"] == "30.00"

    def test_destroy_topical_config(self):
        """18. Pharmacist can delete a topical configuration."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/topical/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        res_del = self.client.delete(f"/api/v2/catalog/topical/{config_id}/")
        assert res_del.status_code == 204
        assert not TopicalFormConfig.objects.filter(config_id=config_id).exists()
