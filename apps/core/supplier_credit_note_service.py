"""
backend/apps/core/supplier_credit_note_service.py
Architecture Decision: ADR-0192 (P192).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer for Supplier Credit Notes:
- Records supplier credit notes as immutable finance events with traceable origin (Invoice, Return, GRN, PO).
- Implements non-destructive corrections via explicit audited reversals (never destructive mutation/deletion).
- Offsets and decrements Accounts Payable balance in SupplierLedgerEntry upon posting.
- Re-credits Accounts Payable balance upon audited reversal.
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
    CreditNoteOriginType,
    GoodsReceiptNote,
    MedicineProduct,
    PurchaseOrder,
    PurchaseReturn,
    Supplier,
    SupplierCreditNote,
    SupplierCreditNoteItem,
    SupplierCreditNoteReason,
    SupplierCreditNoteStatus,
    SupplierInvoice,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)


class SupplierCreditNoteService:

    _ALLOWED_PROCUREMENT_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "AUDITOR",
    }

    _ALLOWED_REVERSAL_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "AUDITOR",
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
                    "detail": "Staff role does not have permission to manage supplier credit notes.",
                }
            )

    @classmethod
    def _assert_reversal_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str not in cls._ALLOWED_REVERSAL_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Managerial or Auditor role required to reverse a posted supplier credit note.",
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
                    "detail": "Tenant account subscription is suspended. Credit note processing locked.",
                }
            )
        return tenant

    @classmethod
    def serialize_item(cls, item: SupplierCreditNoteItem) -> Dict[str, Any]:
        return {
            "item_id": item.item_id,
            "product_id": item.product_id,
            "product_name": item.product_name,
            "quantity": item.quantity,
            "unit_credit_rate": str(item.unit_credit_rate),
            "tax_percentage": str(item.tax_percentage),
            "line_total": str(item.line_total),
            "description": item.description,
        }

    @classmethod
    def serialize_credit_note(cls, scn: SupplierCreditNote) -> Dict[str, Any]:
        return {
            "note_id": scn.note_id,
            "note_number": scn.note_number,
            "supplier_credit_note_ref": scn.supplier_credit_note_ref,
            "tenant_id": scn.tenant_id,
            "branch_id": scn.branch_id,
            "supplier_id": scn.supplier_id,
            "supplier_name": scn.supplier_name or (scn.supplier.name if scn.supplier else ""),
            "status": scn.status,
            "reason": scn.reason,
            "origin_type": scn.origin_type,
            "supplier_invoice_id": scn.supplier_invoice_id,
            "purchase_return_id": scn.purchase_return_id,
            "goods_receipt_id": scn.goods_receipt_id,
            "purchase_order_id": scn.purchase_order_id,
            "origin_reference": scn.origin_reference,
            "note_date": scn.note_date.isoformat() if scn.note_date else None,
            "subtotal_amount": str(scn.subtotal_amount),
            "tax_amount": str(scn.tax_amount),
            "total_credit_amount": str(scn.total_credit_amount),
            "debit_ledger_entry_id": scn.debit_ledger_entry_id,
            "reversal_ledger_entry_id": scn.reversal_ledger_entry_id,
            "reversal_reason": scn.reversal_reason,
            "reversal_posted_at": scn.reversal_posted_at.isoformat() if scn.reversal_posted_at else None,
            "reversed_by": str(getattr(scn.reversed_by, "user_id", scn.reversed_by_id or "")) if scn.reversed_by_id else None,
            "notes": scn.notes,
            "posted_at": scn.posted_at.isoformat() if scn.posted_at else None,
            "created_by": str(getattr(scn.created_by, "user_id", scn.created_by_id or "")) if scn.created_by_id else None,
            "approved_by": str(getattr(scn.approved_by, "user_id", scn.approved_by_id or "")) if scn.approved_by_id else None,
            "is_active": scn.is_active,
            "items": [cls.serialize_item(i) for i in scn.items.all().select_related("product")],
            "created_at": scn.created_at.isoformat() if scn.created_at else None,
            "updated_at": scn.updated_at.isoformat() if scn.updated_at else None,
        }

    @classmethod
    def create_credit_note(
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

        vendor_ref = str(data.get("supplier_credit_note_ref", "")).strip()
        if not vendor_ref:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_VENDOR_REF",
                    "detail": "Supplier's paper credit note reference number is required.",
                }
            )

        # Guard against duplicate credit note reference from same vendor
        if SupplierCreditNote.objects.filter(
            tenant_id=tenant_id, supplier=supplier, supplier_credit_note_ref__iexact=vendor_ref
        ).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_CREDIT_NOTE_REF",
                    "detail": f"Credit note reference '{vendor_ref}' already recorded for this supplier.",
                }
            )

        branch_id = data.get("branch_id") or getattr(actor_user, "branch_id", None)
        branch = tenant.branches.filter(branch_id=branch_id).first() if branch_id else None

        # Reason & Origin
        reason = str(data.get("reason", SupplierCreditNoteReason.PRICE_DIFFERENCE)).upper()
        if reason not in SupplierCreditNoteReason.values:
            reason = SupplierCreditNoteReason.PRICE_DIFFERENCE

        origin_type = str(data.get("origin_type", CreditNoteOriginType.INVOICE)).upper()
        if origin_type not in CreditNoteOriginType.values:
            origin_type = CreditNoteOriginType.INVOICE

        # Traceable origin linkages
        invoice = None
        inv_id = data.get("supplier_invoice_id")
        if inv_id:
            invoice = SupplierInvoice.objects.filter(invoice_id=inv_id, tenant_id=tenant_id).first()

        pret = None
        pret_id = data.get("purchase_return_id")
        if pret_id:
            pret = PurchaseReturn.objects.filter(return_id=pret_id, tenant_id=tenant_id).first()

        grn = None
        grn_id = data.get("goods_receipt_id")
        if grn_id:
            grn = GoodsReceiptNote.objects.filter(grn_id=grn_id, tenant_id=tenant_id).first()

        po = None
        po_id = data.get("purchase_order_id")
        if po_id:
            po = PurchaseOrder.objects.filter(po_id=po_id, tenant_id=tenant_id).first()

        origin_ref = str(data.get("origin_reference", "")).strip()
        if not origin_ref:
            if invoice:
                origin_ref = invoice.invoice_number
            elif pret:
                origin_ref = pret.return_number
            elif grn:
                origin_ref = grn.grn_number
            elif po:
                origin_ref = po.po_number
            else:
                origin_ref = vendor_ref

        # Line items & Financial totals
        items_payload = data.get("items", [])
        calc_subtotal = Decimal("0.0000")
        calc_tax = Decimal("0.0000")
        calc_total = Decimal("0.0000")
        validated_items: List[Dict[str, Any]] = []

        if items_payload:
            for itm in items_payload:
                prod_id = str(itm.get("product_id", "")).strip()
                product = MedicineProduct.objects.filter(product_id=prod_id).first() if prod_id else None
                prod_name = str(itm.get("product_name") or (product.brand_name if product else "Commercial Adjustment")).strip()

                qty = int(itm.get("quantity", 0))
                rate = Decimal(str(itm.get("unit_credit_rate", "0.0000")))
                tax_pct = Decimal(str(itm.get("tax_percentage", "0.00")))

                if qty > 0 and rate > Decimal("0.0000"):
                    line_sub = (rate * Decimal(str(qty))).quantize(Decimal("0.0001"))
                else:
                    line_sub = Decimal(str(itm.get("line_total", "0.0000"))).quantize(Decimal("0.0001"))

                line_tax = (line_sub * tax_pct / Decimal("100.00")).quantize(Decimal("0.0001"))
                line_tot = line_sub + line_tax

                calc_subtotal += line_sub
                calc_tax += line_tax
                calc_total += line_tot

                validated_items.append(
                    {
                        "product": product,
                        "product_name": prod_name,
                        "quantity": qty,
                        "unit_credit_rate": rate,
                        "tax_percentage": tax_pct,
                        "line_total": line_tot,
                        "description": str(itm.get("description", "")).strip(),
                    }
                )
        else:
            # Flat credit note without SKU lines
            raw_total = Decimal(str(data.get("total_credit_amount", "0.0000"))).quantize(Decimal("0.0001"))
            if raw_total <= Decimal("0.0000"):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_CREDIT_AMOUNT",
                        "detail": "Credit note must specify either line items or a positive total credit amount.",
                    }
                )
            calc_subtotal = raw_total
            calc_total = raw_total

        now = timezone.now()
        note_id = f"scn-{uuid.uuid4().hex[:12]}"
        note_num = f"SCN-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}"

        with transaction.atomic():
            scn = SupplierCreditNote.objects.create(
                note_id=note_id,
                note_number=note_num,
                supplier_credit_note_ref=vendor_ref,
                tenant=tenant,
                branch=branch,
                supplier=supplier,
                supplier_name=supplier.name,
                status=SupplierCreditNoteStatus.DRAFT,
                reason=reason,
                origin_type=origin_type,
                supplier_invoice=invoice,
                purchase_return=pret,
                goods_receipt=grn,
                purchase_order=po,
                origin_reference=origin_ref,
                note_date=data.get("note_date") or now.date(),
                subtotal_amount=calc_subtotal,
                tax_amount=calc_tax,
                total_credit_amount=calc_total,
                notes=str(data.get("notes", "")).strip(),
                created_by=actor_user if hasattr(actor_user, "user_id") else None,
                is_active=True,
            )

            for line in validated_items:
                SupplierCreditNoteItem.objects.create(
                    item_id=f"scni-{uuid.uuid4().hex[:12]}",
                    credit_note=scn,
                    tenant=tenant,
                    product=line["product"],
                    product_name=line["product_name"],
                    quantity=line["quantity"],
                    unit_credit_rate=line["unit_credit_rate"],
                    tax_percentage=line["tax_percentage"],
                    line_total=line["line_total"],
                    description=line["description"],
                )

        if auto_post:
            return cls.post_credit_note(tenant_id, actor_user, scn.note_id)

        return cls.serialize_credit_note(scn)

    @classmethod
    def post_credit_note(
        cls,
        tenant_id: str,
        actor_user: Any,
        note_id: str,
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            scn = SupplierCreditNote.objects.select_for_update().get(note_id=note_id, tenant_id=tenant_id)
        except SupplierCreditNote.DoesNotExist:
            raise NotFound({"error_code": "ERR_CREDIT_NOTE_NOT_FOUND", "detail": f"Credit note {note_id} not found."})

        if scn.status == SupplierCreditNoteStatus.POSTED:
            raise ValidationError(
                {"error_code": "ERR_ALREADY_POSTED", "detail": "Credit note has already been posted to AP."}
            )

        if scn.status in (SupplierCreditNoteStatus.CANCELLED, SupplierCreditNoteStatus.REVERSED):
            raise ValidationError(
                {"error_code": "ERR_INVALID_STATUS", "detail": f"Cannot post credit note in {scn.status} status."}
            )

        now = timezone.now()
        with transaction.atomic():
            # In AP accounting, a supplier credit note reduces accounts payable liability (Debit entry)
            last_entry = (
                SupplierLedgerEntry.objects.filter(tenant=tenant, supplier=scn.supplier)
                .order_by("-posted_at", "-posting_id")
                .first()
            )
            prev_balance = last_entry.balance_after if last_entry else Decimal("0.0000")
            new_balance = max(Decimal("0.0000"), prev_balance - scn.total_credit_amount)

            posting_id = f"sup-post-{uuid.uuid4().hex[:12]}"
            ledger_entry = SupplierLedgerEntry.objects.create(
                posting_id=posting_id,
                tenant=tenant,
                branch=scn.branch,
                supplier=scn.supplier,
                posting_type=SupplierPostingType.SUPPLIER_CREDIT_NOTE,
                reference_number=scn.supplier_credit_note_ref or scn.note_number,
                debit_amount=scn.total_credit_amount,
                credit_amount=Decimal("0.0000"),
                balance_after=new_balance,
                currency="BDT",
                notes=f"Supplier Credit Note: {scn.note_number} (Ref: {scn.supplier_credit_note_ref}, Reason: {scn.reason})",
                posted_by=actor_user if hasattr(actor_user, "user_id") else None,
            )

            scn.status = SupplierCreditNoteStatus.POSTED
            scn.debit_ledger_entry = ledger_entry
            scn.posted_at = now
            scn.approved_by = actor_user if hasattr(actor_user, "user_id") else None
            scn.save(update_fields=["status", "debit_ledger_entry", "posted_at", "approved_by", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_CREDIT_NOTE_POSTED",
            resource_type="SupplierCreditNote",
            resource_id=scn.note_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "note_id": scn.note_id,
                "note_number": scn.note_number,
                "credit_amount": str(scn.total_credit_amount),
                "posting_id": ledger_entry.posting_id,
            },
        )

        return cls.serialize_credit_note(scn)

    @classmethod
    def reverse_credit_note(
        cls,
        tenant_id: str,
        actor_user: Any,
        note_id: str,
        reversal_reason: str,
    ) -> Dict[str, Any]:
        """
        Non-destructive correction: reverses an existing posted credit note via an audited reversal entry.
        Re-credits Accounts Payable liability back to the supplier.
        """
        cls._assert_reversal_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        reason_str = str(reversal_reason or "").strip()
        if not reason_str:
            raise ValidationError(
                {"error_code": "ERR_REVERSAL_REASON_REQUIRED", "detail": "Reversal reason is mandatory for financial audit."}
            )

        try:
            scn = SupplierCreditNote.objects.select_for_update().get(note_id=note_id, tenant_id=tenant_id)
        except SupplierCreditNote.DoesNotExist:
            raise NotFound({"error_code": "ERR_CREDIT_NOTE_NOT_FOUND", "detail": f"Credit note {note_id} not found."})

        if scn.status == SupplierCreditNoteStatus.REVERSED:
            raise ValidationError(
                {"error_code": "ERR_ALREADY_REVERSED", "detail": "This credit note has already been reversed."}
            )

        if scn.status != SupplierCreditNoteStatus.POSTED:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_REVERSE_UNPOSTED",
                    "detail": f"Cannot reverse credit note in {scn.status} status. Only POSTED credit notes can be reversed.",
                }
            )

        now = timezone.now()
        with transaction.atomic():
            # In AP accounting, reversing a credit note restores the liability (Credit entry)
            last_entry = (
                SupplierLedgerEntry.objects.filter(tenant=tenant, supplier=scn.supplier)
                .order_by("-posted_at", "-posting_id")
                .first()
            )
            prev_balance = last_entry.balance_after if last_entry else Decimal("0.0000")
            new_balance = prev_balance + scn.total_credit_amount

            reversal_posting_id = f"sup-rev-{uuid.uuid4().hex[:12]}"
            reversal_entry = SupplierLedgerEntry.objects.create(
                posting_id=reversal_posting_id,
                tenant=tenant,
                branch=scn.branch,
                supplier=scn.supplier,
                posting_type=SupplierPostingType.CREDIT_NOTE_REVERSAL,
                reference_number=f"REV-{scn.note_number}",
                debit_amount=Decimal("0.0000"),
                credit_amount=scn.total_credit_amount,
                balance_after=new_balance,
                currency="BDT",
                notes=f"Reversal of SCN {scn.note_number} (Ref: {scn.supplier_credit_note_ref}). Reason: {reason_str}",
                posted_by=actor_user if hasattr(actor_user, "user_id") else None,
            )

            scn.status = SupplierCreditNoteStatus.REVERSED
            scn.reversal_ledger_entry = reversal_entry
            scn.reversal_reason = reason_str
            scn.reversal_posted_at = now
            scn.reversed_by = actor_user if hasattr(actor_user, "user_id") else None
            scn.save(
                update_fields=[
                    "status",
                    "reversal_ledger_entry",
                    "reversal_reason",
                    "reversal_posted_at",
                    "reversed_by",
                    "updated_at",
                ]
            )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_CREDIT_NOTE_REVERSED",
            resource_type="SupplierCreditNote",
            resource_id=scn.note_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "note_id": scn.note_id,
                "note_number": scn.note_number,
                "reversal_reason": reason_str,
                "reversal_posting_id": reversal_entry.posting_id,
            },
        )

        return cls.serialize_credit_note(scn)

    @classmethod
    def cancel_credit_note(
        cls,
        tenant_id: str,
        actor_user: Any,
        note_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            scn = SupplierCreditNote.objects.select_for_update().get(note_id=note_id, tenant_id=tenant_id)
        except SupplierCreditNote.DoesNotExist:
            raise NotFound({"error_code": "ERR_CREDIT_NOTE_NOT_FOUND", "detail": f"Credit note {note_id} not found."})

        if scn.status != SupplierCreditNoteStatus.DRAFT:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_CANCEL_POSTED_CREDIT_NOTE",
                    "detail": f"Cannot cancel credit note in {scn.status} status. Use audited reversal for posted records.",
                }
            )

        scn.status = SupplierCreditNoteStatus.CANCELLED
        scn.notes = f"{scn.notes} | Cancelled: {reason}".strip(" |")
        scn.save(update_fields=["status", "notes", "updated_at"])
        return cls.serialize_credit_note(scn)

    @classmethod
    def get_credit_note(cls, tenant_id: str, note_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        try:
            scn = SupplierCreditNote.objects.select_related(
                "supplier",
                "supplier_invoice",
                "purchase_return",
                "goods_receipt",
                "purchase_order",
                "created_by",
                "approved_by",
                "reversed_by",
                "debit_ledger_entry",
                "reversal_ledger_entry",
            ).get(note_id=note_id, tenant_id=tenant_id)
        except SupplierCreditNote.DoesNotExist:
            raise NotFound({"error_code": "ERR_CREDIT_NOTE_NOT_FOUND", "detail": f"Credit note {note_id} not found."})

        return cls.serialize_credit_note(scn)

    @classmethod
    def list_credit_notes(
        cls,
        tenant_id: str,
        supplier_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        origin_type: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = SupplierCreditNote.objects.filter(tenant_id=tenant_id).select_related(
            "supplier",
            "supplier_invoice",
            "purchase_return",
            "goods_receipt",
            "purchase_order",
            "created_by",
            "approved_by",
            "reversed_by",
            "debit_ledger_entry",
            "reversal_ledger_entry",
        )

        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        if origin_type:
            qs = qs.filter(origin_type=origin_type.upper())
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        return [cls.serialize_credit_note(r) for r in qs]

