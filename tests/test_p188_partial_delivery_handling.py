"""
backend/tests/test_p188_partial_delivery_handling.py
Architecture Decision: ADR-0188 (P188).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P188 Partial Delivery, Excess Tolerance, and Backorders:
1. Valid partial receipt: 60/100 received -> BatchStock has 60 only, undelivered 40 remains backordered (0 stock in inventory), PO status PARTIALLY_RECEIVED.
2. Subsequent delivery: second GRN for remaining 40 units completes PO line (100/100), transitions PO to CLOSED.
3. Multi-line partial delivery: Line 1 received 50/100, Line 2 unreceived 0/500. Verify BatchStock of Line 2 is 0 and PO is PARTIALLY_RECEIVED.
4. Backorder cancellation via API: POST /orders/{pk}/cancel-backorder/ cancels remaining 40 units; PO transitions to CLOSED without counting cancelled units in stock.
5. Over-cancellation guard: cancelling more than backordered_quantity returns HTTP 400 ERR_INVALID_CANCEL_QUANTITY; missing reason returns ERR_MISSING_CANCEL_REASON.
6. Manager-authorized excess delivery: Manager approves extra units with reason; BatchStock and line increment with excess record.
7. Unauthorized excess delivery rejection: excess without manager approval rejected with HTTP 400 ERR_EXCESS_RECEIPT; missing reason rejected with ERR_MISSING_EXCESS_REASON.
8. Promotional bonus / free goods: 20 free bonus units received at 0 cost; increments BatchStock by 20 but adds 0 to AP invoice total.
9. Delivery summary endpoint: GET /orders/{pk}/delivery-summary/ returns accurate ordered, received, backordered, cancelled, and linked GRNs.
10. RBAC and tenant isolation: Cashier blocked from cancelling backorders (HTTP 403 ERR_STAFF_ROLE_DENIED) and foreign tenant blocked (HTTP 403 ERR_CROSS_TENANT_ACCESS).
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
    DeliveryStatus,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPartialDeliveryIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p188-{self.suffix}",
            name="Green Crescent Hospital",
            subdomain=f"greencrescent-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-gc-{self.suffix}",
            tenant=self.tenant,
            name="Main Dispensary",
        )

        # 2. Foreign Tenant & Branch
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-foreign-{self.suffix}",
            name="Delta City Pharmacy",
            subdomain=f"delta-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-delta-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Delta Central Branch",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@greencrescent.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"csh_{self.suffix}@greencrescent.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Dispensary Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@delta.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Delta Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-beximco-{self.suffix}",
            tenant=self.tenant,
            name="Beximco Pharmaceuticals Ltd.",
            contact_person="M. Rahman",
            phone="+8801711223344",
            email="supply@beximco.com",
            is_active=True,
        )

        # 5. Products
        self.napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name="Napa 500mg Tablet",
            generic_name="Paracetamol",
            strength="500 mg",
            dosage_form="Tablet",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.2000"),
            mrp_unit=Decimal("3.0000"),
        )
        self.insulin = MedicineProduct.objects.create(
            product_id=f"prod-ins-{self.suffix}",
            brand_name="Glarin Insulin 100IU",
            generic_name="Insulin Glargine",
            strength="100 IU/ml",
            dosage_form="Injection",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("350.0000"),
            mrp_unit=Decimal("420.0000"),
        )

    def _create_single_line_po(self, quantity: int = 100) -> tuple[PurchaseOrder, PurchaseOrderItem]:
        """Creates an ORDERED PO with 1 Napa line item."""
        po = PurchaseOrder.objects.create(
            po_id=f"po-{uuid.uuid4().hex[:8]}",
            po_number=f"PO-{timezone.now().strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            status=PurchaseOrderStatus.ORDERED,
            subtotal_amount=Decimal(str(quantity * 2.20)),
            total_amount=Decimal(str(quantity * 2.20)),
            created_by=self.manager,
            is_active=True,
        )
        po_item = PurchaseOrderItem.objects.create(
            item_id=f"poi-napa-{uuid.uuid4().hex[:8]}",
            purchase_order=po,
            product=self.napa,
            pack_unit="BOX",
            package_size=1,
            ordered_packages=quantity,
            ordered_quantity=quantity,
            received_quantity=0,
            cancelled_quantity=0,
            delivery_status=DeliveryStatus.PENDING,
            unit_cost_price=Decimal("2.2000"),
            mrp_unit=Decimal("3.0000"),
            line_subtotal=Decimal(str(quantity * 2.20)),
            line_total=Decimal(str(quantity * 2.20)),
        )
        return po, po_item

    def test_01_valid_partial_receipt_stock_non_inclusion(self):
        """1. Valid partial receipt: 60/100 received -> BatchStock has 60 only, undelivered 40 remains backordered."""
        po, po_item = self._create_single_line_po(100)

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "challan_number": "CHAL-PARTIAL-01",
            "receipt_date": timezone.now().date().isoformat(),
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.napa.product_id,
                    "batch_number": "BX-NAPA-P1",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                    "received_quantity_base_units": 60,
                    "unit_cost": "2.2000",
                    "mrp_unit": "3.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert create_res.status_code == status.HTTP_201_CREATED, create_res.data
        grn_id = create_res.data["grn_id"]

        post_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")
        assert post_res.status_code == status.HTTP_200_OK

        # Verify physical BatchStock: exactly 60 units (undelivered 40 units must NOT exist in stock)
        batch = BatchStock.objects.get(product=self.napa, batch_number="BX-NAPA-P1")
        assert batch.quantity_base_units == 60
        assert batch.status == BatchStatus.ACTIVE

        # Total Napa stock across the branch must be exactly 60
        total_stock = sum(b.quantity_base_units for b in BatchStock.objects.filter(product=self.napa, branch=self.branch))
        assert total_stock == 60

        # Verify PO line item & parent PO status
        po.refresh_from_db()
        po_item.refresh_from_db()
        assert po_item.received_quantity == 60
        assert po_item.cancelled_quantity == 0
        assert po_item.backordered_quantity == 40
        assert po_item.delivery_status == DeliveryStatus.PARTIALLY_DELIVERED
        assert po.status == PurchaseOrderStatus.PARTIALLY_RECEIVED

    def test_02_subsequent_delivery_fulfills_and_closes_po(self):
        """2. Subsequent delivery: second GRN for remaining 40 units completes PO line (100/100), transitions PO to CLOSED."""
        po, po_item = self._create_single_line_po(100)
        self.client.force_authenticate(user=self.manager)

        # First GRN (60 units)
        grn1_res = self.client.post(
            "/api/v2/procurement/goods-receipts/",
            {
                "branch_id": self.branch.branch_id,
                "purchase_order_id": po.po_id,
                "challan_number": "CHAL-P1",
                "lines": [
                    {
                        "po_item_id": po_item.item_id,
                        "product_id": self.napa.product_id,
                        "batch_number": "BX-NAPA-P1",
                        "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                        "received_quantity_base_units": 60,
                        "unit_cost": "2.2000",
                        "mrp_unit": "3.0000",
                    }
                ],
            },
            format="json",
        )
        self.client.post(f"/api/v2/procurement/goods-receipts/{grn1_res.data['grn_id']}/post-receipt/")

        # Second GRN (40 units)
        grn2_res = self.client.post(
            "/api/v2/procurement/goods-receipts/",
            {
                "branch_id": self.branch.branch_id,
                "purchase_order_id": po.po_id,
                "challan_number": "CHAL-P2",
                "lines": [
                    {
                        "po_item_id": po_item.item_id,
                        "product_id": self.napa.product_id,
                        "batch_number": "BX-NAPA-P2",
                        "expiry_date": (timezone.now().date() + datetime.timedelta(days=400)).isoformat(),
                        "received_quantity_base_units": 40,
                        "unit_cost": "2.2000",
                        "mrp_unit": "3.0000",
                    }
                ],
            },
            format="json",
        )
        post2_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn2_res.data['grn_id']}/post-receipt/")
        assert post2_res.status_code == status.HTTP_200_OK

        # Verify stock total
        total_stock = sum(b.quantity_base_units for b in BatchStock.objects.filter(product=self.napa, branch=self.branch))
        assert total_stock == 100

        # Verify PO line & parent PO are now FULLY_DELIVERED & CLOSED
        po.refresh_from_db()
        po_item.refresh_from_db()
        assert po_item.received_quantity == 100
        assert po_item.backordered_quantity == 0
        assert po_item.delivery_status == DeliveryStatus.FULLY_DELIVERED
        assert po.status == PurchaseOrderStatus.CLOSED

    def test_03_multi_line_partial_delivery(self):
        """3. Multi-line partial delivery: Line 1 received 50/100, Line 2 unreceived 0/500."""
        po = PurchaseOrder.objects.create(
            po_id=f"po-{uuid.uuid4().hex[:8]}",
            po_number=f"PO-{timezone.now().strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            status=PurchaseOrderStatus.ORDERED,
            subtotal_amount=Decimal("36100.0000"),
            total_amount=Decimal("36100.0000"),
            created_by=self.manager,
        )
        po_ins = PurchaseOrderItem.objects.create(
            item_id=f"poi-ins-{uuid.uuid4().hex[:8]}",
            purchase_order=po,
            product=self.insulin,
            pack_unit="VIAL",
            package_size=1,
            ordered_packages=100,
            ordered_quantity=100,
            received_quantity=0,
            unit_cost_price=Decimal("350.0000"),
            mrp_unit=Decimal("420.0000"),
            line_subtotal=Decimal("35000.0000"),
            line_total=Decimal("35000.0000"),
        )
        po_napa = PurchaseOrderItem.objects.create(
            item_id=f"poi-napa-{uuid.uuid4().hex[:8]}",
            purchase_order=po,
            product=self.napa,
            pack_unit="BOX",
            package_size=1,
            ordered_packages=500,
            ordered_quantity=500,
            received_quantity=0,
            unit_cost_price=Decimal("2.2000"),
            mrp_unit=Decimal("3.0000"),
            line_subtotal=Decimal("1100.0000"),
            line_total=Decimal("1100.0000"),
        )

        self.client.force_authenticate(user=self.manager)
        create_res = self.client.post(
            "/api/v2/procurement/goods-receipts/",
            {
                "branch_id": self.branch.branch_id,
                "purchase_order_id": po.po_id,
                "challan_number": "CHAL-INS-ONLY",
                "storage_condition": "COLD_CHAIN",
                "storage_temperature_celsius": "4.0",
                "storage_check_passed": True,
                "lines": [
                    {
                        "po_item_id": po_ins.item_id,
                        "product_id": self.insulin.product_id,
                        "batch_number": "BX-INS-P50",
                        "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                        "received_quantity_base_units": 50,
                        "unit_cost": "350.0000",
                        "mrp_unit": "420.0000",
                    }
                ],
            },
            format="json",
        )
        assert create_res.status_code == status.HTTP_201_CREATED
        self.client.post(f"/api/v2/procurement/goods-receipts/{create_res.data['grn_id']}/post-receipt/")

        # Verify Insulin stock is 50, Napa stock is strictly 0
        ins_stock = sum(b.quantity_base_units for b in BatchStock.objects.filter(product=self.insulin, branch=self.branch))
        napa_stock = sum(b.quantity_base_units for b in BatchStock.objects.filter(product=self.napa, branch=self.branch))
        assert ins_stock == 50
        assert napa_stock == 0

        # Verify PO statuses
        po.refresh_from_db()
        po_ins.refresh_from_db()
        po_napa.refresh_from_db()
        assert po_ins.delivery_status == DeliveryStatus.PARTIALLY_DELIVERED
        assert po_ins.backordered_quantity == 50
        assert po_napa.delivery_status == DeliveryStatus.PENDING
        assert po_napa.backordered_quantity == 500
        assert po.status == PurchaseOrderStatus.PARTIALLY_RECEIVED

    def test_04_backorder_cancellation_closes_po(self):
        """4. Backorder cancellation via API: POST /orders/{pk}/cancel-backorder/ cancels remaining 40 units; PO closes."""
        po, po_item = self._create_single_line_po(100)
        self.client.force_authenticate(user=self.manager)

        # Receive 60 units
        create_res = self.client.post(
            "/api/v2/procurement/goods-receipts/",
            {
                "branch_id": self.branch.branch_id,
                "purchase_order_id": po.po_id,
                "challan_number": "CHAL-P1",
                "lines": [
                    {
                        "po_item_id": po_item.item_id,
                        "product_id": self.napa.product_id,
                        "batch_number": "BX-NAPA-P1",
                        "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                        "received_quantity_base_units": 60,
                        "unit_cost": "2.2000",
                        "mrp_unit": "3.0000",
                    }
                ],
            },
            format="json",
        )
        self.client.post(f"/api/v2/procurement/goods-receipts/{create_res.data['grn_id']}/post-receipt/")

        # Cancel remaining 40 backordered units via API
        cancel_res = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel-backorder/",
            {
                "item_id": po_item.item_id,
                "cancel_quantity": 40,
                "reason": "Supplier confirmed stock out for this quarter",
            },
            format="json",
        )
        assert cancel_res.status_code == status.HTTP_200_OK, cancel_res.data
        assert cancel_res.data["cancelled_quantity"] == 40
        assert cancel_res.data["backordered_quantity"] == 0
        assert cancel_res.data["po_status"] == PurchaseOrderStatus.CLOSED

        # Verify DB state
        po.refresh_from_db()
        po_item.refresh_from_db()
        assert po.status == PurchaseOrderStatus.CLOSED
        assert po_item.received_quantity == 60
        assert po_item.cancelled_quantity == 40
        assert po_item.backordered_quantity == 0
        assert po_item.delivery_status == DeliveryStatus.FULLY_DELIVERED

        # Stock must remain strictly 60 units (cancelled units never enter inventory)
        total_stock = sum(b.quantity_base_units for b in BatchStock.objects.filter(product=self.napa, branch=self.branch))
        assert total_stock == 60

    def test_05_over_cancellation_guard(self):
        """5. Over-cancellation guard: cancelling more than backordered_quantity returns HTTP 400 ERR_INVALID_CANCEL_QUANTITY."""
        po, po_item = self._create_single_line_po(100)
        self.client.force_authenticate(user=self.manager)

        # Attempt to cancel 120 when backordered is 100
        res = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel-backorder/",
            {
                "item_id": po_item.item_id,
                "cancel_quantity": 120,
                "reason": "Invalid excess cancel",
            },
            format="json",
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_INVALID_CANCEL_QUANTITY"

        # Missing reason
        res_no_reason = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel-backorder/",
            {
                "item_id": po_item.item_id,
                "cancel_quantity": 10,
                "reason": "",
            },
            format="json",
        )
        assert res_no_reason.status_code == status.HTTP_400_BAD_REQUEST
        assert res_no_reason.data["error_code"] == "ERR_MISSING_CANCEL_REASON"

    def test_06_manager_authorized_excess_delivery(self):
        """6. Manager-authorized excess delivery: Manager approves extra units with reason; BatchStock and line increment."""
        po, po_item = self._create_single_line_po(100)
        self.client.force_authenticate(user=self.manager)

        # Receive 120 units with allow_excess=True and excess_reason
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "challan_number": "CHAL-EXCESS-OK",
            "allow_excess": True,
            "excess_reason": "Supplier bonus packaging approved by Branch Manager",
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.napa.product_id,
                    "batch_number": "BX-NAPA-EXCESS-1",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                    "received_quantity_base_units": 120,
                    "unit_cost": "2.2000",
                    "mrp_unit": "3.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert create_res.status_code == status.HTTP_201_CREATED, create_res.data
        assert create_res.data["lines"][0]["excess_quantity"] == 20
        assert create_res.data["lines"][0]["excess_reason"] == "Supplier bonus packaging approved by Branch Manager"

        post_res = self.client.post(f"/api/v2/procurement/goods-receipts/{create_res.data['grn_id']}/post-receipt/")
        assert post_res.status_code == status.HTTP_200_OK

        # BatchStock has full 120 units
        batch = BatchStock.objects.get(product=self.napa, batch_number="BX-NAPA-EXCESS-1")
        assert batch.quantity_base_units == 120

    def test_07_unauthorized_excess_delivery_rejection(self):
        """7. Unauthorized excess delivery rejection: excess without manager approval rejected with ERR_EXCESS_RECEIPT."""
        po, po_item = self._create_single_line_po(100)
        self.client.force_authenticate(user=self.manager)

        # 120 units without allow_excess
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "challan_number": "CHAL-EXCESS-FAIL",
            "allow_excess": False,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.napa.product_id,
                    "batch_number": "BX-NAPA-FAIL",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                    "received_quantity_base_units": 120,
                    "unit_cost": "2.2000",
                    "mrp_unit": "3.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_EXCESS_RECEIPT"

        # allow_excess=True but missing reason
        payload["allow_excess"] = True
        payload["excess_reason"] = ""
        res2 = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res2.status_code == status.HTTP_400_BAD_REQUEST
        assert res2.data["error_code"] == "ERR_MISSING_EXCESS_REASON"

    def test_08_promotional_bonus_free_goods_zero_liability(self):
        """8. Promotional bonus free goods: 20 free bonus units received at 0 cost; increments BatchStock by 20 with 0 AP liability."""
        self.client.force_authenticate(user=self.manager)

        payload = {
            "branch_id": self.branch.branch_id,
            "supplier_id": self.supplier.supplier_id,
            "challan_number": "CHAL-FREE-SAMPLE",
            "lines": [
                {
                    "product_id": self.napa.product_id,
                    "batch_number": "BX-BONUS-NAPA-01",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                    "received_quantity_base_units": 20,
                    "unit_cost": "2.2000",  # Must be forced to 0.0000 because is_bonus_goods=True
                    "mrp_unit": "3.0000",
                    "is_bonus_goods": True,
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert create_res.status_code == status.HTTP_201_CREATED, create_res.data
        data = create_res.data
        assert data["total_received_base_units"] == 20
        assert Decimal(str(data["total_invoice_amount"])) == Decimal("0.0000")
        assert data["lines"][0]["is_bonus_goods"] is True
        assert Decimal(str(data["lines"][0]["unit_cost"])) == Decimal("0.0000")
        assert Decimal(str(data["lines"][0]["line_total"])) == Decimal("0.0000")

        # Post to stock
        post_res = self.client.post(f"/api/v2/procurement/goods-receipts/{data['grn_id']}/post-receipt/")
        assert post_res.status_code == status.HTTP_200_OK
        assert Decimal(str(post_res.data["ap_invoice_amount"])) == Decimal("0.0000")

        # Physical stock incremented by 20
        batch = BatchStock.objects.get(product=self.napa, batch_number="BX-BONUS-NAPA-01")
        assert batch.quantity_base_units == 20

    def test_09_delivery_summary_endpoint(self):
        """9. Delivery summary endpoint: GET /orders/{pk}/delivery-summary/ returns accurate line breakdown and linked GRNs."""
        po, po_item = self._create_single_line_po(100)
        self.client.force_authenticate(user=self.manager)

        # Receive 60 units
        create_res = self.client.post(
            "/api/v2/procurement/goods-receipts/",
            {
                "branch_id": self.branch.branch_id,
                "purchase_order_id": po.po_id,
                "challan_number": "CHAL-SUMM-1",
                "lines": [
                    {
                        "po_item_id": po_item.item_id,
                        "product_id": self.napa.product_id,
                        "batch_number": "BX-SUMM-1",
                        "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                        "received_quantity_base_units": 60,
                        "unit_cost": "2.2000",
                        "mrp_unit": "3.0000",
                    }
                ],
            },
            format="json",
        )
        self.client.post(f"/api/v2/procurement/goods-receipts/{create_res.data['grn_id']}/post-receipt/")

        # Cancel 10 units
        self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel-backorder/",
            {
                "item_id": po_item.item_id,
                "cancel_quantity": 10,
                "reason": "Damaged cartons cancelled from backorder",
            },
            format="json",
        )

        # Fetch delivery summary
        sum_res = self.client.get(f"/api/v2/procurement/orders/{po.po_id}/delivery-summary/")
        assert sum_res.status_code == status.HTTP_200_OK, sum_res.data
        data = sum_res.data

        assert data["po_id"] == po.po_id
        assert data["po_number"] == po.po_number
        assert data["total_ordered_units"] == 100
        assert data["total_received_units"] == 60
        assert data["total_cancelled_units"] == 10
        assert data["total_backordered_units"] == 30
        assert len(data["items"]) == 1
        item_data = data["items"][0]
        assert item_data["item_id"] == po_item.item_id
        assert item_data["ordered_quantity"] == 100
        assert item_data["received_quantity"] == 60
        assert item_data["cancelled_quantity"] == 10
        assert item_data["backordered_quantity"] == 30
        assert item_data["delivery_status"] == DeliveryStatus.PARTIALLY_DELIVERED
        assert len(data["goods_receipts"]) == 1
        assert data["goods_receipts"][0]["challan_number"] == "CHAL-SUMM-1"

    def test_10_rbac_and_tenant_isolation(self):
        """10. RBAC and tenant isolation: Cashier blocked from cancelling backorders and foreign tenant blocked."""
        po, po_item = self._create_single_line_po(100)

        # 1. Cashier role blocked from cancel-backorder
        self.client.force_authenticate(user=self.cashier)
        csh_cancel = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel-backorder/",
            {
                "item_id": po_item.item_id,
                "cancel_quantity": 10,
                "reason": "Cashier unauthorized attempt",
            },
            format="json",
        )
        assert csh_cancel.status_code == status.HTTP_403_FORBIDDEN
        assert csh_cancel.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Cashier role blocked from authorizing excess delivery
        csh_excess = self.client.post(
            "/api/v2/procurement/goods-receipts/",
            {
                "branch_id": self.branch.branch_id,
                "purchase_order_id": po.po_id,
                "challan_number": "CHAL-CSH-EXCESS",
                "allow_excess": True,
                "excess_reason": "Cashier approving excess",
                "lines": [
                    {
                        "po_item_id": po_item.item_id,
                        "product_id": self.napa.product_id,
                        "batch_number": "BX-CSH-01",
                        "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                        "received_quantity_base_units": 120,
                        "unit_cost": "2.2000",
                        "mrp_unit": "3.0000",
                    }
                ],
            },
            format="json",
        )
        assert csh_excess.status_code == status.HTTP_403_FORBIDDEN
        assert csh_excess.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 3. Foreign tenant blocked from cancel-backorder
        self.client.force_authenticate(user=self.foreign_manager)
        foreign_cancel = self.client.post(
            f"/api/v2/procurement/orders/{po.po_id}/cancel-backorder/",
            {
                "item_id": po_item.item_id,
                "cancel_quantity": 10,
                "reason": "Foreign tenant attempt",
            },
            format="json",
        )
        assert foreign_cancel.status_code == status.HTTP_403_FORBIDDEN
        assert foreign_cancel.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

        # 4. Foreign tenant blocked from delivery-summary
        foreign_summary = self.client.get(f"/api/v2/procurement/orders/{po.po_id}/delivery-summary/")
        assert foreign_summary.status_code == status.HTTP_403_FORBIDDEN
        assert foreign_summary.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

