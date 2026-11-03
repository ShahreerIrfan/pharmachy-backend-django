"""
backend/tests/test_p146_inhaler_editor.py
Architecture Decision: ADR-0146 (P146).
Test suite for Inhaler Dosage-Form & Actuation Packaging Editor.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.core.inhaler_editor_service import InhalerEditorService
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    InhalerDeviceType,
    InhalerFormConfig,
    InhalerRoute,
    InhalerStorageCondition,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestInhalerEditorIntegration:
    """Integration test suite for P146 Inhaler Editor."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p146-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p146-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p146-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p146-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p146-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p146-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p146-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p146-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p146-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p146-basic-{self.suffix}",
            name=f"Basic Health {self.suffix}",
            subdomain=f"basic-p146-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p146-basic-{self.suffix}",
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
            product_id=f"med-salbutamol-{self.suffix}",
            brand_name="Ventolin Evohaler",
            generic_name="Salbutamol 100mcg",
            dosage_form="INHALER",
            strength="100mcg/actuation",
            mrp_unit=Decimal("250.00"),
            cost_unit=Decimal("200.00"),
        )

        self.valid_payload = {
            "product_id": self.product.product_id,
            "tenant_id": self.tenant_a.tenant_id,
            "device_type": InhalerDeviceType.MDI_PRESSURIZED,
            "administration_route": InhalerRoute.ORAL_INHALATION,
            "dose_per_actuation": "100.0000",
            "dose_unit": "MCG",
            "total_actuations": 200,
            "total_active_content": "20000.0000",
            "has_dose_counter": True,
            "has_spacer_included": False,
            "storage_condition": InhalerStorageCondition.ROOM_15_25,
            "priming_required": True,
            "priming_puffs": 2,
            "canister_pressure_warning": True,
            "is_active": True,
        }

    def test_get_schema_success(self):
        """1. Schema returns supported device types, routes, and forbidden fields."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.get("/api/v2/catalog/inhaler/schema/")
        assert response.status_code == 200
        data = response.json()
        assert data["dosage_form_family"] == "INHALER_RESPIRATORY"
        assert "strips_per_box" in data["forbidden_fields"]
        assert any(d["code"] == "MDI_PRESSURIZED" for d in data["supported_device_types"])
        assert any(r["code"] == "ORAL_INHALATION" for r in data["supported_routes"])

    def test_validate_valid_payload(self):
        """2. Valid inhaler payload passes non-destructive validation."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/inhaler/validate/", self.valid_payload, format="json")
        assert response.status_code == 200
        assert response.json()["valid"] is True

    def test_validate_rejects_solid_oral_attributes(self):
        """3. Zero-trust isolation against solid oral packaging fields."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["strips_per_box"] = 5
        response = self.client.post("/api/v2/catalog/inhaler/validate/", payload, format="json")
        assert response.status_code == 422
        data = response.json()
        assert data["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_validate_rejects_invalid_device_type(self):
        """4. Invalid inhaler device mechanism is rejected."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["device_type"] = "INJECTION_PEN"
        response = self.client.post("/api/v2/catalog/inhaler/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_DEVICE_TYPE"

    def test_validate_rejects_invalid_administration_route(self):
        """5. Invalid route (e.g. INTRAVENOUS or TOPICAL) is rejected."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["administration_route"] = "INTRAVENOUS"
        response = self.client.post("/api/v2/catalog/inhaler/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_ADMINISTRATION_ROUTE"

    def test_validate_rejects_zero_or_negative_dose(self):
        """6. Rejects non-positive dose per actuation."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["dose_per_actuation"] = "0.0000"
        response = self.client.post("/api/v2/catalog/inhaler/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_DOSE_PER_ACTUATION"

    def test_validate_rejects_out_of_bounds_actuations(self):
        """7. Rejects total actuations < 10 or > 1000."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["total_actuations"] = 5
        response = self.client.post("/api/v2/catalog/inhaler/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_ACTUATIONS_COUNT"

    def test_validate_rejects_dimension_consistency_mismatch(self):
        """8. Rejects dimension mismatch: total active content != dose * actuations."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        # 100 * 200 = 20000, mismatch with 25000
        payload["total_active_content"] = "25000.0000"
        response = self.client.post("/api/v2/catalog/inhaler/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_DIMENSION_CONSISTENCY_MISMATCH"

    def test_create_inhaler_config_pharmacist_success(self):
        """9. Pharmacist successfully authors inhaler configuration with auto counseling."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/inhaler/", self.valid_payload, format="json")
        assert response.status_code == 201
        data = response.json()
        assert data["device_type"] == InhalerDeviceType.MDI_PRESSURIZED
        assert data["total_actuations"] == 200
        assert data["has_dose_counter"] is True
        assert "মুখের মাধ্যমে শ্বাসনালীতে" in InhalerEditorService._translate_route(data["administration_route"])

        # Check DB persistence
        cfg = InhalerFormConfig.objects.get(config_id=data["config_id"])
        assert cfg.product_id == self.product.product_id
        assert cfg.tenant_id == self.tenant_a.tenant_id
        assert cfg.dose_per_actuation == Decimal("100.0000")

    def test_cashier_cannot_create_or_update(self):
        """10. Cashier mutation is blocked with ERR_PERMISSION_DENIED."""
        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.post("/api/v2/catalog/inhaler/", self.valid_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_PERMISSION_DENIED"

    def test_cross_tenant_violation_blocked(self):
        """11. Staff cannot modify monograph configuration for another tenant."""
        self.client.force_authenticate(user=self.pharmacist_a)
        cross_payload = dict(self.valid_payload)
        cross_payload["tenant_id"] = self.tenant_b.tenant_id
        response = self.client.post("/api/v2/catalog/inhaler/", cross_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_CROSS_TENANT_VIOLATION"

    def test_suspended_tenant_write_blocked(self):
        """12. Suspended tenant cannot author or update configurations."""
        self.client.force_authenticate(user=self.pharma_suspended)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_suspended.tenant_id
        response = self.client.post("/api/v2/catalog/inhaler/", payload, format="json")
        assert response.status_code == 403
        assert response.json().get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_basic_plan_cannot_author_custom_monograph(self):
        """13. Basic plan tenant is gated from custom monograph authoring."""
        self.client.force_authenticate(user=self.pharma_basic)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_basic.tenant_id
        response = self.client.post("/api/v2/catalog/inhaler/", payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_SUBSCRIPTION_UNENTITLED"

    def test_partial_update_patch_success(self):
        """14. Pharmacist can update actuations or dose counter via PATCH."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/inhaler/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        patch_data = {"total_actuations": 120, "total_active_content": "12000.0000"}
        res_patch = self.client.patch(f"/api/v2/catalog/inhaler/{config_id}/", patch_data, format="json")
        assert res_patch.status_code == 200
        assert res_patch.json()["total_actuations"] == 120
        assert res_patch.json()["total_active_content"] == "12000.0000"

    def test_destroy_inhaler_config(self):
        """15. Pharmacist can delete an inhaler configuration."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/inhaler/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        res_del = self.client.delete(f"/api/v2/catalog/inhaler/{config_id}/")
        assert res_del.status_code == 204
        assert not InhalerFormConfig.objects.filter(config_id=config_id).exists()
