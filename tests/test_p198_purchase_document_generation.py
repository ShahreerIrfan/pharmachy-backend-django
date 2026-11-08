"""
Integration Tests for Purchase Document Generation (ADR-0198 / P198).
Covers versioned PDF/print document creation, PDF binary rendering via ReportLab,
version incrementation and superseding, financial decimal precision, RBAC,
and cross-tenant isolation.
"""

from decimal import Decimal
import datetime
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.identity.models import PharmacyUser, UserRole
from apps.core.models import (
    Branch,
    GoodsReceiptLine,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseDocument,
    PurchaseDocumentStatus,
    PurchaseDocumentType,
    PurchaseOrder,
    PurchaseOrderItem,
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


@pytest.mark.django_db
class TestPurchaseDocumentGenerationIntegration:
    """
    Test suite verifying end-to-end purchase document generation workflows.
    """

    def setup_method(self):
        self.client = APIClient()

        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id="tenant-doc-001",
            name="Dhaka Prime Pharmacy",
            subdomain="dhaka-prime-doc",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id="tenant-doc-002",
            name="Chittagong Care Pharmacy",
            subdomain="ctg-care-doc",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # 2. Branches
        self.branch_a = Branch.objects.create(
            branch_id="br-doc-001",
            tenant=self.tenant_a,
            name="Main Dispensing Branch",
        )
        self.branch_b = Branch.objects.create(
            branch_id="br-doc-002",
            tenant=self.tenant_b,
            name="Port City Branch",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email="manager.doc@dhakaprime.com",
            phone_number="+8801711000001",
            password="SecurePassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email="cashier.doc@dhakaprime.com",
            phone_number="+8801711000002",
            password="SecurePassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.manager_b = PharmacyUser.objects.create_user(
            email="manager.doc.b@ctgcare.com",
            phone_number="+8801711000003",
            password="SecurePassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            tenant=self.tenant_a,
            supplier_id="sup-doc-sq",
            name="Square Pharmaceuticals Ltd.",
            contact_person="Rafiqul Islam",
            phone="+8801711223344",
            email="orders@squarepharma.com.bd",
            lead_time_days=2,
            current_balance=Decimal("45000.0000"),
        )

        # 5. Products
        self.product_1 = MedicineProduct.objects.create(
            product_id="prod-doc-001",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            strength="500mg + 65mg",
            dosage_form="TABLET",
            manufacturer="Square Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.5000"),
            mrp_unit=Decimal("3.0000"),
        )
        self.product_2 = MedicineProduct.objects.create(
            product_id="prod-doc-002",
            brand_name="Seclo 20mg Capsule",
            generic_name="Omeprazole",
            strength="20mg",
            dosage_form="CAPSULE",
            manufacturer="Square Pharmaceuticals Ltd.",
            cost_unit=Decimal("5.0000"),
            mrp_unit=Decimal("6.0000"),
        )

        # 6. Purchase Order & Items
        self.po = PurchaseOrder.objects.create(
            po_id="po-doc-1001",
            po_number="PO-202611-1001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            status=PurchaseOrderStatus.APPROVED,
            payment_terms_days=30,
            currency="BDT",
            subtotal_amount=Decimal("500.0000"),
            discount_amount=Decimal("0.0000"),
            tax_amount=Decimal("0.0000"),
            total_amount=Decimal("500.0000"),
            ordered_at=datetime.datetime(2026, 11, 6, 10, 0, tzinfo=datetime.timezone.utc),
            notes="Standard monthly procurement batch",
        )
        self.po_item_1 = PurchaseOrderItem.objects.create(
            item_id="poi-doc-001",
            purchase_order=self.po,
            product=self.product_1,
            package_type="BOX",
            package_size=100,
            ordered_packages=1,
            ordered_quantity=100,
            unit_cost_price=Decimal("2.5000"),
            package_cost_price=Decimal("250.0000"),
            line_subtotal=Decimal("250.0000"),
            line_total=Decimal("250.0000"),
        )
        self.po_item_2 = PurchaseOrderItem.objects.create(
            item_id="poi-doc-002",
            purchase_order=self.po,
            product=self.product_2,
            package_type="BOX",
            package_size=50,
            ordered_packages=1,
            ordered_quantity=50,
            unit_cost_price=Decimal("5.0000"),
            package_cost_price=Decimal("250.0000"),
            line_subtotal=Decimal("250.0000"),
            line_total=Decimal("250.0000"),
        )

        # 7. GRN & Items
        self.grn = GoodsReceiptNote.objects.create(
            grn_id="grn-doc-2001",
            grn_number="GRN-202611-2001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            supplier_name=self.supplier.name,
            purchase_order=self.po,
            purchase_order_number=self.po.po_number,
            challan_number="CH-SQ-8899",
            challan_date=datetime.date(2026, 11, 7),
            receipt_date=datetime.date(2026, 11, 7),
            storage_condition="Ambient 15-25°C",
            status=GoodsReceiptStatus.POSTED,
            total_received_base_units=150,
            total_invoice_amount=Decimal("500.0000"),
            notes="Physical receipt verified",
        )
        self.grn_line = GoodsReceiptLine.objects.create(
            line_id="grnl-doc-001",
            grn=self.grn,
            tenant=self.tenant_a,
            product=self.product_1,
            po_item=self.po_item_1,
            batch_number="BATCH-NP-DOC",
            expiry_date=datetime.date(2028, 5, 31),
            pack_unit="BOX",
            package_size=100,
            received_packages=1,
            ordered_quantity_base_units=100,
            received_quantity_base_units=100,
            unit_cost=Decimal("2.5000"),
            line_total=Decimal("250.0000"),
        )

        # 8. Purchase Return & Items
        self.ret = PurchaseReturn.objects.create(
            return_id="pret-doc-3001",
            return_number="RET-202611-3001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            supplier_name=self.supplier.name,
            goods_receipt=self.grn,
            purchase_order=self.po,
            status=PurchaseReturnStatus.POSTED_DEBIT_NOTE,
            return_reason=PurchaseReturnReason.DAMAGED_DELIVERY,
            disposition=StockDisposition.RETURNED_TO_VENDOR,
            reason_notes="Outer blister crushed during courier handling",
            total_return_quantity=10,
            total_debit_amount=Decimal("25.0000"),
            debit_note_reference="DN-202611-001",
        )
        self.ret_item = PurchaseReturnItem.objects.create(
            item_id="pri-doc-001",
            purchase_return=self.ret,
            tenant=self.tenant_a,
            product=self.product_1,
            batch_number="BATCH-NP-DOC",
            return_quantity=10,
            unit_cost=Decimal("2.5000"),
            line_debit_amount=Decimal("25.0000"),
            reason=PurchaseReturnReason.DAMAGED_DELIVERY,
        )

        # 9. Supplier Ledger Entry
        self.ledger_entry = SupplierLedgerEntry.objects.create(
            posting_id="sle-doc-4001",
            tenant=self.tenant_a,
            supplier=self.supplier,
            posting_type=SupplierPostingType.GRN_INVOICE,
            reference_number="INV-SQ-9900",
            notes="Goods Receipt Note Posting",
            credit_amount=Decimal("500.0000"),
            debit_amount=Decimal("0.0000"),
            balance_after=Decimal("45000.0000"),
            posted_at=datetime.datetime(2026, 11, 7, 10, 0, tzinfo=datetime.timezone.utc),
        )

    def test_01_generate_purchase_order_document_v1(self):
        """Generates version 1 PO document with valid ReportLab PDF and SHA-256 hash."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {
                "document_type": "PURCHASE_ORDER",
                "source_id": self.po.po_id,
                "notes": "Initial purchase order print version",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["document_type"] == "PURCHASE_ORDER"
        assert data["version"] == 1
        assert data["is_latest"] is True
        assert data["status"] == "FINAL"
        assert Decimal(data["total_amount"]) == Decimal("500.0000")
        assert len(data["immutable_verification_hash"]) == 64
        assert data["pdf_size_bytes"] > 500  # Valid ReportLab PDF generated

        # Check line items inside snapshot payload
        payload = data["document_payload"]
        assert len(payload["line_items"]) == 2
        assert payload["supplier_name"] == self.supplier.name

    def test_02_regenerate_increments_version_and_supersedes(self):
        """Regenerating the same PO document increments version to 2 and supersedes version 1."""
        self.client.force_authenticate(user=self.manager)

        # Generate v1
        resp_v1 = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {"document_type": "PURCHASE_ORDER", "source_id": self.po.po_id},
            format="json",
        )
        assert resp_v1.status_code == status.HTTP_201_CREATED
        doc_v1_id = resp_v1.json()["document_id"]

        # Regenerate v2
        resp_v2 = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {"document_type": "PURCHASE_ORDER", "source_id": self.po.po_id, "notes": "Revised after discount confirmation"},
            format="json",
        )
        assert resp_v2.status_code == status.HTTP_201_CREATED
        data_v2 = resp_v2.json()

        assert data_v2["version"] == 2
        assert data_v2["is_latest"] is True
        assert data_v2["status"] == "FINAL"

        # Check that v1 in DB is superseded
        doc_v1 = PurchaseDocument.objects.get(document_id=doc_v1_id)
        assert doc_v1.version == 1
        assert doc_v1.is_latest is False
        assert doc_v1.status == PurchaseDocumentStatus.SUPERSEDED

        # Check versions endpoint
        resp_versions = self.client.get(
            f"/api/v2/procurement/documents/versions/?document_type=PURCHASE_ORDER&source_id={self.po.po_id}"
        )
        assert resp_versions.status_code == status.HTTP_200_OK
        versions_list = resp_versions.json()
        assert len(versions_list) == 2
        assert versions_list[0]["version"] == 2
        assert versions_list[1]["version"] == 1

    def test_03_generate_goods_receipt_document(self):
        """Generates Goods Receipt Note document snapshot with challan and temperature."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {
                "document_type": "GOODS_RECEIPT_NOTE",
                "source_id": self.grn.grn_id,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["document_type"] == "GOODS_RECEIPT_NOTE"
        assert data["document_number"].startswith("DOC-GRN-")
        payload = data["document_payload"]
        assert payload["challan_number"] == "CH-SQ-8899"
        assert len(payload["line_items"]) == 1

    def test_04_generate_purchase_return_document(self):
        """Generates Purchase Return voucher document with debit note reference."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {
                "document_type": "PURCHASE_RETURN",
                "source_id": self.ret.return_id,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["document_type"] == "PURCHASE_RETURN"
        assert data["document_number"].startswith("DOC-RET-")
        assert Decimal(data["total_amount"]) == Decimal("25.0000")
        payload = data["document_payload"]
        assert payload["debit_note_reference"] == "DN-202611-001"

    def test_05_generate_supplier_statement_document(self):
        """Generates Supplier Statement document with ledger entries and dues."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {
                "document_type": "SUPPLIER_STATEMENT",
                "supplier_id": self.supplier.supplier_id,
                "notes": "November 2026 Month-end ledger reconciliation",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["document_type"] == "SUPPLIER_STATEMENT"
        assert data["document_number"].startswith("DOC-STMT-")
        payload = data["document_payload"]
        assert len(payload["line_items"]) >= 1
        assert payload["line_items"][0]["reference"] == "INV-SQ-9900"

    def test_06_rbac_cashier_denied(self):
        """Cashier role is restricted with HTTP 403 Forbidden."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {
                "document_type": "PURCHASE_ORDER",
                "source_id": self.po.po_id,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json()["error_code"] in ("ERR_STAFF_ROLE_DENIED", "ERR_PERMISSION_DENIED")

    def test_07_cross_tenant_isolation(self):
        """Tenant B user cannot access Tenant A documents."""
        # Generate doc under tenant A
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {"document_type": "PURCHASE_ORDER", "source_id": self.po.po_id},
            format="json",
        )
        doc_id = gen_resp.json()["document_id"]

        # Authenticate as Tenant B
        self.client.force_authenticate(user=self.manager_b)
        resp = self.client.get(f"/api/v2/procurement/documents/{doc_id}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_08_download_pdf_binary_stream(self):
        """GET /api/v2/procurement/documents/{id}/pdf/ returns application/pdf binary."""
        self.client.force_authenticate(user=self.manager)
        gen_resp = self.client.post(
            "/api/v2/procurement/documents/generate/",
            {"document_type": "PURCHASE_ORDER", "source_id": self.po.po_id},
            format="json",
        )
        doc_id = gen_resp.json()["document_id"]

        pdf_resp = self.client.get(f"/api/v2/procurement/documents/{doc_id}/pdf/")
        assert pdf_resp.status_code == status.HTTP_200_OK
        assert pdf_resp["Content-Type"] == "application/pdf"
        assert pdf_resp.content.startswith(b"%PDF-")
        assert len(pdf_resp.content) > 500
