"""
backend/tests/test_p168_batch_receipt_linking.py
Architecture Decision: ADR-0168 (P168).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Goods Receipt Note (GRN) to verified batch linking,
multi-batch split allocations, unblended cost lot binding, and anti-duplicate
inventory increment guardrails.
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

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    GoodsReceiptBatchLink,
    GoodsReceiptLine,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    MedicineProduct,
    PlanTier,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestBatchReceiptLinkingIntegration:
    """Integration test suite for P168 Batch Receipt Linking in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        # 1. Tenant A & Branch A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p168-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p168-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p168-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Tenant B & Branch B (cross-tenant tests)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p168-b-{self.suffix}",
            name=f"Beximco Pharmacy {self.suffix}",
            subdomain=f"beximco-p168-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p168-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p168-susp-{self.suffix}",
            name="Suspended Tenant",
            subdomain=f"susp-p168-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p168-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Users
        self.pharmacist_user = PharmacyUser.objects.create_user(
            email=f"pharmacist-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Staff Pharmacist",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.branch_manager_user = PharmacyUser.objects.create_user(
            email=f"manager-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier User",
            password="Password123!",
            role=UserRole.CASHIER,
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
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

        # 5. Products & Batches
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"med-p168-napa-{self.suffix}",
            brand_name="Napa 500mg Tablet",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="500mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("1.2000"),
            mrp_unit=Decimal("1.5000"),
        )

        self.batch_1 = BatchStock.objects.create(
            batch_id=f"bat-p168-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"BX-2026-A-{self.suffix}",
            quantity_base_units=0,
            cost_per_unit=Decimal("1.20"),
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=365),
            status=BatchStatus.ACTIVE,
        )

        self.batch_2 = BatchStock.objects.create(
            batch_id=f"bat-p168-2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"BX-2026-B-{self.suffix}",
            quantity_base_units=0,
            cost_per_unit=Decimal("1.20"),
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=400),
            status=BatchStatus.ACTIVE,
        )

        self.batch_expired = BatchStock.objects.create(
            batch_id=f"bat-p168-exp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"BX-EXP-{self.suffix}",
            quantity_base_units=0,
            cost_per_unit=Decimal("1.20"),
            purchase_date=self.today - datetime.timedelta(days=400),
            expiry_date=self.today - datetime.timedelta(days=10),
            status=BatchStatus.EXPIRED,
        )

        self.batch_recalled = BatchStock.objects.create(
            batch_id=f"bat-p168-rec-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"BX-REC-{self.suffix}",
            quantity_base_units=0,
            cost_per_unit=Decimal("1.20"),
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=365),
            status=BatchStatus.RECALLED,
            recall_reason="DGDA Notice 2026-04",
        )

        # 6. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-p168-{self.suffix}",
            tenant=self.tenant_a,
            name="Beximco Distribution",
            phone="+8801700000000",
            email="distrib@beximco.com",
            is_active=True,
        )

    def test_01_create_grn_header_and_add_line(self):
        """Creates GRN header and appends a line item successfully."""
        self.client.force_authenticate(user=self.pharmacist_user)

        # 1. Create GRN
        res = self.client.post(
            "/api/v2/inventory/goods-receipts/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "supplier_id": self.supplier.supplier_id,
                "purchase_order_number": "PO-2026-001",
                "supplier_invoice_number": "INV-BX-9988",
                "receipt_date": str(self.today),
                "notes": "Urgent medicine shipment",
            },
            format="json",
        )
        assert res.status_code == status.HTTP_201_CREATED, res.data
        grn_id = res.data["grn_id"]
        assert res.data["status"] == "DRAFT"
        assert res.data["purchase_order_number"] == "PO-2026-001"

        # 2. Add Line Item (500 units)
        res_line = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn_id}/add-line/",
            {
                "product_id": self.product_napa.product_id,
                "ordered_quantity_base_units": 500,
                "received_quantity_base_units": 500,
                "unit_cost": "1.2000",
            },
            format="json",
        )
        assert res_line.status_code == status.HTTP_201_CREATED, res_line.data
        line_id = res_line.data["line_id"]
        assert line_id is not None
        assert res_line.data["received_quantity_base_units"] == 500
        assert res_line.data["is_fully_allocated"] is False

    def test_02_multi_batch_split_allocation_to_single_line(self):
        """Binds multiple distinct manufacturer batches to a single receipt line."""
        self.client.force_authenticate(user=self.branch_manager_user)

        # 1. Create GRN & Line
        grn_res = self.client.post(
            "/api/v2/inventory/goods-receipts/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "supplier_id": self.supplier.supplier_id,
                "purchase_order_number": "PO-MULTI-BATCH",
            },
            format="json",
        )
        grn_id = grn_res.data["grn_id"]

        line_res = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn_id}/add-line/",
            {
                "product_id": self.product_napa.product_id,
                "ordered_quantity_base_units": 500,
                "received_quantity_base_units": 500,
                "unit_cost": "1.2000",
            },
            format="json",
        )
        line_id = line_res.data["line_id"]

        # 2. Allocate 300 units to Batch 1
        link1_res = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn_id}/link-batch/",
            {
                "line_id": line_id,
                "batch_id": self.batch_1.batch_id,
                "quantity_base_units": 300,
                "unit_cost": "1.2000",
            },
            format="json",
        )
        assert link1_res.status_code == status.HTTP_200_OK, link1_res.data
        assert link1_res.data["quantity_base_units"] == 300
        assert link1_res.data["is_posted"] is False

        # 3. Allocate 200 units to Batch 2
        link2_res = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn_id}/link-batch/",
            {
                "line_id": line_id,
                "batch_id": self.batch_2.batch_id,
                "quantity_base_units": 200,
                "unit_cost": "1.2000",
            },
            format="json",
        )
        assert link2_res.status_code == status.HTTP_200_OK, link2_res.data
        assert link2_res.data["quantity_base_units"] == 200

        # 4. Verify line state
        grn_detail = self.client.get(f"/api/v2/inventory/goods-receipts/{grn_id}/?tenant_id={self.tenant_a.tenant_id}")
        assert grn_detail.status_code == status.HTTP_200_OK
        line_data = grn_detail.data["lines"][0]
        assert line_data["allocated_quantity_base_units"] == 500
        assert line_data["is_fully_allocated"] is True
        assert len(line_data["batch_allocations"]) == 2

    def test_03_allocation_exceeding_line_received_quantity_rejected(self):
        """Rejects batch allocations when sum of allocations exceeds line received quantity."""
        self.client.force_authenticate(user=self.pharmacist_user)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-exc-{self.suffix}",
            grn_number=f"GRN-EXC-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            receipt_date=self.today,
            status=GoodsReceiptStatus.DRAFT,
        )
        line = GoodsReceiptLine.objects.create(
            line_id=f"line-exc-{self.suffix}",
            grn=grn,
            tenant=self.tenant_a,
            product=self.product_napa,
            ordered_quantity_base_units=500,
            received_quantity_base_units=500,
            unit_cost=Decimal("1.2000"),
        )

        # Allocate 400 to batch 1
        self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn.grn_id}/link-batch/",
            {"line_id": line.line_id, "batch_id": self.batch_1.batch_id, "quantity_base_units": 400},
            format="json",
        )

        # Attempt to allocate 150 to batch 2 (400 + 150 = 550 > 500)
        res = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn.grn_id}/link-batch/",
            {"line_id": line.line_id, "batch_id": self.batch_2.batch_id, "quantity_base_units": 150},
            format="json",
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_ALLOCATION_EXCEEDS_LINE"

    def test_04_incomplete_allocation_blocks_posting(self):
        """Posting is blocked when line is partially allocated."""
        self.client.force_authenticate(user=self.pharmacist_user)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-inc-{self.suffix}",
            grn_number=f"GRN-INC-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            receipt_date=self.today,
            status=GoodsReceiptStatus.RECEIVED,
        )
        line = GoodsReceiptLine.objects.create(
            line_id=f"line-inc-{self.suffix}",
            grn=grn,
            tenant=self.tenant_a,
            product=self.product_napa,
            ordered_quantity_base_units=500,
            received_quantity_base_units=500,
            unit_cost=Decimal("1.2000"),
        )

        # Allocate only 300 out of 500 units
        self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn.grn_id}/link-batch/",
            {"line_id": line.line_id, "batch_id": self.batch_1.batch_id, "quantity_base_units": 300},
            format="json",
        )

        # Post attempt should fail
        res = self.client.post(f"/api/v2/inventory/goods-receipts/{grn.grn_id}/post-receipt/", format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_ALLOCATION_MISMATCH"

        # Verify stock was NOT touched
        self.batch_1.refresh_from_db()
        assert self.batch_1.quantity_base_units == 0

    def test_05_post_grn_increments_stock_and_creates_cost_lots_exactly_once(self):
        """Posting GRN creates distinct PurchaseCostLots and increments BatchStock exactly once."""
        self.client.force_authenticate(user=self.branch_manager_user)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-post-{self.suffix}",
            grn_number=f"GRN-POST-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            purchase_order_number="PO-FINAL-POST",
            supplier_invoice_number="INV-2026-99",
            receipt_date=self.today,
            status=GoodsReceiptStatus.RECEIVED,
        )
        line = GoodsReceiptLine.objects.create(
            line_id=f"line-post-{self.suffix}",
            grn=grn,
            tenant=self.tenant_a,
            product=self.product_napa,
            ordered_quantity_base_units=500,
            received_quantity_base_units=500,
            unit_cost=Decimal("1.2000"),
        )
        link1 = GoodsReceiptBatchLink.objects.create(
            link_id=f"link1-{self.suffix}",
            tenant=self.tenant_a,
            grn=grn,
            line=line,
            batch=self.batch_1,
            quantity_base_units=300,
            unit_cost=Decimal("1.2000"),
            is_posted=False,
        )
        link2 = GoodsReceiptBatchLink.objects.create(
            link_id=f"link2-{self.suffix}",
            tenant=self.tenant_a,
            grn=grn,
            line=line,
            batch=self.batch_2,
            quantity_base_units=200,
            unit_cost=Decimal("1.2000"),
            is_posted=False,
        )
        line.is_fully_allocated = True
        line.save()

        # Execute Post
        res = self.client.post(f"/api/v2/inventory/goods-receipts/{grn.grn_id}/post-receipt/", format="json")
        assert res.status_code == status.HTTP_200_OK, res.data
        assert res.data["status"] == "POSTED"

        # Verify stock incremented exactly once
        self.batch_1.refresh_from_db()
        self.batch_2.refresh_from_db()
        assert self.batch_1.quantity_base_units == 300
        assert self.batch_2.quantity_base_units == 200

        # Verify links marked posted
        link1.refresh_from_db()
        link2.refresh_from_db()
        assert link1.is_posted is True
        assert link2.is_posted is True
        assert link1.cost_lot_id is not None
        assert link2.cost_lot_id is not None

        # Verify PurchaseCostLots created
        lot1 = PurchaseCostLot.objects.get(lot_id=link1.cost_lot_id)
        lot2 = PurchaseCostLot.objects.get(lot_id=link2.cost_lot_id)
        assert lot1.initial_quantity == 300
        assert lot1.remaining_quantity == 300
        assert lot1.status == PurchaseCostLotStatus.ACTIVE
        assert lot2.initial_quantity == 200
        assert lot2.remaining_quantity == 200

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(
            tenant_id=self.tenant_a.tenant_id,
            resource_type="GoodsReceiptNote",
            resource_id=grn.grn_id,
            action="GRN_POSTED",
        ).first()
        assert audit is not None

    def test_06_anti_duplicate_posting_guardrail(self):
        """Strictly blocks re-posting an already posted GRN to prevent duplicate stock increments."""
        self.client.force_authenticate(user=self.branch_manager_user)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-dup-{self.suffix}",
            grn_number=f"GRN-DUP-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            receipt_date=self.today,
            status=GoodsReceiptStatus.POSTED,
        )

        res = self.client.post(f"/api/v2/inventory/goods-receipts/{grn.grn_id}/post-receipt/", format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_ALREADY_POSTED"

    def test_07_binding_expired_or_recalled_batch_blocked(self):
        """Blocks binding expired or recalled batches to incoming receipt line."""
        self.client.force_authenticate(user=self.pharmacist_user)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-expcheck-{self.suffix}",
            grn_number=f"GRN-EXP-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            receipt_date=self.today,
            status=GoodsReceiptStatus.DRAFT,
        )
        line = GoodsReceiptLine.objects.create(
            line_id=f"line-expcheck-{self.suffix}",
            grn=grn,
            tenant=self.tenant_a,
            product=self.product_napa,
            ordered_quantity_base_units=100,
            received_quantity_base_units=100,
            unit_cost=Decimal("1.2000"),
        )

        # 1. Expired batch
        res_exp = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn.grn_id}/link-batch/",
            {"line_id": line.line_id, "batch_id": self.batch_expired.batch_id, "quantity_base_units": 50},
            format="json",
        )
        assert res_exp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res_exp.data.get("error_code") == "ERR_CANNOT_RECEIVE_INTO_EXPIRED_BATCH"

        # 2. Recalled batch
        res_rec = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn.grn_id}/link-batch/",
            {"line_id": line.line_id, "batch_id": self.batch_recalled.batch_id, "quantity_base_units": 50},
            format="json",
        )
        assert res_rec.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res_rec.data.get("error_code") == "ERR_CANNOT_RECEIVE_INTO_RECALLED_BATCH"

    def test_08_reversal_of_posted_grn_deducts_stock_cleanly(self):
        """Reversing a posted GRN cleanly deducts inventory and marks cost lots reversed."""
        self.client.force_authenticate(user=self.branch_manager_user)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-rev-{self.suffix}",
            grn_number=f"GRN-REV-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            receipt_date=self.today,
            status=GoodsReceiptStatus.POSTED,
        )
        line = GoodsReceiptLine.objects.create(
            line_id=f"line-rev-{self.suffix}",
            grn=grn,
            tenant=self.tenant_a,
            product=self.product_napa,
            ordered_quantity_base_units=100,
            received_quantity_base_units=100,
            unit_cost=Decimal("1.2000"),
            is_fully_allocated=True,
        )
        cost_lot = PurchaseCostLot.objects.create(
            lot_id=f"pcl-rev-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch=self.batch_1,
            purchase_date=self.today,
            unit_cost=Decimal("1.2000"),
            initial_quantity=100,
            remaining_quantity=100,
            status=PurchaseCostLotStatus.ACTIVE,
        )
        link = GoodsReceiptBatchLink.objects.create(
            link_id=f"link-rev-{self.suffix}",
            tenant=self.tenant_a,
            grn=grn,
            line=line,
            batch=self.batch_1,
            cost_lot=cost_lot,
            quantity_base_units=100,
            unit_cost=Decimal("1.2000"),
            is_posted=True,
        )

        # Set initial stock to 100
        self.batch_1.quantity_base_units = 100
        self.batch_1.save()

        # Reversal
        res = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn.grn_id}/reverse-receipt/",
            {"reason": "Damaged goods return to manufacturer"},
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        assert res.data["status"] == "REVERSED"

        # Verify stock deducted
        self.batch_1.refresh_from_db()
        assert self.batch_1.quantity_base_units == 0
        assert self.batch_1.status == BatchStatus.DEPLETED

        # Verify cost lot reversed
        cost_lot.refresh_from_db()
        assert cost_lot.remaining_quantity == 0
        assert cost_lot.status == PurchaseCostLotStatus.REVERSED

        link.refresh_from_db()
        assert link.is_posted is False

    def test_09_reversal_blocked_when_stock_already_consumed(self):
        """Reversal is blocked if any portion of the received stock was already dispensed."""
        self.client.force_authenticate(user=self.branch_manager_user)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-cons-{self.suffix}",
            grn_number=f"GRN-CONS-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            receipt_date=self.today,
            status=GoodsReceiptStatus.POSTED,
        )
        line = GoodsReceiptLine.objects.create(
            line_id=f"line-cons-{self.suffix}",
            grn=grn,
            tenant=self.tenant_a,
            product=self.product_napa,
            ordered_quantity_base_units=100,
            received_quantity_base_units=100,
            unit_cost=Decimal("1.2000"),
        )
        cost_lot = PurchaseCostLot.objects.create(
            lot_id=f"pcl-cons-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch=self.batch_1,
            purchase_date=self.today,
            unit_cost=Decimal("1.2000"),
            initial_quantity=100,
            remaining_quantity=30,  # 70 units already dispensed!
            status=PurchaseCostLotStatus.ACTIVE,
        )
        GoodsReceiptBatchLink.objects.create(
            link_id=f"link-cons-{self.suffix}",
            tenant=self.tenant_a,
            grn=grn,
            line=line,
            batch=self.batch_1,
            cost_lot=cost_lot,
            quantity_base_units=100,
            unit_cost=Decimal("1.2000"),
            is_posted=True,
        )
        self.batch_1.quantity_base_units = 30
        self.batch_1.save()

        # Reversal attempt
        res = self.client.post(
            f"/api/v2/inventory/goods-receipts/{grn.grn_id}/reverse-receipt/",
            {"reason": "Wrong delivery"},
            format="json",
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_CANNOT_REVERSE_CONSUMED_STOCK"

        # Ensure stock remained unchanged
        self.batch_1.refresh_from_db()
        assert self.batch_1.quantity_base_units == 30

    def test_10_rbac_unauthorized_role_blocked(self):
        """Cashiers cannot create, allocate, post, or reverse goods receipts."""
        self.client.force_authenticate(user=self.cashier_user)

        res = self.client.post(
            "/api/v2/inventory/goods-receipts/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert res.data.get("error_code") == "ERR_ROLE_UNAUTHORIZED"

    def test_11_cross_tenant_isolation_blocked(self):
        """Tenant B user cannot access or modify Tenant A goods receipts."""
        self.client.force_authenticate(user=self.user_b)

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-cross-{self.suffix}",
            grn_number=f"GRN-CROSS-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            receipt_date=self.today,
            status=GoodsReceiptStatus.DRAFT,
        )

        res = self.client.get(f"/api/v2/inventory/goods-receipts/{grn.grn_id}/?tenant_id={self.tenant_a.tenant_id}")
        assert res.status_code == status.HTTP_403_FORBIDDEN
        payload = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert payload.get("error_code") in ("ERR_TENANT_MISMATCH", "ERR_CROSS_TENANT_VIOLATION")

    def test_12_suspended_tenant_blocked(self):
        """Suspended tenants cannot create or manage goods receipts."""
        self.client.force_authenticate(user=self.user_susp)

        res = self.client.post(
            "/api/v2/inventory/goods-receipts/",
            {
                "tenant_id": self.tenant_suspended.tenant_id,
                "branch_id": self.branch_suspended.branch_id,
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

