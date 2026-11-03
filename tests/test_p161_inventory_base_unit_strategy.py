"""
backend/tests/test_p161_inventory_base_unit_strategy.py
Architecture Decision: ADR-0161 (P161).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Inventory Base-Unit Strategy, Multi-Tier Packaging Conversions,
Indivisibility Rules, Decimal Valuation, and Cross-Tenant Boundaries.
"""

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    InventoryBaseUnitStrategy,
    MedicineProduct,
    PlanTier,
    StorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestInventoryBaseUnitStrategyIntegration:
    """Integration test suite for P161 Inventory Base-Unit Strategy."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p161-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"popular-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p161-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Active Tenant B (for cross-tenant tests)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p161-b-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p161-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p161-susp-{self.suffix}",
            name=f"Suspended Meds {self.suffix}",
            subdomain=f"susp-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p161-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Products
        self.product_tablet = MedicineProduct.objects.create(
            product_id=f"prod-tab-{self.suffix}",
            brand_name=f"Napa Extra {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            manufacturer="Beximco",
            cost_unit=Decimal("0.8000"),
            mrp_unit=Decimal("1.2000"),
        )
        self.product_vial = MedicineProduct.objects.create(
            product_id=f"prod-vial-{self.suffix}",
            brand_name=f"Rocephin 1g {self.suffix}",
            generic_name="Ceftriaxone",
            dosage_form="Injection",
            manufacturer="Roche",
            cost_unit=Decimal("150.0000"),
            mrp_unit=Decimal("190.0000"),
        )

        # 5. Users
        self.user_pharmacist_a = PharmacyUser.objects.create_user(
            email=f"pharma_a_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacist A",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_pharmacist_b = PharmacyUser.objects.create_user(
            email=f"pharma_b_{self.suffix}@lazz.test",
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
            full_name="Suspended Staff",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

    def test_create_valid_tablet_strategy_and_convert(self):
        """1. Valid scenario: Configures tablet strategy and executes multi-tier conversion with exact valuation."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        payload = {
            "product_id": self.product_tablet.product_id,
            "branch_id": self.branch_a.branch_id,
            "base_unit": "TABLET",
            "is_indivisible": True,
            "allow_decimal_quantities": False,
            "decimal_places_allowed": 0,
            "minimum_dispense_quantity": "1.0000",
            "pack_unit_label": "BOX",
            "intermediate_unit_label": "STRIP",
            "pack_to_intermediate_multiplier": 10,
            "intermediate_to_base_multiplier": 10,
            "total_base_units_per_pack": 100,
            "cost_per_base_unit": "0.8000",
            "mrp_per_base_unit": "1.2000",
        }

        # 1a. Create strategy
        create_resp = self.client.post("/api/v2/inventory/base-unit-strategies/", payload, format="json")
        assert create_resp.status_code == status.HTTP_201_CREATED
        strategy_id = create_resp.data["strategy_id"]
        assert create_resp.data["total_base_units_per_pack"] == 100

        # 1b. Convert 2 PACKS -> 200 base tablets
        conv_resp = self.client.post(
            f"/api/v2/inventory/base-unit-strategies/{strategy_id}/convert/",
            {"quantity": "2", "unit_tier": "PACK"},
            format="json",
        )
        assert conv_resp.status_code == status.HTTP_200_OK
        data = conv_resp.data
        assert data["base_unit_quantity"] == 200
        assert data["cost_valuation_bdt"] == "160.0000"  # 200 * 0.80
        assert data["mrp_valuation_bdt"] == "240.0000"   # 200 * 1.20

        # 1c. Convert 3 STRIPS -> 30 base tablets
        strip_resp = self.client.post(
            f"/api/v2/inventory/base-unit-strategies/{strategy_id}/convert/",
            {"quantity": "3", "unit_tier": "INTERMEDIATE"},
            format="json",
        )
        assert strip_resp.status_code == status.HTTP_200_OK
        assert strip_resp.data["base_unit_quantity"] == 30
        assert strip_resp.data["cost_valuation_bdt"] == "24.0000"

    def test_decompose_reversal_unpacking(self):
        """2. Valid scenario: Reversal / decomposition of 125 base units into packs, strips, loose pieces."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        strategy = InventoryBaseUnitStrategy.objects.create(
            strategy_id=f"ibus-dec-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_tablet,
            base_unit="TABLET",
            is_indivisible=True,
            pack_unit_label="BOX",
            intermediate_unit_label="STRIP",
            pack_to_intermediate_multiplier=10,
            intermediate_to_base_multiplier=10,
            total_base_units_per_pack=100,
            cost_per_base_unit=Decimal("0.8000"),
            mrp_per_base_unit=Decimal("1.2000"),
        )

        response = self.client.post(
            f"/api/v2/inventory/base-unit-strategies/{strategy.strategy_id}/decompose/",
            {"total_base_units": 125},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert data["total_base_units"] == 125
        assert data["packs"] == 1          # 1 * 100
        assert data["intermediate"] == 2   # 2 * 10
        assert data["loose_base_units"] == 5  # 5 loose
        assert "1 * 100 + 2 * 10 + 5 = 125" in data["exact_recon_formula"]

    def test_indivisible_fractional_conversion_rejected(self):
        """3. Invalid scenario: Fractional conversion on indivisible vial is strictly rejected with 422."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        strategy = InventoryBaseUnitStrategy.objects.create(
            strategy_id=f"ibus-vial-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_vial,
            base_unit="VIAL",
            is_indivisible=True,
            pack_unit_label="BOX",
            intermediate_unit_label="TRAY",
            pack_to_intermediate_multiplier=1,
            intermediate_to_base_multiplier=5,
            total_base_units_per_pack=5,
            cost_per_base_unit=Decimal("150.0000"),
            mrp_per_base_unit=Decimal("190.0000"),
        )

        # 0.5 box of 5 vials = 2.5 vials -> fractional piece on indivisible item
        response = self.client.post(
            f"/api/v2/inventory/base-unit-strategies/{strategy.strategy_id}/convert/",
            {"quantity": "0.5", "unit_tier": "PACK"},
            format="json",
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data["error_code"] == "ERR_INDIVISIBLE_UNIT_FRACTION"

    def test_validate_dispense_quantity_indivisible_and_min(self):
        """4. Invalid scenario: Dispense quantity validation rejects below-min and fractional steps."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        strategy = InventoryBaseUnitStrategy.objects.create(
            strategy_id=f"ibus-val-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_vial,
            base_unit="VIAL",
            is_indivisible=True,
            minimum_dispense_quantity=Decimal("1.0000"),
            cost_per_base_unit=Decimal("150.0000"),
            mrp_per_base_unit=Decimal("190.0000"),
        )

        # 4a. Fractional quantity 1.5 on indivisible item
        resp_frac = self.client.post(
            f"/api/v2/inventory/base-unit-strategies/{strategy.strategy_id}/validate-quantity/",
            {"quantity": "1.5"},
            format="json",
        )
        assert resp_frac.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp_frac.data["error_code"] == "ERR_INDIVISIBLE_UNIT_FRACTION"

        # 4b. Below minimum quantity 0.5
        resp_below = self.client.post(
            f"/api/v2/inventory/base-unit-strategies/{strategy.strategy_id}/validate-quantity/",
            {"quantity": "0.5"},
            format="json",
        )
        assert resp_below.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp_below.data["error_code"] == "ERR_BELOW_MINIMUM_DISPENSE_QTY"

    def test_invalid_multiplier_math_rejected(self):
        """5. Invalid scenario: Declared total pack base units does not match multipliers."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        payload = {
            "product_id": self.product_tablet.product_id,
            "base_unit": "TABLET",
            "pack_to_intermediate_multiplier": 10,
            "intermediate_to_base_multiplier": 10,
            "total_base_units_per_pack": 150,  # 10 * 10 is 100, not 150
            "cost_per_base_unit": "1.0000",
            "mrp_per_base_unit": "1.5000",
        }

        response = self.client.post("/api/v2/inventory/base-unit-strategies/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error_code"] == "ERR_INVALID_UNIT_MULTIPLIER"

    def test_mrp_below_cost_rejected(self):
        """6. Invalid scenario: MRP lower than cost is rejected."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        payload = {
            "product_id": self.product_tablet.product_id,
            "base_unit": "TABLET",
            "pack_to_intermediate_multiplier": 1,
            "intermediate_to_base_multiplier": 1,
            "total_base_units_per_pack": 1,
            "cost_per_base_unit": "10.0000",
            "mrp_per_base_unit": "8.0000",  # MRP < Cost
        }

        response = self.client.post("/api/v2/inventory/base-unit-strategies/", payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error_code"] == "ERR_MRP_BELOW_COST"

    def test_cashier_role_blocked_from_creation(self):
        """7. Unauthorized scenario: Cashier role blocked from strategy configuration with 403."""
        self.client.force_authenticate(user=self.user_cashier_a)

        payload = {
            "product_id": self.product_tablet.product_id,
            "base_unit": "TABLET",
            "cost_per_base_unit": "1.0000",
            "mrp_per_base_unit": "1.5000",
        }

        response = self.client.post("/api/v2/inventory/base-unit-strategies/", payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_cross_tenant_isolation(self):
        """8. Cross-tenant scenario: Tenant B cannot access or modify Tenant A's strategy."""
        strategy_a = InventoryBaseUnitStrategy.objects.create(
            strategy_id=f"ibus-cross-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_tablet,
            base_unit="TABLET",
            cost_per_base_unit=Decimal("1.0000"),
            mrp_per_base_unit=Decimal("1.5000"),
        )

        # Authenticate as Tenant B staff
        self.client.force_authenticate(user=self.user_pharmacist_b)

        # 8a. Retrieve across tenant boundary -> 404
        get_resp = self.client.get(f"/api/v2/inventory/base-unit-strategies/{strategy_a.strategy_id}/")
        assert get_resp.status_code == status.HTTP_404_NOT_FOUND
        assert get_resp.data["error_code"] == "ERR_STRATEGY_NOT_FOUND"

        # 8b. Convert across tenant boundary -> 404
        conv_resp = self.client.post(
            f"/api/v2/inventory/base-unit-strategies/{strategy_a.strategy_id}/convert/",
            {"quantity": "1", "unit_tier": "BASE"},
            format="json",
        )
        assert conv_resp.status_code == status.HTTP_404_NOT_FOUND

    def test_suspended_tenant_blocked(self):
        """9. Unauthorized scenario: Suspended tenant staff is blocked with 403."""
        self.client.force_authenticate(user=self.user_suspended)

        payload = {
            "product_id": self.product_tablet.product_id,
            "base_unit": "TABLET",
            "cost_per_base_unit": "1.0000",
            "mrp_per_base_unit": "1.5000",
        }

        response = self.client.post("/api/v2/inventory/base-unit-strategies/", payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

    def test_inventory_valuation_exact_decimal(self):
        """10. Financial precision: Calculates total inventory valuation in exact Decimal BDT."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        # Seed two batches with known base quantities and lot costs
        # Batch 1: 500 base units @ 0.8500 = 425.0000
        BatchStock.objects.create(
            batch_id=f"bat-val-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_tablet,
            batch_number=f"BN-VAL-1-{self.suffix}",
            quantity_base_units=500,
            expiry_date="2027-12-31",
            purchase_date="2026-01-10",
            cost_per_unit=Decimal("0.8500"),
            status=BatchStatus.ACTIVE,
            storage_condition=StorageCondition.ROOM_TEMPERATURE,
        )
        # Batch 2: 200 base units @ 0.9200 = 184.0000
        BatchStock.objects.create(
            batch_id=f"bat-val-2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_tablet,
            batch_number=f"BN-VAL-2-{self.suffix}",
            quantity_base_units=200,
            expiry_date="2028-06-30",
            purchase_date="2026-02-15",
            cost_per_unit=Decimal("0.9200"),
            status=BatchStatus.ACTIVE,
            storage_condition=StorageCondition.ROOM_TEMPERATURE,
        )
        # Total: 700 base units, 425.0000 + 184.0000 = 609.0000 BDT

        response = self.client.get(f"/api/v2/inventory/base-unit-strategies/valuation/?product_id={self.product_tablet.product_id}")
        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert data["total_base_units"] == 700
        assert data["total_cost_valuation_bdt"] == "609.0000"

