"""
backend/tests/test_p197_supply_chain_traceability.py
Architecture Decision: ADR-0197 (P197).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration test suite verifying Supply-Chain Traceability:
- Reconstructs full lot ancestry (supplier, PO, shipment, GRN, storage, sales, returns).
- Verifies physical stock reconciliation formulas and cold-chain integrity flags.
- Generates audited DGDA compliance traceability records with cryptographic verification hashes.
- Enforces RBAC (Cashier blocked) and cross-tenant isolation.
"""

import datetime
from decimal import Decimal
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStock,
    Branch,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    MedicineProduct,
    ProcurementShipment,
    PurchaseCostLot,
    PurchaseOrder,
    PurchaseOrderStatus,
    SaleInvoice,
    SaleInvoiceItem,
    SaleLotAllocation,
    ShipmentStage,
    Supplier,
    SupplyChainTraceabilityRecord,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestSupplyChainTraceabilityIntegration:
    @classmethod
    def setup_class(cls):
        cls.client = APIClient()

    def setup_method(self):
        # 1. Tenants
        self.tenant_a = Tenant.objects.create(
            tenant_id="tenant-trc-001",
            name="Dhaka Central Pharmacy",
            subdomain="dhaka-central-trc",
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id="tenant-trc-002",
            name="Chittagong Care Pharmacy",
            subdomain="ctg-care-trc",
        )

        # 2. Branches
        self.branch_a = Branch.objects.create(
            branch_id="br-trc-001",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email="manager@dhaka-central.com",
            phone_number="+8801713000001",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            role=UserRole.BRANCH_MANAGER,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email="cashier@dhaka-central.com",
            phone_number="+8801713000002",
            full_name="Front Cashier",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            role=UserRole.CASHIER,
        )
        self.manager_b = PharmacyUser.objects.create_user(
            email="manager@ctg-care.com",
            phone_number="+8801713000003",
            full_name="CTG Manager",
            password="StrongPassword123!",
            tenant_id=self.tenant_b.tenant_id,
            role=UserRole.BRANCH_MANAGER,
        )

        # 4. Product
        self.product = MedicineProduct.objects.create(
            product_id="prod-humalog-100",
            brand_name="Humalog 100IU/ml Insulin",
            generic_name="Insulin Lispro",
            strength="100IU/ml",
            dosage_form="INJECTION",
            manufacturer="Eli Lilly / Square",
            requires_cold_chain=True,
            cost_unit=Decimal("450.0000"),
            mrp_unit=Decimal("520.0000"),
        )

        # 5. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id="sup-sq-trc",
            tenant=self.tenant_a,
            name="Square Pharmaceuticals Cold Chain Depot",
            code="SQUARE-COLD",
            dl_number="DL-DHAKA-112233",
            tin_number="TIN-99887766",
            bin_number="BIN-11223344",
            proof_of_origin_verified=True,
            is_active=True,
        )

        # 6. Purchase Order
        self.po = PurchaseOrder.objects.create(
            po_id="po-trc-001",
            po_number="PO-202611-TRC",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            status=PurchaseOrderStatus.CLOSED,
            total_amount=Decimal("45000.0000"),
            expected_delivery_date=datetime.date(2026, 11, 5),
            is_active=True,
        )

        # 7. Procurement Shipment
        self.shipment = ProcurementShipment.objects.create(
            shipment_id="shp-trc-001",
            tracking_number="SHP-202611-TRC01",
            tenant=self.tenant_a,
            branch=self.branch_a,
            purchase_order=self.po,
            supplier=self.supplier,
            carrier_name="Square Dedicated Reefer Van",
            consignment_number="SQ-REEFER-991",
            vehicle_registration_number="Dhaka Metro-U-14-9988",
            driver_name="Harunur Rashid",
            driver_phone="+8801711998877",
            stage=ShipmentStage.RECEIVING_CONFIRMED,
            estimated_delivery_at=datetime.datetime(2026, 11, 5, 14, 0, tzinfo=datetime.timezone.utc),
            actual_delivery_at=datetime.datetime(2026, 11, 5, 13, 30, tzinfo=datetime.timezone.utc),
            requires_cold_chain=True,
            transit_temp_celsius=Decimal("4.20"),
            cold_chain_breached=False,
            total_packages_dispatched=100,
            total_packages_received=100,
            is_active=True,
        )

        # 8. Goods Receipt Note
        self.grn = GoodsReceiptNote.objects.create(
            grn_id="grn-trc-001",
            grn_number="GRN-202611-TRC01",
            tenant=self.tenant_a,
            branch=self.branch_a,
            supplier=self.supplier,
            purchase_order=self.po,
            challan_number="CH-SQ-887766",
            challan_date=datetime.date(2026, 11, 5),
            receipt_date=datetime.date(2026, 11, 5),
            storage_condition="REFRIGERATED_2_TO_8",
            storage_temperature_celsius=Decimal("4.00"),
            storage_check_passed=True,
            proof_of_origin=True,
            status=GoodsReceiptStatus.POSTED,
            total_received_base_units=100,
            is_active=True,
        )

        # 9. BatchStock & PurchaseCostLot
        self.batch = BatchStock.objects.create(
            batch_id="bat-trc-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number="BATCH-INS-2026A",
            lot_reference="LOT-SQ-INS-01",
            quantity_base_units=85,  # 100 received - 18 sold + 3 returned = 85 on hand
            reserved_quantity_base_units=0,
            purchase_date=datetime.date(2026, 11, 5),
            expiry_date=datetime.date(2027, 10, 31),
            cost_per_unit=Decimal("450.0000"),
            manufacturing_date=datetime.date(2025, 11, 1),
            storage_condition="Cold Storage 2°C - 8°C",
            is_quarantined=False,
            is_active=True,
        )

        self.cost_lot = PurchaseCostLot.objects.create(
            lot_id="lot-trc-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            supplier=self.supplier,
            supplier_name=self.supplier.name,
            purchase_order_number=self.po.po_number,
            grn_number=self.grn.grn_number,
            invoice_reference="INV-SUP-8899",
            purchase_date=datetime.date(2026, 11, 5),
            unit_cost=Decimal("450.0000"),
            initial_quantity=100,
            remaining_quantity=85,
            status="ACTIVE",
            is_active=True,
        )

        # 10. Downstream Sale Allocation
        self.invoice = SaleInvoice.objects.create(
            invoice_id="inv-trc-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice_number="INV-202611-0088",
            total_amount=Decimal("9360.0000"),
            net_amount=Decimal("9360.0000"),
            status="COMPLETED",
        )

        self.sale_alloc = SaleLotAllocation.objects.create(
            allocation_id="sla-trc-001",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch,
            cost_lot=self.cost_lot,
            quantity_base_units=18,
            returned_quantity_base_units=3,
            unit_selling_price=Decimal("520.0000"),
            unit_cost=Decimal("450.0000"),
            total_selling_price=Decimal("9360.0000"),
            total_cost=Decimal("8100.0000"),
            gross_profit=Decimal("1260.0000"),
            is_returned=True,
            status="PARTIALLY_RETURNED",
        )

    def test_01_trace_batch_lineage_upstream_and_downstream(self):
        """Reconstructs unbroken lineage from supplier origin to customer dispensing."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get(
            f"/api/v2/procurement/traceability/trace/?batch_number={self.batch.batch_number}"
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()

        # Product details
        prod_data = data["product"]
        assert prod_data["product_id"] == self.product.product_id
        assert prod_data["brand_name"] == "Humalog 100IU/ml Insulin"
        assert prod_data["requires_cold_chain"] is True

        # Upstream Supplier & PO
        upstream = data["upstream_procurement"]
        assert len(upstream["suppliers"]) == 1
        assert upstream["suppliers"][0]["dl_number"] == "DL-DHAKA-112233"
        assert upstream["suppliers"][0]["proof_of_origin_verified"] is True

        assert len(upstream["purchase_orders"]) == 1
        assert upstream["purchase_orders"][0]["po_number"] == self.po.po_number

        assert len(upstream["shipments"]) == 1
        assert upstream["shipments"][0]["vehicle_registration_number"] == "Dhaka Metro-U-14-9988"
        assert upstream["shipments"][0]["cold_chain_breached"] is False

        assert len(upstream["goods_receipt_notes"]) == 1
        assert upstream["goods_receipt_notes"][0]["challan_number"] == "CH-SQ-887766"

        # Downstream Sales
        downstream = data["downstream_dispensing"]
        assert downstream["total_dispensed_events"] == 1
        assert downstream["sales"][0]["invoice_number"] == "INV-202611-0088"
        assert downstream["sales"][0]["quantity_dispensed"] == 18
        assert downstream["sales"][0]["quantity_returned"] == 3

        # Stock Reconciliation
        rec = data["reconciliation"]
        assert rec["total_received_quantity"] == 100
        assert rec["total_dispensed_quantity"] == 18
        assert rec["total_returned_quantity"] == 3
        assert rec["current_stock_on_hand"] == 85
        # 100 - 18 + 3 = 85 -> difference is 0
        assert rec["discrepancy_quantity"] == 0
        assert rec["cold_chain_breached_in_transit"] is False

    def test_02_generate_audited_traceability_record(self):
        """Generates formal traceability snapshot with cryptographic verification hash."""
        self.client.force_authenticate(user=self.manager)
        payload = {
            "batch_number": self.batch.batch_number,
            "audit_purpose": "REGULATORY_INSPECTION",
            "notes": "DGDA annual good distribution practice inspection audit",
        }
        resp = self.client.post("/api/v2/procurement/traceability/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["trace_number"].startswith("TRC-")
        assert data["audit_purpose"] == "REGULATORY_INSPECTION"
        assert data["total_received_quantity"] == 100
        assert data["current_stock_on_hand"] == 85
        assert len(data["immutable_verification_hash"]) == 64  # SHA-256 hash length

        # Verify DB persistence
        record = SupplyChainTraceabilityRecord.objects.get(trace_id=data["trace_id"])
        assert record.immutable_verification_hash == data["immutable_verification_hash"]
        assert record.compute_hash() == data["immutable_verification_hash"]

    def test_03_invalid_batch_returns_404(self):
        """Non-existent batch raises HTTP 404."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/v2/procurement/traceability/trace/?batch_number=NON-EXISTENT-BATCH")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["error_code"] in ("ERR_BATCH_NOT_FOUND", "ERR_RESOURCE_NOT_FOUND")

    def test_04_missing_batch_param_returns_400(self):
        """Missing batch parameter raises HTTP 400."""
        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/v2/procurement/traceability/trace/")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_BATCH_REQUIRED" in str(resp.json())

    def test_05_rbac_cashier_denied(self):
        """Cashier role is barred from traceability access."""
        self.client.force_authenticate(user=self.cashier)
        resp = self.client.get(f"/api/v2/procurement/traceability/trace/?batch_number={self.batch.batch_number}")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.json().get("error_code") in ("ERR_STAFF_ROLE_DENIED", "ERR_PERMISSION_DENIED")

    def test_06_cross_tenant_isolation(self):
        """Tenant B manager cannot trace Tenant A's batches."""
        self.client.force_authenticate(user=self.manager_b)
        resp = self.client.get(f"/api/v2/procurement/traceability/trace/?batch_number={self.batch.batch_number}")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
