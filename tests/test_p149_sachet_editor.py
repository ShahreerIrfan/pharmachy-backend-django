"""
backend/tests/test_p149_sachet_editor.py
Architecture Decision: ADR-0149 (P149).
Test suite for Sachet, Powder, and Reconstitution Granule Editor.
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
    PowderCategory,
    PowderDiluentType,
    PowderStorageCondition,
    SachetFormConfig,
    SachetRoute,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestSachetEditorIntegration:
    """Integration test suite for P149 Sachet/Powder Editor."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p149-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p149-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p149-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p149-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p149-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p149-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p149-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p149-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p149-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p149-basic-{self.suffix}",
            name=f"Basic Health {self.suffix}",
            subdomain=f"basic-p149-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p149-basic-{self.suffix}",
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
            product_id=f"med-ors-saline-{self.suffix}",
            brand_name="Orsaline-N Sachet",
            generic_name="Oral Rehydration Salts (WHO formula)",
            dosage_form="POWDER_SACHET",
            strength="10.25g",
            mrp_unit=Decimal("6.00"),
            cost_unit=Decimal("4.50"),
        )

        self.valid_payload = {
            "product_id": self.product.product_id,
            "tenant_id": self.tenant_a.tenant_id,
            "powder_category": PowderCategory.ORAL_REHYDRATION_SALTS,
            "administration_route": SachetRoute.ORAL,
            "grams_per_sachet": "10.25",
            "sachets_per_pack": 25,
            "total_pack_weight_grams": "256.25",
            "loose_sachet_sale_eligible": True,
            "requires_dissolution": True,
            "diluent_type": PowderDiluentType.POTABLE_WATER,
            "diluent_volume_ml": "500.00",
            "reconstituted_stability_hours": 24,
            "storage_condition": PowderStorageCondition.COOL_DRY_BELOW_30,
            "is_active": True,
        }

    def test_get_schema_success(self):
        """1. Schema returns supported categories, routes, diluents, and forbidden fields."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.get("/api/v2/catalog/sachets/schema/")
        assert response.status_code == 200
        data = response.json()
        assert data["dosage_form_family"] == "SACHET_POWDER_GRANULES"
        assert "strips_per_box" in data["forbidden_fields"]
        assert any(c["code"] == "ORAL_REHYDRATION_SALTS" for c in data["supported_categories"])
        assert any(r["code"] == "ORAL" for r in data["supported_routes"])

    def test_validate_valid_payload(self):
        """2. Valid ORS sachet payload passes non-destructive validation."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/sachets/validate/", self.valid_payload, format="json")
        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert "বিশুদ্ধ খাবার পানিতে" in response.json()["validated_payload"]["patient_instructions_bn"]

    def test_validate_rejects_solid_oral_tablet_attributes(self):
        """3. Zero-trust isolation against tablet/capsule packaging fields."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["strips_per_box"] = 10
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_validate_rejects_needle_gauge(self):
        """4. Prohibits injectable needle gauge attributes."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["needle_gauge"] = "25G"
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_validate_rejects_invalid_powder_category(self):
        """5. Invalid powder category is rejected."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["powder_category"] = "NASAL_SPRAY"
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_POWDER_CATEGORY"

    def test_validate_rejects_invalid_administration_route(self):
        """6. Rejects unsupported route (e.g. OPHTHALMIC or INTRAVENOUS)."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["administration_route"] = "OPHTHALMIC"
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_ADMINISTRATION_ROUTE"

    def test_validate_rejects_non_positive_grams_per_sachet(self):
        """7. Rejects grams_per_sachet <= 0."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["grams_per_sachet"] = "0.00"
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_GRAMS_PER_SACHET"

    def test_validate_rejects_out_of_bounds_sachets_per_pack(self):
        """8. Rejects sachets_per_pack < 1 or > 500."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["sachets_per_pack"] = 0
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_INVALID_SACHETS_PER_PACK"

    def test_validate_rejects_dimension_consistency_mismatch(self):
        """9. Rejects dimension mismatch: total pack weight != grams * sachets."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        # 10.25 * 25 = 256.25, mismatch with 300.00
        payload["total_pack_weight_grams"] = "300.00"
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_DIMENSION_CONSISTENCY_MISMATCH"

    def test_validate_enforces_diluent_volume_when_dissolution_required(self):
        """10. Requires diluent_volume_ml when requires_dissolution is True."""
        self.client.force_authenticate(user=self.pharmacist_a)
        payload = dict(self.valid_payload)
        payload["requires_dissolution"] = True
        payload["diluent_volume_ml"] = None
        response = self.client.post("/api/v2/catalog/sachets/validate/", payload, format="json")
        assert response.status_code == 422
        assert response.json()["error_code"] == "ERR_MISSING_DILUENT_VOLUME"

    def test_create_sachet_config_pharmacist_success(self):
        """11. Pharmacist authors sachet configuration with DB persistence."""
        self.client.force_authenticate(user=self.pharmacist_a)
        response = self.client.post("/api/v2/catalog/sachets/", self.valid_payload, format="json")
        assert response.status_code == 201
        data = response.json()
        assert data["powder_category"] == PowderCategory.ORAL_REHYDRATION_SALTS
        assert data["grams_per_sachet"] == "10.25"
        assert data["sachets_per_pack"] == 25
        assert data["loose_sachet_sale_eligible"] is True

        # DB persistence check
        cfg = SachetFormConfig.objects.get(config_id=data["config_id"])
        assert cfg.product_id == self.product.product_id
        assert cfg.tenant_id == self.tenant_a.tenant_id
        assert cfg.grams_per_sachet == Decimal("10.25")

    def test_cashier_cannot_create_or_update(self):
        """12. Cashier mutation is blocked with ERR_PERMISSION_DENIED."""
        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.post("/api/v2/catalog/sachets/", self.valid_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_PERMISSION_DENIED"

    def test_cross_tenant_violation_blocked(self):
        """13. Staff cannot modify sachet monograph configuration for another tenant."""
        self.client.force_authenticate(user=self.pharmacist_a)
        cross_payload = dict(self.valid_payload)
        cross_payload["tenant_id"] = self.tenant_b.tenant_id
        response = self.client.post("/api/v2/catalog/sachets/", cross_payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_CROSS_TENANT_VIOLATION"

    def test_suspended_tenant_write_blocked(self):
        """14. Suspended tenant cannot author or update configurations."""
        self.client.force_authenticate(user=self.pharma_suspended)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_suspended.tenant_id
        response = self.client.post("/api/v2/catalog/sachets/", payload, format="json")
        assert response.status_code == 403
        assert response.json().get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_basic_plan_cannot_author_custom_monograph(self):
        """15. Basic plan tenant is gated from custom monograph authoring."""
        self.client.force_authenticate(user=self.pharma_basic)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_basic.tenant_id
        response = self.client.post("/api/v2/catalog/sachets/", payload, format="json")
        assert response.status_code == 403
        assert response.json()["error_code"] == "ERR_SUBSCRIPTION_UNENTITLED"

    def test_partial_update_patch_success(self):
        """16. Pharmacist can update sachets per pack via PATCH."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/sachets/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        patch_data = {"sachets_per_pack": 50, "total_pack_weight_grams": "512.50"}
        res_patch = self.client.patch(f"/api/v2/catalog/sachets/{config_id}/", patch_data, format="json")
        assert res_patch.status_code == 200
        assert res_patch.json()["sachets_per_pack"] == 50
        assert res_patch.json()["total_pack_weight_grams"] == "512.50"

    def test_destroy_sachet_config(self):
        """17. Pharmacist can delete a sachet configuration."""
        self.client.force_authenticate(user=self.pharmacist_a)
        res_create = self.client.post("/api/v2/catalog/sachets/", self.valid_payload, format="json")
        config_id = res_create.json()["config_id"]

        res_del = self.client.delete(f"/api/v2/catalog/sachets/{config_id}/")
        assert res_del.status_code == 204
        assert not SachetFormConfig.objects.filter(config_id=config_id).exists()
