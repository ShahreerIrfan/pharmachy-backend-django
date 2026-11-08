"""
backend/tests/test_p191_purchase_returns.py
Architecture Decision: ADR-0191 (P191).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P191 Supplier Purchase Returns & Accounts Payable Debit Notes:
1. Create draft return tied to batch and cost lot -> status is DRAFT, line amounts calculated correctly.
2. Post return -> BatchStock decremented, PurchaseCostLot decremented, status transitions to POSTED_DEBIT_NOTE.
3. AP Debit Note posting -> creates immutable SupplierLedgerEntry debit note, reducing payable balance.
4. Stock integrity guard -> attempting return exceeding available stock returns HTTP 400 ERR_INSUFFICIENT_STOCK_FOR_RETURN.
5. 100% batch return -> marks BatchStock.status=DEPLETED and PurchaseCostLot.status=DEPLETED.
6. Cancel draft return -> transitions status to CANCELLED.
7. Cannot cancel posted return -> returns HTTP 400 ERR_CANNOT_CANCEL_POSTED_RETURN.
8. Cannot post already posted return -> returns HTTP 400 ERR_ALREADY_POSTED.
9. List and filter returns -> filters by supplier_id and status.
10. RBAC and cross-tenant isolation -> Cashier blocked (403 ERR_STAFF_ROLE_DENIED), foreign tenant blocked.
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
    GoodsReceiptNote,
    GoodsReceiptStatus,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseReturn,
    PurchaseReturnItem,
    PurchaseReturnReason,
    PurchaseReturnStatus,
    StockDisposition,
    Supplier,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPurchaseReturnIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p191-{self.suffix}",
            name="Square Health Care",
            subdomain=f"square-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-sq-{self.suffix}",
            tenant=self.tenant,
            name="Main Dispensary",
        )

        # 2. Foreign Tenant & Branch
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-foreign-{self.suffix}",
            name="Popular Pharma Ltd",
            subdomain=f"popular-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-pop-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Popular Central",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Front Desk Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@popular.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-sq-{self.suffix}",
            tenant=self.tenant,
            name="Square Pharmaceuticals Ltd",
            contact_person="M. Rahman",
            phone="+8801700000000",
            email="supply@squarepharma.com.bd",
            is_active=True,
        )

        # 5. Product
        self.product = MedicineProduct.objects.create(
            product_id=f"med-ins-{self.suffix}",
            brand_name="Insulin Regular 100IU",
            generic_name="Human Insulin",
            strength="100 IU/ml",
            dosage_form="Injection",
            manufacturer="Square Pharmaceuticals PLC",
            cost_unit=Decimal("450.0000"),
            mrp_unit=Decimal("520.0000"),
        )

        # 6. Physical Batch Stock
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number=f"INSR-{self.suffix.upper()}",
            expiry_date=datetime.date.today() + datetime.timedelta(days=180),
            purchase_date=datetime.date.today(),
            quantity_base_units=100,
            reserved_quantity_base_units=0,
            cost_per_unit=Decimal("450.0000"),
            status=BatchStatus.ACTIVE,
        )

        # 7. Purchase Cost Lot
        self.cost_lot = PurchaseCostLot.objects.create(
            lot_id=f"lot-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch=self.batch,
            purchase_date=datetime.date.today(),
            initial_quantity=100,
            remaining_quantity=100,
            unit_cost=Decimal("450.0000"),
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # 8. PO and GRN
        self.po = PurchaseOrder.objects.create(
            po_id=f"po-{self.suffix}",
            po_number=f"PO-{self.suffix.upper()}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            status=PurchaseOrderStatus.PARTIALLY_RECEIVED,
            subtotal_amount=Decimal("45000.0000"),
            total_amount=Decimal("45000.0000"),
        )
        self.grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-{self.suffix}",
            grn_number=f"GRN-{self.suffix.upper()}",
            tenant=self.tenant,
            branch=self.branch,
            purchase_order=self.po,
            purchase_order_number=self.po.po_number,
            supplier=self.supplier,
            supplier_name=self.supplier.name,
            challan_number=f"CH-SQ-{self.suffix}",
            receipt_date=datetime.date.today(),
            status=GoodsReceiptStatus.POSTED,
            total_received_base_units=100,
            total_invoice_amount=Decimal("45000.0000"),
        )

    def test_01_create_draft_purchase_return(self):
        """Creates a draft supplier purchase return with calculated debit line totals."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "goods_receipt_id": self.grn.grn_id,
            "purchase_order_id": self.po.po_id,
            "return_reason": "DAMAGED_DELIVERY",
            "disposition": "RETURNED_TO_VENDOR",
            "reason_notes": "Damaged vials during carrier transit",
            "items": [
                {
                    "product_id": self.product.product_id,
                    "batch_stock_id": self.batch.batch_id,
                    "cost_lot_id": self.cost_lot.lot_id,
                    "return_quantity": 20,
                    "unit_cost": "450.0000",
                    "reason": "DAMAGED_DELIVERY",
                    "notes": "Seal broken on 20 vials",
                }
            ],
        }

        resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["status"] == "DRAFT"
        assert data["total_return_quantity"] == 20
        assert Decimal(data["total_debit_amount"]) == Decimal("9000.0000")  # 20 * 450
        assert len(data["items"]) == 1
        assert data["items"][0]["return_quantity"] == 20
        assert Decimal(data["items"][0]["line_debit_amount"]) == Decimal("9000.0000")

    def test_02_post_purchase_return_decrements_stock_and_lot(self):
        """Posting a purchase return atomically decrements BatchStock and PurchaseCostLot."""
        self.client.force_authenticate(user=self.manager)
        # Create draft return for 25 units
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "items": [
                {
                    "batch_stock_id": self.batch.batch_id,
                    "return_quantity": 25,
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        assert create_resp.status_code == status.HTTP_201_CREATED
        return_id = create_resp.json()["return_id"]

        # Post return
        post_resp = self.client.post(f"/api/v2/procurement/purchase-returns/{return_id}/post-return/")
        assert post_resp.status_code == status.HTTP_200_OK
        data = post_resp.json()
        assert data["status"] == "POSTED_DEBIT_NOTE"
        assert data["posted_at"] is not None

        # Verify BatchStock decremented: 100 - 25 = 75
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 75
        assert self.batch.status == BatchStatus.ACTIVE

        # Verify CostLot decremented: 100 - 25 = 75
        self.cost_lot.refresh_from_db()
        assert self.cost_lot.remaining_quantity == 75
        assert self.cost_lot.status == PurchaseCostLotStatus.ACTIVE

    def test_03_post_purchase_return_creates_ap_debit_ledger_entry(self):
        """Posting a purchase return issues a SupplierLedgerEntry debit note and reduces payable balance."""
        # Pre-seed an existing credit balance of ৳50,000 in the supplier ledger
        SupplierLedgerEntry.objects.create(
            posting_id=f"sup-init-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            posting_type=SupplierPostingType.GRN_INVOICE,
            reference_number="INV-INIT-001",
            debit_amount=Decimal("0.0000"),
            credit_amount=Decimal("50000.0000"),
            balance_after=Decimal("50000.0000"),
            currency="BDT",
        )

        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "debit_note_reference": f"DN-TEST-{self.suffix}",
            "items": [
                {
                    "batch_stock_id": self.batch.batch_id,
                    "return_quantity": 10,  # 10 * 450 = ৳4,500 debit
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        return_id = create_resp.json()["return_id"]

        post_resp = self.client.post(f"/api/v2/procurement/purchase-returns/{return_id}/post-return/")
        assert post_resp.status_code == status.HTTP_200_OK

        # Verify AP ledger entry
        ledger_entry = SupplierLedgerEntry.objects.filter(
            tenant=self.tenant,
            supplier=self.supplier,
            posting_type=SupplierPostingType.RETURN_DEBIT_NOTE,
        ).first()

        assert ledger_entry is not None
        assert ledger_entry.debit_amount == Decimal("4500.0000")
        assert ledger_entry.credit_amount == Decimal("0.0000")
        # 50,000 - 4,500 = 45,500
        assert ledger_entry.balance_after == Decimal("45500.0000")

    def test_04_insufficient_stock_guard(self):
        """Attempting to return more stock than is currently physically available throws ERR_INSUFFICIENT_STOCK_FOR_RETURN."""
        self.client.force_authenticate(user=self.manager)

        # Batch currently has 100 units. Attempt to return 120 units.
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "items": [
                {
                    "batch_stock_id": self.batch.batch_id,
                    "return_quantity": 120,
                }
            ],
        }

        resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        err = resp.json()
        assert err["error_code"] == "ERR_INSUFFICIENT_STOCK_FOR_RETURN"

    def test_05_complete_batch_return_marks_depleted(self):
        """Returning 100% of batch stock marks BatchStock and PurchaseCostLot as DEPLETED."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "items": [
                {
                    "batch_stock_id": self.batch.batch_id,
                    "return_quantity": 100,  # All 100 units returned
                }
            ],
        }
        create_resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        return_id = create_resp.json()["return_id"]

        post_resp = self.client.post(f"/api/v2/procurement/purchase-returns/{return_id}/post-return/")
        assert post_resp.status_code == status.HTTP_200_OK

        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 0
        assert self.batch.status == BatchStatus.DEPLETED

        self.cost_lot.refresh_from_db()
        assert self.cost_lot.remaining_quantity == 0
        assert self.cost_lot.status == PurchaseCostLotStatus.DEPLETED

    def test_06_cancel_draft_purchase_return(self):
        """Cancels an unposted draft purchase return."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "items": [{"batch_stock_id": self.batch.batch_id, "return_quantity": 5}],
        }
        create_resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        return_id = create_resp.json()["return_id"]

        cancel_resp = self.client.post(
            f"/api/v2/procurement/purchase-returns/{return_id}/cancel-return/",
            data={"reason": "Vendor requested exchange instead of return"},
            format="json",
        )
        assert cancel_resp.status_code == status.HTTP_200_OK
        assert cancel_resp.json()["status"] == "CANCELLED"

        # Verify stock was untouched
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 100

    def test_07_cannot_cancel_posted_return(self):
        """Attempting to cancel an already posted return throws ERR_CANNOT_CANCEL_POSTED_RETURN."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "items": [{"batch_stock_id": self.batch.batch_id, "return_quantity": 5}],
        }
        create_resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        return_id = create_resp.json()["return_id"]

        self.client.post(f"/api/v2/procurement/purchase-returns/{return_id}/post-return/")

        cancel_resp = self.client.post(
            f"/api/v2/procurement/purchase-returns/{return_id}/cancel-return/",
            data={"reason": "Should fail"},
            format="json",
        )
        assert cancel_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert cancel_resp.json()["error_code"] == "ERR_CANNOT_CANCEL_POSTED_RETURN"

    def test_08_cannot_post_already_posted_return(self):
        """Attempting to post an already posted return throws ERR_ALREADY_POSTED."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "items": [{"batch_stock_id": self.batch.batch_id, "return_quantity": 5}],
        }
        create_resp = self.client.post("/api/v2/procurement/purchase-returns/", data=payload, format="json")
        return_id = create_resp.json()["return_id"]

        self.client.post(f"/api/v2/procurement/purchase-returns/{return_id}/post-return/")

        post_again_resp = self.client.post(f"/api/v2/procurement/purchase-returns/{return_id}/post-return/")
        assert post_again_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert post_again_resp.json()["error_code"] == "ERR_ALREADY_POSTED"

    def test_09_list_and_filter_purchase_returns(self):
        """Lists purchase returns with filtering by status and supplier."""
        self.client.force_authenticate(user=self.manager)

        # Create return 1 (Draft)
        self.client.post(
            "/api/v2/procurement/purchase-returns/",
            data={"supplier_id": self.supplier.supplier_id, "items": [{"batch_stock_id": self.batch.batch_id, "return_quantity": 2}]},
            format="json",
        )

        # Create return 2 (Auto-posted)
        self.client.post(
            "/api/v2/procurement/purchase-returns/?auto_post=true",
            data={"supplier_id": self.supplier.supplier_id, "items": [{"batch_stock_id": self.batch.batch_id, "return_quantity": 3}]},
            format="json",
        )

        # List all
        resp = self.client.get("/api/v2/procurement/purchase-returns/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["count"] == 2

        # Filter by status=POSTED_DEBIT_NOTE
        resp_posted = self.client.get("/api/v2/procurement/purchase-returns/?status=POSTED_DEBIT_NOTE")
        assert resp_posted.status_code == status.HTTP_200_OK
        assert resp_posted.json()["count"] == 1
        assert resp_posted.json()["results"][0]["status"] == "POSTED_DEBIT_NOTE"

    def test_10_rbac_and_cross_tenant_isolation(self):
        """Cashier role is blocked from creating returns, and foreign tenants cannot access records."""
        # 1. Cashier attempts to create return -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        resp_cashier = self.client.post(
            "/api/v2/procurement/purchase-returns/",
            data={"supplier_id": self.supplier.supplier_id, "items": [{"batch_stock_id": self.batch.batch_id, "return_quantity": 1}]},
            format="json",
        )
        assert resp_cashier.status_code == status.HTTP_403_FORBIDDEN
        assert resp_cashier.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Manager creates return
        self.client.force_authenticate(user=self.manager)
        create_resp = self.client.post(
            "/api/v2/procurement/purchase-returns/",
            data={"supplier_id": self.supplier.supplier_id, "items": [{"batch_stock_id": self.batch.batch_id, "return_quantity": 1}]},
            format="json",
        )
        return_id = create_resp.json()["return_id"]

        # 3. Foreign tenant attempts to view return -> 404 Not Found
        self.client.force_authenticate(user=self.foreign_manager)
        resp_foreign = self.client.get(f"/api/v2/procurement/purchase-returns/{return_id}/")
        assert resp_foreign.status_code == status.HTTP_404_NOT_FOUND
