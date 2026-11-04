"""
backend/tests/test_p169_fefo_algorithm.py
Architecture Decision: ADR-0169 (P169).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Deterministic FEFO Allocation with Expiry Blocks,
Quarantine Protections, Active Reservations, and Multi-Tier Tie-Breakers.
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    StockReservationStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestFefoAlgorithmIntegration:
    """Integration test suite for P169 FEFO Algorithm in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        # 1. Tenant A & Branch A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p169-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p169-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p169-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Tenant B & Branch B (cross-tenant tests)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p169-b-{self.suffix}",
            name=f"Beximco Pharmacy {self.suffix}",
            subdomain=f"beximco-p169-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p169-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p169-susp-{self.suffix}",
            name="Suspended Tenant",
            subdomain=f"susp-p169-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p169-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Users
        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier POS",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist_user = PharmacyUser.objects.create_user(
            email=f"pharmacist-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Staff Pharmacist",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.auditor_user = PharmacyUser.objects.create_user(
            email=f"auditor-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Auditor User",
            password="Password123!",
            role=UserRole.AUDITOR,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.user_b = PharmacyUser.objects.create_user(
            email=f"userb-{self.suffix}@pharmab.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="User B Manager",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        self.user_susp = PharmacyUser.objects.create_user(
            email=f"susp-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended User",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

        # 5. Product
        self.product = MedicineProduct.objects.create(
            product_id=f"med-p169-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.0000"),
            mrp_unit=Decimal("2.5000"),
        )

        # 6. Batches with distinct expiry and purchase dates
        self.batch_expiring_soon = BatchStock.objects.create(
            batch_id=f"bat-soon-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BX-SOON-{self.suffix}",
            quantity_base_units=100,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("2.00"),
            purchase_date=self.today - datetime.timedelta(days=10),
            expiry_date=self.today + datetime.timedelta(days=30),  # Expiring soonest
            status=BatchStatus.ACTIVE,
        )

        self.batch_expiring_later = BatchStock.objects.create(
            batch_id=f"bat-later-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BX-LATER-{self.suffix}",
            quantity_base_units=200,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("2.10"),
            purchase_date=self.today - datetime.timedelta(days=5),
            expiry_date=self.today + datetime.timedelta(days=120),  # Expiring later
            status=BatchStatus.ACTIVE,
        )

        self.batch_expired = BatchStock.objects.create(
            batch_id=f"bat-exp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BX-EXP-{self.suffix}",
            quantity_base_units=500,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("1.80"),
            purchase_date=self.today - datetime.timedelta(days=100),
            expiry_date=self.today - datetime.timedelta(days=2),  # Expired
            status=BatchStatus.EXPIRED,
        )

        self.batch_quarantined = BatchStock.objects.create(
            batch_id=f"bat-quar-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BX-QUAR-{self.suffix}",
            quantity_base_units=300,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("2.00"),
            purchase_date=self.today - datetime.timedelta(days=15),
            expiry_date=self.today + datetime.timedelta(days=60),
            status=BatchStatus.ACTIVE,
            is_quarantined=True,  # Quarantined
        )

        self.batch_recalled = BatchStock.objects.create(
            batch_id=f"bat-rec-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BX-REC-{self.suffix}",
            quantity_base_units=400,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("2.00"),
            purchase_date=self.today - datetime.timedelta(days=15),
            expiry_date=self.today + datetime.timedelta(days=90),
            status=BatchStatus.RECALLED,  # Recalled
            recall_reason="DGDA Notice 2026-99",
        )

    def test_01_preview_fefo_batches_deterministic_order(self):
        """Previews eligible FEFO batches in strict deterministic ordering."""
        self.client.force_authenticate(user=self.cashier_user)

        res = self.client.get(
            f"/api/v2/inventory/fefo/batches-preview/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a.branch_id}&product_id={self.product.product_id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        batches = res.data
        # Expired, quarantined, and recalled batches must be excluded!
        assert len(batches) == 2
        # Earliest expiry first
        assert batches[0]["batch_id"] == self.batch_expiring_soon.batch_id
        assert batches[1]["batch_id"] == self.batch_expiring_later.batch_id

    def test_02_valid_fefo_allocation_single_batch(self):
        """Requested units fulfilled completely from earliest expiring batch."""
        self.client.force_authenticate(user=self.cashier_user)

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 60,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        assert res.data["is_fully_allocated"] is True
        assert res.data["allocated_quantity_base_units"] == 60
        assert len(res.data["allocations"]) == 1
        alloc = res.data["allocations"][0]
        assert alloc["batch_id"] == self.batch_expiring_soon.batch_id
        assert alloc["allocated_units"] == 60
        assert alloc["lot_cogs"] == "120.00"

    def test_03_valid_fefo_allocation_split_multiple_batches(self):
        """Splits allocation across batches when earliest batch is exhausted."""
        self.client.force_authenticate(user=self.cashier_user)

        # Request 150 units (batch_soon has 100, batch_later has 200)
        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 150,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        assert res.data["is_fully_allocated"] is True
        assert res.data["allocated_quantity_base_units"] == 150
        assert len(res.data["allocations"]) == 2
        # 1st: 100 units from batch_soon
        assert res.data["allocations"][0]["batch_id"] == self.batch_expiring_soon.batch_id
        assert res.data["allocations"][0]["allocated_units"] == 100
        # 2nd: 50 units from batch_later
        assert res.data["allocations"][1]["batch_id"] == self.batch_expiring_later.batch_id
        assert res.data["allocations"][1]["allocated_units"] == 50
        # Total COGS = 100*2.00 + 50*2.10 = 200.00 + 105.00 = 305.00
        assert Decimal(res.data["total_cogs"]) == Decimal("305.00")

    def test_04_tie_breaker_rules_identical_expiry(self):
        """Tie-breaker selects older purchase date when expiry dates match."""
        self.client.force_authenticate(user=self.cashier_user)

        tie_product = MedicineProduct.objects.create(
            product_id=f"med-tie-{self.suffix}",
            brand_name="Tie Test Product",
            dosage_form="Tablet",
            cost_unit=Decimal("1.5000"),
            mrp_unit=Decimal("2.0000"),
        )
        same_expiry = self.today + datetime.timedelta(days=90)

        # Batch 1: Older purchase date
        batch_older_purchase = BatchStock.objects.create(
            batch_id=f"bat-tie1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=tie_product,
            batch_number=f"BX-TIE-Z-{self.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("1.50"),
            purchase_date=self.today - datetime.timedelta(days=20),
            expiry_date=same_expiry,
            status=BatchStatus.ACTIVE,
        )

        # Batch 2: Newer purchase date
        BatchStock.objects.create(
            batch_id=f"bat-tie2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=tie_product,
            batch_number=f"BX-TIE-A-{self.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("1.50"),
            purchase_date=self.today - datetime.timedelta(days=5),
            expiry_date=same_expiry,
            status=BatchStatus.ACTIVE,
        )

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": tie_product.product_id,
                "requested_quantity_base_units": 30,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        assert res.data["allocations"][0]["batch_id"] == batch_older_purchase.batch_id

    def test_05_hard_block_expired_batches(self):
        """Expired batches cannot be allocated."""
        self.client.force_authenticate(user=self.cashier_user)

        exp_product = MedicineProduct.objects.create(
            product_id=f"med-onlyexp-{self.suffix}",
            brand_name="Expired Only Product",
            dosage_form="Tablet",
            cost_unit=Decimal("1.0000"),
            mrp_unit=Decimal("2.0000"),
        )
        BatchStock.objects.create(
            batch_id=f"bat-onlyexp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=exp_product,
            batch_number=f"BX-ONLYEXP-{self.suffix}",
            quantity_base_units=100,
            cost_per_unit=Decimal("1.00"),
            purchase_date=self.today - datetime.timedelta(days=50),
            expiry_date=self.today - datetime.timedelta(days=1),
            status=BatchStatus.EXPIRED,
        )

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": exp_product.product_id,
                "requested_quantity_base_units": 10,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_NO_ELIGIBLE_BATCHES"

    def test_06_insufficient_stock_returns_error(self):
        """Fails when requested quantity exceeds available dispensable stock."""
        self.client.force_authenticate(user=self.cashier_user)

        # Total available = 100 (batch_soon) + 200 (batch_later) = 300 units
        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 500,  # 500 > 300
            },
            format="json",
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_INSUFFICIENT_STOCK"
        assert int(res.data.get("available_quantity_base_units")) == 300

    def test_07_invalid_quantity_input(self):
        """Rejects zero or negative requested quantity."""
        self.client.force_authenticate(user=self.cashier_user)

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 0,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_INVALID_QUANTITY"

    def test_08_concurrency_reservation_lifecycle_reserve_commit_release(self):
        """Tests reserve -> commit and reserve -> release state transitions."""
        self.client.force_authenticate(user=self.cashier_user)

        # 1. Allocate with reserve=True
        res_alloc = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 40,
                "reserve": True,
                "reference_type": "POS_CART",
                "reference_id": "cart-1234",
            },
            format="json",
        )
        assert res_alloc.status_code == status.HTTP_200_OK, res_alloc.data
        resv_id = res_alloc.data["allocations"][0]["reservation_id"]
        assert resv_id is not None

        # Verify batch reserved quantity increased
        self.batch_expiring_soon.refresh_from_db()
        assert self.batch_expiring_soon.reserved_quantity_base_units == 40
        assert self.batch_expiring_soon.available_quantity_base_units == 60

        # 2. Commit Reservation
        res_commit = self.client.post(
            "/api/v2/inventory/fefo/commit-reservation/",
            {"tenant_id": self.tenant_a.tenant_id, "reservation_id": resv_id},
            format="json",
        )
        assert res_commit.status_code == status.HTTP_200_OK, res_commit.data
        assert res_commit.data["status"] == StockReservationStatus.COMMITTED

        # Verify stock and reservations decremented atomically
        self.batch_expiring_soon.refresh_from_db()
        assert self.batch_expiring_soon.quantity_base_units == 60
        assert self.batch_expiring_soon.reserved_quantity_base_units == 0
        assert self.batch_expiring_soon.available_quantity_base_units == 60

        # 3. New reservation and release
        res_alloc2 = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 20,
                "reserve": True,
            },
            format="json",
        )
        resv_id2 = res_alloc2.data["allocations"][0]["reservation_id"]
        self.batch_expiring_soon.refresh_from_db()
        assert self.batch_expiring_soon.reserved_quantity_base_units == 20

        # Release
        res_release = self.client.post(
            "/api/v2/inventory/fefo/release-reservation/",
            {"tenant_id": self.tenant_a.tenant_id, "reservation_id": resv_id2, "reason": "Customer cancelled"},
            format="json",
        )
        assert res_release.status_code == status.HTTP_200_OK, res_release.data
        assert res_release.data["status"] == StockReservationStatus.RELEASED

        self.batch_expiring_soon.refresh_from_db()
        assert self.batch_expiring_soon.reserved_quantity_base_units == 0
        assert self.batch_expiring_soon.available_quantity_base_units == 60

    def test_09_unblended_purchase_cost_lot_cogs_integration(self):
        """Allocations resolve purchase cost lots to provide unblended FIFO COGS."""
        self.client.force_authenticate(user=self.pharmacist_user)

        cost_lot = PurchaseCostLot.objects.create(
            lot_id=f"pcl-cogs-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_expiring_soon,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("2.0500"),
            initial_quantity=100,
            remaining_quantity=100,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 20,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        alloc = res.data["allocations"][0]
        assert alloc["cost_lot_id"] == cost_lot.lot_id
        assert alloc["unit_cost"] == "2.0500"
        assert alloc["lot_cogs"] == "41.0000"

    def test_10_rbac_unauthorized_role_blocked(self):
        """Auditor role cannot allocate or reserve inventory."""
        self.client.force_authenticate(user=self.auditor_user)

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 10,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert res.data.get("error_code") == "ERR_ROLE_UNAUTHORIZED"

    def test_11_cross_tenant_isolation_blocked(self):
        """Tenant B user cannot allocate Tenant A batches."""
        self.client.force_authenticate(user=self.user_b)

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 10,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        payload = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert payload.get("error_code") in ("ERR_TENANT_MISMATCH", "ERR_CROSS_TENANT_VIOLATION")

    def test_12_suspended_tenant_blocked(self):
        """Suspended tenant is blocked from FEFO operations."""
        self.client.force_authenticate(user=self.user_susp)

        res = self.client.post(
            "/api/v2/inventory/fefo/allocate/",
            {
                "tenant_id": self.tenant_suspended.tenant_id,
                "branch_id": self.branch_suspended.branch_id,
                "product_id": self.product.product_id,
                "requested_quantity_base_units": 10,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        payload = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert payload.get("error_code") in (
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_CROSS_TENANT_VIOLATION",
        )
