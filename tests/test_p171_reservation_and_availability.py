"""
backend/tests/test_p171_reservation_and_availability.py
Architecture Decision: ADR-0171 (P171).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P171 multi-tier stock availability matrix:
- Distinguishing on-hand, reserved, quarantined, pending, physically available, and server-confirmed stock
- Stock conservation invariant: server_confirmed + reserved + quarantined == on_hand
- Packaging unit conversions (Box, Strip, Tablet)
- Expiry and DGDA recall quarantine guards
- Real-time POS cart pre-checkout checks
- Concurrency reservation and release synchronization
- Administrative matrix refresh and audit logging
- Role-based authorization, cross-tenant isolation, and suspended subscription blocks
"""

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
    BranchStockAvailability,
    GoodsReceiptLine,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    InventoryBaseUnitStrategy,
    MedicineProduct,
    StockReservation,
    StockReservationStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestStockAvailabilityIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.unique_id = uuid.uuid4().hex[:6]

        # 1. Setup Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-avail-{self.unique_id}",
            name="Dhanmondi Central Pharmacy",
            subdomain=f"dhanmondi-avail-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-avail-{self.unique_id}",
            tenant=self.tenant,
            name="Main Branch",
        )

        # 2. Setup Cashier and Manager Users
        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier_{self.unique_id}@pharmacy.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Cashier POS",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.manager_user = PharmacyUser.objects.create_user(
            email=f"manager_{self.unique_id}@pharmacy.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        # 3. Setup Product & Packaging Strategy
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.unique_id}",
            brand_name="Napa 500mg Tablet",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="500mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("2.5000"),
            cost_unit=Decimal("1.8000"),
        )
        self.strategy = InventoryBaseUnitStrategy.objects.create(
            strategy_id=f"strat-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            base_unit="TABLET",
            pack_unit_label="BOX",
            intermediate_unit_label="STRIP",
            pack_to_intermediate_multiplier=10,
            intermediate_to_base_multiplier=10,
            total_base_units_per_pack=100,
            is_active=True,
        )

        # 4. Setup Batches:
        # Batch 1: Active, 100 units, 20 reserved, expiry 1 year later
        self.batch1 = BatchStock.objects.create(
            batch_id=f"bat-1-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-2026-001",
            quantity_base_units=100,
            reserved_quantity_base_units=20,
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("1.8000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

        # Batch 2: Active, 50 units, 0 reserved, expiry 1 year later
        self.batch2 = BatchStock.objects.create(
            batch_id=f"bat-2-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-2026-002",
            quantity_base_units=50,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=400),
            purchase_date=self.today - datetime.timedelta(days=5),
            cost_per_unit=Decimal("1.8000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

        # Batch 3: Quarantined, 30 units
        self.batch3 = BatchStock.objects.create(
            batch_id=f"bat-3-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-2026-003-Q",
            quantity_base_units=30,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=500),
            purchase_date=self.today - datetime.timedelta(days=20),
            cost_per_unit=Decimal("1.7500"),
            status=BatchStatus.QUARANTINED,
            is_quarantined=True,
            is_active=True,
        )

        # 5. Setup Pending Goods Receipt Note (GRN) with 40 units ordered
        self.grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            receipt_date=self.today,
            status=GoodsReceiptStatus.RECEIVED,
        )
        self.grn_line = GoodsReceiptLine.objects.create(
            line_id=f"line-{self.unique_id}",
            tenant=self.tenant,
            grn=self.grn,
            product=self.product,
            ordered_quantity_base_units=40,
            received_quantity_base_units=40,
            unit_cost=Decimal("1.8000"),
        )

    def test_01_stock_matrix_invariants_and_quantities(self):
        """
        Validates that all 6 quantities adhere to statutory definitions and stock conservation:
        on_hand (180) = server_confirmed (130) + reserved (20) + quarantined (30).
        """
        self.client.force_authenticate(user=self.cashier_user)
        url = (
            f"/api/v2/inventory/availability/product-details/"
            f"?branch_id={self.branch.branch_id}&product_id={self.product.product_id}"
        )
        res = self.client.get(url)
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert data["quantity_on_hand"] == 180
        assert data["quantity_reserved"] == 20
        assert data["quantity_quarantined"] == 30
        assert data["quantity_pending_inward"] == 40
        assert data["quantity_physically_available"] == 150  # 180 - 30
        assert data["quantity_server_confirmed_available"] == 130  # 150 - 20
        assert data["is_out_of_stock"] is False

        # Conservation Invariant
        assert (
            data["quantity_server_confirmed_available"]
            + data["quantity_reserved"]
            + data["quantity_quarantined"]
            == data["quantity_on_hand"]
        )

    def test_02_packaging_breakdown_conversion(self):
        """
        Validates automatic unit conversion into Box, Strip, and Loose Tablets.
        130 server-confirmed base units -> 1 Box (100) + 3 Strips (30) + 0 Tablets.
        """
        self.client.force_authenticate(user=self.cashier_user)
        url = (
            f"/api/v2/inventory/availability/product-details/"
            f"?branch_id={self.branch.branch_id}&product_id={self.product.product_id}"
        )
        res = self.client.get(url)
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        bd = data["packaging_breakdown"]
        assert bd["packs"] == 1
        assert bd["intermediates"] == 3
        assert bd["loose_base_units"] == 0
        assert "1 BOX" in bd["formatted_summary"]
        assert "3 STRIPs" in bd["formatted_summary"]

    def test_03_expired_batch_treated_as_quarantined(self):
        """
        Batches with expiry_date <= today are legally quarantined and barred from sale.
        """
        # Create expired batch of 25 units
        BatchStock.objects.create(
            batch_id=f"bat-exp-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-EXPIRED-25",
            quantity_base_units=25,
            reserved_quantity_base_units=0,
            expiry_date=self.today - datetime.timedelta(days=2),
            purchase_date=self.today - datetime.timedelta(days=60),
            cost_per_unit=Decimal("1.8000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

        self.client.force_authenticate(user=self.cashier_user)
        url = (
            f"/api/v2/inventory/availability/product-details/"
            f"?branch_id={self.branch.branch_id}&product_id={self.product.product_id}"
        )
        res = self.client.get(url)
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        # Total on hand = 180 + 25 = 205
        assert data["quantity_on_hand"] == 205
        # Quarantined now includes 30 + 25 = 55
        assert data["quantity_quarantined"] == 55
        # Physically available = 205 - 55 = 150
        assert data["quantity_physically_available"] == 150
        # Server confirmed remains 130
        assert data["quantity_server_confirmed_available"] == 130

    def test_04_precheck_pos_cart_success(self):
        """
        Validates fast POS pre-checkout check when requested quantity is fully available.
        """
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "branch_id": self.branch.branch_id,
            "items": [
                {
                    "product_id": self.product.product_id,
                    "requested_quantity_base_units": 50,
                }
            ],
        }
        res = self.client.post("/api/v2/inventory/availability/precheck/", payload, format="json")
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert data["all_items_available"] is True
        item = data["items"][0]
        assert item["is_available"] is True
        assert item["shortage_units"] == 0
        assert item["server_confirmed_available"] == 130

    def test_05_precheck_pos_cart_shortage(self):
        """
        Validates POS pre-checkout flags shortages accurately when requested quantity exceeds confirmed stock.
        """
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "branch_id": self.branch.branch_id,
            "items": [
                {
                    "product_id": self.product.product_id,
                    "requested_quantity_base_units": 200,
                }
            ],
        }
        res = self.client.post("/api/v2/inventory/availability/precheck/", payload, format="json")
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert data["all_items_available"] is False
        item = data["items"][0]
        assert item["is_available"] is False
        assert item["shortage_units"] == 70  # 200 - 130
        assert item["server_confirmed_available"] == 130

    def test_06_stock_reservation_deducts_server_confirmed_and_release_restores(self):
        """
        Reserving stock transactionally deducts from server-confirmed availability,
        and cancelling/releasing the reservation restores availability immediately.
        """
        self.client.force_authenticate(user=self.cashier_user)

        # 1. Create a reservation of 40 base units on batch 2
        resv = StockReservation.objects.create(
            reservation_id=f"resv-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch=self.batch2,
            quantity_base_units=40,
            status=StockReservationStatus.ACTIVE,
            expires_at=timezone.now() + datetime.timedelta(minutes=15),
        )
        self.batch2.reserved_quantity_base_units += 40
        self.batch2.save()

        url = (
            f"/api/v2/inventory/availability/product-details/"
            f"?branch_id={self.branch.branch_id}&product_id={self.product.product_id}"
        )
        res = self.client.get(url)
        assert res.status_code == status.HTTP_200_OK
        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})

        # Total reserved was 20, now 20 + 40 = 60
        assert data["quantity_reserved"] == 60
        # Server confirmed was 130, now 150 - 60 = 90
        assert data["quantity_server_confirmed_available"] == 90
        # On-hand and physically available remain unchanged
        assert data["quantity_on_hand"] == 180
        assert data["quantity_physically_available"] == 150

        # 2. Release reservation
        resv.status = StockReservationStatus.RELEASED
        resv.save()
        self.batch2.reserved_quantity_base_units -= 40
        self.batch2.save()

        res_restored = self.client.get(url)
        data_restored = (
            res_restored.json()
            if callable(getattr(res_restored, "json", None))
            else getattr(res_restored, "data", {})
        )
        assert data_restored["quantity_reserved"] == 20
        assert data_restored["quantity_server_confirmed_available"] == 130

    def test_07_refresh_branch_matrix_action(self):
        """
        Administrative action POST /api/v2/inventory/availability/refresh/ recalculates matrix and logs audit.
        """
        self.client.force_authenticate(user=self.manager_user)
        payload = {
            "branch_id": self.branch.branch_id,
            "product_id": self.product.product_id,
        }
        res = self.client.post("/api/v2/inventory/availability/refresh/", payload, format="json")
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert data["status"] == "SUCCESS"
        assert data["refreshed_products_count"] == 1

        # Verify BranchStockAvailability projection updated
        rec = BranchStockAvailability.objects.filter(
            tenant=self.tenant, branch=self.branch, product=self.product
        ).first()
        assert rec is not None
        assert rec.quantity_server_confirmed_available == 130

    def test_08_rbac_unauthorized_role_blocked(self):
        """
        Unauthenticated or unauthorized roles cannot execute availability recalculations.
        """
        unauth_user = PharmacyUser.objects.create_user(
            email=f"unauth_{self.unique_id}@pharmacy.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Delivery Rider",
            password="StrongPassword123!",
            role="DELIVERY_RIDER",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.client.force_authenticate(user=unauth_user)

        res = self.client.post(
            "/api/v2/inventory/availability/refresh/",
            {"branch_id": self.branch.branch_id},
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_09_cross_tenant_isolation_blocked(self):
        """
        Users from Tenant B cannot access or precheck stock in Tenant A's branch.
        """
        tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.unique_id}",
            name="Other Pharmacy Tenant",
            subdomain=f"sub-b-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        user_b = PharmacyUser.objects.create_user(
            email=f"user_b_{self.unique_id}@pharmacy.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Tenant B Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=tenant_b.tenant_id,
        )
        self.client.force_authenticate(user=user_b)

        # Attempt to access Tenant A's availability details
        url = (
            f"/api/v2/inventory/availability/product-details/"
            f"?branch_id={self.branch.branch_id}&product_id={self.product.product_id}&tenant_id={self.tenant.tenant_id}"
        )
        res = self.client.get(url)
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_10_suspended_tenant_blocked(self):
        """
        Suspended tenants are completely locked from inventory availability operations.
        """
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        self.client.force_authenticate(user=self.cashier_user)
        url = (
            f"/api/v2/inventory/availability/product-details/"
            f"?branch_id={self.branch.branch_id}&product_id={self.product.product_id}"
        )
        res = self.client.get(url)
        assert res.status_code == status.HTTP_403_FORBIDDEN

