"""
backend/apps/core/goods_receipt_service.py
Architecture Decision: ADR-0187 (P187).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Goods Receipt Notes (GRN) Service:
Matching PO lines to received batches, storage condition checks,
idempotent stock posting, AP liability tracking, and reversible inventory workflows.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    ColdChainQuarantineStatus,
    DeliveryStatus,
    GoodsReceiptBatchLink,
    GoodsReceiptLine,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    MedicineProduct,
    PackagingCondition,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    ReceiptColdChainEvidence,
    StorageCondition,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_PROCUREMENT_ROLES = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.STAFF_PHARMACIST,
    UserRole.INVENTORY_CLERK,
    "SUPERADMIN",
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "PROCUREMENT_OFFICER",
    "OWNER_ADMIN",
    "ADMIN",
}


class GoodsReceiptService:
    """
    Core business logic for Goods Received Notes (GRN).
    Enforces purchase order reconciliation, cold-chain storage checks,
    idempotent stock increments, and Accounts Payable (AP) liability confirmation.
    """

    @classmethod
    def _assert_procurement_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if str(role).upper() == "CASHIER" or role not in ALLOWED_PROCUREMENT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and unauthorized staff cannot create or post goods receipts.",
                    "message_en": "Cashier and unauthorized staff cannot create or post goods receipts.",
                    "message_bn": "ক্যাশিয়ার এবং অননুমোদিত কর্মীরা গুডস রিসিট তৈরি বা পোস্ট করতে পারবেন না।",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant subscription is suspended. Goods receipt operations are locked.",
                    "message_en": "Tenant subscription is suspended. Goods receipt operations are locked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। গুডস রিসিট মডিউল বন্ধ।",
                }
            )
        return tenant

    @classmethod
    def serialize_line(cls, line: GoodsReceiptLine) -> Dict[str, Any]:
        """Serializes a GRN line with full details."""
        return {
            "line_id": line.line_id,
            "po_item_id": str(line.po_item_id) if line.po_item_id else None,
            "product_id": str(line.product_id),
            "product_brand_name": line.product.brand_name if line.product else "",
            "batch_number": line.batch_number,
            "expiry_date": line.expiry_date.isoformat() if line.expiry_date else None,
            "mfg_date": line.mfg_date.isoformat() if line.mfg_date else None,
            "pack_unit": line.pack_unit,
            "package_size": line.package_size,
            "received_packages": line.received_packages,
            "ordered_quantity_base_units": line.ordered_quantity_base_units,
            "received_quantity_base_units": line.received_quantity_base_units,
            "unit_cost": str(line.unit_cost),
            "mrp_unit": str(line.mrp_unit),
            "discount_percentage": str(line.discount_percentage),
            "tax_percentage": str(line.tax_percentage),
            "line_total": str(line.line_total),
            "storage_location": line.storage_location,
            "is_bonus_goods": line.is_bonus_goods,
            "excess_quantity": line.excess_quantity,
            "excess_reason": line.excess_reason,
            "is_quarantined": line.is_quarantined,
            "is_fully_allocated": line.is_fully_allocated,
            "created_at": line.created_at.isoformat() if line.created_at else None,
        }

    @classmethod
    def serialize_cold_chain_evidence(cls, evidence: ReceiptColdChainEvidence) -> Dict[str, Any]:
        """Serializes a ReceiptColdChainEvidence record."""
        return {
            "evidence_id": evidence.evidence_id,
            "tenant_id": str(evidence.tenant_id),
            "branch_id": str(evidence.branch_id),
            "grn_id": str(evidence.grn_id),
            "grn_line_id": str(evidence.grn_line_id) if evidence.grn_line_id else None,
            "product_id": str(evidence.product_id),
            "batch_number": evidence.batch_number,
            "logger_serial_number": evidence.logger_serial_number,
            "recorded_temperature_celsius": str(evidence.recorded_temperature_celsius),
            "min_temperature_celsius": str(evidence.min_temperature_celsius) if evidence.min_temperature_celsius is not None else None,
            "max_temperature_celsius": str(evidence.max_temperature_celsius) if evidence.max_temperature_celsius is not None else None,
            "excursion_detected": evidence.excursion_detected,
            "excursion_duration_minutes": evidence.excursion_duration_minutes,
            "packaging_condition": evidence.packaging_condition,
            "carrier_name": evidence.carrier_name,
            "vehicle_type": evidence.vehicle_type,
            "proof_attachment_reference": evidence.proof_attachment_reference,
            "inspection_notes": evidence.inspection_notes,
            "quarantine_status": evidence.quarantine_status,
            "quarantine_reason": evidence.quarantine_reason,
            "reviewed_by_id": str(evidence.reviewed_by_id) if evidence.reviewed_by_id else None,
            "reviewed_at": evidence.reviewed_at.isoformat() if evidence.reviewed_at else None,
            "review_notes": evidence.review_notes,
            "resolution_action": evidence.resolution_action,
            "created_at": evidence.created_at.isoformat() if evidence.created_at else None,
        }

    @classmethod
    def serialize_grn(cls, grn: GoodsReceiptNote) -> Dict[str, Any]:
        """Serializes a complete Goods Received Note."""
        lines = [cls.serialize_line(line) for line in grn.lines.select_related("product", "po_item").all()]
        evidences = [cls.serialize_cold_chain_evidence(e) for e in grn.cold_chain_evidences.all()]
        return {
            "grn_id": grn.grn_id,
            "grn_number": grn.grn_number,
            "tenant_id": str(grn.tenant_id),
            "branch_id": str(grn.branch_id),
            "purchase_order_id": str(grn.purchase_order_id) if grn.purchase_order_id else None,
            "purchase_order_number": grn.purchase_order_number,
            "supplier_id": str(grn.supplier_id) if grn.supplier_id else None,
            "supplier_name": grn.supplier_name,
            "challan_number": grn.challan_number,
            "challan_date": grn.challan_date.isoformat() if grn.challan_date else None,
            "supplier_invoice_number": grn.supplier_invoice_number,
            "receipt_date": grn.receipt_date.isoformat() if grn.receipt_date else None,
            "storage_condition": grn.storage_condition,
            "storage_temperature_celsius": str(grn.storage_temperature_celsius) if grn.storage_temperature_celsius is not None else None,
            "storage_check_passed": grn.storage_check_passed,
            "proof_of_origin": grn.proof_of_origin,
            "status": grn.status,
            "total_received_base_units": grn.total_received_base_units,
            "total_invoice_amount": str(grn.total_invoice_amount),
            "ap_posting_status": grn.ap_posting_status,
            "ap_invoice_amount": str(grn.ap_invoice_amount),
            "notes": grn.notes,
            "is_active": grn.is_active,
            "lines": lines,
            "cold_chain_evidences": evidences,
            "created_at": grn.created_at.isoformat() if grn.created_at else None,
            "updated_at": grn.updated_at.isoformat() if grn.updated_at else None,
        }

    @classmethod
    def create_grn(
        cls,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Creates a Goods Received Note reconciled with an authorized Purchase Order."""
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        target_tenant = data.get("tenant_id") or tenant_id
        if str(target_tenant) != str(tenant_id) and not getattr(actor_user, "is_superuser", False):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant GRN creation forbidden."})

        branch_id = data.get("branch_id") or getattr(actor_user, "branch_id", None)
        if not branch_id:
            raise ValidationError({"error_code": "ERR_MISSING_BRANCH_ID", "detail": "branch_id is required."})

        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        # Purchase Order Reconciliation
        po = None
        po_id = data.get("purchase_order_id")
        if po_id:
            po = PurchaseOrder.objects.filter(po_id=po_id).first()
            if not po:
                raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
            if str(po.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant purchase order access forbidden."})
            if po.status not in (PurchaseOrderStatus.ORDERED, PurchaseOrderStatus.PARTIALLY_RECEIVED):
                raise ValidationError(
                    {
                        "error_code": "ERR_PO_NOT_RECEIVABLE",
                        "detail": f"Cannot receive against purchase order in '{po.status}' status. PO must be in ORDERED or PARTIALLY_RECEIVED status.",
                    }
                )

        supplier = None
        supplier_id = data.get("supplier_id") or (po.supplier_id if po else None)
        supplier_name = data.get("supplier_name", "")
        if supplier_id:
            supplier = Supplier.objects.filter(supplier_id=supplier_id, tenant=tenant).first()
            if supplier:
                supplier_name = supplier.name
            elif po and po.supplier:
                supplier = po.supplier
                supplier_name = po.supplier.name

        # Storage Inspection & Cold-Chain Checks
        storage_condition = data.get("storage_condition", "ROOM_TEMPERATURE")
        storage_temp = data.get("storage_temperature_celsius")
        storage_check_passed = bool(data.get("storage_check_passed", True))
        quarantine_suspicious = bool(data.get("quarantine_suspicious", False))
        cce_payload = data.get("cold_chain_evidence") or {}

        is_cold_chain = "COLD_CHAIN" in storage_condition.upper() or bool(cce_payload)
        temp_dec = None
        if storage_temp is not None:
            temp_dec = Decimal(str(storage_temp))
        elif cce_payload.get("recorded_temperature_celsius") is not None:
            temp_dec = Decimal(str(cce_payload["recorded_temperature_celsius"]))

        has_temp_violation = False
        if is_cold_chain and temp_dec is not None:
            if temp_dec < Decimal("2.0") or temp_dec > Decimal("8.0"):
                has_temp_violation = True

        pack_cond_raw = str(cce_payload.get("packaging_condition", "")).upper()
        has_pack_violation = pack_cond_raw in (
            PackagingCondition.ICE_PACKS_MELTED,
            PackagingCondition.SEAL_BROKEN,
            PackagingCondition.CONTAINER_DAMAGED,
        )
        is_suspicious_delivery = has_temp_violation or has_pack_violation or (not storage_check_passed) or quarantine_suspicious

        if is_suspicious_delivery:
            if quarantine_suspicious or (is_cold_chain and bool(cce_payload.get("quarantine_suspicious", False))):
                # P189: Suspicious cold-chain items quarantined pending review
                pass
            else:
                # P187 baseline: strict gate rejection when quarantine is not requested
                if has_temp_violation:
                    raise ValidationError(
                        {
                            "error_code": "ERR_STORAGE_CHECK_FAILED",
                            "detail": f"Cold-chain temperature violation ({temp_dec}°C). Accepted range is 2.0°C to 8.0°C.",
                        }
                    )
                else:
                    raise ValidationError(
                        {
                            "error_code": "ERR_STORAGE_CHECK_FAILED",
                            "detail": "Cold-chain physical inspection failed. Goods receipt blocked." if is_cold_chain else "Warehouse storage physical inspection failed.",
                        }
                    )

        receipt_date_raw = data.get("receipt_date")
        if receipt_date_raw:
            try:
                receipt_date = (
                    receipt_date_raw
                    if isinstance(receipt_date_raw, datetime.date)
                    else datetime.date.fromisoformat(str(receipt_date_raw))
                )
            except ValueError:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "receipt_date must be YYYY-MM-DD."})
        else:
            receipt_date = timezone.now().date()

        challan_date_raw = data.get("challan_date")
        challan_date = None
        if challan_date_raw:
            try:
                challan_date = (
                    challan_date_raw
                    if isinstance(challan_date_raw, datetime.date)
                    else datetime.date.fromisoformat(str(challan_date_raw))
                )
            except ValueError:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "challan_date must be YYYY-MM-DD."})

        lines_data = data.get("lines")
        if not lines_data or not isinstance(lines_data, list):
            raise ValidationError({"error_code": "ERR_EMPTY_GRN", "detail": "Goods receipt must contain at least one line."})

        grn_id = data.get("grn_id") or f"grn-{uuid.uuid4().hex[:12]}"
        if not grn_id.startswith("grn-"):
            grn_id = f"grn-{grn_id}"

        date_prefix = timezone.now().strftime("%Y%m")
        grn_number = data.get("grn_number") or f"GRN-{date_prefix}-{uuid.uuid4().hex[:6].upper()}"

        today = timezone.now().date()
        validated_lines = []
        total_units = 0
        total_amount = Decimal("0.0000")

        # Validate line items
        for idx, line_payload in enumerate(lines_data):
            po_item = None
            po_item_id = line_payload.get("po_item_id")
            if po_item_id:
                po_item = PurchaseOrderItem.objects.filter(item_id=po_item_id).first()
                if not po_item:
                    raise NotFound({"error_code": "ERR_PO_ITEM_NOT_FOUND", "detail": f"PO Item '{po_item_id}' not found."})
                if po and po_item.purchase_order_id != po.po_id:
                    raise ValidationError(
                        {
                            "error_code": "ERR_PO_ITEM_MISMATCH",
                            "detail": f"PO item '{po_item_id}' does not belong to purchase order '{po.po_id}'.",
                        }
                    )

            prod_id = line_payload.get("product_id") or (po_item.product_id if po_item else None)
            if not prod_id:
                raise ValidationError({"error_code": "ERR_MISSING_PRODUCT", "detail": f"Line {idx} missing product_id."})

            product = MedicineProduct.objects.filter(product_id=prod_id).first()
            if not product:
                raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{prod_id}' not found."})

            batch_number = str(line_payload.get("batch_number", "")).strip()
            if not batch_number:
                raise ValidationError({"error_code": "ERR_MISSING_BATCH_NUMBER", "detail": f"Batch number required on line {idx}."})

            expiry_raw = line_payload.get("expiry_date")
            if not expiry_raw:
                raise ValidationError({"error_code": "ERR_MISSING_EXPIRY_DATE", "detail": f"Expiry date required on line {idx}."})
            try:
                expiry_date = (
                    expiry_raw
                    if isinstance(expiry_raw, datetime.date)
                    else datetime.date.fromisoformat(str(expiry_raw))
                )
            except ValueError:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "expiry_date must be YYYY-MM-DD."})

            if expiry_date <= today:
                raise ValidationError(
                    {
                        "error_code": "ERR_EXPIRED_BATCH_RECEIVED",
                        "detail": f"Batch '{batch_number}' is expired ({expiry_date}). Expired inventory cannot be received.",
                    }
                )

            mfg_date = None
            mfg_raw = line_payload.get("mfg_date")
            if mfg_raw:
                try:
                    mfg_date = mfg_raw if isinstance(mfg_raw, datetime.date) else datetime.date.fromisoformat(str(mfg_raw))
                except ValueError:
                    raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "mfg_date must be YYYY-MM-DD."})

            pkg_size = max(1, int(line_payload.get("package_size", getattr(po_item, "package_size", 1))))
            rec_packages = max(0, int(line_payload.get("received_packages", 0)))
            rec_units = line_payload.get("received_quantity_base_units")

            if rec_units is not None:
                calc_qty = int(rec_units)
            elif rec_packages > 0:
                calc_qty = rec_packages * pkg_size
            else:
                calc_qty = 1

            if calc_qty <= 0:
                raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Received quantity must be >= 1."})

            is_bonus_goods = bool(line_payload.get("is_bonus_goods", False))
            allow_excess = bool(data.get("allow_excess", False) or line_payload.get("allow_excess", False))
            excess_reason = str(line_payload.get("excess_reason", data.get("excess_reason", ""))).strip()
            excess_qty = 0

            # Over-receipt check against PO item
            if po_item:
                remaining_qty = max(0, po_item.ordered_quantity - po_item.received_quantity - po_item.cancelled_quantity)
                if calc_qty > remaining_qty:
                    if is_bonus_goods:
                        excess_qty = calc_qty - remaining_qty
                    elif allow_excess:
                        role = getattr(actor_user, "role", None)
                        if str(role).upper() not in ("BRANCH_MANAGER", "PHARMACY_OWNER", "SUPERADMIN"):
                            raise PermissionDenied(
                                {
                                    "error_code": "ERR_STAFF_ROLE_DENIED",
                                    "detail": "Only Branch Managers and Owners can authorize excess delivery quantities.",
                                }
                            )
                        if not excess_reason:
                            raise ValidationError(
                                {
                                    "error_code": "ERR_MISSING_EXCESS_REASON",
                                    "detail": "Managerial justification (excess_reason) is required to authorize excess deliveries.",
                                }
                            )
                        excess_qty = calc_qty - remaining_qty
                    else:
                        raise ValidationError(
                            {
                                "error_code": "ERR_EXCESS_RECEIPT",
                                "detail": f"Received quantity ({calc_qty}) exceeds remaining unreceived quantity ({remaining_qty}) on PO item.",
                            }
                        )

            if is_bonus_goods:
                unit_cost = Decimal("0.0000")
                line_total = Decimal("0.0000")
                mrp_unit = Decimal(str(line_payload.get("mrp_unit", getattr(po_item, "mrp_unit", product.mrp_unit or "0.0000"))))
                disc_pct = Decimal("0.00")
                tax_pct = Decimal("0.00")
            else:
                unit_cost = Decimal(str(line_payload.get("unit_cost", getattr(po_item, "unit_cost_price", product.cost_unit or "1.0000"))))
                if unit_cost <= Decimal("0.0000"):
                    raise ValidationError({"error_code": "ERR_INVALID_COST", "detail": "Unit cost must be positive."})

                mrp_unit = Decimal(str(line_payload.get("mrp_unit", getattr(po_item, "mrp_unit", product.mrp_unit or "0.0000"))))
                if mrp_unit > Decimal("0.0000") and unit_cost > mrp_unit:
                    raise ValidationError(
                        {
                            "error_code": "ERR_COST_EXCEEDS_MRP",
                            "detail": f"Unit cost ({unit_cost}) cannot exceed maximum retail price ({mrp_unit}).",
                        }
                    )

                disc_pct = Decimal(str(line_payload.get("discount_percentage", getattr(po_item, "discount_percentage", "0.00"))))
                tax_pct = Decimal(str(line_payload.get("tax_percentage", getattr(po_item, "tax_percentage", "0.00"))))

                disc_factor = Decimal("1.0000") - (disc_pct / Decimal("100.00"))
                net_cost = (unit_cost * disc_factor).quantize(Decimal("0.0001"))
                line_subtotal = (net_cost * Decimal(calc_qty)).quantize(Decimal("0.0001"))
                line_tax = (line_subtotal * (tax_pct / Decimal("100.00"))).quantize(Decimal("0.0001"))
                line_total = (line_subtotal + line_tax).quantize(Decimal("0.0001"))

            total_units += calc_qty
            total_amount += line_total

            validated_lines.append(
                {
                    "line_id": line_payload.get("line_id") or f"grnl-{uuid.uuid4().hex[:12]}",
                    "po_item": po_item,
                    "product": product,
                    "batch_number": batch_number,
                    "expiry_date": expiry_date,
                    "mfg_date": mfg_date,
                    "pack_unit": (line_payload.get("pack_unit") or getattr(po_item, "pack_unit", "BOX")).upper(),
                    "package_size": pkg_size,
                    "received_packages": rec_packages,
                    "ordered_quantity_base_units": int(line_payload.get("ordered_quantity_base_units", getattr(po_item, "ordered_quantity", calc_qty))),
                    "received_quantity_base_units": calc_qty,
                    "unit_cost": unit_cost,
                    "mrp_unit": mrp_unit,
                    "discount_percentage": disc_pct,
                    "tax_percentage": tax_pct,
                    "line_total": line_total,
                    "storage_location": str(line_payload.get("storage_location", "MAIN_RACK")).strip(),
                    "is_bonus_goods": is_bonus_goods,
                    "excess_quantity": excess_qty,
                    "excess_reason": excess_reason,
                    "is_quarantined": is_suspicious_delivery or bool(line_payload.get("is_quarantined", False)),
                    "cold_chain_evidence": line_payload.get("cold_chain_evidence"),
                }
            )

        with transaction.atomic():
            grn = GoodsReceiptNote.objects.create(
                grn_id=grn_id,
                grn_number=grn_number,
                tenant=tenant,
                branch=branch,
                purchase_order=po,
                purchase_order_number=po.po_number if po else data.get("purchase_order_number", ""),
                supplier=supplier,
                supplier_name=supplier_name,
                challan_number=data.get("challan_number", "").strip(),
                challan_date=challan_date,
                supplier_invoice_number=data.get("supplier_invoice_number", "").strip(),
                receipt_date=receipt_date,
                storage_condition=storage_condition,
                storage_temperature_celsius=Decimal(str(storage_temp)) if storage_temp is not None else None,
                storage_check_passed=storage_check_passed,
                proof_of_origin=data.get("proof_of_origin", "DGDA Invoice & Delivery Challan").strip(),
                status=GoodsReceiptStatus.RECEIVED,
                total_received_base_units=total_units,
                total_invoice_amount=total_amount,
                ap_posting_status="PENDING",
                ap_invoice_amount=Decimal("0.0000"),
                received_by=actor_user if hasattr(actor_user, "user_id") else None,
                notes=data.get("notes", "").strip(),
                is_active=True,
            )

            for line_dict in validated_lines:
                grn_line = GoodsReceiptLine.objects.create(
                    line_id=line_dict["line_id"],
                    grn=grn,
                    tenant=tenant,
                    product=line_dict["product"],
                    po_item=line_dict["po_item"],
                    batch_number=line_dict["batch_number"],
                    expiry_date=line_dict["expiry_date"],
                    mfg_date=line_dict["mfg_date"],
                    pack_unit=line_dict["pack_unit"],
                    package_size=line_dict["package_size"],
                    received_packages=line_dict["received_packages"],
                    ordered_quantity_base_units=line_dict["ordered_quantity_base_units"],
                    received_quantity_base_units=line_dict["received_quantity_base_units"],
                    unit_cost=line_dict["unit_cost"],
                    mrp_unit=line_dict["mrp_unit"],
                    discount_percentage=line_dict["discount_percentage"],
                    tax_percentage=line_dict["tax_percentage"],
                    line_total=line_dict["line_total"],
                    storage_location=line_dict["storage_location"],
                    is_bonus_goods=line_dict.get("is_bonus_goods", False),
                    excess_quantity=line_dict.get("excess_quantity", 0),
                    excess_reason=line_dict.get("excess_reason", ""),
                    is_quarantined=line_dict.get("is_quarantined", False),
                    is_fully_allocated=False,
                )

                if is_cold_chain:
                    line_cce = line_dict.get("cold_chain_evidence") or cce_payload or {}
                    recorded_temp = Decimal(str(line_cce.get("recorded_temperature_celsius") or storage_temp or "4.0"))
                    min_temp = Decimal(str(line_cce["min_temperature_celsius"])) if line_cce.get("min_temperature_celsius") is not None else None
                    max_temp = Decimal(str(line_cce["max_temperature_celsius"])) if line_cce.get("max_temperature_celsius") is not None else None
                    pack_cond = str(line_cce.get("packaging_condition") or (
                        PackagingCondition.INTACT_SEALED if storage_check_passed else PackagingCondition.ICE_PACKS_MELTED
                    )).upper()
                    if pack_cond not in PackagingCondition.values:
                        pack_cond = PackagingCondition.INTACT_SEALED

                    line_is_suspicious = line_dict.get("is_quarantined", False)
                    q_status = (
                        ColdChainQuarantineStatus.PENDING_REVIEW if line_is_suspicious
                        else ColdChainQuarantineStatus.NOT_QUARANTINED
                    )
                    q_reason = str(line_cce.get("quarantine_reason") or data.get("quarantine_reason", "Cold-chain anomaly detected")).strip() if line_is_suspicious else ""

                    ReceiptColdChainEvidence.objects.create(
                        evidence_id=f"cce-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        branch=branch,
                        grn=grn,
                        grn_line=grn_line,
                        product=line_dict["product"],
                        batch_number=line_dict["batch_number"],
                        logger_serial_number=str(line_cce.get("logger_serial_number", "")).strip(),
                        recorded_temperature_celsius=recorded_temp,
                        min_temperature_celsius=min_temp,
                        max_temperature_celsius=max_temp,
                        excursion_detected=line_is_suspicious,
                        excursion_duration_minutes=int(line_cce.get("excursion_duration_minutes", 0)),
                        packaging_condition=pack_cond,
                        carrier_name=str(line_cce.get("carrier_name", "")).strip(),
                        vehicle_type=str(line_cce.get("vehicle_type", "REFRIGERATED_VAN")).strip(),
                        proof_attachment_reference=str(line_cce.get("proof_attachment_reference", grn.proof_of_origin or "")).strip(),
                        inspection_notes=str(line_cce.get("inspection_notes", grn.notes or "")).strip(),
                        quarantine_status=q_status,
                        quarantine_reason=q_reason,
                    )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="GRN_CREATED",
            resource_type="GoodsReceiptNote",
            resource_id=grn.grn_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "grn_number": grn.grn_number,
                "po_number": grn.purchase_order_number,
                "total_units": total_units,
                "total_amount": str(total_amount),
            },
        )

        return cls.serialize_grn(grn)

    @classmethod
    @transaction.atomic
    def post_grn_to_stock(
        cls,
        tenant_id: str,
        actor_user: Any,
        grn_id: str,
    ) -> Dict[str, Any]:
        """
        Executes stock-posting command:
        1. Increments BatchStock base units exactly once.
        2. Creates distinct PurchaseCostLot entries for FEFO accounting.
        3. Updates received_quantity on linked PurchaseOrderItem lines.
        4. Updates parent PurchaseOrder status (PARTIALLY_RECEIVED or CLOSED).
        5. Confirms Accounts Payable (AP) liability.
        Idempotent: Duplicate post attempts are strictly rejected with ERR_ALREADY_POSTED.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        grn = GoodsReceiptNote.objects.select_for_update().filter(grn_id=grn_id).first()
        if not grn:
            raise NotFound({"error_code": "ERR_GRN_NOT_FOUND", "detail": f"GRN '{grn_id}' not found."})
        if str(grn.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant GRN post forbidden."})

        if grn.status == GoodsReceiptStatus.POSTED:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_POSTED",
                    "detail": "Goods receipt is already posted to inventory. Duplicate posting is prohibited.",
                }
            )

        if grn.status in (GoodsReceiptStatus.CANCELLED, GoodsReceiptStatus.REVERSED):
            raise ValidationError(
                {
                    "error_code": "ERR_GRN_INACTIVE",
                    "detail": f"Cannot post '{grn.status}' goods receipt note.",
                }
            )

        lines = list(grn.lines.select_related("product", "po_item").all())
        if not lines:
            raise ValidationError({"error_code": "ERR_EMPTY_GRN", "detail": "GRN has no lines to post."})

        now = timezone.now()
        posted_lots = []

        for line in lines:
            is_line_quarantined = line.is_quarantined
            target_status = BatchStatus.QUARANTINED if is_line_quarantined else BatchStatus.ACTIVE
            cost_lot_status = PurchaseCostLotStatus.QUARANTINED if is_line_quarantined else PurchaseCostLotStatus.ACTIVE

            # 1. Update or create BatchStock
            batch = BatchStock.objects.filter(
                tenant=grn.tenant,
                branch=grn.branch,
                product=line.product,
                batch_number=line.batch_number,
            ).first()

            if not batch:
                cond_val = StorageCondition.ROOM_TEMPERATURE
                if "COLD_CHAIN" in str(grn.storage_condition).upper():
                    cond_val = StorageCondition.COLD_CHAIN
                elif "FREEZ" in str(grn.storage_condition).upper():
                    cond_val = StorageCondition.FREEZER

                batch = BatchStock.objects.create(
                    batch_id=f"bat-{uuid.uuid4().hex[:12]}",
                    tenant=grn.tenant,
                    branch=grn.branch,
                    product=line.product,
                    batch_number=line.batch_number,
                    quantity_base_units=line.received_quantity_base_units,
                    expiry_date=line.expiry_date,
                    purchase_date=grn.receipt_date,
                    cost_per_unit=line.unit_cost,
                    status=target_status,
                    is_quarantined=is_line_quarantined,
                    manufacturing_date=line.mfg_date,
                    storage_condition=cond_val,
                    is_active=True,
                )
            else:
                batch.quantity_base_units += line.received_quantity_base_units
                if is_line_quarantined:
                    batch.status = BatchStatus.QUARANTINED
                    batch.is_quarantined = True
                elif batch.status == BatchStatus.DEPLETED:
                    batch.status = BatchStatus.ACTIVE
                    batch.is_quarantined = False
                batch.save(update_fields=["quantity_base_units", "status", "is_quarantined", "updated_at"])

            # 2. Create distinct PurchaseCostLot
            cost_lot = PurchaseCostLot.objects.create(
                lot_id=f"pcl-{uuid.uuid4().hex[:12]}",
                tenant=grn.tenant,
                branch=grn.branch,
                product=line.product,
                batch=batch,
                supplier=grn.supplier,
                supplier_name=grn.supplier_name,
                purchase_order_number=grn.purchase_order_number,
                grn_number=grn.grn_number,
                invoice_reference=grn.supplier_invoice_number or grn.challan_number,
                purchase_date=grn.receipt_date,
                unit_cost=line.unit_cost,
                initial_quantity=line.received_quantity_base_units,
                remaining_quantity=line.received_quantity_base_units,
                status=cost_lot_status,
                notes=f"Posted via GRN {grn.grn_number} line {line.line_id}",
                is_active=True,
            )

            # 3. Create GoodsReceiptBatchLink
            GoodsReceiptBatchLink.objects.create(
                link_id=f"grnbl-{uuid.uuid4().hex[:12]}",
                tenant=grn.tenant,
                line=line,
                grn=grn,
                batch=batch,
                cost_lot=cost_lot,
                quantity_base_units=line.received_quantity_base_units,
                unit_cost=line.unit_cost,
                is_posted=True,
                posted_at=now,
            )

            line.is_fully_allocated = True
            line.save(update_fields=["is_fully_allocated", "updated_at"])

            # 4. Update PO line item received quantity & delivery status
            if line.po_item:
                line.po_item.received_quantity += line.received_quantity_base_units
                if (line.po_item.received_quantity + line.po_item.cancelled_quantity) >= line.po_item.ordered_quantity:
                    if line.po_item.received_quantity > 0:
                        line.po_item.delivery_status = DeliveryStatus.FULLY_DELIVERED
                    else:
                        line.po_item.delivery_status = DeliveryStatus.CANCELLED
                elif line.po_item.received_quantity > 0:
                    line.po_item.delivery_status = DeliveryStatus.PARTIALLY_DELIVERED
                line.po_item.save(update_fields=["received_quantity", "delivery_status"])

            posted_lots.append(cost_lot.lot_id)

        # 5. Evaluate Parent Purchase Order Status
        if grn.purchase_order:
            po = grn.purchase_order
            po_items = list(po.items.all())
            all_fulfilled = all((item.received_quantity + item.cancelled_quantity) >= item.ordered_quantity for item in po_items)
            any_received = any(item.received_quantity > 0 for item in po_items)

            if all_fulfilled:
                po.status = PurchaseOrderStatus.CLOSED
                po.closed_at = now
                po.closing_notes = f"Fully received/fulfilled via GRN {grn.grn_number}"
            elif any_received:
                po.status = PurchaseOrderStatus.PARTIALLY_RECEIVED

            po.save(update_fields=["status", "closed_at", "closing_notes", "updated_at"])

        # 6. Finalize GRN & AP Liability
        grn.status = GoodsReceiptStatus.POSTED
        grn.verified_by = actor_user if hasattr(actor_user, "user_id") else None
        grn.ap_posting_status = "POSTED"
        grn.ap_invoice_amount = grn.total_invoice_amount
        grn.save(update_fields=["status", "verified_by", "ap_posting_status", "ap_invoice_amount", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="GRN_STOCK_POSTED",
            resource_type="GoodsReceiptNote",
            resource_id=grn.grn_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "grn_number": grn.grn_number,
                "posted_lots_count": len(posted_lots),
                "ap_invoice_amount": str(grn.ap_invoice_amount),
            },
        )

        return cls.serialize_grn(grn)

    @classmethod
    @transaction.atomic
    def reverse_grn(
        cls,
        tenant_id: str,
        actor_user: Any,
        grn_id: str,
        reason: str = "Shipment Return / Supplier Correction",
    ) -> Dict[str, Any]:
        """
        Reverses a posted Goods Received Note, decrementing batch stock
        and marking linked purchase cost lots as REVERSED.
        Blocks reversal if any portion of the stock was already dispensed to patients.
        """
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        grn = GoodsReceiptNote.objects.select_for_update().filter(grn_id=grn_id).first()
        if not grn:
            raise NotFound({"error_code": "ERR_GRN_NOT_FOUND", "detail": f"GRN '{grn_id}' not found."})
        if str(grn.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant GRN reversal forbidden."})

        if grn.status != GoodsReceiptStatus.POSTED:
            grn.status = GoodsReceiptStatus.CANCELLED
            grn.save(update_fields=["status", "updated_at"])
            return cls.serialize_grn(grn)

        # Check whether stock was consumed
        batch_links = grn.batch_links.filter(is_posted=True).select_related("batch", "cost_lot")
        for link in batch_links:
            cost_lot = link.cost_lot
            if not cost_lot or cost_lot.remaining_quantity < link.quantity_base_units:
                consumed = link.quantity_base_units - (cost_lot.remaining_quantity if cost_lot else 0)
                raise ValidationError(
                    {
                        "error_code": "ERR_CANNOT_REVERSE_CONSUMED_STOCK",
                        "detail": f"Cannot reverse GRN; {consumed} units of batch '{link.batch.batch_number}' have already been dispensed.",
                    }
                )

        # Reverse stock & cost lots
        for link in batch_links:
            batch = BatchStock.objects.select_for_update().get(batch_id=link.batch_id)
            batch.quantity_base_units = max(0, batch.quantity_base_units - link.quantity_base_units)
            if batch.quantity_base_units == 0:
                batch.status = BatchStatus.DEPLETED
            batch.save(update_fields=["quantity_base_units", "status", "updated_at"])

            if link.cost_lot:
                link.cost_lot.remaining_quantity = 0
                link.cost_lot.status = PurchaseCostLotStatus.REVERSED
                link.cost_lot.notes = f"Reversed via GRN reversal: {reason}"
                link.cost_lot.save(update_fields=["remaining_quantity", "status", "notes", "updated_at"])

            link.is_posted = False
            link.save(update_fields=["is_posted"])

        # Roll back PO item received quantities & delivery status
        for line in grn.lines.select_related("po_item").all():
            if line.po_item:
                line.po_item.received_quantity = max(0, line.po_item.received_quantity - line.received_quantity_base_units)
                if (line.po_item.received_quantity + line.po_item.cancelled_quantity) >= line.po_item.ordered_quantity:
                    if line.po_item.received_quantity > 0:
                        line.po_item.delivery_status = DeliveryStatus.FULLY_DELIVERED
                    else:
                        line.po_item.delivery_status = DeliveryStatus.CANCELLED
                elif line.po_item.received_quantity > 0:
                    line.po_item.delivery_status = DeliveryStatus.PARTIALLY_DELIVERED
                elif line.po_item.cancelled_quantity >= line.po_item.ordered_quantity:
                    line.po_item.delivery_status = DeliveryStatus.CANCELLED
                else:
                    line.po_item.delivery_status = DeliveryStatus.PENDING
                line.po_item.save(update_fields=["received_quantity", "delivery_status"])

        # Roll back parent Purchase Order status
        if grn.purchase_order:
            po = grn.purchase_order
            po_items = list(po.items.all())
            all_fulfilled = all((item.received_quantity + item.cancelled_quantity) >= item.ordered_quantity for item in po_items)
            any_received = any(item.received_quantity > 0 for item in po_items)
            if all_fulfilled:
                po.status = PurchaseOrderStatus.CLOSED
            elif any_received:
                po.status = PurchaseOrderStatus.PARTIALLY_RECEIVED
            else:
                po.status = PurchaseOrderStatus.ORDERED
                po.closed_at = None
            po.save(update_fields=["status", "closed_at", "updated_at"])

        grn.status = GoodsReceiptStatus.REVERSED
        grn.ap_posting_status = "REVERSED"
        grn.notes = f"{grn.notes} | Reversal Reason: {reason}".strip()
        grn.save(update_fields=["status", "ap_posting_status", "notes", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="GRN_REVERSED",
            resource_type="GoodsReceiptNote",
            resource_id=grn.grn_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"grn_number": grn.grn_number, "reason": reason},
        )

        return cls.serialize_grn(grn)

    @classmethod
    def get_grn(cls, tenant_id: str, grn_id: str) -> Dict[str, Any]:
        """Retrieves a single GRN."""
        cls._assert_tenant_active(tenant_id)
        grn = GoodsReceiptNote.objects.select_related("supplier", "purchase_order").prefetch_related("lines__product", "lines__po_item").filter(grn_id=grn_id).first()
        if not grn:
            raise NotFound({"error_code": "ERR_GRN_NOT_FOUND", "detail": f"GRN '{grn_id}' not found."})
        if str(grn.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant GRN access forbidden."})
        return cls.serialize_grn(grn)

    @classmethod
    def list_grns(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        po_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists GRNs for tenant with optional filters."""
        tenant = cls._assert_tenant_active(tenant_id)
        qs = GoodsReceiptNote.objects.select_related("supplier", "purchase_order").prefetch_related("lines__product", "lines__po_item").filter(tenant=tenant, is_active=True)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if po_id:
            qs = qs.filter(purchase_order_id=po_id)
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if status:
            qs = qs.filter(status=status.upper())
        qs = qs.order_by("-receipt_date", "-created_at")
        return [cls.serialize_grn(grn) for grn in qs]

    @classmethod
    @transaction.atomic
    def cancel_po_backorder(
        cls,
        tenant_id: str,
        actor_user: Any,
        po_id: str,
        item_id: str,
        cancel_quantity: int,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Cancels unfulfillable backordered quantities on a PO line item.
        Guarded: Only Managers/Owners can cancel. Unreceived stock is never counted in inventory.
        If all line backorders are cancelled/fulfilled, transitions the PO to CLOSED.
        """
        role = getattr(actor_user, "role", None)
        if str(role).upper() not in ("BRANCH_MANAGER", "PHARMACY_OWNER", "SUPERADMIN"):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Only Branch Managers and Owners can cancel purchase order backorders.",
                }
            )
        tenant = cls._assert_tenant_active(tenant_id)

        po = PurchaseOrder.objects.select_for_update().filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant PO access forbidden."})

        if po.status not in (PurchaseOrderStatus.ORDERED, PurchaseOrderStatus.PARTIALLY_RECEIVED):
            raise ValidationError(
                {
                    "error_code": "ERR_PO_NOT_MODIFIABLE",
                    "detail": f"Cannot cancel backorders on PO in '{po.status}' status.",
                }
            )

        po_item = po.items.select_for_update().filter(item_id=item_id).first()
        if not po_item:
            raise NotFound({"error_code": "ERR_PO_ITEM_NOT_FOUND", "detail": f"PO item '{item_id}' not found on order '{po_id}'."})

        cancel_qty = int(cancel_quantity)
        if cancel_qty <= 0:
            raise ValidationError({"error_code": "ERR_INVALID_CANCEL_QUANTITY", "detail": "cancel_quantity must be positive."})

        current_backorder = po_item.backordered_quantity
        if cancel_qty > current_backorder:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_CANCEL_QUANTITY",
                    "detail": f"Cannot cancel {cancel_qty} units; only {current_backorder} units are currently backordered.",
                }
            )

        if not str(reason).strip():
            raise ValidationError({"error_code": "ERR_MISSING_CANCEL_REASON", "detail": "Cancellation reason is required."})

        po_item.cancelled_quantity += cancel_qty
        if (po_item.received_quantity + po_item.cancelled_quantity) >= po_item.ordered_quantity:
            if po_item.received_quantity > 0:
                po_item.delivery_status = DeliveryStatus.FULLY_DELIVERED
            else:
                po_item.delivery_status = DeliveryStatus.CANCELLED
        else:
            po_item.delivery_status = DeliveryStatus.PARTIALLY_DELIVERED

        po_item.save(update_fields=["cancelled_quantity", "delivery_status"])

        # Check if all lines are satisfied
        po_items = list(po.items.all())
        all_fulfilled = all((item.received_quantity + item.cancelled_quantity) >= item.ordered_quantity for item in po_items)
        if all_fulfilled:
            po.status = PurchaseOrderStatus.CLOSED
            po.closed_at = timezone.now()
            po.closing_notes = f"Closed after backorder cancellation: {reason.strip()}"
            po.save(update_fields=["status", "closed_at", "closing_notes", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PO_BACKORDER_CANCELLED",
            resource_type="PurchaseOrderItem",
            resource_id=po_item.item_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "po_id": po.po_id,
                "item_id": po_item.item_id,
                "cancelled_quantity": cancel_qty,
                "remaining_backorder": po_item.backordered_quantity,
                "reason": reason.strip(),
            },
        )

        return {
            "item_id": po_item.item_id,
            "product_id": str(po_item.product_id),
            "ordered_quantity": po_item.ordered_quantity,
            "received_quantity": po_item.received_quantity,
            "cancelled_quantity": po_item.cancelled_quantity,
            "backordered_quantity": po_item.backordered_quantity,
            "delivery_status": po_item.delivery_status,
            "po_status": po.status,
        }

    @classmethod
    def get_po_delivery_summary(cls, tenant_id: str, po_id: str) -> Dict[str, Any]:
        """Returns delivery progress, line fulfillment, backorders, and linked GRNs for a PO."""
        cls._assert_tenant_active(tenant_id)
        po = PurchaseOrder.objects.filter(po_id=po_id).first()
        if not po:
            raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order '{po_id}' not found."})
        if str(po.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant PO access forbidden."})

        items_summary = []
        for itm in po.items.select_related("product").all():
            items_summary.append({
                "item_id": itm.item_id,
                "product_id": str(itm.product_id),
                "product_brand_name": itm.product.brand_name if itm.product else "",
                "pack_unit": itm.pack_unit,
                "package_size": itm.package_size,
                "ordered_quantity": itm.ordered_quantity,
                "received_quantity": itm.received_quantity,
                "cancelled_quantity": itm.cancelled_quantity,
                "backordered_quantity": itm.backordered_quantity,
                "delivery_status": itm.delivery_status,
                "unit_cost_price": str(itm.unit_cost_price),
                "mrp_unit": str(itm.mrp_unit),
            })

        grns_summary = []
        for grn in po.goods_receipt_notes.all():
            grns_summary.append({
                "grn_id": grn.grn_id,
                "grn_number": grn.grn_number,
                "challan_number": grn.challan_number,
                "receipt_date": grn.receipt_date.isoformat(),
                "status": grn.status,
                "total_received_base_units": grn.total_received_base_units,
                "total_invoice_amount": str(grn.total_invoice_amount),
            })

        total_ordered = sum(i["ordered_quantity"] for i in items_summary)
        total_received = sum(i["received_quantity"] for i in items_summary)
        total_cancelled = sum(i["cancelled_quantity"] for i in items_summary)
        total_backordered = sum(i["backordered_quantity"] for i in items_summary)

        return {
            "po_id": po.po_id,
            "po_number": po.po_number,
            "po_status": po.status,
            "tenant_id": str(po.tenant_id),
            "branch_id": str(po.branch_id),
            "total_ordered_units": total_ordered,
            "total_received_units": total_received,
            "total_cancelled_units": total_cancelled,
            "total_backordered_units": total_backordered,
            "items": items_summary,
            "linked_grns": grns_summary,
            "goods_receipts": grns_summary,
            "has_backorders": any(i["backordered_quantity"] > 0 for i in items_summary),
        }

    @classmethod
    @transaction.atomic
    def resolve_cold_chain_quarantine(
        cls,
        tenant_id: str,
        actor_user: Any,
        evidence_id: str,
        resolution_action: str,
        justification: str,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Executes a formal review of a quarantined cold-chain receipt evidence:
        - APPROVE_RELEASE: Releases batch & cost lot to ACTIVE status with documented stability justification.
        - REJECT_RETURN: Condemns batch for return to supplier; sets batch to RECALLED, reverses cost lot, credits AP liability.
        - DISPOSE_DESTROY: Condemns batch for disposal/destruction; sets batch to DEPLETED, marks cost lot DEPLETED.
        """
        cls._assert_procurement_role(actor_user)
        role = str(getattr(actor_user, "role", "")).upper()
        if role not in ("BRANCH_MANAGER", "STAFF_PHARMACIST", "PHARMACY_OWNER", "SUPERADMIN"):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Only Branch Managers and Registered Pharmacists can resolve cold-chain quarantine.",
                }
            )
        tenant = cls._assert_tenant_active(tenant_id)

        evidence = ReceiptColdChainEvidence.objects.select_for_update().filter(evidence_id=evidence_id).first()
        if not evidence:
            raise NotFound({"error_code": "ERR_EVIDENCE_NOT_FOUND", "detail": f"Cold-chain evidence '{evidence_id}' not found."})
        if str(evidence.tenant_id) != str(tenant_id):
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant quarantine resolution forbidden."})

        if evidence.quarantine_status != ColdChainQuarantineStatus.PENDING_REVIEW:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_RESOLVED",
                    "detail": f"Evidence is already in status '{evidence.quarantine_status}'. Cannot resolve again.",
                }
            )

        act = str(resolution_action).strip().upper()
        if act not in ("APPROVE_RELEASE", "REJECT_RETURN", "DISPOSE_DESTROY"):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_RESOLUTION_ACTION",
                    "detail": f"Invalid resolution action '{resolution_action}'. Must be APPROVE_RELEASE, REJECT_RETURN, or DISPOSE_DESTROY.",
                }
            )

        just = str(justification).strip()
        if not just:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_JUSTIFICATION",
                    "detail": "Documented stability justification is mandatory to resolve quarantined cold-chain stock.",
                }
            )

        now = timezone.now()
        evidence.resolution_action = act
        evidence.review_notes = just if not notes.strip() else f"{just} | {notes.strip()}"
        evidence.reviewed_by = actor_user
        evidence.reviewed_at = now

        batch = BatchStock.objects.select_for_update().filter(
            tenant_id=evidence.tenant_id,
            branch_id=evidence.branch_id,
            product_id=evidence.product_id,
            batch_number=evidence.batch_number,
        ).first()

        cost_lots = []
        if evidence.grn_line_id:
            links = GoodsReceiptBatchLink.objects.filter(line_id=evidence.grn_line_id).select_related("cost_lot")
            cost_lots = [link.cost_lot for link in links if link.cost_lot]

        grn = evidence.grn

        if act == "APPROVE_RELEASE":
            evidence.quarantine_status = ColdChainQuarantineStatus.RELEASED
            if batch:
                batch.status = BatchStatus.ACTIVE
                batch.is_quarantined = False
                batch.save(update_fields=["status", "is_quarantined", "updated_at"])
            for cl in cost_lots:
                cl.status = PurchaseCostLotStatus.ACTIVE
                cl.save(update_fields=["status", "updated_at"])
            audit_action = "COLD_CHAIN_QUARANTINE_RELEASED"

        elif act == "REJECT_RETURN":
            evidence.quarantine_status = ColdChainQuarantineStatus.RETURNED_TO_SUPPLIER
            if batch:
                batch.status = BatchStatus.RECALLED
                batch.is_quarantined = False
                batch.save(update_fields=["status", "is_quarantined", "updated_at"])
            for cl in cost_lots:
                cl.status = PurchaseCostLotStatus.REVERSED
                cl.save(update_fields=["status", "updated_at"])
            # Credit AP invoice liability on parent GRN
            if evidence.grn_line and grn.ap_posting_status == "POSTED":
                grn.ap_invoice_amount = max(Decimal("0.0000"), grn.ap_invoice_amount - evidence.grn_line.line_total)
                grn.save(update_fields=["ap_invoice_amount", "updated_at"])
            audit_action = "COLD_CHAIN_QUARANTINE_RETURNED"

        else:  # DISPOSE_DESTROY
            evidence.quarantine_status = ColdChainQuarantineStatus.DISPOSED
            if batch:
                batch.status = BatchStatus.DEPLETED
                batch.is_quarantined = False
                batch.save(update_fields=["status", "is_quarantined", "updated_at"])
            for cl in cost_lots:
                cl.status = PurchaseCostLotStatus.DEPLETED
                cl.save(update_fields=["status", "updated_at"])
            audit_action = "COLD_CHAIN_QUARANTINE_DESTROYED"

        evidence.save(update_fields=["quarantine_status", "resolution_action", "review_notes", "reviewed_by", "reviewed_at", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action=audit_action,
            resource_type="ReceiptColdChainEvidence",
            resource_id=evidence.evidence_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "evidence_id": evidence.evidence_id,
                "batch_number": evidence.batch_number,
                "action": act,
                "justification": just,
                "grn_id": grn.grn_id,
            },
        )

        return cls.serialize_cold_chain_evidence(evidence)

    @classmethod
    def get_cold_chain_evidences(
        cls,
        tenant_id: str,
        grn_id: Optional[str] = None,
        quarantine_status: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = ReceiptColdChainEvidence.objects.filter(tenant_id=tenant_id).select_related("product", "grn", "grn_line", "reviewed_by")
        if grn_id:
            qs = qs.filter(grn_id=grn_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if quarantine_status:
            qs = qs.filter(quarantine_status=quarantine_status.upper())
        return [cls.serialize_cold_chain_evidence(e) for e in qs]

