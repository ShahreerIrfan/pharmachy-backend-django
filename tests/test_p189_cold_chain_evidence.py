"""
backend/tests/test_p189_cold_chain_evidence.py
Architecture Decision: ADR-0189 (P189).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P189 Receipt Cold-Chain Evidence, Temperature Logging, and Quarantine:
1. Valid cold chain receipt: 4.5°C within 2-8°C creates ReceiptColdChainEvidence with quarantine_status=NOT_QUARANTINED, excursion_detected=False.
2. Post valid cold chain GRN: BatchStock is ACTIVE (is_quarantined=False) and PurchaseCostLot is ACTIVE.
3. Suspicious temperature excursion: 9.2°C excursion with quarantine_suspicious=True creates GRN with quarantine_status=PENDING_REVIEW, excursion_detected=True, line is_quarantined=True.
4. Post suspicious GRN to stock: BatchStock is QUARANTINED (is_quarantined=True) and PurchaseCostLot is QUARANTINED; available_quantity_base_units=0 for sales.
5. Strict physical inspection failure without quarantine_suspicious: returns HTTP 400 ERR_STORAGE_CHECK_FAILED (P187 regression guard).
6. Authorized quarantine resolution (APPROVE_RELEASE): Manager resolves quarantine with manufacturer stability evidence; updates BatchStock to ACTIVE, PurchaseCostLot to ACTIVE, and evidence to RELEASED.
7. Authorized quarantine resolution (REJECT_RETURN): Manager rejects spoiled batch; updates BatchStock to RECALLED, PurchaseCostLot to REVERSED, and reduces AP invoice liability on parent GRN.
8. Authorized quarantine resolution (DISPOSE_DESTROY): Manager condemns batch; updates BatchStock to DEPLETED, PurchaseCostLot to DEPLETED, and evidence to DISPOSED.
9. Quarantined shipment listing & filtering: GET /cold-chain-quarantine/?quarantine_status=PENDING_REVIEW returns pending items; GET /{id}/cold-chain-evidence/ returns GRN evidence.
10. RBAC and cross-tenant isolation: Cashier blocked from resolving quarantine (HTTP 403 ERR_STAFF_ROLE_DENIED) and foreign tenant blocked (HTTP 403 ERR_CROSS_TENANT_ACCESS).
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
    ColdChainQuarantineStatus,
    GoodsReceiptLine,
    GoodsReceiptNote,
    MedicineProduct,
    PackagingCondition,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    ReceiptColdChainEvidence,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestColdChainEvidenceIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p189-{self.suffix}",
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
            full_name="Cold Chain Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@greencrescent.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Registered Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
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
            email="coldchain@beximco.com",
            is_active=True,
        )

        # 5. Cold-Chain Product (Insulin)
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

    def _create_ordered_po(self, qty: int = 50) -> PurchaseOrder:
        """Helper to create and advance a PO to ORDERED status for Insulin."""
        po = PurchaseOrder.objects.create(
            po_id=f"po-{uuid.uuid4().hex[:8]}",
            po_number=f"PO-{timezone.now().strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            status=PurchaseOrderStatus.ORDERED,
            subtotal_amount=Decimal(str(qty * 350)),
            total_amount=Decimal(str(qty * 350)),
            created_by=self.manager,
            is_active=True,
        )
        PurchaseOrderItem.objects.create(
            item_id=f"poi-ins-{uuid.uuid4().hex[:8]}",
            purchase_order=po,
            product=self.insulin,
            pack_unit="VIAL",
            package_size=1,
            ordered_packages=qty,
            ordered_quantity=qty,
            received_quantity=0,
            unit_cost_price=Decimal("350.0000"),
            mrp_unit=Decimal("420.0000"),
            line_subtotal=Decimal(str(qty * 350)),
            line_total=Decimal(str(qty * 350)),
        )
        return po

    def test_01_valid_cold_chain_receipt_creates_evidence(self):
        """1. Valid cold chain receipt: 4.5°C creates ReceiptColdChainEvidence with NOT_QUARANTINED."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-{self.suffix}-01",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 4.5,
            "storage_check_passed": True,
            "proof_of_origin": "DGDA Inspected Cold Truck CH-9901",
            "cold_chain_evidence": {
                "recorded_temperature_celsius": 4.5,
                "min_temperature_celsius": 3.8,
                "max_temperature_celsius": 5.2,
                "logger_serial_number": "LOGGER-SN-9982",
                "packaging_condition": "INTACT_SEALED",
                "carrier_name": "Transcom Cold Logistics",
                "vehicle_type": "REFRIGERATED_VAN",
                "proof_attachment_reference": "DOC-CC-CH9901.PDF",
                "inspection_notes": "Gel packs intact and frozen; data logger readings within 2-8 C range",
            },
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": f"BEX-INS-OK-{self.suffix}",
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                    "storage_location": "COLD_ROOM_A",
                }
            ],
        }

        response = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data

        assert data["storage_condition"] == "COLD_CHAIN_2_8C"
        assert Decimal(str(data["storage_temperature_celsius"])) == Decimal("4.50")
        assert data["storage_check_passed"] is True

        # Verify cold chain evidence created
        evidences = ReceiptColdChainEvidence.objects.filter(grn_id=data["grn_id"])
        assert evidences.count() == 1
        ev = evidences.first()
        assert ev.quarantine_status == ColdChainQuarantineStatus.NOT_QUARANTINED
        assert ev.excursion_detected is False
        assert ev.recorded_temperature_celsius == Decimal("4.50")
        assert ev.logger_serial_number == "LOGGER-SN-9982"
        assert ev.packaging_condition == PackagingCondition.INTACT_SEALED

        line = GoodsReceiptLine.objects.get(grn_id=data["grn_id"])
        assert line.is_quarantined is False

    def test_02_post_valid_cold_chain_grn_to_active_stock(self):
        """2. Post valid cold chain GRN: BatchStock is ACTIVE and PurchaseCostLot is ACTIVE."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        batch_no = f"BEX-INS-POST-{self.suffix}"
        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-{self.suffix}-02",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 5.0,
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": batch_no,
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                    "storage_location": "COLD_ROOM_A",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert create_res.status_code == status.HTTP_201_CREATED
        grn_id = create_res.data["grn_id"]

        post_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")
        assert post_res.status_code == status.HTTP_200_OK

        # Verify BatchStock is ACTIVE and not quarantined
        batch_stock = BatchStock.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch_number=batch_no,
        )
        assert batch_stock.status == BatchStatus.ACTIVE
        assert batch_stock.is_quarantined is False
        assert batch_stock.quantity_base_units == 50
        assert batch_stock.available_quantity_base_units == 50

        # Verify PurchaseCostLot is ACTIVE
        cost_lot = PurchaseCostLot.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch=batch_stock,
        )
        assert cost_lot.status == PurchaseCostLotStatus.ACTIVE

    def test_03_suspicious_temperature_excursion_quarantined(self):
        """3. Suspicious temperature excursion: 9.2°C with quarantine_suspicious=True creates PENDING_REVIEW evidence."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        batch_no = f"BEX-INS-WARM-{self.suffix}"
        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-WARM-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 9.2,
            "storage_check_passed": False,
            "quarantine_suspicious": True,
            "cold_chain_evidence": {
                "recorded_temperature_celsius": 9.2,
                "min_temperature_celsius": 4.0,
                "max_temperature_celsius": 9.8,
                "excursion_duration_minutes": 45,
                "packaging_condition": "ICE_PACKS_MELTED",
                "logger_serial_number": "LOGGER-SN-WARM-01",
                "carrier_name": "Local Cold Courier",
                "vehicle_type": "INSULATED_BOX_TRUCK",
                "inspection_notes": "Ice packs thawed, probe registered 9.2C excursion during transit",
                "quarantine_reason": "Cold chain temperature excursion above 8C limit",
            },
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": batch_no,
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                    "storage_location": "QUARANTINE_REFRIGERATOR",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED, res.data
        grn_id = res.data["grn_id"]

        evidence = ReceiptColdChainEvidence.objects.get(grn_id=grn_id)
        assert evidence.quarantine_status == ColdChainQuarantineStatus.PENDING_REVIEW
        assert evidence.excursion_detected is True
        assert evidence.packaging_condition == PackagingCondition.ICE_PACKS_MELTED
        assert evidence.recorded_temperature_celsius == Decimal("9.20")
        assert evidence.excursion_duration_minutes == 45

        line = GoodsReceiptLine.objects.get(grn_id=grn_id)
        assert line.is_quarantined is True

    def test_04_post_suspicious_grn_quarantines_inventory_and_cost_lot(self):
        """4. Post suspicious GRN: BatchStock is QUARANTINED and PurchaseCostLot is QUARANTINED; available_quantity is 0."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        batch_no = f"BEX-INS-HOLD-{self.suffix}"
        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-HOLD-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 10.5,
            "storage_check_passed": False,
            "quarantine_suspicious": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": batch_no,
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                    "storage_location": "QUARANTINE_FRIDGE_1",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert create_res.status_code == status.HTTP_201_CREATED
        grn_id = create_res.data["grn_id"]

        post_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")
        assert post_res.status_code == status.HTTP_200_OK

        batch_stock = BatchStock.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch_number=batch_no,
        )
        assert batch_stock.status == BatchStatus.QUARANTINED
        assert batch_stock.is_quarantined is True
        assert batch_stock.quantity_base_units == 50
        assert batch_stock.is_dispensable is False  # Crucial guard: NOT dispensable for sales

        cost_lot = PurchaseCostLot.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch=batch_stock,
        )
        assert cost_lot.status == PurchaseCostLotStatus.QUARANTINED

    def test_05_strict_inspection_failure_without_quarantine_suspicious(self):
        """5. Strict physical inspection failure without quarantine_suspicious returns HTTP 400 ERR_STORAGE_CHECK_FAILED."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-REJ-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 12.0,
            "storage_check_passed": False,
            "quarantine_suspicious": False,  # Strict rejection at the gate
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": f"BEX-INS-REJ-{self.suffix}",
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }

        res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        assert res.status_code == status.HTTP_400_BAD_REQUEST
        assert res.data["error_code"] == "ERR_STORAGE_CHECK_FAILED"

    def test_06_authorized_resolution_approve_release(self):
        """6. Authorized quarantine resolution (APPROVE_RELEASE): releases batch to ACTIVE stock with justification."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        batch_no = f"BEX-INS-REL-{self.suffix}"
        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-REL-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 8.5,
            "storage_check_passed": False,
            "quarantine_suspicious": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": batch_no,
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        grn_id = create_res.data["grn_id"]
        self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")

        evidence = ReceiptColdChainEvidence.objects.get(grn_id=grn_id)
        assert evidence.quarantine_status == ColdChainQuarantineStatus.PENDING_REVIEW

        # Pharmacist approves release
        self.client.force_authenticate(user=self.pharmacist)
        resolve_payload = {
            "evidence_id": evidence.evidence_id,
            "resolution_action": "APPROVE_RELEASE",
            "justification": "Beximco QA stability data confirms efficacy retained at 8.5C for up to 12 hours. Approved for dispensing.",
            "notes": "Logged under Cold Chain Stability Protocol CC-2026-08",
        }

        resolve_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/resolve-quarantine/", resolve_payload, format="json")
        assert resolve_res.status_code == status.HTTP_200_OK, resolve_res.data
        assert resolve_res.data["quarantine_status"] == ColdChainQuarantineStatus.RELEASED
        assert resolve_res.data["resolution_action"] == "APPROVE_RELEASE"

        # Verify BatchStock and PurchaseCostLot are now ACTIVE
        batch_stock = BatchStock.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch_number=batch_no,
        )
        assert batch_stock.status == BatchStatus.ACTIVE
        assert batch_stock.is_quarantined is False
        assert batch_stock.available_quantity_base_units == 50

        cost_lot = PurchaseCostLot.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch=batch_stock,
        )
        assert cost_lot.status == PurchaseCostLotStatus.ACTIVE

    def test_07_authorized_resolution_reject_return_credits_ap_invoice(self):
        """7. Authorized quarantine resolution (REJECT_RETURN): marks batch RECALLED, REVERSED, and reduces AP invoice."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        batch_no = f"BEX-INS-SPOIL-{self.suffix}"
        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-SPOIL-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 16.0,
            "storage_check_passed": False,
            "quarantine_suspicious": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": batch_no,
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        grn_id = create_res.data["grn_id"]
        post_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")
        assert post_res.data["ap_invoice_amount"] == "17500.0000"

        evidence = ReceiptColdChainEvidence.objects.get(grn_id=grn_id)

        # Pharmacist rejects & returns to supplier
        self.client.force_authenticate(user=self.pharmacist)
        resolve_payload = {
            "evidence_id": evidence.evidence_id,
            "resolution_action": "REJECT_RETURN",
            "justification": "Major temperature excursion to 16C caused irreversible protein denaturation. Batch rejected for vendor return.",
            "notes": "Supplier credit note requested from Beximco representative.",
        }

        resolve_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/resolve-quarantine/", resolve_payload, format="json")
        assert resolve_res.status_code == status.HTTP_200_OK
        assert resolve_res.data["quarantine_status"] == ColdChainQuarantineStatus.RETURNED_TO_SUPPLIER
        assert resolve_res.data["resolution_action"] == "REJECT_RETURN"

        # Verify BatchStock and PurchaseCostLot
        batch_stock = BatchStock.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch_number=batch_no,
        )
        assert batch_stock.status == BatchStatus.RECALLED
        assert batch_stock.is_dispensable is False

        cost_lot = PurchaseCostLot.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch=batch_stock,
        )
        assert cost_lot.status == PurchaseCostLotStatus.REVERSED

        # Verify AP invoice liability on parent GRN is credited
        grn = GoodsReceiptNote.objects.get(grn_id=grn_id)
        assert grn.ap_invoice_amount == Decimal("0.0000")

    def test_08_authorized_resolution_dispose_destroy(self):
        """8. Authorized quarantine resolution (DISPOSE_DESTROY): marks batch DEPLETED and cost lot DEPLETED."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        batch_no = f"BEX-INS-DEST-{self.suffix}"
        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-DEST-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 18.0,
            "storage_check_passed": False,
            "quarantine_suspicious": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": batch_no,
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        grn_id = create_res.data["grn_id"]
        self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/post-receipt/")

        evidence = ReceiptColdChainEvidence.objects.get(grn_id=grn_id)

        # Manager destroys spoiled stock
        resolve_payload = {
            "evidence_id": evidence.evidence_id,
            "resolution_action": "DISPOSE_DESTROY",
            "justification": "Contaminated/denatured batch destroyed in biomedical autoclave according to waste management SOP.",
        }

        resolve_res = self.client.post(f"/api/v2/procurement/goods-receipts/{grn_id}/resolve-quarantine/", resolve_payload, format="json")
        assert resolve_res.status_code == status.HTTP_200_OK
        assert resolve_res.data["quarantine_status"] == ColdChainQuarantineStatus.DISPOSED
        assert resolve_res.data["resolution_action"] == "DISPOSE_DESTROY"

        batch_stock = BatchStock.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch_number=batch_no,
        )
        assert batch_stock.status == BatchStatus.DEPLETED

        cost_lot = PurchaseCostLot.objects.get(
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            product_id=self.insulin.product_id,
            batch=batch_stock,
        )
        assert cost_lot.status == PurchaseCostLotStatus.DEPLETED

    def test_09_quarantined_shipment_listing_and_filtering(self):
        """9. Filter quarantined shipments endpoint: GET /cold-chain-quarantine/?quarantine_status=PENDING_REVIEW."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        # Create one normal and one quarantined shipment
        normal_payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-OK-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 4.0,
            "storage_check_passed": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": f"BEX-OK-{self.suffix}",
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 20,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }
        self.client.post("/api/v2/procurement/goods-receipts/", normal_payload, format="json")

        quarantine_payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-WARN-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 9.5,
            "storage_check_passed": False,
            "quarantine_suspicious": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": f"BEX-WARN-{self.suffix}",
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 30,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }
        res_q = self.client.post("/api/v2/procurement/goods-receipts/", quarantine_payload, format="json")
        q_grn_id = res_q.data["grn_id"]

        # List pending review
        list_res = self.client.get("/api/v2/procurement/goods-receipts/cold-chain-quarantine/?quarantine_status=PENDING_REVIEW")
        assert list_res.status_code == status.HTTP_200_OK
        results = list_res.data["results"]
        assert any(e["grn_id"] == q_grn_id and e["quarantine_status"] == "PENDING_REVIEW" for e in results)
        assert not any(e["batch_number"] == f"BEX-OK-{self.suffix}" for e in results)

        # Get evidence directly for GRN
        ev_res = self.client.get(f"/api/v2/procurement/goods-receipts/{q_grn_id}/cold-chain-evidence/")
        assert ev_res.status_code == status.HTTP_200_OK
        assert ev_res.data["count"] == 1
        assert ev_res.data["results"][0]["batch_number"] == f"BEX-WARN-{self.suffix}"

    def test_10_rbac_and_cross_tenant_isolation(self):
        """10. RBAC and cross-tenant isolation: Cashier blocked from resolve-quarantine; foreign tenant blocked."""
        po = self._create_ordered_po(50)
        po_item = po.items.first()
        self.client.force_authenticate(user=self.manager)

        batch_no = f"BEX-INS-SEC-{self.suffix}"
        payload = {
            "purchase_order_id": po.po_id,
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "challan_number": f"CH-CC-SEC-{self.suffix}",
            "storage_condition": "COLD_CHAIN_2_8C",
            "storage_temperature_celsius": 10.0,
            "storage_check_passed": False,
            "quarantine_suspicious": True,
            "lines": [
                {
                    "po_item_id": po_item.item_id,
                    "product_id": self.insulin.product_id,
                    "batch_number": batch_no,
                    "expiry_date": (timezone.now() + datetime.timedelta(days=365)).strftime("%Y-%m-%d"),
                    "received_packages": 50,
                    "package_size": 1,
                    "unit_cost": "350.0000",
                    "mrp_unit": "420.0000",
                }
            ],
        }

        create_res = self.client.post("/api/v2/procurement/goods-receipts/", payload, format="json")
        grn_id = create_res.data["grn_id"]
        evidence = ReceiptColdChainEvidence.objects.get(grn_id=grn_id)

        # 1. Cashier attempts resolution -> 403 ERR_STAFF_ROLE_DENIED
        self.client.force_authenticate(user=self.cashier)
        cashier_res = self.client.post(
            f"/api/v2/procurement/goods-receipts/{grn_id}/resolve-quarantine/",
            {"evidence_id": evidence.evidence_id, "resolution_action": "APPROVE_RELEASE", "justification": "Cashier release"},
            format="json",
        )
        assert cashier_res.status_code == status.HTTP_403_FORBIDDEN
        assert cashier_res.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Foreign tenant attempts resolution -> 403 ERR_CROSS_TENANT_ACCESS
        self.client.force_authenticate(user=self.foreign_manager)
        foreign_res = self.client.post(
            f"/api/v2/procurement/goods-receipts/{grn_id}/resolve-quarantine/",
            {"evidence_id": evidence.evidence_id, "resolution_action": "APPROVE_RELEASE", "justification": "Foreign release"},
            format="json",
        )
        assert foreign_res.status_code == status.HTTP_403_FORBIDDEN
        assert foreign_res.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"
