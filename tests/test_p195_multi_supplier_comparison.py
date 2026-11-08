"""
backend/tests/test_p195_multi_supplier_comparison.py
Architecture Decision: ADR-0195 (P195).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration test suite verifying Multi-Supplier Comparison:
- Compares suppliers by product cost, delivery reliability, lead time, pack size, and approved terms.
- Deterministic multi-factor scoring and recommended vendor identification.
- Audited comparison snapshots and managerial selection converting to Purchase Orders.
- MOQ enforcement, duplicate order guard, RBAC (Cashier blocked), and cross-tenant isolation.
"""

import datetime
from decimal import Decimal
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderStatus,
    Supplier,
    SupplierComparisonEvaluation,
    SupplierComparisonStatus,
    Tenant,
    VendorProductPriceList,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestMultiSupplierComparisonIntegration:
    @classmethod
    def setup_class(cls):
        cls.client = APIClient()

    def setup_method(self):
        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id="tenant-cmp-001",
            name="Dhaka Central Pharmacy",
            subdomain="dhaka-central",
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id="tenant-cmp-002",
            name="Chittagong Care Pharmacy",
            subdomain="ctg-care",
        )

        # 2. Branches
        self.branch_a = Branch.objects.create(
            branch_id="br-cmp-001",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email="manager@dhaka-central.com",
            phone_number="+8801711000001",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            role=UserRole.BRANCH_MANAGER,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email="cashier@dhaka-central.com",
            phone_number="+8801711000002",
            full_name="Front Cashier",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            role=UserRole.CASHIER,
        )
        self.manager_b = PharmacyUser.objects.create_user(
            email="manager@ctg-care.com",
            phone_number="+8801711000003",
            full_name="CTG Manager",
            password="StrongPassword123!",
            tenant_id=self.tenant_b.tenant_id,
            role=UserRole.BRANCH_MANAGER,
        )

        # 4. Medicine Product
        self.product = MedicineProduct.objects.create(
            product_id="prod-napa-500",
            brand_name="Napa 500mg Tablet",
            generic_name="Paracetamol",
            strength="500mg",
            dosage_form="TABLET",
            cost_unit=Decimal("2.0000"),
            mrp_unit=Decimal("2.5000"),
        )

        # 5. Suppliers for Tenant A
        # Supplier 1: Square Pharma (High reliability 100%, lower lead time 1 day, moderate discount 5%)
        self.sup_square = Supplier.objects.create(
            supplier_id="sup-square-001",
            tenant=self.tenant_a,
            name="Square Pharmaceuticals Depot",
            code="SQUARE-DHAKA",
            lead_time_days=1,
            payment_terms_days=30,
            proof_of_origin_verified=True,
            is_active=True,
        )

        # Supplier 2: Beximco Pharma (Medium lead time 3 days, higher discount 10%, 1 late delivery historical)
        self.sup_beximco = Supplier.objects.create(
            supplier_id="sup-beximco-002",
            tenant=self.tenant_a,
            name="Beximco Pharma Distribution",
            code="BEXIMCO-DHAKA",
            lead_time_days=3,
            payment_terms_days=45,
            proof_of_origin_verified=True,
            is_active=True,
        )

        # Supplier 3: Incepta Depot (No price list entry, fallback catalog)
        self.sup_incepta = Supplier.objects.create(
            supplier_id="sup-incepta-003",
            tenant=self.tenant_a,
            name="Incepta Pharmaceuticals Depot",
            code="INCEPTA-DHAKA",
            lead_time_days=4,
            payment_terms_days=15,
            proof_of_origin_verified=True,
            is_active=True,
        )

        # 6. Price List Quotations
        VendorProductPriceList.objects.create(
            price_entry_id="pe-sq-001",
            tenant=self.tenant_a,
            supplier=self.sup_square,
            product=self.product,
            package_type="BOX_100",
            package_size=100,
            unit_cost_price=Decimal("1.8000"),
            package_cost_price=Decimal("180.0000"),
            mrp_unit=Decimal("2.5000"),
            discount_percentage=Decimal("5.00"),  # effective unit cost: 1.80 * 0.95 = 1.7100
            minimum_order_quantity=50,
            effective_from=datetime.date.today(),
            is_active=True,
        )

        VendorProductPriceList.objects.create(
            price_entry_id="pe-bx-002",
            tenant=self.tenant_a,
            supplier=self.sup_beximco,
            product=self.product,
            package_type="BOX_50",
            package_size=50,
            unit_cost_price=Decimal("1.8500"),
            package_cost_price=Decimal("92.5000"),
            mrp_unit=Decimal("2.5000"),
            discount_percentage=Decimal("10.00"),  # effective unit cost: 1.85 * 0.90 = 1.6650
            minimum_order_quantity=100,
            effective_from=datetime.date.today(),
            is_active=True,
        )

        # 7. Historical Delivery Data for Beximco (1 on-time, 1 late = 50% reliability)
        po_on_time = PurchaseOrder.objects.create(
            po_id="po-hist-001",
            po_number="PO-HIST-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.sup_beximco,
            expected_delivery_date=datetime.date(2026, 10, 10),
            status=PurchaseOrderStatus.CLOSED,
        )
        GoodsReceiptNote.objects.create(
            grn_id="grn-hist-001",
            grn_number="GRN-HIST-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.sup_beximco,
            purchase_order=po_on_time,
            receipt_date=datetime.date(2026, 10, 9),  # On-time
            status=GoodsReceiptStatus.POSTED,
        )

        po_late = PurchaseOrder.objects.create(
            po_id="po-hist-002",
            po_number="PO-HIST-002",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.sup_beximco,
            expected_delivery_date=datetime.date(2026, 10, 20),
            status=PurchaseOrderStatus.CLOSED,
        )
        GoodsReceiptNote.objects.create(
            grn_id="grn-hist-002",
            grn_number="GRN-HIST-002",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.sup_beximco,
            purchase_order=po_late,
            receipt_date=datetime.date(2026, 10, 25),  # 5 days late
            status=GoodsReceiptStatus.POSTED,
        )

    def test_01_dynamic_supplier_comparison(self):
        """Compares active suppliers on cost, reliability, lead time, and terms."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get(
            f"/api/v2/procurement/supplier-comparison/compare/?product_id={self.product.product_id}&target_quantity=100"
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        assert data["product_id"] == self.product.product_id
        assert data["total_suppliers_compared"] == 3
        lines = data["comparison_lines"]

        # Check line details
        sq_line = next(l for l in lines if l["supplier_id"] == self.sup_square.supplier_id)
        bx_line = next(l for l in lines if l["supplier_id"] == self.sup_beximco.supplier_id)

        # Square: 1.80 * 0.95 = 1.7100, 100% reliability, lead time 1 day
        assert Decimal(str(sq_line["effective_net_unit_cost"])) == Decimal("1.7100")
        assert Decimal(str(sq_line["delivery_reliability_score"])) == Decimal("100.00")
        assert sq_line["lead_time_days"] == 1

        # Beximco: 1.85 * 0.90 = 1.6650, 50% reliability (1 of 2 on-time), lead time 3 days
        assert Decimal(str(bx_line["effective_net_unit_cost"])) == Decimal("1.6650")
        assert Decimal(str(bx_line["delivery_reliability_score"])) == Decimal("50.00")
        assert bx_line["lead_time_days"] == 3

        # Square should have high composite score due to 100% reliability and 1 day lead time
        assert sq_line["is_recommended"] is True
        assert sq_line["composite_rank"] == 1

    def test_02_create_persisted_comparison_evaluation(self):
        """Persists formal comparison evaluation snapshot with audit trail."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "product_id": self.product.product_id,
            "target_order_quantity": 200,
            "branch_id": self.branch_a.branch_id,
            "notes": "Quarterly paracetamol procurement committee review",
        }
        resp = self.client.post("/api/v2/procurement/supplier-comparison/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["status"] == "DRAFT"
        assert data["target_order_quantity"] == 200
        assert len(data["lines"]) == 3
        assert data["evaluation_number"].startswith("SCE-")

        # Verify DB persistence
        ev = SupplierComparisonEvaluation.objects.get(evaluation_id=data["evaluation_id"])
        assert ev.target_order_quantity == 200
        assert ev.comparison_lines.count() == 3

    def test_03_select_supplier_and_create_purchase_order(self):
        """Selects winning supplier and converts quotation into official Purchase Order."""
        self.client.force_authenticate(user=self.manager)

        # Create evaluation
        ev_resp = self.client.post(
            "/api/v2/procurement/supplier-comparison/",
            data={"product_id": self.product.product_id, "target_order_quantity": 100},
            format="json",
        )
        ev_id = ev_resp.json()["evaluation_id"]

        # Select Square Pharma
        select_payload = {
            "selected_supplier_id": self.sup_square.supplier_id,
            "selection_reason": "Top composite score with verified 100% reliability and next-day delivery.",
            "order_quantity": 100,
        }
        resp = self.client.post(
            f"/api/v2/procurement/supplier-comparison/{ev_id}/select-supplier/",
            data=select_payload,
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        po_data = data["purchase_order"]
        assert po_data["supplier_id"] == self.sup_square.supplier_id
        assert po_data["ordered_quantity"] == 100
        # 100 units * 1.80 = 180.00 subtotal, - 5% discount (9.00) = 171.00 total
        assert Decimal(po_data["total_amount"]) == Decimal("171.0000")

        # Evaluation status transitioned to ORDER_PLACED
        assert data["evaluation"]["status"] == "ORDER_PLACED"

    def test_04_moq_enforcement(self):
        """Fails with HTTP 400 when order quantity is below supplier MOQ."""
        self.client.force_authenticate(user=self.manager)
        ev_resp = self.client.post(
            "/api/v2/procurement/supplier-comparison/",
            data={"product_id": self.product.product_id, "target_order_quantity": 20},
            format="json",
        )
        ev_id = ev_resp.json()["evaluation_id"]

        # Beximco MOQ is 100 units; trying to order 20 units fails
        select_payload = {
            "selected_supplier_id": self.sup_beximco.supplier_id,
            "selection_reason": "Testing MOQ violation",
            "order_quantity": 20,
        }
        resp = self.client.post(
            f"/api/v2/procurement/supplier-comparison/{ev_id}/select-supplier/",
            data=select_payload,
            format="json",
        )
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_BELOW_MINIMUM_ORDER_QTY" in str(resp.json())

    def test_05_duplicate_order_placement_blocked(self):
        """Prevents duplicate PO creation from already converted evaluation."""
        self.client.force_authenticate(user=self.manager)
        ev_resp = self.client.post(
            "/api/v2/procurement/supplier-comparison/",
            data={"product_id": self.product.product_id, "target_order_quantity": 100},
            format="json",
        )
        ev_id = ev_resp.json()["evaluation_id"]

        # First placement succeeds
        self.client.post(
            f"/api/v2/procurement/supplier-comparison/{ev_id}/select-supplier/",
            data={"selected_supplier_id": self.sup_square.supplier_id, "selection_reason": "Order 1"},
            format="json",
        )

        # Second placement fails
        resp = self.client.post(
            f"/api/v2/procurement/supplier-comparison/{ev_id}/select-supplier/",
            data={"selected_supplier_id": self.sup_square.supplier_id, "selection_reason": "Order 2"},
            format="json",
        )
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_ORDER_ALREADY_PLACED" in str(resp.json())

    def test_06_rbac_cashier_denied(self):
        """Cashier role is barred from comparison evaluation and PO creation."""
        self.client.force_authenticate(user=self.cashier)

        resp = self.client.get(
            f"/api/v2/procurement/supplier-comparison/compare/?product_id={self.product.product_id}"
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json().get("error_code") in ("ERR_STAFF_ROLE_DENIED", "ERR_PERMISSION_DENIED")

        resp_create = self.client.post(
            "/api/v2/procurement/supplier-comparison/",
            data={"product_id": self.product.product_id},
            format="json",
        )
        assert resp_create.status_code == status.HTTP_403_FORBIDDEN

    def test_07_cross_tenant_isolation(self):
        """Tenant B manager cannot access or evaluate Tenant A's comparisons."""
        self.client.force_authenticate(user=self.manager)
        ev_resp = self.client.post(
            "/api/v2/procurement/supplier-comparison/",
            data={"product_id": self.product.product_id, "target_order_quantity": 100},
            format="json",
        )
        ev_id = ev_resp.json()["evaluation_id"]

        # Switch to Tenant B
        self.client.force_authenticate(user=self.manager_b)
        resp = self.client.get(f"/api/v2/procurement/supplier-comparison/{ev_id}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
