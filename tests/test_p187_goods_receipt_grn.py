"""
backend/tests/test_p187_goods_receipt_grn.py
Architecture Decision: ADR-0187 (P187).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P187 Goods Receipt (GRN):
1. Create valid GRN matching PO lines with batch, expiry, challan, and storage verification.
2. Post GRN to stock: increments BatchStock, creates PurchaseCostLot, updates PO item received_quantity,
   transitions PO status to CLOSED, and confirms AP liability (ap_posting_status="POSTED").
3. Idempotent stock posting: secondary post attempt on already posted GRN returns HTTP 400 ERR_ALREADY_POSTED.
4. Cold-chain storage inspection guard: temperature > 8.0°C or storage_check_passed=False returns HTTP 400 ERR_STORAGE_CHECK_FAILED.
5. Expired batch receipt guard: batch with expiry_date <= today returns HTTP 400 ERR_EXPIRED_BATCH_RECEIVED.
6. Over-receipt guard: receiving quantity exceeding remaining unreceived PO units returns HTTP 400 ERR_EXCESS_RECEIPT.
7. Unreceivable PO guard: attempting receipt against DRAFT or CLOSED PO returns HTTP 400 ERR_PO_NOT_RECEIVABLE.
8. Reverse posted GRN: restores batch stock and cost lots; blocks reversal if stock is already dispensed (ERR_CANNOT_REVERSE_CONSUMED_STOCK).
9. Cross-tenant isolation: foreign tenant barred from creating or viewing GRN on another tenant's PO (HTTP 403 ERR_CROSS_TENANT_ACCESS).
10. RBAC and tenant status: Cashier blocked (HTTP 403 ERR_STAFF_ROLE_DENIED) and suspended tenant blocked (HTTP 403 ERR_TENANT_SUSPENDED).
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
    GoodsReceiptStatus,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestGoodsReceiptIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p187-{self.suffix}",
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

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name="Suspended Pharma Ltd",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 4. Users
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

        # 5. Suppliers
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-beximco-{self.suffix}",
            tenant=self.tenant,
            name="Beximco Pharmaceuticals Ltd.",
            contact_person="M. Rahman",
            phone="+8801711223344",
            email="supply@beximco.com",
            is_active=True,
        )

        # 6. Products
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

    def _create_authorized_po(self) -> PurchaseOrder:
        """Helper to create and advance a PO to ORDERED status with 2 items."""
        po = PurchaseOrder.objects.create(
            po_id=f"po-{uuid.uuid4().hex[:8]}",
            po_number=f"PO-{timezone.now().strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            status=PurchaseOrderStatus.ORDERED,
            subtotal_amount=Decimal("37200.0000"),
            total_amount=Decimal("37200.0000"),
            created_by=self.manager,
            is_active=True,
        )
        # Line 1: Insulin 100 vials @ 350.00
        PurchaseOrderItem.objects.create(
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
        # Line 2: Napa 1000 tablets (100 boxes of 10) @ 2.20
        PurchaseOrderItem.objects.create(
            item_id=f"poi-napa-{uuid.uuid4().hex[:8]}",
            purchase_order=po,
            product=self.napa,
            pack_unit="BOX",
            package_size=10,
            ordered_packages=100,
            ordered_quantity=1000,
            received_quantity=0,
            unit_cost_price=Decimal("2.2000"),
            mrp_unit=Decimal("3.0000"),
            line_subtotal=Decimal("2200.0000"),
            line_total=Decimal("2200.0000"),
        )
        return po

    def test_01_valid_grn_creation_with_storage_verification(self):
        """1. Create valid GRN matching PO lines with batch, expiry, challan, and cold chain verification."""
        po = self._create_authorized_po()
        po_item = po.items.filter(product=self.insulin).first()

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "challan_number": "CHAL-88912",
            "challan_date": timezone.now().date().isoformat(),
            "receipt_date": timezone.now().date().isoformat(),
            "storage_condition": "COLD_CHAIN",
            "storage_temperature_celsius": "4.5",
            "storage_check_passed": True,
            "proof_of_origin": "Beximco Cold Chain Distribution Slip",
            "notes": "Direct van delivery, intact ice packs",
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-INS-2026-A",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                    "mfg_date": (timezone.now().date() - datetime.timedelta(days=30)).isoformat(),
                    "pack_unit": "VIAL",
                    "package_size": 1,
                    "received_packages": 100,
                    "received_quantity_base_units": 100,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                    "storage_location": "COLD_ROOM_SHELF_2",
                }
            ],
        }

        response = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert data["status"] == GoodsReceiptStatus.RECEIVED
        assert data["ap_posting_status"] == "PENDING"
        assert data["storage_condition"] == "COLD_CHAIN"
        assert Decimal(str(data["storage_temperature_celsius"])) == Decimal("4.5")
        assert data["total_received_base_units"] == 100
        assert Decimal(str(data["total_invoice_amount"])) == Decimal("35000.0000")
        assert len(data["lines"]) == 1
        assert data["lines"][0]["batch_number"] == "BX-INS-2026-A"

    def test_02_post_grn_to_stock_and_ap_liability_confirmation(self):
        """2. Post GRN to stock: increments BatchStock, creates PurchaseCostLot, updates PO item received_quantity,
        transitions PO status to CLOSED when all fulfilled, and confirms AP liability."""
        po = self._create_authorized_po()
        po_ins = po.items.filter(product=self.insulin).first()
        po_napa = po.items.filter(product=self.napa).first()

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "challan_number": "CHAL-FULL-01",
            "storage_condition": "ROOM_TEMPERATURE",
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_ins.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-INS-LOT-1",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=400)).isoformat(),
                    "received_quantity_base_units": 100,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                },
                {
                    "po_item_id": po_napa.item_id,
                    "product_id": self.napa.product_id,
                    "batch_number": "BX-NAPA-LOT-2",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=700)).isoformat(),
                    "received_packages": 100,
                    "package_size": 10,
                    "received_quantity_base_units": 1000,
                    "unit_cost": "2.2000",
                    "mrp_unit": "3.0000",
                },
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert create_res.status_code == status.HTTP_201_CREATED
        grn_id = create_res.data["grn_id"]

        # Post receipt to stock
        post_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")
        assert post_res.status_code == status.HTTP_200_OK, post_res.data
        grn_data = post_res.data
        assert grn_data["status"] == GoodsReceiptStatus.POSTED
        assert grn_data["ap_posting_status"] == "POSTED"
        assert Decimal(str(grn_data["ap_invoice_amount"])) == Decimal("37200.0000")

        # Verify BatchStock increments
        ins_batch = BatchStock.objects.get(product=self.insulin, batch_number="BX-INS-LOT-1")
        assert ins_batch.quantity_base_units == 100
        assert ins_batch.status == BatchStatus.ACTIVE

        napa_batch = BatchStock.objects.get(product=self.napa, batch_number="BX-NAPA-LOT-2")
        assert napa_batch.quantity_base_units == 1000

        # Verify PurchaseCostLots created for FEFO
        ins_lot = PurchaseCostLot.objects.get(product=self.insulin, batch=ins_batch)
        assert ins_lot.initial_quantity == 100
        assert ins_lot.remaining_quantity == 100
        assert ins_lot.status == PurchaseCostLotStatus.ACTIVE

        # Verify PO items and status updated to CLOSED
        po.refresh_from_db()
        po_ins.refresh_from_db()
        po_napa.refresh_from_db()
        assert po_ins.received_quantity == 100
        assert po_napa.received_quantity == 1000
        assert po.status == PurchaseOrderStatus.CLOSED
        assert po.closed_at is not None

    def test_03_idempotent_stock_posting_guard(self):
        """3. Secondary post attempt on already posted GRN returns HTTP 400 ERR_ALREADY_POSTED."""
        po = self._create_authorized_po()
        po_ins = po.items.filter(product=self.insulin).first()

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "ROOM_TEMPERATURE",
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_ins.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-IDEM-01",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=300)).isoformat(),
                    "received_quantity_base_units": 50,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        grn_id = create_res.data["grn_id"]

        # First post succeeds
        res1 = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")
        assert res1.status_code == status.HTTP_200_OK

        # Duplicate post rejected
        res2 = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")
        assert res2.status_code == status.HTTP_400_BAD_REQUEST
        assert res2.data["error_code"] == "ERR_ALREADY_POSTED"

    def test_04_cold_chain_storage_inspection_guard(self):
        """4. Cold-chain storage inspection guard: temperature outside 2-8°C or check failed returns ERR_STORAGE_CHECK_FAILED."""
        po = self._create_authorized_po()
        po_ins = po.items.filter(product=self.insulin).first()

        self.client.force_authenticate(user=self.manager)

        # Case A: Temperature too high (12.5 C)
        payload_hot = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "COLD_CHAIN",
            "storage_temperature_celsius": "12.5",
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_ins.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-HOT-01",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=300)).isoformat(),
                    "received_quantity_base_units": 10,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }
        res_hot = self.client.post("/api/v2/procurement/goods-receipts/", payload_hot, format="json")
        assert res_hot.status_code == status.HTTP_400_BAD_REQUEST
        assert res_hot.data["error_code"] == "ERR_STORAGE_CHECK_FAILED"
        assert "Cold-chain temperature violation" in res_hot.data["detail"]

        # Case B: Inspection check failed (broken vials / thawed)
        payload_failed = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "COLD_CHAIN",
            "storage_temperature_celsius": "4.0",
            "storage_check_passed": False,
            "lines": [
                {
                    "po_item_id": po_ins.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-THAW-01",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=300)).isoformat(),
                    "received_quantity_base_units": 10,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }
        res_failed = self.client.post("/api/v2/procurement/goods-receipts/", payload_failed, format="json")
        assert res_failed.status_code == status.HTTP_400_BAD_REQUEST
        assert res_failed.data["error_code"] == "ERR_STORAGE_CHECK_FAILED"

    def test_05_expired_batch_receipt_guard(self):
        """5. Expired batch receipt guard: batch with expiry_date <= today returns HTTP 400 ERR_EXPIRED_BATCH_RECEIVED."""
        po = self._create_authorized_po()
        po_napa = po.items.filter(product=self.napa).first()

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "ROOM_TEMPERATURE",
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_napa.item_id,
                    "product_id": self.napa.product_id,
                    "batch_number": "BX-EXPIRED-01",
                    "expiry_date": (timezone.now().date() - datetime.timedelta(days=1)).isoformat(),
                    "received_quantity_base_units": 100,
                    "unit_cost": "2.2000",
                    "mrp_unit": "3.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_EXPIRED_BATCH_RECEIVED"

    def test_06_over_receipt_guard(self):
        """6. Over-receipt guard: receiving quantity exceeding remaining unreceived PO units returns ERR_EXCESS_RECEIPT."""
        po = self._create_authorized_po()
        po_ins = po.items.filter(product=self.insulin).first()  # Ordered 100

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "ROOM_TEMPERATURE",
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_ins.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-EXCESS-01",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=200)).isoformat(),
                    "received_quantity_base_units": 101,  # Exceeds 100
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_EXCESS_RECEIPT"

    def test_07_unreceivable_po_guard(self):
        """7. Unreceivable PO guard: attempting receipt against DRAFT or CLOSED PO returns ERR_PO_NOT_RECEIVABLE."""
        draft_po = PurchaseOrder.objects.create(
            po_id=f"po-draft-{uuid.uuid4().hex[:8]}",
            po_number=f"PO-DRAFT-{uuid.uuid4().hex[:4]}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            status=PurchaseOrderStatus.DRAFT,
            is_active=True,
        )
        PurchaseOrderItem.objects.create(
            item_id=f"poi-draft-{uuid.uuid4().hex[:8]}",
            purchase_order=draft_po,
            product=self.insulin,
            ordered_quantity=10,
            unit_cost_price=Decimal("350.0000"),
            line_subtotal=Decimal("3500.0000"),
            line_total=Decimal("3500.0000"),
        )

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": draft_po.po_id,
            "storage_condition": "ROOM_TEMPERATURE",
            "storage_check_passed": True,
            "lines": [
                {
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-DRAFT-REC",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=200)).isoformat(),
                    "received_quantity_base_units": 10,
                    "unit_cost": "350.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_PO_NOT_RECEIVABLE"

    def test_08_reverse_posted_grn_and_consumed_stock_guard(self):
        """8. Reverse posted GRN: restores batch stock and cost lots; blocks reversal if stock is already dispensed."""
        po = self._create_authorized_po()
        po_napa = po.items.filter(product=self.napa).first()

        self.client.force_authenticate(user=self.manager)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "ROOM_TEMPERATURE",
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_napa.item_id,
                    "product_id": self.napa.product_id,
                    "batch_number": "BX-REV-01",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=365)).isoformat(),
                    "received_quantity_base_units": 200,
                    "unit_cost": "2.2000",
                    "mrp_unit": "3.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        grn_id = create_res.data["grn_id"]
        self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")

        # Case A: Simulate partial dispensing from this cost lot
        cost_lot = PurchaseCostLot.objects.get(grn_number=create_res.data["grn_number"])
        cost_lot.remaining_quantity = 150  # 50 dispensed
        cost_lot.save()

        # Reversal must be blocked
        rev_fail = self.client.post(
            f"/api/v2/procurement/goods-receipts/{grn_id}/reverse-receipt/",
            {"reason": "Supplier recalled lot"},
            format="json",
        )
        assert rev_fail.status_code == status.HTTP_400_BAD_REQUEST
        assert rev_fail.data["error_code"] == "ERR_CANNOT_REVERSE_CONSUMED_STOCK"

        # Case B: Restore cost lot remaining quantity to 200 (unconsumed) and reverse cleanly
        cost_lot.remaining_quantity = 200
        cost_lot.save()

        rev_success = self.client.post(
            f"/api/v2/procurement/goods-receipts/{grn_id}/reverse-receipt/",
            {"reason": "Packaging compromised during transit"},
            format="json",
        )
        assert rev_success.status_code == status.HTTP_200_OK
        assert rev_success.data["status"] == GoodsReceiptStatus.REVERSED

        batch = BatchStock.objects.get(product=self.napa, batch_number="BX-REV-01")
        assert batch.quantity_base_units == 0
        assert batch.status == BatchStatus.DEPLETED

        cost_lot.refresh_from_db()
        assert cost_lot.status == PurchaseCostLotStatus.REVERSED

    def test_09_cross_tenant_isolation_guards(self):
        """9. Foreign tenant barred from creating or viewing GRN on another tenant's PO (HTTP 403)."""
        po = self._create_authorized_po()

        # Foreign manager tries to view or create against PO of primary tenant
        self.client.force_authenticate(user=self.foreign_manager)
        payload = {
            "branch_id": self.foreign_branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "ROOM_TEMPERATURE",
            "storage_check_passed": True,
            "lines": [
                {
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-FOREIGN-ATTEMPT",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=200)).isoformat(),
                    "received_quantity_base_units": 10,
                    "unit_cost": "350.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert res.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_10_rbac_and_tenant_lifecycle_guards(self):
        """10. Cashier role blocked (ERR_STAFF_ROLE_DENIED) and suspended tenant blocked (ERR_TENANT_SUSPENDED)."""
        po = self._create_authorized_po()

        # Cashier blocked
        self.client.force_authenticate(user=self.cashier)
        payload = {
            "branch_id": self.branch.branch_id,
            "purchase_order_id": po.po_id,
            "storage_condition": "ROOM_TEMPERATURE",
            "lines": [
                {
                    "product_id": self.insulin.product_id,
                    "batch_number": "BX-CASHIER-TRY",
                    "expiry_date": (timezone.now().date() + datetime.timedelta(days=200)).isoformat(),
                    "received_quantity_base_units": 10,
                    "unit_cost": "350.0000",
                }
            ],
        }
        res_cashier = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res_cashier.status_code == status.HTTP_403_FORBIDDEN
        assert res_cashier.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # Suspended tenant blocked
        susp_manager = PharmacyUser.objects.create_user(
            email=f"susp_mgr_{self.suffix}@susp.com",
            phone_number=f"+880179{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )
        self.client.force_authenticate(user=susp_manager)
        res_susp = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res_susp.status_code == status.HTTP_403_FORBIDDEN
        susp_data = res_susp.data if hasattr(res_susp, "data") else res_susp.json()
        assert susp_data.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]

