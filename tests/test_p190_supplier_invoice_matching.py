"""
backend/tests/test_p190_supplier_invoice_matching.py
Architecture Decision: ADR-0190 (P190).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P190 3-Way Supplier Invoice Matching, Variance Tolerances, and Exception Queues:
1. Valid 3-Way Match within tolerance: PO (agreeing ৳350) + GRN (50 units received) + Invoice (50 units @ ৳350) -> MATCHED, has_discrepancy=False.
2. Post matched invoice to AP: POST /{id}/post-to-ap/ transitions status to POSTED_TO_AP, creates immutable SupplierLedgerEntry credit posting.
3. Price variance exceeding 1.5% tolerance: Invoice billing ৳370 vs PO ৳350 -> routes to DISCREPANCY_QUEUE with PRICE_VARIANCE reason.
4. Physical quantity overbilling: Invoice billing 60 units vs GRN 50 units received -> routes to DISCREPANCY_QUEUE with QUANTITY_OVER_BILLING reason.
5. Unmatched item on invoice: Billed product not present in GRN shipment -> routes to DISCREPANCY_QUEUE with UNMATCHED_ITEM reason.
6. Exception queue endpoint: GET /exception-queue/ filters only invoices in DISCREPANCY_QUEUE.
7. Managerial exception approval: POST /{id}/approve-exception/ with justification -> transitions to APPROVED_EXCEPTION, unlocks AP posting.
8. Managerial invoice rejection: POST /{id}/reject-invoice/ with reason -> transitions to REJECTED, blocks AP posting.
9. Duplicate invoice guard: Submitting same invoice number twice for same supplier returns HTTP 400 ERR_DUPLICATE_INVOICE.
10. RBAC and cross-tenant isolation: Cashier blocked (HTTP 403 ERR_STAFF_ROLE_DENIED) and foreign tenant blocked (HTTP 403 ERR_CROSS_TENANT_ACCESS).
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
    Branch,
    GoodsReceiptLine,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    InvoiceMatchingStatus,
    ItemMatchStatus,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Supplier,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSupplierInvoiceMatchingIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p190-{self.suffix}",
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
            email=f"csh_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Dispensary Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@popular.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Popular Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-square-{self.suffix}",
            tenant=self.tenant,
            name="Square Pharmaceuticals PLC",
            contact_person="Kabir Ahmed",
            phone="+8801711998877",
            email="commercial@squarepharma.com",
            is_active=True,
        )

        # 5. Products
        self.insulin = MedicineProduct.objects.create(
            product_id=f"prod-ins-{self.suffix}",
            brand_name="Glarin Insulin 100IU",
            generic_name="Insulin Glargine",
            strength="100 IU/ml",
            dosage_form="Injection",
            manufacturer="Square Pharmaceuticals PLC",
            cost_unit=Decimal("350.0000"),
            mrp_unit=Decimal("420.0000"),
        )
        self.napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name="Napa Extra Tablet",
            generic_name="Paracetamol + Caffeine",
            strength="500mg/65mg",
            dosage_form="Tablet",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.5000"),
            mrp_unit=Decimal("3.5000"),
        )

    def _setup_po_and_grn(self, qty: int = 50, price: Decimal = Decimal("350.0000")) -> tuple[PurchaseOrder, GoodsReceiptNote]:
        """Creates an ORDERED PO and a POSTED GRN for insulin."""
        po = PurchaseOrder.objects.create(
            po_id=f"po-{uuid.uuid4().hex[:8]}",
            po_number=f"PO-{timezone.now().strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            status=PurchaseOrderStatus.ORDERED,
            subtotal_amount=Decimal(str(qty * price)),
            total_amount=Decimal(str(qty * price)),
            created_by=self.manager,
            is_active=True,
        )
        po_item = PurchaseOrderItem.objects.create(
            item_id=f"poi-ins-{uuid.uuid4().hex[:8]}",
            purchase_order=po,
            product=self.insulin,
            pack_unit="VIAL",
            package_size=1,
            ordered_packages=qty,
            ordered_quantity=qty,
            received_quantity=qty,
            unit_cost_price=price,
            mrp_unit=Decimal("420.0000"),
            line_subtotal=Decimal(str(qty * price)),
            line_total=Decimal(str(qty * price)),
        )

        grn = GoodsReceiptNote.objects.create(
            grn_id=f"grn-{uuid.uuid4().hex[:8]}",
            grn_number=f"GRN-{timezone.now().strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            supplier_name=self.supplier.name,
            purchase_order=po,
            purchase_order_number=po.po_number,
            challan_number=f"CH-SQ-{self.suffix}",
            receipt_date=timezone.now().date(),
            storage_condition="COLD_CHAIN_2_8C",
            storage_temperature_celsius=Decimal("4.50"),
            storage_check_passed=True,
            status=GoodsReceiptStatus.POSTED,
            total_received_base_units=qty,
            total_invoice_amount=Decimal(str(qty * price)),
            ap_posting_status="PENDING",
            ap_invoice_amount=Decimal("0.0000"),
            is_active=True,
        )
        GoodsReceiptLine.objects.create(
            line_id=f"grnl-ins-{uuid.uuid4().hex[:8]}",
            grn=grn,
            tenant=self.tenant,
            product=self.insulin,
            po_item=po_item,
            batch_number=f"SQ-INS-{self.suffix}",
            expiry_date=timezone.now().date() + datetime.timedelta(days=365),
            pack_unit="VIAL",
            package_size=1,
            received_packages=qty,
            ordered_quantity_base_units=qty,
            received_quantity_base_units=qty,
            unit_cost=price,
            mrp_unit=Decimal("420.0000"),
            line_total=Decimal(str(qty * price)),
            storage_location="COLD_ROOM_A",
            is_fully_allocated=True,
        )
        return po, grn

    def test_01_valid_three_way_match_success(self):
        """1. Valid 3-Way Match within tolerance sets MATCHED status."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-01",
            "invoice_date": timezone.now().date().isoformat(),
            "due_date": (timezone.now().date() + datetime.timedelta(days=30)).isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "currency": "BDT",
            "tolerated_price_variance_percentage": "1.50",
            "tolerated_amount_variance_bdt": "50.0000",
            "proof_attachment_reference": "INV_SCAN_01.PDF",
            "notes": "Standard commercial billing",
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "batch_number": grn_line.batch_number,
                    "billed_quantity": 50,
                    "billed_unit_price": "350.0000",
                    "line_tax_percentage": "0.00",
                    "line_discount_percentage": "0.00",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED, res.data
        data = res.data

        assert data["matching_status"] == InvoiceMatchingStatus.MATCHED
        assert data["has_discrepancy"] is False
        assert len(data["discrepancy_reasons"]) == 0
        assert Decimal(data["total_amount"]) == Decimal("17500.0000")
        assert Decimal(data["price_variance_amount"]) == Decimal("0.0000")
        assert data["quantity_variance_units"] == 0

        # Verify items
        item = data["items"][0]
        assert item["match_status"] == ItemMatchStatus.MATCHED
        assert item["billed_quantity"] == 50
        assert item["received_quantity"] == 50
        assert item["ordered_quantity"] == 50

    def test_02_post_matched_invoice_to_ap_ledger(self):
        """2. Post matched invoice to AP creates immutable SupplierLedgerEntry and updates AP status."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-02",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "350.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        assert create_res.status_code == status.HTTP_201_CREATED
        invoice_id = create_res.data["invoice_id"]

        # Post to AP
        post_res = self.client.post(f"/api/v2/procurement/supplier-invoices/{invoice_id}/post-to-ap/")
        assert post_res.status_code == status.HTTP_200_OK, post_res.data
        data = post_res.data

        assert data["matching_status"] == InvoiceMatchingStatus.POSTED_TO_AP
        assert data["posted_to_ap_at"] is not None
        assert data["ap_ledger_entry_id"] is not None

        # Verify SupplierLedgerEntry created in database
        ledger_entry = SupplierLedgerEntry.objects.get(posting_id=data["ap_ledger_entry_id"])
        assert ledger_entry.posting_type == SupplierPostingType.GRN_INVOICE
        assert ledger_entry.reference_number == f"INV-SQ-{self.suffix}-02"
        assert ledger_entry.credit_amount == Decimal("17500.0000")
        assert ledger_entry.debit_amount == Decimal("0.0000")
        assert ledger_entry.balance_after == Decimal("17500.0000")

        # Verify parent GRN AP confirmation updated
        grn.refresh_from_db()
        assert grn.ap_posting_status == "POSTED"
        assert grn.ap_invoice_amount == Decimal("17500.0000")

    def test_03_price_variance_exceeding_tolerance_routes_to_discrepancy_queue(self):
        """3. Price variance exceeding 1.5% tolerance routes to DISCREPANCY_QUEUE."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        # Vendor bills ৳370 instead of agreed ৳350 (+5.7% price spike)
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-PRICE-VAR",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "tolerated_price_variance_percentage": "1.50",
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "370.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.data

        assert data["matching_status"] == InvoiceMatchingStatus.DISCREPANCY_QUEUE
        assert data["has_discrepancy"] is True
        assert any("PRICE_VARIANCE" in reason for reason in data["discrepancy_reasons"])
        assert Decimal(data["price_variance_amount"]) == Decimal("1000.0000")  # (370 - 350) * 50

        # Attempting to post unapproved discrepant invoice returns HTTP 400 ERR_INVOICE_NOT_MATCHED
        invoice_id = data["invoice_id"]
        post_res = self.client.post(f"/api/v2/procurement/supplier-invoices/{invoice_id}/post-to-ap/")
        assert post_res.status_code == status.HTTP_400_BAD_REQUEST
        assert post_res.data["error_code"] == "ERR_INVOICE_NOT_MATCHED"

    def test_04_quantity_overbilling_routes_to_discrepancy_queue(self):
        """4. Physical quantity overbilling (billed 60 > received 50) routes to DISCREPANCY_QUEUE."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        # Vendor bills 60 units, but warehouse only received 50
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-QTY-VAR",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 60,
                    "billed_unit_price": "350.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.data

        assert data["matching_status"] == InvoiceMatchingStatus.DISCREPANCY_QUEUE
        assert data["has_discrepancy"] is True
        assert any("QUANTITY_OVER_BILLING" in reason for reason in data["discrepancy_reasons"])
        assert data["quantity_variance_units"] == 10

    def test_05_unmatched_item_on_invoice_flagged(self):
        """5. Unmatched item on invoice routes to DISCREPANCY_QUEUE with UNMATCHED_ITEM."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))

        self.client.force_authenticate(user=self.manager)

        # Invoice includes Napa Extra which was never in the GRN shipment
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-UNMATCHED",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.napa.product_id,  # Unmatched product
                    "billed_quantity": 100,
                    "billed_unit_price": "2.5000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.data

        assert data["matching_status"] == InvoiceMatchingStatus.DISCREPANCY_QUEUE
        assert any("UNMATCHED_ITEM" in reason for reason in data["discrepancy_reasons"])
        assert data["items"][0]["match_status"] == ItemMatchStatus.UNMATCHED_ITEM

    def test_06_exception_queue_listing_endpoint(self):
        """6. Exception queue endpoint: GET /exception-queue/ returns only DISCREPANCY_QUEUE invoices."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        # 1. Matched invoice
        normal_payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-OK",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "350.0000",
                }
            ],
        }
        self.client.post("/api/v2/procurement/supplier-invoices/", normal_payload, format="json")

        # 2. Discrepant invoice
        discrepant_payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-DISCREPANT",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "390.0000",
                }
            ],
        }
        res_disc = self.client.post("/api/v2/procurement/supplier-invoices/", discrepant_payload, format="json")
        disc_invoice_id = res_disc.data["invoice_id"]

        # Fetch exception queue
        queue_res = self.client.get("/api/v2/procurement/supplier-invoices/exception-queue/")
        assert queue_res.status_code == status.HTTP_200_OK
        results = queue_res.data["results"]

        assert any(inv["invoice_id"] == disc_invoice_id for inv in results)
        assert not any(inv["invoice_number"] == f"INV-SQ-{self.suffix}-OK" for inv in results)

    def test_07_managerial_exception_approval_unlocks_ap(self):
        """7. Managerial exception approval unlocks AP posting with documented justification."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-OVERRIDE",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "365.0000",  # Higher price
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        invoice_id = res.data["invoice_id"]
        assert res.data["matching_status"] == InvoiceMatchingStatus.DISCREPANCY_QUEUE

        # Missing justification returns HTTP 400 ERR_MISSING_JUSTIFICATION
        bad_override = self.client.post(
            f"/api/v2/procurement/supplier-invoices/{invoice_id}/approve-exception/",
            {"justification": ""},
            format="json",
        )
        assert bad_override.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_override.data["error_code"] == "ERR_MISSING_JUSTIFICATION"

        # Manager approves with justification
        approve_res = self.client.post(
            f"/api/v2/procurement/supplier-invoices/{invoice_id}/approve-exception/",
            {
                "justification": "Price variance approved per manufacturer notice on imported cold packaging costs.",
                "notes": "Verified by Procurement Head",
            },
            format="json",
        )
        assert approve_res.status_code == status.HTTP_200_OK
        assert approve_res.data["matching_status"] == InvoiceMatchingStatus.APPROVED_EXCEPTION
        assert approve_res.data["exception_approved_by"] == str(self.manager.user_id)

        # Now can be posted to AP
        post_res = self.client.post(f"/api/v2/procurement/supplier-invoices/{invoice_id}/post-to-ap/")
        assert post_res.status_code == status.HTTP_200_OK
        assert post_res.data["matching_status"] == InvoiceMatchingStatus.POSTED_TO_AP

    def test_08_managerial_invoice_rejection(self):
        """8. Managerial invoice rejection sets REJECTED status and blocks AP posting."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SQ-{self.suffix}-REJECT",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "420.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        invoice_id = res.data["invoice_id"]

        # Reject invoice
        reject_res = self.client.post(
            f"/api/v2/procurement/supplier-invoices/{invoice_id}/reject-invoice/",
            {
                "rejection_reason": "Billed price ৳420 exceeds maximum retail price limit. Returned for revised invoice.",
                "notes": "Emailed supplier accounts team",
            },
            format="json",
        )
        assert reject_res.status_code == status.HTTP_200_OK
        assert reject_res.data["matching_status"] == InvoiceMatchingStatus.REJECTED
        assert reject_res.data["rejected_by"] == str(self.manager.user_id)

        # Cannot post rejected invoice to AP
        post_res = self.client.post(f"/api/v2/procurement/supplier-invoices/{invoice_id}/post-to-ap/")
        assert post_res.status_code == status.HTTP_400_BAD_REQUEST
        assert post_res.data["error_code"] == "ERR_INVOICE_REJECTED"

    def test_09_duplicate_invoice_number_guard(self):
        """9. Submitting duplicate invoice number for same supplier returns HTTP 400 ERR_DUPLICATE_INVOICE."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        inv_num = f"INV-DUP-{self.suffix}"
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": inv_num,
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "350.0000",
                }
            ],
        }

        first_res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        assert first_res.status_code == status.HTTP_201_CREATED

        # Duplicate submission attempt
        dup_res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        assert dup_res.status_code == status.HTTP_400_BAD_REQUEST
        assert dup_res.data["error_code"] == "ERR_DUPLICATE_INVOICE"

    def test_10_rbac_and_cross_tenant_isolation(self):
        """10. RBAC and cross-tenant isolation: Cashier blocked; foreign tenant blocked."""
        po, grn = self._setup_po_and_grn(50, Decimal("350.0000"))
        po_item = po.items.first()
        grn_line = grn.lines.first()

        self.client.force_authenticate(user=self.manager)

        payload = {
            "supplier_id": self.supplier.supplier_id,
            "invoice_number": f"INV-SEC-{self.suffix}",
            "invoice_date": timezone.now().date().isoformat(),
            "purchase_order_id": po.po_id,
            "goods_receipt_id": grn.grn_id,
            "items": [
                {
                    "product_id": self.insulin.product_id,
                    "po_item_id": po_item.item_id,
                    "grn_line_id": grn_line.line_id,
                    "billed_quantity": 50,
                    "billed_unit_price": "350.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/supplier-invoices/", payload, format="json")
        invoice_id = create_res.data["invoice_id"]

        # 1. Cashier role blocked from invoice actions -> HTTP 403 ERR_STAFF_ROLE_DENIED
        self.client.force_authenticate(user=self.cashier)
        cashier_res = self.client.post(f"/api/v2/procurement/supplier-invoices/{invoice_id}/post-to-ap/")
        assert cashier_res.status_code == status.HTTP_403_FORBIDDEN
        assert cashier_res.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Foreign tenant manager blocked from invoice access -> HTTP 404/403 ERR_INVOICE_NOT_FOUND / ERR_CROSS_TENANT_ACCESS
        self.client.force_authenticate(user=self.foreign_manager)
        foreign_res = self.client.get(f"/api/v2/procurement/supplier-invoices/{invoice_id}/")
        assert foreign_res.status_code in [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND]
