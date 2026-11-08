"""
Integration Tests for Supplier Operations Dashboard (ADR-0199 / P199).
Covers open orders, overdue deliveries, due balances, low-stock sourcing opportunities,
auditable snapshots with SHA-256 integrity hash, RBAC, and cross-tenant boundaries.
"""

from decimal import Decimal
import datetime
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.identity.models import PharmacyUser, UserRole
from apps.core.models import (
    BatchStock,
    Branch,
    BranchStockAvailability,
    MedicineProduct,
    PaymentSchedulePriority,
    PaymentScheduleStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Supplier,
    SupplierDashboardSnapshot,
    SupplierPaymentSchedule,
    Tenant,
    TenantLifecycleStatus,
    VendorProductPriceList,
)


@pytest.mark.django_db
class TestSupplierDashboardIntegration:
    """
    Test suite verifying end-to-end supplier operations dashboard workflows.
    """

    def setup_method(self):
        self.client = APIClient()

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id="tenant-sdash-001",
            name="Dhaka Prime Health",
            subdomain="dhaka-prime-sdash",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id="tenant-sdash-002",
            name="Chittagong Care Health",
            subdomain="ctg-care-sdash",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # 2. Branches
        self.branch_a = Branch.objects.create(
            branch_id="br-sdash-001",
            tenant=self.tenant_a,
            name="Dhanmondi Central Branch",
        )
        self.branch_b = Branch.objects.create(
            branch_id="br-sdash-002",
            tenant=self.tenant_b,
            name="Agrabad Main Branch",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email="manager.sdash@dhakaprime.com",
            phone_number="+8801711000011",
            password="SecurePassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email="cashier.sdash@dhakaprime.com",
            phone_number="+8801711000012",
            password="SecurePassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.manager_b = PharmacyUser.objects.create_user(
            email="manager.sdash.b@ctgcare.com",
            phone_number="+8801711000013",
            password="SecurePassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        # 4. Suppliers
        self.supplier_sq = Supplier.objects.create(
            tenant=self.tenant_a,
            supplier_id="sup-sdash-sq",
            name="Square Pharmaceuticals Ltd.",
            contact_person="Rafiqul Islam",
            phone="+8801711223344",
            email="orders@squarepharma.com.bd",
            payment_terms_days=30,
            lead_time_days=2,
            current_balance=Decimal("45000.0000"),
            is_active=True,
        )
        self.supplier_bx = Supplier.objects.create(
            tenant=self.tenant_a,
            supplier_id="sup-sdash-bx",
            name="Beximco Pharmaceuticals Ltd.",
            contact_person="Kamrul Hasan",
            phone="+8801711556677",
            email="orders@beximcopharma.com.bd",
            payment_terms_days=45,
            lead_time_days=3,
            current_balance=Decimal("20000.0000"),
            is_active=True,
        )

        # 5. Products
        self.product_napa = MedicineProduct.objects.create(
            product_id="prod-sdash-001",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            strength="500mg + 65mg",
            dosage_form="TABLET",
            manufacturer="Square Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.5000"),
            mrp_unit=Decimal("3.0000"),
        )
        self.product_seclo = MedicineProduct.objects.create(
            product_id="prod-sdash-002",
            brand_name="Seclo 20mg Capsule",
            generic_name="Omeprazole",
            strength="20mg",
            dosage_form="CAPSULE",
            manufacturer="Square Pharmaceuticals Ltd.",
            cost_unit=Decimal("5.0000"),
            mrp_unit=Decimal("6.0000"),
        )

        # 6. Preferred Supplier Price List
        self.price_list = VendorProductPriceList.objects.create(
            price_entry_id="vpl-sdash-001",
            tenant=self.tenant_a,
            supplier=self.supplier_sq,
            product=self.product_napa,
            package_type="BOX",
            package_size=100,
            unit_cost_price=Decimal("2.4000"),
            package_cost_price=Decimal("240.0000"),
            effective_from=datetime.date.today(),
            is_active=True,
        )

        # 7. Stock Availability (Low Stock)
        self.stock_avail = BranchStockAvailability.objects.create(
            availability_id="bsa-sdash-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            quantity_on_hand=15,
            quantity_reserved=0,
            quantity_quarantined=0,
            quantity_pending_inward=0,
            quantity_physically_available=15,
            quantity_server_confirmed_available=15,
            reorder_level_base_units=100,
            is_out_of_stock=False,
            is_below_reorder_level=True,
        )

        # 8. Purchase Orders (1 open on-time, 1 open overdue)
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=3)
        tomorrow = today + datetime.timedelta(days=5)

        self.po_ontime = PurchaseOrder.objects.create(
            po_id="po-sdash-101",
            po_number="PO-202611-101",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier_bx,
            status=PurchaseOrderStatus.ORDERED,
            payment_terms_days=45,
            currency="BDT",
            subtotal_amount=Decimal("10000.0000"),
            total_amount=Decimal("10000.0000"),
            expected_delivery_date=tomorrow,
            ordered_at=datetime.datetime(2026, 11, 5, tzinfo=datetime.timezone.utc),
        )

        self.po_overdue = PurchaseOrder.objects.create(
            po_id="po-sdash-102",
            po_number="PO-202611-102",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier_sq,
            status=PurchaseOrderStatus.ORDERED,
            payment_terms_days=30,
            currency="BDT",
            subtotal_amount=Decimal("15000.0000"),
            total_amount=Decimal("15000.0000"),
            expected_delivery_date=yesterday,
            ordered_at=datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc),
        )

        # 9. Payment Schedules
        self.sched_overdue = SupplierPaymentSchedule.objects.create(
            schedule_id="sps-sdash-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier_sq,
            reference_note="INV-SQ-001",
            due_date=yesterday,
            planned_amount=Decimal("45000.0000"),
            remaining_amount=Decimal("45000.0000"),
            status=PaymentScheduleStatus.OVERDUE,
            priority=PaymentSchedulePriority.HIGH,
        )

    def test_01_dashboard_summary_open_orders_and_overdue(self):
        """Computes dynamic open orders and overdue delivery metrics."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/v2/procurement/supplier-dashboard/summary/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        # Open Orders
        open_orders = data["open_orders"]
        assert open_orders["count"] == 2
        assert Decimal(open_orders["total_value_bdt"]) == Decimal("25000.0000")

        # Overdue Deliveries
        overdue = data["overdue_deliveries"]
        assert overdue["count"] == 1
        assert Decimal(overdue["total_delayed_value_bdt"]) == Decimal("15000.0000")
        assert len(overdue["items"]) == 1
        assert overdue["items"][0]["po_id"] == self.po_overdue.po_id
        assert overdue["items"][0]["days_overdue"] >= 3

    def test_02_dashboard_summary_due_balances_and_schedules(self):
        """Computes total supplier AP balances and overdue payment schedules."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/v2/procurement/supplier-dashboard/summary/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        dues = data["due_balances"]
        # Total due across Square (45000) + Beximco (20000) = 65000
        assert Decimal(dues["total_due_balance_bdt"]) == Decimal("65000.0000")
        assert Decimal(dues["overdue_due_balance_bdt"]) == Decimal("45000.0000")
        assert len(dues["supplier_dues"]) == 2

    def test_03_dashboard_summary_low_stock_sourcing(self):
        """Identifies low-stock items with preferred supplier matching and restock cost."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/v2/procurement/supplier-dashboard/summary/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        sourcing = data["low_stock_sourcing"]
        assert sourcing["count"] >= 1
        assert Decimal(sourcing["estimated_reorder_cost_bdt"]) > 0

        # Verify candidate matches preferred supplier
        candidate = next(c for c in sourcing["candidates"] if c["product_id"] == self.product_napa.product_id)
        assert candidate["preferred_supplier_name"] == self.supplier_sq.name
        assert Decimal(candidate["quoted_unit_cost"]) == Decimal("2.4000")
        assert candidate["current_stock"] == 15
        assert candidate["recommended_reorder_qty"] == 185  # (100 * 2) - 15

    def test_04_create_and_retrieve_dashboard_snapshot(self):
        """Persists a formal auditable snapshot with SHA-256 hash and retrieves it."""
        self.client.force_authenticate(user=self.manager)
        snap_resp = self.client.post(
            "/api/v2/procurement/supplier-dashboard/snapshot/",
            {"branch_id": self.branch_a.branch_id, "notes": "Weekly executive procurement audit"},
            format="json",
        )
        assert snap_resp.status_code == status.HTTP_201_CREATED
        snap_data = snap_resp.json()

        assert snap_data["snapshot_id"].startswith("sdash-")
        assert len(snap_data["immutable_verification_hash"]) == 64
        assert snap_data["open_orders_count"] == 2
        assert Decimal(snap_data["total_due_balance"]) == Decimal("65000.0000")

        # Retrieve single snapshot
        get_resp = self.client.get(f"/api/v2/procurement/supplier-dashboard/{snap_data['snapshot_id']}/")
        assert get_resp.status_code == status.HTTP_200_OK
        assert get_resp.json()["snapshot_id"] == snap_data["snapshot_id"]

    def test_05_list_snapshots(self):
        """Lists historical snapshots for the tenant."""
        self.client.force_authenticate(user=self.manager)
        self.client.post(
            "/api/v2/procurement/supplier-dashboard/snapshot/",
            {"notes": "Snap 1"},
            format="json",
        )
        self.client.post(
            "/api/v2/procurement/supplier-dashboard/snapshot/",
            {"notes": "Snap 2"},
            format="json",
        )

        resp = self.client.get("/api/v2/procurement/supplier-dashboard/snapshots/")
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.json()) == 2

    def test_06_branch_scoping_filter(self):
        """Filters dashboard summary strictly to the requested branch."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get(f"/api/v2/procurement/supplier-dashboard/summary/?branch_id={self.branch_a.branch_id}")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["branch_id"] == self.branch_a.branch_id
        assert data["open_orders"]["count"] == 2

    def test_07_rbac_cashier_denied(self):
        """Cashier role is restricted from accessing supplier dashboard with HTTP 403."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get("/api/v2/procurement/supplier-dashboard/summary/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error_code"] in ("ERR_STAFF_ROLE_DENIED", "ERR_PERMISSION_DENIED")

    def test_08_cross_tenant_isolation(self):
        """Tenant B manager cannot access Tenant A snapshots."""
        self.client.force_authenticate(user=self.manager)
        snap_resp = self.client.post(
            "/api/v2/procurement/supplier-dashboard/snapshot/",
            {"notes": "Tenant A Private Snapshot"},
            format="json",
        )
        snap_id = snap_resp.json()["snapshot_id"]

        # Authenticate as Tenant B
        self.client.force_authenticate(user=self.manager_b)
        resp = self.client.get(f"/api/v2/procurement/supplier-dashboard/{snap_id}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
