"""
backend/apps/core/purchase_return_service.py
Architecture Decision: ADR-0191 (P191).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer for Supplier Purchase Returns & Debit Notes:
- Ties supplier returns directly to original PurchaseCostLots and physical BatchStock.
- Prevents returning stock already dispensed to patients or reserved for active POS carts.
- Decrements BatchStock and CostLot remaining quantities atomically.
- Issues immutable Accounts Payable debit notes (SupplierLedgerEntry) reducing vendor liability.
"""

from __future__ import annotations

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
    GoodsReceiptNote,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    PurchaseOrder,
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


class PurchaseReturnService:

    _ALLOWED_PROCUREMENT_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
    }

    @classmethod
    def _assert_procurement_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_PROCUREMENT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role does not have permission to execute supplier purchase returns.",
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
                    "detail": "Tenant account subscription is suspended. Purchase returns locked.",
                }
            )
        return tenant

    @classmethod
    def serialize_return_item(cls, item: PurchaseReturnItem) -> Dict[str, Any]:
        return {
            "item_id": item.item_id,
            "product_id": item.product_id,
            "product_brand_name": item.product.brand_name if item.product else "",
            "batch_stock_id": item.batch_stock_id,
            "cost_lot_id": item.cost_lot_id,
            "batch_number": item.batch_number,
            "return_quantity": item.return_quantity,
            "unit_cost": str(item.unit_cost),
            "line_debit_amount": str(item.line_debit_amount),
            "reason": item.reason,
            "notes": item.notes,
        }

    @classmethod
    def serialize_purchase_return(cls, pret: PurchaseReturn) -> Dict[str, Any]:
        return {
            "return_id": pret.return_id,
            "return_number": pret.return_number,
            "tenant_id": pret.tenant_id,
            "branch_id": pret.branch_id,
            "supplier_id": pret.supplier_id,
            "supplier_name": pret.supplier_name or (pret.supplier.name if pret.supplier else ""),
            "goods_receipt_id": pret.goods_receipt_id,
            "purchase_order_id": pret.purchase_order_id,
            "status": pret.status,
            "return_reason": pret.return_reason,
            "disposition": pret.disposition,
            "reason_notes": pret.reason_notes,
            "total_return_quantity": pret.total_return_quantity,
            "total_debit_amount": str(pret.total_debit_amount),
            "debit_note_reference": pret.debit_note_reference,
            "vendor_credit_note_ref": pret.vendor_credit_note_ref,
            "carrier_pickup_reference": pret.carrier_pickup_reference,
            "debit_ledger_entry_id": pret.debit_ledger_entry_id,
            "posted_at": pret.posted_at.isoformat() if pret.posted_at else None,
            "created_by": str(getattr(pret.created_by, "user_id", pret.created_by_id or "")) if pret.created_by_id else None,
            "approved_by": str(getattr(pret.approved_by, "user_id", pret.approved_by_id or "")) if pret.approved_by_id else None,
            "is_active": pret.is_active,
            "items": [cls.serialize_return_item(item) for item in pret.items.all().select_related("product", "batch_stock", "cost_lot")],
            "created_at": pret.created_at.isoformat() if pret.created_at else None,
            "updated_at": pret.updated_at.isoformat() if pret.updated_at else None,
        }

    @classmethod
    def create_purchase_return(
        cls,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
        auto_post: bool = False,
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        supplier_id = str(data.get("supplier_id", "")).strip()
        if not supplier_id:
            raise ValidationError({"error_code": "ERR_MISSING_SUPPLIER", "detail": "Supplier ID is required."})

        try:
            supplier = Supplier.objects.get(supplier_id=supplier_id, tenant_id=tenant_id)
        except Supplier.DoesNotExist:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier {supplier_id} not found."})

        branch_id = data.get("branch_id") or getattr(actor_user, "branch_id", None)
        branch = tenant.branches.filter(branch_id=branch_id).first() if branch_id else None

        items_payload = data.get("items", [])
        if not items_payload or not isinstance(items_payload, list):
            raise ValidationError({"error_code": "ERR_EMPTY_RETURN", "detail": "Purchase return must contain at least one line item."})

        # Return reason & disposition
        reason = str(data.get("return_reason", PurchaseReturnReason.DAMAGED_DELIVERY)).upper()
        if reason not in PurchaseReturnReason.values:
            reason = PurchaseReturnReason.DAMAGED_DELIVERY

        disposition = str(data.get("disposition", StockDisposition.RETURNED_TO_VENDOR)).upper()
        if disposition not in StockDisposition.values:
            disposition = StockDisposition.RETURNED_TO_VENDOR

        grn = None
        if data.get("goods_receipt_id"):
            grn = GoodsReceiptNote.objects.filter(grn_id=data["goods_receipt_id"], tenant_id=tenant_id).first()

        po = None
        if data.get("purchase_order_id"):
            po = PurchaseOrder.objects.filter(po_id=data["purchase_order_id"], tenant_id=tenant_id).first()

        # Validate line items against batch stock & cost lots
        validated_lines: List[Dict[str, Any]] = []
        calc_total_qty = 0
        calc_total_debit = Decimal("0.0000")

        for itm in items_payload:
            batch_stock_id = itm.get("batch_stock_id")
            batch_stock = None
            if batch_stock_id:
                batch_stock = BatchStock.objects.select_for_update().filter(
                    batch_id=batch_stock_id,
                    tenant_id=tenant_id,
                ).first()

            prod_id = str(itm.get("product_id", "")).strip()
            product = None
            if prod_id:
                product = MedicineProduct.objects.filter(product_id=prod_id).first()
            elif batch_stock:
                product = batch_stock.product

            if not product:
                raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product {prod_id} not found."})

            qty = int(itm.get("return_quantity", 0))
            if qty <= 0:
                raise ValidationError({"error_code": "ERR_INVALID_RETURN_QUANTITY", "detail": "Return quantity must be positive."})

            if not batch_stock:
                batch_no = str(itm.get("batch_number", "")).strip()
                if batch_no:
                    batch_stock = BatchStock.objects.select_for_update().filter(
                        tenant_id=tenant_id,
                        branch=branch,
                        product=product,
                        batch_number=batch_no,
                    ).first()

            if not batch_stock:
                # Try finding any batch for product in branch
                batch_stock = BatchStock.objects.select_for_update().filter(
                    tenant_id=tenant_id,
                    branch=branch,
                    product=product,
                ).first()

            if not batch_stock:
                raise NotFound({"error_code": "ERR_BATCH_NOT_FOUND", "detail": "Batch not found for return."})

            # Guard: ensure enough stock is physically in inventory and unreserved
            if batch_stock.available_quantity_base_units < qty:
                raise ValidationError(
                    {
                        "error_code": "ERR_INSUFFICIENT_STOCK_FOR_RETURN",
                        "detail": f"Cannot return {qty} units of batch {batch_stock.batch_number}; only {batch_stock.available_quantity_base_units} units available (dispensed or reserved).",
                    }
                )

            # Match Cost Lot
            cost_lot = None
            lot_id = itm.get("cost_lot_id")
            if lot_id:
                cost_lot = PurchaseCostLot.objects.select_for_update().filter(lot_id=lot_id, tenant_id=tenant_id).first()
            if not cost_lot:
                cost_lot = PurchaseCostLot.objects.select_for_update().filter(
                    tenant_id=tenant_id,
                    branch=branch,
                    product=product,
                    batch=batch_stock,
                ).first()

            unit_cost = Decimal(str(itm.get("unit_cost") or (cost_lot.unit_cost if cost_lot else batch_stock.cost_per_unit) or "0.0000"))
            if unit_cost <= Decimal("0.0000"):
                unit_cost = Decimal("1.0000")

            line_debit = (unit_cost * Decimal(str(qty))).quantize(Decimal("0.0001"))
            calc_total_qty += qty
            calc_total_debit += line_debit

            validated_lines.append(
                {
                    "product": product,
                    "batch_stock": batch_stock,
                    "cost_lot": cost_lot,
                    "batch_number": batch_stock.batch_number,
                    "return_quantity": qty,
                    "unit_cost": unit_cost,
                    "line_debit_amount": line_debit,
                    "reason": itm.get("reason", reason),
                    "notes": str(itm.get("notes", "")).strip(),
                }
            )

        now = timezone.now()
        ret_id = f"pret-{uuid.uuid4().hex[:12]}"
        ret_num = f"PR-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}"
        debit_ref = data.get("debit_note_reference") or f"DN-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}"

        with transaction.atomic():
            pret = PurchaseReturn.objects.create(
                return_id=ret_id,
                return_number=ret_num,
                tenant=tenant,
                branch=branch,
                supplier=supplier,
                supplier_name=supplier.name,
                goods_receipt=grn,
                purchase_order=po,
                status=PurchaseReturnStatus.DRAFT,
                return_reason=reason,
                disposition=disposition,
                reason_notes=str(data.get("reason_notes", "")).strip(),
                total_return_quantity=calc_total_qty,
                total_debit_amount=calc_total_debit,
                debit_note_reference=debit_ref,
                vendor_credit_note_ref=str(data.get("vendor_credit_note_ref", "")).strip(),
                carrier_pickup_reference=str(data.get("carrier_pickup_reference", "")).strip(),
                created_by=actor_user if hasattr(actor_user, "user_id") else None,
                is_active=True,
            )

            for line in validated_lines:
                PurchaseReturnItem.objects.create(
                    item_id=f"pri-{uuid.uuid4().hex[:12]}",
                    purchase_return=pret,
                    tenant=tenant,
                    product=line["product"],
                    batch_stock=line["batch_stock"],
                    cost_lot=line["cost_lot"],
                    batch_number=line["batch_number"],
                    return_quantity=line["return_quantity"],
                    unit_cost=line["unit_cost"],
                    line_debit_amount=line["line_debit_amount"],
                    reason=line["reason"],
                    notes=line["notes"],
                )

        if auto_post:
            return cls.post_purchase_return(tenant_id, actor_user, pret.return_id)

        return cls.serialize_purchase_return(pret)

    @classmethod
    def post_purchase_return(
        cls,
        tenant_id: str,
        actor_user: Any,
        return_id: str,
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            pret = PurchaseReturn.objects.select_for_update().get(return_id=return_id, tenant_id=tenant_id)
        except PurchaseReturn.DoesNotExist:
            raise NotFound({"error_code": "ERR_RETURN_NOT_FOUND", "detail": f"Purchase return {return_id} not found."})

        if pret.status == PurchaseReturnStatus.POSTED_DEBIT_NOTE:
            raise ValidationError({"error_code": "ERR_ALREADY_POSTED", "detail": "Purchase return is already posted as a debit note."})

        if pret.status == PurchaseReturnStatus.CANCELLED:
            raise ValidationError({"error_code": "ERR_RETURN_CANCELLED", "detail": "Cannot post a cancelled purchase return."})

        now = timezone.now()
        with transaction.atomic():
            # 1. Decrement inventory from BatchStock and PurchaseCostLot
            for item in pret.items.all().select_related("batch_stock", "cost_lot"):
                batch = item.batch_stock
                if batch:
                    if batch.available_quantity_base_units < item.return_quantity:
                        raise ValidationError(
                            {
                                "error_code": "ERR_INSUFFICIENT_STOCK_FOR_RETURN",
                                "detail": f"Stock depleted before posting return. Only {batch.available_quantity_base_units} units available.",
                            }
                        )
                    batch.quantity_base_units -= item.return_quantity
                    if batch.quantity_base_units <= 0:
                        batch.quantity_base_units = 0
                        batch.status = BatchStatus.DEPLETED
                    batch.save(update_fields=["quantity_base_units", "status", "updated_at"])

                cost_lot = item.cost_lot
                if cost_lot:
                    cost_lot.remaining_quantity = max(0, cost_lot.remaining_quantity - item.return_quantity)
                    if cost_lot.remaining_quantity == 0:
                        cost_lot.status = PurchaseCostLotStatus.DEPLETED
                    cost_lot.save(update_fields=["remaining_quantity", "status", "updated_at"])

            # 2. Post Debit Note to SupplierLedgerEntry (Accounts Payable reduction)
            last_entry = (
                SupplierLedgerEntry.objects.filter(tenant=tenant, supplier=pret.supplier)
                .order_by("-posted_at", "-posting_id")
                .first()
            )
            prev_balance = last_entry.balance_after if last_entry else Decimal("0.0000")
            new_balance = max(Decimal("0.0000"), prev_balance - pret.total_debit_amount)

            posting_id = f"sup-post-{uuid.uuid4().hex[:12]}"
            ledger_entry = SupplierLedgerEntry.objects.create(
                posting_id=posting_id,
                tenant=tenant,
                branch=pret.branch,
                supplier=pret.supplier,
                posting_type=SupplierPostingType.RETURN_DEBIT_NOTE,
                reference_number=pret.debit_note_reference or pret.return_number,
                debit_amount=pret.total_debit_amount,
                credit_amount=Decimal("0.0000"),
                balance_after=new_balance,
                currency="BDT",
                notes=f"Purchase Return Debit Note: {pret.return_number} (Reason: {pret.return_reason})",
                posted_by=actor_user if hasattr(actor_user, "user_id") else None,
            )

            pret.status = PurchaseReturnStatus.POSTED_DEBIT_NOTE
            pret.debit_ledger_entry = ledger_entry
            pret.posted_at = now
            pret.approved_by = actor_user if hasattr(actor_user, "user_id") else None
            pret.save(update_fields=["status", "debit_ledger_entry", "posted_at", "approved_by", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_RETURN_DEBIT_NOTE_POSTED",
            resource_type="PurchaseReturn",
            resource_id=pret.return_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "return_id": pret.return_id,
                "return_number": pret.return_number,
                "debit_amount": str(pret.total_debit_amount),
                "posting_id": ledger_entry.posting_id,
            },
        )

        return cls.serialize_purchase_return(pret)

    @classmethod
    def cancel_purchase_return(
        cls,
        tenant_id: str,
        actor_user: Any,
        return_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            pret = PurchaseReturn.objects.select_for_update().get(return_id=return_id, tenant_id=tenant_id)
        except PurchaseReturn.DoesNotExist:
            raise NotFound({"error_code": "ERR_RETURN_NOT_FOUND", "detail": f"Purchase return {return_id} not found."})

        if pret.status != PurchaseReturnStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_CANCEL_POSTED_RETURN",
                    "detail": f"Cannot cancel return in {pret.status} status. Only DRAFT returns can be cancelled.",
                }
            )

        pret.status = PurchaseReturnStatus.CANCELLED
        pret.reason_notes = f"{pret.reason_notes} | Cancelled: {reason}".strip(" |")
        pret.save(update_fields=["status", "reason_notes", "updated_at"])
        return cls.serialize_purchase_return(pret)

    @classmethod
    def get_purchase_return(cls, tenant_id: str, return_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        try:
            pret = PurchaseReturn.objects.select_related(
                "supplier", "goods_receipt", "purchase_order", "created_by", "approved_by", "debit_ledger_entry"
            ).get(return_id=return_id, tenant_id=tenant_id)
        except PurchaseReturn.DoesNotExist:
            raise NotFound({"error_code": "ERR_RETURN_NOT_FOUND", "detail": f"Purchase return {return_id} not found."})

        return cls.serialize_purchase_return(pret)

    @classmethod
    def list_purchase_returns(
        cls,
        tenant_id: str,
        supplier_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = PurchaseReturn.objects.filter(tenant_id=tenant_id).select_related(
            "supplier", "goods_receipt", "purchase_order", "created_by", "approved_by", "debit_ledger_entry"
        )

        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        return [cls.serialize_purchase_return(r) for r in qs]
