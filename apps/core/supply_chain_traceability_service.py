"""
backend/apps/core/supply_chain_traceability_service.py
Architecture Decision: ADR-0197 (P197).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer governing Supply-Chain Traceability:
- Reconstructs complete unbroken medicine lot lineage:
  Supplier Origin -> Purchase Order -> Carrier Shipment -> Goods Receipt Note (GRN) ->
  Batch Stock & Quarantine -> Dispensing & Customer Sales -> Returns & Reversals.
- Computes physical stock reconciliation and transit cold-chain integrity flags.
- Generates audited DGDA compliance traceability records with cryptographic verification hashes.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStock,
    Branch,
    GoodsReceiptNote,
    MedicineProduct,
    ProcurementShipment,
    PurchaseCostLot,
    PurchaseOrder,
    SaleInvoice,
    SaleLotAllocation,
    Supplier,
    SupplyChainTraceabilityRecord,
    Tenant,
    TenantLifecycleStatus,
    TraceAuditPurpose,
)

logger = logging.getLogger(__name__)


class SupplyChainTraceabilityService:
    """
    Authoritative service reconstructing comprehensive upstream procurement and downstream dispensing lineage.
    """

    _ALLOWED_TRACE_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "AUDITOR",
    }

    @classmethod
    def _assert_trace_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_TRACE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role does not have permission to access supply chain traceability.",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant {tenant_id} not found."})

        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is suspended.",
                }
            )
        return tenant

    @classmethod
    def trace_batch_lineage(
        cls,
        tenant_id: str,
        actor_user: Any,
        batch_id_or_number: str,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Reconstructs the full upstream and downstream lineage for a batch/lot.
        """
        cls._assert_trace_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        # 1. Locate BatchStock
        batch_qs = BatchStock.objects.filter(tenant=tenant)
        if branch_id:
            batch_qs = batch_qs.filter(branch_id=branch_id)

        batch = batch_qs.filter(batch_id=batch_id_or_number).first()
        if not batch:
            batch = batch_qs.filter(batch_number=batch_id_or_number).first()

        if not batch:
            raise NotFound(
                {
                    "error_code": "ERR_BATCH_NOT_FOUND",
                    "detail": f"Batch '{batch_id_or_number}' not found for tenant {tenant_id}.",
                }
            )

        product = batch.product

        # 2. Upstream Trace: Cost Lots, GRN, PO, Shipments, Supplier
        cost_lots = PurchaseCostLot.objects.filter(
            tenant=tenant,
            batch=batch,
        ).select_related("supplier")

        total_received = 0
        lot_summaries = []
        po_numbers = set()
        grn_numbers = set()
        supplier_ids = set()

        for cl in cost_lots:
            total_received += cl.initial_quantity
            if cl.purchase_order_number:
                po_numbers.add(cl.purchase_order_number)
            if cl.grn_number:
                grn_numbers.add(cl.grn_number)
            if cl.supplier_id:
                supplier_ids.add(cl.supplier_id)

            lot_summaries.append(
                {
                    "lot_id": cl.lot_id,
                    "supplier_name": cl.supplier_name,
                    "po_number": cl.purchase_order_number,
                    "grn_number": cl.grn_number,
                    "invoice_reference": cl.invoice_reference,
                    "purchase_date": cl.purchase_date.isoformat() if cl.purchase_date else None,
                    "unit_cost": str(cl.unit_cost),
                    "initial_quantity": cl.initial_quantity,
                    "remaining_quantity": cl.remaining_quantity,
                    "status": cl.status,
                }
            )

        # In case lot reference exists on batch itself
        if total_received == 0:
            total_received = batch.available_quantity_base_units

        # Fetch Purchase Orders
        pos = PurchaseOrder.objects.filter(
            tenant=tenant,
            po_number__in=po_numbers,
        ).select_related("supplier")

        po_summaries = []
        po_ids = set()
        for p in pos:
            po_ids.add(p.po_id)
            po_summaries.append(
                {
                    "po_id": p.po_id,
                    "po_number": p.po_number,
                    "supplier_name": p.supplier.name if p.supplier else "",
                    "ordered_at": p.ordered_at.isoformat() if p.ordered_at else None,
                    "status": p.status,
                    "expected_delivery_date": p.expected_delivery_date.isoformat() if p.expected_delivery_date else None,
                    "total_amount": str(p.total_amount),
                    "payment_terms_days": p.payment_terms_days,
                }
            )

        # Fetch GRNs
        grns = GoodsReceiptNote.objects.filter(
            tenant=tenant,
            grn_number__in=grn_numbers,
        )
        grn_summaries = []
        for g in grns:
            grn_summaries.append(
                {
                    "grn_id": g.grn_id,
                    "grn_number": g.grn_number,
                    "challan_number": g.challan_number,
                    "challan_date": g.challan_date.isoformat() if g.challan_date else None,
                    "receipt_date": g.receipt_date.isoformat() if g.receipt_date else None,
                    "storage_condition": g.storage_condition,
                    "storage_temperature_celsius": str(g.storage_temperature_celsius) if g.storage_temperature_celsius is not None else None,
                    "storage_check_passed": g.storage_check_passed,
                    "proof_of_origin": g.proof_of_origin,
                    "status": g.status,
                }
            )

        # Fetch Shipments tied to POs
        shipments = ProcurementShipment.objects.filter(
            tenant=tenant,
            purchase_order__po_number__in=po_numbers,
        )
        shipment_summaries = []
        cold_chain_breached_any = False
        for s in shipments:
            if s.cold_chain_breached:
                cold_chain_breached_any = True
            shipment_summaries.append(
                {
                    "shipment_id": s.shipment_id,
                    "tracking_number": s.tracking_number,
                    "carrier_name": s.carrier_name,
                    "consignment_number": s.consignment_number,
                    "vehicle_registration_number": s.vehicle_registration_number,
                    "driver_name": s.driver_name,
                    "driver_phone": s.driver_phone,
                    "stage": s.stage,
                    "dispatched_at": s.dispatched_at.isoformat() if s.dispatched_at else None,
                    "estimated_delivery_at": s.estimated_delivery_at.isoformat() if s.estimated_delivery_at else None,
                    "actual_delivery_at": s.actual_delivery_at.isoformat() if s.actual_delivery_at else None,
                    "requires_cold_chain": s.requires_cold_chain,
                    "transit_temp_celsius": str(s.transit_temp_celsius) if s.transit_temp_celsius is not None else None,
                    "cold_chain_breached": s.cold_chain_breached,
                    "discrepancy_type": s.discrepancy_type,
                }
            )

        # Fetch Suppliers
        suppliers = Supplier.objects.filter(
            tenant=tenant,
            supplier_id__in=supplier_ids,
        )
        supplier_summaries = []
        for sup in suppliers:
            supplier_summaries.append(
                {
                    "supplier_id": sup.supplier_id,
                    "name": sup.name,
                    "code": sup.code,
                    "dl_number": sup.dl_number,
                    "tin_number": sup.tin_number,
                    "bin_number": sup.bin_number,
                    "proof_of_origin_verified": sup.proof_of_origin_verified,
                }
            )

        # 3. Downstream Trace: Dispensing and Sales Allocations
        sale_allocs = SaleLotAllocation.objects.filter(
            tenant=tenant,
            batch=batch,
        ).select_related("invoice")

        total_dispensed = 0
        total_returned = 0
        sale_events = []

        for sa in sale_allocs:
            dispensed_qty = sa.quantity_base_units
            ret_qty = sa.returned_quantity_base_units
            total_dispensed += dispensed_qty
            total_returned += ret_qty

            inv = sa.invoice
            sale_events.append(
                {
                    "allocation_id": sa.allocation_id,
                    "invoice_id": str(getattr(inv, "invoice_id", sa.invoice_id or "")),
                    "invoice_number": str(getattr(inv, "invoice_number", "")),
                    "dispensed_at": inv.created_at.isoformat() if (inv and getattr(inv, "created_at", None)) else sa.created_at.isoformat(),
                    "quantity_dispensed": dispensed_qty,
                    "quantity_returned": ret_qty,
                    "unit_selling_price": str(sa.unit_selling_price),
                    "is_returned": sa.is_returned,
                    "status": sa.status,
                }
            )

        current_stock = batch.available_quantity_base_units
        reconciliation_discrepancy = (total_received - total_dispensed + total_returned) - current_stock

        return {
            "product": {
                "product_id": product.product_id,
                "brand_name": product.brand_name,
                "generic_name": product.generic_name,
                "strength": product.strength,
                "dosage_form": product.dosage_form,
                "manufacturer": product.manufacturer,
                "dgda_dar_number": getattr(product, "dgda_dar_number", ""),
                "requires_cold_chain": product.requires_cold_chain,
            },
            "batch": {
                "batch_id": batch.batch_id,
                "batch_number": batch.batch_number,
                "lot_reference": batch.lot_reference,
                "manufacturing_date": batch.manufacturing_date.isoformat() if batch.manufacturing_date else None,
                "expiry_date": batch.expiry_date.isoformat() if batch.expiry_date else None,
                "cost_per_unit": str(batch.cost_per_unit),
                "status": batch.status,
                "storage_condition": batch.storage_condition,
                "is_quarantined": batch.is_quarantined,
                "recall_notice": batch.recall_notice,
                "recall_reason": batch.recall_reason,
                "available_quantity": batch.available_quantity_base_units,
                "reserved_quantity": batch.reserved_quantity_base_units,
            },
            "upstream_procurement": {
                "suppliers": supplier_summaries,
                "purchase_orders": po_summaries,
                "shipments": shipment_summaries,
                "goods_receipt_notes": grn_summaries,
                "cost_lots": lot_summaries,
            },
            "downstream_dispensing": {
                "total_dispensed_events": len(sale_events),
                "sales": sale_events,
            },
            "reconciliation": {
                "total_received_quantity": total_received,
                "total_dispensed_quantity": total_dispensed,
                "total_returned_quantity": total_returned,
                "current_stock_on_hand": current_stock,
                "discrepancy_quantity": reconciliation_discrepancy,
                "cold_chain_breached_in_transit": cold_chain_breached_any,
            },
        }

    @classmethod
    def serialize_record(cls, tr: SupplyChainTraceabilityRecord) -> Dict[str, Any]:
        return {
            "trace_id": tr.trace_id,
            "trace_number": tr.trace_number,
            "tenant_id": tr.tenant_id,
            "branch_id": tr.branch_id,
            "product_id": tr.product_id,
            "product_name": tr.product.brand_name if tr.product else "",
            "batch_id": tr.batch_id,
            "batch_number": tr.batch_number,
            "lot_reference": tr.lot_reference,
            "audit_purpose": tr.audit_purpose,
            "total_received_quantity": tr.total_received_quantity,
            "total_dispensed_quantity": tr.total_dispensed_quantity,
            "total_returned_quantity": tr.total_returned_quantity,
            "current_stock_on_hand": tr.current_stock_on_hand,
            "discrepancy_quantity": tr.discrepancy_quantity,
            "cold_chain_breached_in_transit": tr.cold_chain_breached_in_transit,
            "immutable_verification_hash": tr.immutable_verification_hash,
            "generated_by": str(getattr(tr.generated_by, "user_id", tr.generated_by_id or "")) if tr.generated_by_id else None,
            "generated_at": tr.generated_at.isoformat() if tr.generated_at else None,
            "notes": tr.notes,
            "lineage_snapshot": tr.lineage_snapshot,
            "created_at": tr.created_at.isoformat() if tr.created_at else None,
        }

    @classmethod
    @transaction.atomic
    def generate_traceability_record(
        cls,
        tenant_id: str,
        actor_user: Any,
        batch_id_or_number: str,
        audit_purpose: str = TraceAuditPurpose.INTERNAL_AUDIT,
        branch_id: Optional[str] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Generates and saves a formal, audited traceability record with immutable SHA-256 hash.
        """
        cls._assert_trace_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        lineage = cls.trace_batch_lineage(
            tenant_id=tenant_id,
            actor_user=actor_user,
            batch_id_or_number=batch_id_or_number,
            branch_id=branch_id,
        )

        batch_id = lineage["batch"]["batch_id"]
        batch = BatchStock.objects.get(batch_id=batch_id)
        reconcil = lineage["reconciliation"]

        now = timezone.now()
        trc_id = f"trc-{uuid.uuid4().hex[:12]}"
        trc_num = f"TRC-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:6].upper()}"

        record = SupplyChainTraceabilityRecord(
            trace_id=trc_id,
            trace_number=trc_num,
            tenant=tenant,
            branch=batch.branch or tenant.branches.first(),
            product=batch.product,
            batch=batch,
            batch_number=batch.batch_number,
            lot_reference=batch.lot_reference or "",
            audit_purpose=audit_purpose,
            lineage_snapshot=lineage,
            total_received_quantity=reconcil["total_received_quantity"],
            total_dispensed_quantity=reconcil["total_dispensed_quantity"],
            total_returned_quantity=reconcil["total_returned_quantity"],
            current_stock_on_hand=reconcil["current_stock_on_hand"],
            discrepancy_quantity=reconcil["discrepancy_quantity"],
            cold_chain_breached_in_transit=reconcil["cold_chain_breached_in_transit"],
            generated_by=actor_user if hasattr(actor_user, "user_id") else None,
            generated_at=now,
            notes=notes,
            is_active=True,
        )
        record.save()

        return cls.serialize_record(record)

    @classmethod
    def get_traceability_record(cls, tenant_id: str, actor_user: Any, trace_id: str) -> Dict[str, Any]:
        cls._assert_trace_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            record = SupplyChainTraceabilityRecord.objects.get(
                tenant=tenant,
                trace_id=trace_id,
                is_active=True,
            )
        except SupplyChainTraceabilityRecord.DoesNotExist:
            raise NotFound({"error_code": "ERR_TRACE_NOT_FOUND", "detail": f"Trace record {trace_id} not found."})

        return cls.serialize_record(record)

    @classmethod
    def list_traceability_records(
        cls,
        tenant_id: str,
        actor_user: Any,
        batch_number: Optional[str] = None,
        audit_purpose: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_trace_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        qs = SupplyChainTraceabilityRecord.objects.filter(tenant=tenant, is_active=True)
        if batch_number:
            qs = qs.filter(batch_number=batch_number)
        if audit_purpose:
            qs = qs.filter(audit_purpose=audit_purpose)

        return [cls.serialize_record(r) for r in qs.order_by("-generated_at")[:50]]
