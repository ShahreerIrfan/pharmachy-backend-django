"""
backend/tests/test_p147_drops_editor.py
Architecture Decision: ADR-0147 (P147).
Test suite for Ophthalmic, Otic, and Nasal Drops Formulation Editor.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    DropsContainerType,
    DropsFormConfig,
    DropsRoute,
    DropsStorageTemp,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestDropsEditorIntegration:
    """Integration test suite for P147 Drops Editor."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p147-a-{self.suffix}",
            name=f"Square Pharmacy {self.suffix}",
            subdomain=f"square-p147-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p147-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p147-b-{self.suffix}",
            name=f"Labaid Pharmacy {self.suffix}",
            subdomain=f"labaid-p147-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p147-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p147-susp-{self.suffix}",
            name=f"Suspended Care {self.suffix}",
            subdomain=f"suspended-p147-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p147-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        self.tenant_basic = Tenant.objects.create(
            tenant_id=f"tn-p147-basic-{self.suffix}",
            name=f"Basic Health {self.suffix}",
            subdomain=f"basic-p147-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_basic = Branch.objects.create(
            branch_id=f"br-p147-basic-{self.suffix}",
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
            product_id=f"med-moxiflox-{self.suffix}",
            brand_name="Moxiflox Eye Drops",
            generic_name="Moxifloxacin 0.5%",
            dosage_form="DROPS",
            strength="0.5%",
            mrp_unit=Decimal("150.00"),
            cost_unit=Decimal("120.00"),
        )

        self.valid_payload = {
            "product_id": self.product.product_id,
            "tenant_id": self.tenant_a.tenant_id,
            "route": DropsRoute.OPHTHALMIC,
            "container_type": DropsContainerType.DROPPER_BOTTLE,
            "volume_ml": "5.00",
            "concentration_value": "0.5000",
            "concentration_unit": "PERCENT_W_V",
            "is_preservative_free": True,
            "preservative_name": "",
            "opened_shelf_life_days": 28,
            "storage_temp": DropsStorageTemp.ROOM_15_25,
            "is_active": True,
        }

    def test_01_schema_endpoint_conformance(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        res = self.client.get("/api/v2/catalog/drops/schema/")
        assert res.status_code == 200
        data = res.json()
        assert data["dosage_form_family"] == "DROPS_OPHTHALMIC_OTIC_NASAL"
        assert "OPHTHALMIC" in [r["code"] for r in data["supported_routes"]]
        assert "DROPPER_BOTTLE" in [c["code"] for c in data["supported_container_types"]]
        assert "strips_per_box" in data["forbidden_fields"]

    def test_02_validate_endpoint_valid_payload(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        res = self.client.post("/api/v2/catalog/drops/validate/", self.valid_payload, format="json")
        assert res.status_code == 200
        data = res.json()
        assert data["valid"] is True
        assert data["validated_payload"]["opened_shelf_life_days"] == 28
        assert "চোখে" in data["validated_payload"]["patient_instructions_bn"]

    def test_03_reject_hidden_solid_fields(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        bad_payload = dict(self.valid_payload)
        bad_payload["strips_per_box"] = 10
        res = self.client.post("/api/v2/catalog/drops/validate/", bad_payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_HIDDEN_FIELD_VALUE_REJECTED"

    def test_04_invalid_drops_route_rejected(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        bad_payload = dict(self.valid_payload)
        bad_payload["route"] = "ORAL_SWALLOW"
        res = self.client.post("/api/v2/catalog/drops/validate/", bad_payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_DROPS_ROUTE"

    def test_05_invalid_volume_rejected(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        bad_payload = dict(self.valid_payload)
        bad_payload["volume_ml"] = "-5.00"
        res = self.client.post("/api/v2/catalog/drops/validate/", bad_payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_VOLUME"

    def test_06_invalid_concentration_rejected(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        bad_payload = dict(self.valid_payload)
        bad_payload["concentration_value"] = "0"
        res = self.client.post("/api/v2/catalog/drops/validate/", bad_payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_CONCENTRATION"

    def test_07_opened_shelf_life_range_validation(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        bad_payload = dict(self.valid_payload)
        bad_payload["opened_shelf_life_days"] = 90
        res = self.client.post("/api/v2/catalog/drops/validate/", bad_payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_INVALID_OPENED_SHELF_LIFE"

    def test_08_single_dose_minim_opened_expiry_rule(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        bad_payload = dict(self.valid_payload)
        bad_payload["container_type"] = DropsContainerType.SINGLE_DOSE_MINIM
        bad_payload["opened_shelf_life_days"] = 7
        res = self.client.post("/api/v2/catalog/drops/validate/", bad_payload, format="json")
        assert res.status_code == 422
        assert res.json()["error_code"] == "ERR_SINGLE_DOSE_MINIM_EXPIRY"

        # Corrected minim payload
        good_payload = dict(bad_payload)
        good_payload["opened_shelf_life_days"] = 1
        res2 = self.client.post("/api/v2/catalog/drops/validate/", good_payload, format="json")
        assert res2.status_code == 200
        assert res2.json()["validated_payload"]["opened_shelf_life_days"] == 1

    def test_09_cashier_mutation_blocked(self):
        self.client.force_authenticate(user=self.cashier_a)
        res = self.client.post("/api/v2/catalog/drops/", self.valid_payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_PERMISSION_DENIED"

    def test_10_cross_tenant_isolation(self):
        self.client.force_authenticate(user=self.pharmacist_a)
        cross_payload = dict(self.valid_payload)
        cross_payload["tenant_id"] = self.tenant_b.tenant_id
        res = self.client.post("/api/v2/catalog/drops/", cross_payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_CROSS_TENANT_VIOLATION"

    def test_11_suspended_tenant_blocked(self):
        self.client.force_authenticate(user=self.pharma_suspended)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_suspended.tenant_id
        res = self.client.post("/api/v2/catalog/drops/", payload, format="json")
        assert res.status_code == 403
        assert res.json().get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_12_basic_tier_unentitled(self):
        self.client.force_authenticate(user=self.pharma_basic)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_basic.tenant_id
        res = self.client.post("/api/v2/catalog/drops/", payload, format="json")
        assert res.status_code == 403
        assert res.json()["error_code"] == "ERR_SUBSCRIPTION_UNENTITLED"

    def test_13_global_monograph_creation_by_superadmin(self):
        self.client.force_authenticate(user=self.superadmin)
        global_payload = dict(self.valid_payload)
        global_payload["tenant_id"] = None
        res = self.client.post("/api/v2/catalog/drops/", global_payload, format="json")
        assert res.status_code == 201
        data = res.json()
        assert data["tenant_id"] is None
        assert data["route"] == "OPHTHALMIC"

    def test_14_crud_lifecycle_and_deletion(self):
        self.client.force_authenticate(user=self.owner_b)
        payload = dict(self.valid_payload)
        payload["tenant_id"] = self.tenant_b.tenant_id
        payload["route"] = DropsRoute.OTIC
        payload["opened_shelf_life_days"] = 14

        # Create
        create_res = self.client.post("/api/v2/catalog/drops/", payload, format="json")
        assert create_res.status_code == 201
        config_id = create_res.json()["config_id"]
        assert create_res.json()["route"] == "OTIC"

        # Read
        get_res = self.client.get(f"/api/v2/catalog/drops/{config_id}/")
        assert get_res.status_code == 200
        assert get_res.json()["config_id"] == config_id

        # Update
        patch_res = self.client.patch(
            f"/api/v2/catalog/drops/{config_id}/",
            {"opened_shelf_life_days": 21},
            format="json",
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["opened_shelf_life_days"] == 21

        # Delete
        del_res = self.client.delete(f"/api/v2/catalog/drops/{config_id}/")
        assert del_res.status_code == 204

        # Verify 404
        assert not DropsFormConfig.objects.filter(config_id=config_id).exists()
