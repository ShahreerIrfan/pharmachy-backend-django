"""
backend/tests/test_p162_packaging_hierarchy_schema.py
Architecture Decision: ADR-0162 (P162).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for N-Tier Packaging Hierarchy Schema,
Tree Resolution, Effective-Dated Conversion Factors, Cycle Detection,
and Cross-Tenant Isolation.
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
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestPackagingHierarchySchemaIntegration:
    """Integration test suite for P162 Packaging Hierarchy Schema in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p162-a-{self.suffix}",
            name=f"Square Healthcare {self.suffix}",
            subdomain=f"square-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p162-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Principal Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Active Tenant B (for cross-tenant checks)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p162-b-{self.suffix}",
            name=f"Incepta Meds {self.suffix}",
            subdomain=f"incepta-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p162-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Secondary Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p162-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p162-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Products
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name=f"Napa Extra {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            cost_unit=Decimal("0.8000"),
            mrp_unit=Decimal("1.2000"),
        )
        self.product_syrup = MedicineProduct.objects.create(
            product_id=f"prod-syrup-{self.suffix}",
            brand_name=f"Tofen Syrup {self.suffix}",
            generic_name="Ketotifen",
            dosage_form="Syrup",
            cost_unit=Decimal("55.0000"),
            mrp_unit=Decimal("70.0000"),
        )

        # 5. Users
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
            email=f"pharma_b_{self.suffix}@incepta.test",
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

    def test_create_3tier_packaging_tree_and_resolve(self):
        """1. Valid scenario: Configures 3-tier tree (Carton -> Box -> Strip -> Tablet) and resolves cumulative multipliers."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        # 1a. Create Tier 3: CARTON -> BOX (x10)
        c_to_b = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "CARTON",
            "child_unit": "BOX",
            "conversion_factor": 10,
            "tier_level": 3,
            "effective_from": "2026-01-01",
        }, format="json")
        assert c_to_b.status_code == status.HTTP_201_CREATED

        # 1b. Create Tier 2: BOX -> STRIP (x10)
        b_to_s = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "BOX",
            "child_unit": "STRIP",
            "conversion_factor": 10,
            "tier_level": 2,
            "effective_from": "2026-01-01",
        }, format="json")
        assert b_to_s.status_code == status.HTTP_201_CREATED

        # 1c. Create Tier 1: STRIP -> TABLET (x10)
        s_to_t = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "STRIP",
            "child_unit": "TABLET",
            "conversion_factor": 10,
            "tier_level": 1,
            "is_base_level": True,
            "effective_from": "2026-01-01",
        }, format="json")
        assert s_to_t.status_code == status.HTTP_201_CREATED

        # 1d. Resolve Tree
        tree_resp = self.client.get(
            f"/api/v2/inventory/packaging-hierarchy/tree/?product_id={self.product_napa.product_id}&as_of_date=2026-06-01"
        )
        assert tree_resp.status_code == status.HTTP_200_OK
        data = tree_resp.data
        assert data["node_count"] == 3
        assert data["base_unit"] == "TABLET"
        assert data["cumulative_factors"]["CARTON"] == 1000  # 10 * 10 * 10
        assert data["cumulative_factors"]["BOX"] == 100       # 10 * 10
        assert data["cumulative_factors"]["STRIP"] == 10      # 10
        assert data["cumulative_factors"]["TABLET"] == 1

    def test_convert_packaging_tiers(self):
        """2. Valid scenario: Converts 2 Cartons to Strips and 5 Boxes to Tablets."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        # Setup nodes directly
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-c-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="CARTON",
            child_unit="BOX",
            conversion_factor=10,
            tier_level=3,
            effective_from="2026-01-01",
        )
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-b-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=10,
            tier_level=2,
            effective_from="2026-01-01",
        )
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-s-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="STRIP",
            child_unit="TABLET",
            conversion_factor=10,
            tier_level=1,
            is_base_level=True,
            effective_from="2026-01-01",
        )

        # 2a. 2 Cartons -> Strips: (2 * 1000) / 10 = 200
        conv1 = self.client.post("/api/v2/inventory/packaging-hierarchy/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "CARTON",
            "to_unit": "STRIP",
            "quantity": "2",
            "as_of_date": "2026-06-01",
        }, format="json")
        assert conv1.status_code == status.HTTP_200_OK
        assert conv1.data["converted_quantity"] == "200"

        # 2b. 5 Boxes -> Tablets: (5 * 100) / 1 = 500
        conv2 = self.client.post("/api/v2/inventory/packaging-hierarchy/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "BOX",
            "to_unit": "TABLET",
            "quantity": "5",
            "as_of_date": "2026-06-01",
        }, format="json")
        assert conv2.status_code == status.HTTP_200_OK
        assert conv2.data["converted_quantity"] == "500"

    def test_effective_date_switching(self):
        """3. Valid scenario: Historical factor (10 strips/box) vs new factor (20 strips/box) based on as_of_date."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        # Era 1: 2025-01-01 to 2026-05-31: 1 Box = 10 Strips
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-old-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=10,
            tier_level=2,
            effective_from="2025-01-01",
            effective_to="2026-05-31",
        )
        # Era 2: 2026-06-01 onwards: 1 Box = 20 Strips
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-new-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=20,
            tier_level=2,
            effective_from="2026-06-01",
        )
        # Strip to tablet constant x10
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-str-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="STRIP",
            child_unit="TABLET",
            conversion_factor=10,
            tier_level=1,
            is_base_level=True,
            effective_from="2025-01-01",
        )

        # Query in Era 1 (2026-05-15): 1 Box -> 100 Tablets
        res_old = self.client.post("/api/v2/inventory/packaging-hierarchy/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "BOX",
            "to_unit": "TABLET",
            "quantity": "1",
            "as_of_date": "2026-05-15",
        }, format="json")
        assert res_old.status_code == status.HTTP_200_OK
        assert res_old.data["converted_quantity"] == "100"

        # Query in Era 2 (2026-07-01): 1 Box -> 200 Tablets
        res_new = self.client.post("/api/v2/inventory/packaging-hierarchy/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "BOX",
            "to_unit": "TABLET",
            "quantity": "1",
            "as_of_date": "2026-07-01",
        }, format="json")
        assert res_new.status_code == status.HTTP_200_OK
        assert res_new.data["converted_quantity"] == "200"

    def test_overlapping_effective_date_range_rejected(self):
        """4. Invalid scenario: Overlapping active date range for same parent and child is rejected with 400."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        # Existing node: 2026-01-01 to 2026-12-31
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-ex-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=10,
            effective_from="2026-01-01",
            effective_to="2026-12-31",
        )

        # Overlapping attempt: 2026-06-01 to 2027-06-01
        response = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "BOX",
            "child_unit": "STRIP",
            "conversion_factor": 15,
            "effective_from": "2026-06-01",
            "effective_to": "2027-06-01",
        }, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error_code"] == "ERR_OVERLAPPING_PACKAGING_FACTOR"

    def test_self_cycle_rejected(self):
        """5. Invalid scenario: Parent unit equal to child unit (BOX -> BOX) is rejected with 400."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        response = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "BOX",
            "child_unit": "BOX",
            "conversion_factor": 1,
            "effective_from": "2026-01-01",
        }, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error_code"] == "ERR_PACKAGING_CYCLE_SELF"

    def test_circular_loop_rejected(self):
        """6. Invalid scenario: Circular hierarchy loop (TABLET -> CARTON when CARTON -> BOX -> TABLET exists) is rejected."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-loop1-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="CARTON",
            child_unit="BOX",
            conversion_factor=10,
            effective_from="2026-01-01",
        )
        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-loop2-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="TABLET",
            conversion_factor=100,
            effective_from="2026-01-01",
        )

        # Attempt to create edge TABLET -> CARTON
        response = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "TABLET",
            "child_unit": "CARTON",
            "conversion_factor": 1,
            "effective_from": "2026-01-01",
        }, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["error_code"] == "ERR_PACKAGING_CYCLE_DETECTED"

    def test_unlinked_tier_conversion_rejected(self):
        """7. Invalid scenario: Converting between unlinked tiers returns 422 with ERR_UNLINKED_PACKAGING_TIER."""
        self.client.force_authenticate(user=self.user_pharmacist_a)

        PackagingHierarchyNode.objects.create(
            node_id=f"pkn-b2s-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=10,
            effective_from="2026-01-01",
        )

        # Try to convert BOTTLE to STRIP (BOTTLE is unlinked)
        response = self.client.post("/api/v2/inventory/packaging-hierarchy/convert/", {
            "product_id": self.product_napa.product_id,
            "from_unit": "BOTTLE",
            "to_unit": "STRIP",
            "quantity": "1",
        }, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data["error_code"] == "ERR_UNLINKED_PACKAGING_TIER"

    def test_cashier_role_blocked_from_creation(self):
        """8. Unauthorized scenario: Cashier role blocked from creating packaging node with 403."""
        self.client.force_authenticate(user=self.user_cashier_a)

        response = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "BOX",
            "child_unit": "STRIP",
            "conversion_factor": 10,
            "effective_from": "2026-01-01",
        }, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_cross_tenant_isolation(self):
        """9. Cross-tenant scenario: Tenant B cannot access or modify Tenant A's hierarchy nodes."""
        node_a = PackagingHierarchyNode.objects.create(
            node_id=f"pkn-cross-{self.suffix}",
            tenant=self.tenant_a,
            product=self.product_napa,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=10,
            effective_from="2026-01-01",
        )

        self.client.force_authenticate(user=self.user_pharmacist_b)

        # 9a. Retrieve cross-tenant -> 404
        get_resp = self.client.get(f"/api/v2/inventory/packaging-hierarchy/{node_a.node_id}/")
        assert get_resp.status_code == status.HTTP_404_NOT_FOUND
        assert get_resp.data["error_code"] == "ERR_NODE_NOT_FOUND"

        # 9b. Delete cross-tenant -> 404
        del_resp = self.client.delete(f"/api/v2/inventory/packaging-hierarchy/{node_a.node_id}/")
        assert del_resp.status_code == status.HTTP_404_NOT_FOUND

    def test_suspended_tenant_blocked(self):
        """10. Unauthorized scenario: Suspended tenant staff is blocked with 403."""
        self.client.force_authenticate(user=self.user_suspended)

        response = self.client.post("/api/v2/inventory/packaging-hierarchy/", {
            "product_id": self.product_napa.product_id,
            "parent_unit": "BOX",
            "child_unit": "STRIP",
            "conversion_factor": 10,
            "effective_from": "2026-01-01",
        }, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data["error_code"] in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]

