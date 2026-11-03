"""
backend/tests/test_p150_suppository_editor.py
Architecture Decision: ADR-0150 (P150).
Test suite for Suppository, Pessary, and Bougie Dosage-Form Editor.
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
    SuppositoryBaseType,
    SuppositoryFormConfig,
    SuppositoryRoute,
    SuppositoryStorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestSuppositoryEditorIntegration:
    """Integration test suite for P150 Suppository Editor."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p150-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p150-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p150-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p150-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p150-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p150-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p150-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p150-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p150-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p150-basic-{self.suffix}",
            name=f"Basic Health {self.suffix}",
            subdomain=f"basic-p150-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p150-basic-{self.suffix}",
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
            product_id=f"med-napa-supp-{self.suffix}",
            brand_name="Napa 250 Suppository",
            generic_name="Paracetamol 250mg",
            dosage_form="SUPPOSITORY",
            strength="250mg",
            mrp_unit=Decimal("12.00"),
            cost_unit=Decimal("9.50"),
        )

        self.valid_payload = {
            "product_id": self.product.product_id,
            "tenant_id": self.tenant_a.tenant_id,
            "suppository_base_type": SuppositoryBaseType.FATTY_BASE_COCOA_BUTTER,
            "administration_route": SuppositoryRoute.RECTAL,
            "strength_value": "250.0000",
            "strength_unit": "MG",
            "pieces_per_strip": 5,
            "strips_per_pack": 2,
            "total_pieces_per_pack": 10,
            "loose_piece_sale_eligible": True,
            "requires_refrigeration": True,
            "storage_condition": SuppositoryStorageCondition.REFRIGERATED_2_8,
            "is_active": True,
        }

    def test_get_schema_success(self):
        """1. Schema returns supported bases, routes, storage conditions, and forbidden fields."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.get("/api/v2/catalog/suppositories/schema/")
        assert response.status_code == 200
        data = response.json()
        assert data["dosage_form_family"] == "SUPPOSITORY_PESSARY"
        assert "tablets_per_strip" in data["forbidden_fields"]
        assert any(b["code"] == "FATTY_BASE_COCOA_BUTTER" for b in data["supported_bases"])
        assert any(r["code"] == "RECTAL" for r in data["supported_routes"])

    def test_validate_valid_payload(self):
        """2. Valid suppository payload passes non-destructive validation."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/suppositories/validate/", self.valid_payload, format="json")
        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert "মলদ্বারে" in response.json()["validated_payload"]["patient_instructions_bn"]

    def test_validate_rejects_solid_oral_attributes(self):
        """3. Zero-trust isolation against tablet/capsule attributes."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["tablets_per_strip"] = 10
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_validate_rejects_needle_gauge(self):
        """4. Prohibits injectable needle gauge attributes."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["needle_gauge"] = "25G"
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_validate_rejects_invalid_suppository_base(self):
        """5. Invalid suppository base type is rejected."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["suppository_base_type"] = "PETROLATUM_JELLY"
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_SUPPOSITORY_BASE"

    def test_validate_rejects_invalid_administration_route(self):
        """6. Rejects unsupported route (e.g. ORAL_SWALLOW)."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["administration_route"] = "ORAL_SWALLOW"
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_SUPPOSITORY_ROUTE"

    def test_validate_rejects_non_positive_strength(self):
        """7. Rejects strength_value <= 0."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["strength_value"] = "0.0000"
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_STRENGTH"

    def test_validate_rejects_out_of_bounds_pieces(self):
        """8. Rejects pieces_per_strip < 1 or > 50."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["pieces_per_strip"] = 0
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_PIECES_PER_STRIP"

    def test_validate_rejects_dimension_consistency_mismatch(self):
        """9. Rejects dimension mismatch: total pieces != pieces * strips."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        # 5 * 2 = 10, mismatch with 15
        payload["total_pieces_per_pack"] = 15
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_DIMENSION_CONSISTENCY_MISMATCH"

    def test_validate_enforces_melt_protection_when_refrigeration_required(self):
        """10. Requires cool/refrigerated storage when requires_refrigeration is True."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["requires_refrigeration"] = True
        payload["storage_condition"] = SuppositoryStorageCondition.COOL_BELOW_30
        response = self.client.post("/api/v2/catalog/suppositories/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_MELT_PROTECTION_STORAGE_REQUIRED"

    def test_create_suppository_config_pharmacist_success(self):
        """11. Pharmacist authors suppository configuration with DB persistence."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/suppositories/", self.valid_payload, format="json")
        assert response.status_code == 201
        data = response.json()
        assert data["suppository_base_type"] == SuppositoryBaseType.FATTY_BASE_COCOA_BUTTER
        assert data["pieces_per_strip"] == 5
        assert data["strips_per_pack"] == 2
        assert data["loose_piece_sale_eligible"] is True

        # DB persistence check
        cfg = SuppositoryFormConfig.objects.get(config_id=data["config_id"])
        assert cfg.product_id == self.product.product_id
        assert cfg.tenant_id == self.tenant_a.tenant_id
        assert cfg.strength_value == Decimal("250.0000")

    def test_cashier_cannot_create_or_update(self):
        """12. Cashier mutation is blocked with ERR_PERMISSION_DENIED."""
        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.post("/api/v2/catalog/suppositories/", self.valid_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_PERMISSION_DENIED"

    def test_cross_tenant_violation_blocked(self):
        """13. Staff cannot modify suppository monograph configuration for another tenant."""
        self.client.force_authenticate(user=self.pharmacist_a)
        cross_payload = dict(self.valid_payload)
        cross_payload["tenant_id"] = self.tenant_b.tenant_id
        response = self.client.post("/api/v2/catalog/suppositories/", cross_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_CROSS_TENANT_VIOLATION"

    def test_suspended_tenant_write_blocked(self):
        """14. Suspended tenant cannot author or update configurations."""
        self.client.force_authenticate(user=self.pharma_suspended)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_suspended.tenant_id
        response = self.client.post("/api/v2/catalog/suppositories/", payload, format="json")
        assert response.status_code == 403
        assert response.json().get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_basic_plan_cannot_author_custom_monograph(self):
        """15. Basic plan tenant is gated from custom monograph authoring."""
        self.client.force_authenticate(user=self.pharma_basic)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_basic.tenant_id
        response = self.client.post("/api/v2/catalog/suppositories/", payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_SUBSCRIPTION_UNENTITLED"

    def test_partial_update_patch_success(self):
        """16. Pharmacist can update strips per pack via PATCH."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/suppositories/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        patch_data = {"strips_per_pack": 4, "total_pieces_per_pack": 20}
        res_patch = self.client.patch(f"/api/v2/catalog/suppositories/{config_id}/", patch_data, format="json")
        assert res_patch.status_code == 200
        assert res_patch.json()["strips_per_pack"] == 4
        assert res_patch.json()["total_pieces_per_pack"] == 20

    def test_destroy_suppository_config(self):
        """17. Pharmacist can delete a suppository configuration."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/suppositories/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        res_del = self.client.delete(f"/api/v2/catalog/suppositories/{config_id}/")
        assert res_del.status_code == 204
        assert not SuppositoryFormConfig.objects.filter(config_id=config_id).exists()
