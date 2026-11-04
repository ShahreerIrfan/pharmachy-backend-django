"""
backend/tests/test_p163_multi_level_unit_conversion_api.py
Architecture Decision: ADR-0163 (P163).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Multi-Level Unit Conversion API,
Fractional Tablet Prevention, Dosing Unit Validation, and Cross-Tenant Boundaries.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PackagingHierarchyNode,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
    UnitConversionPolicy,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestMultiLevelUnitConversionApiIntegration:
    """Integration test suite for P163 Multi-Level Unit Conversion API in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p163-a-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"square-p163-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p163-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Principal Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Active Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p163-b-{self.suffix}",
            name=f"Beximco Meds {self.suffix}",
            subdomain=f"beximco-p163-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p163-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Branch B",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p163-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p163-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p163-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Products
        # 4a. Unsplit Tablet
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name=f"Napa Extra {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            cost_unit=Decimal("0.8000"),
            mrp_unit=Decimal("1.2000"),
        )
        # 4b. Scored/Splittable Tablet
        self.product_bisoprolol = MedicineProduct.objects.create(
            product_id=f"prod-biso-{self.suffix}",
            brand_name=f"Bisocor 5mg {self.suffix}",
            generic_name="Bisoprolol Fumarate",
            dosage_form="Tablet",
            cost_unit=Decimal("6.0000"),
            mrp_unit=Decimal("8.0000"),
        )
        # 4c. Injectable Vial
        self.product_vial = MedicineProduct.objects.create(
            product_id=f"prod-vial-{self.suffix}",
            brand_name=f"Rocephin 1g {self.suffix}",
            generic_name="Ceftriaxone",
            dosage_form="Injection",
            cost_unit=Decimal("150.0000"),
            mrp_unit=Decimal("190.0000"),
        )

        # 5. Packaging Hierarchy Nodes for Napa
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-c2b-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="CARTON",
            child_unit="BOX",
            conversion_factor=10,
            tier_level=3,
            effective_from="2026-01-01",
        )
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-b2s-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=10,
            tier_level=2,
            effective_from="2026-01-01",
        )
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-s2t-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="STRIP",
            child_unit="TABLET",
            conversion_factor=10,
            tier_level=1,
            is_base_level=True,
            effective_from="2026-01-01",
        )

        # 6. Users
        self.user_pharmacist_a = PharmacyUser.objects.create_user(
            email=f"pharma_a_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacist A",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.suffix}@square.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_pharmacist_b = PharmacyUser.objects.create_user(
            email=f"pharma_b_{self.suffix}@beximco.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacist B",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@suspended.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended User",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

    def test_multi_level_packaging_conversion_success(self):
        """1. Valid scenario: Multi-tier packaging conversion (2 Cartons -> 2,000 Tablets) with Decimal valuation."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        response = self.client.post("/api/v2/inventory/unit-conversion/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "CARTON",
            "to_unit": "TABLET",
            "quantity": "2",
        }, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert data["converted_quantity"] == "2000"
        assert data["cost_valuation_bdt"] == "1600.0000"  # 2000 * 0.80
        assert data["mrp_valuation_bdt"] == "2400.0000"   # 2000 * 1.20

    def test_fractional_tablet_blocked_on_unsplit_tablet(self):
        """2. Guardrail: Dispensing fractional amount of unsplit tablet is rejected with 422."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        # Configure policy: not splittable
        UnitConversionPolicy.objects.create(
            policy_id=f"ucp-napa-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            is_splittable=False,
            allowed_split_fractions=[1.0],
        )

        response = self.client.post("/api/v2/inventory/unit-conversion/validate/", {
            "product_id": self.product_napa.product_id,
            "unit": "TABLET",
            "quantity": "1.5",
        }, format="json")

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data["error_code"] == "ERR_FRACTIONAL_TABLET_NOT_ALLOWED"

    def test_splittable_tablet_allows_authorized_split_fraction(self):
        """3. Valid scenario: Scored tablet allows authorized half-tablet split (0.5 and 2.5)."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        UnitConversionPolicy.objects.create(
            policy_id=f"ucp-biso-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_bisoprolol,
            is_splittable=True,
            allowed_split_fractions=[0.5, 1.0],
        )

        # 3a. 0.5 tablet
        resp1 = self.client.post("/api/v2/inventory/unit-conversion/validate/", {
            "product_id": self.product_bisoprolol.product_id,
            "unit": "TABLET",
            "quantity": "0.5",
        }, format="json")
        assert resp1.status_code == status.HTTP_200_OK
        assert resp1.data["is_valid"] is True

        # 3b. 2.5 tablets
        resp2 = self.client.post("/api/v2/inventory/unit-conversion/validate/", {
            "product_id": self.product_bisoprolol.product_id,
            "unit": "TABLET",
            "quantity": "2.5",
        }, format="json")
        assert resp2.status_code == status.HTTP_200_OK
        assert resp2.data["is_valid"] is True

    def test_splittable_tablet_rejects_unauthorized_fractional_step(self):
        """4. Guardrail: Scored tablet with half-split rejects arbitrary split (0.25)."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        UnitConversionPolicy.objects.create(
            policy_id=f"ucp-biso-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_bisoprolol,
            is_splittable=True,
            allowed_split_fractions=[0.5, 1.0],
        )

        response = self.client.post("/api/v2/inventory/unit-conversion/validate/", {
            "product_id": self.product_bisoprolol.product_id,
            "unit": "TABLET",
            "quantity": "0.25",
        }, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data["error_code"] == "ERR_INVALID_TABLET_SPLIT"

    def test_incompatible_dosing_unit_conversion_rejected(self):
        """5. Guardrail: Converting Tablet to ML or PUFF is strictly rejected with 422."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        # 5a. Convert TABLET to ML
        resp_ml = self.client.post("/api/v2/inventory/unit-conversion/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "TABLET",
            "to_unit": "ML",
            "quantity": "10",
        }, format="json")
        assert resp_ml.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp_ml.data["error_code"] == "ERR_INCOMPATIBLE_DOSING_UNIT"

        # 5b. Validate dispense unit PUFF for Tablet
        resp_puff = self.client.post("/api/v2/inventory/unit-conversion/validate/", {
            "product_id": self.product_napa.product_id,
            "unit": "PUFF",
            "quantity": "2",
        }, format="json")
        assert resp_puff.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp_puff.data["error_code"] == "ERR_INCOMPATIBLE_DOSING_UNIT"

    def test_clinical_dosing_unit_potency_calculation(self):
        """6. Clinical calculation: Tablets to active substance MG calculates potency without stock drift."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        UnitConversionPolicy.objects.create(
            policy_id=f"ucp-pot-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            is_splittable=False,
            clinical_dosing_unit="MG",
            dosing_conversion_ratio=Decimal("500.0000"),
        )

        response = self.client.post("/api/v2/inventory/unit-conversion/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "TABLET",
            "to_unit": "MG",
            "quantity": "2",
        }, format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert data["is_clinical_dosing_calculation"] is True
        assert data["converted_quantity"] == "1000.0000"
        assert data["base_units_consumed"] == "2"

    def test_get_allowed_units_metadata(self):
        """7. Introspection: GET /allowed-units/ returns packaging, base, and prohibited units."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        response = self.client.get(
            f"/api/v2/inventory/unit-conversion/allowed-units/?product_id={self.product_napa.product_id}"
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert "BOX" in data["packaging_units"]
        assert "STRIP" in data["packaging_units"]
        assert "TABLET" in data["packaging_units"]
        assert data["base_unit"] == "TABLET"
        assert "ML" in data["prohibited_units"]
        assert "PUFF" in data["prohibited_units"]

    def test_cashier_role_blocked_from_policy_configuration(self):
        """8. Unauthorized scenario: Cashier blocked from configuring unit conversion policy with 403."""
        self.client.force_authenticate(user=self.user_cashier_a)

        response = self.client.post("/api/v2/inventory/unit-conversion/", {
            "product_id": self.product_napa.product_id,
            "is_splittable": True,
            "allowed_split_fractions": [0.5, 1.0],
        }, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_cross_tenant_isolation(self):
        """9. Cross-tenant scenario: Tenant B cannot access or modify Tenant A's conversion policy."""
        policy_a = UnitConversionPolicy.objects.create(
            policy_id=f"ucp-cross-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            is_splittable=False,
        )

        self.client.force_authenticate(user=self.user_pharmacist_b)

        # 9a. Retrieve cross-tenant -> 404
        get_resp = self.client.get(f"/api/v2/inventory/unit-conversion/{policy_a.policy_id}/")
        assert get_resp.status_code == status.HTTP_404_NOT_FOUND
        assert get_resp.data["error_code"] == "ERR_POLICY_NOT_FOUND"

        # 9b. Delete cross-tenant -> 404
        del_resp = self.client.delete(f"/api/v2/inventory/unit-conversion/{policy_a.policy_id}/")
        assert del_resp.status_code == status.HTTP_404_NOT_FOUND

    def test_suspended_tenant_blocked(self):
        """10. Unauthorized scenario: Suspended tenant staff blocked from configuring policy with 403."""
        self.client.force_authenticate(user=self.user_suspended)

        response = self.client.post("/api/v2/inventory/unit-conversion/", {
            "product_id": self.product_napa.product_id,
            "is_splittable": False,
        }, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

