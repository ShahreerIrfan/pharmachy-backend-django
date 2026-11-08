"""
backend/apps/core/supplier_invoice_service.py
Architecture Decision: ADR-0190 (P190).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer for 3-Way Supplier Invoice Matching:
- Reconciles Purchase Orders (commercial agreement) <-> Goods Receipt Notes (physical custody) <-> Supplier Invoices (billing).
- Tolerates minor price and rounding variances (default 1.5% or ৳50 BDT).
- Routes physical over-billing, price spikes, and unmatched items to the Discrepancy Exception Queue.
- Enforces managerial override with documented justification for approved exceptions.
- Posts matched/approved invoices to the immutable Supplier Accounts Payable ledger (SupplierLedgerEntry).
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
    GoodsReceiptLine,
    GoodsReceiptNote,
    InvoiceMatchingStatus,
    ItemMatchStatus,
    MedicineProduct,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceItem,
    SupplierLedgerEntry,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)


class SupplierInvoiceService:

    _ALLOWED_PROCUREMENT_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "AUDITOR",
    }

    _ALLOWED_EXCEPTION_APPROVER_ROLES = {
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
                    "detail": "Staff role does not have authorization for supplier invoice operations.",
                }
            )

    @classmethod
    def _assert_exception_approver_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str not in cls._ALLOWED_EXCEPTION_APPROVER_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Only Branch Managers, Owners, and Auditors can approve invoice discrepancy exceptions.",
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
                    "detail": "Tenant account is suspended. Supplier invoice processing is locked.",
                }
            )
        return tenant

    @classmethod
    def serialize_invoice_item(cls, item: SupplierInvoiceItem) -> Dict[str, Any]:
        return {
            "item_id": item.item_id,
            "product_id": item.product_id,
            "product_brand_name": item.product.brand_name if item.product else "",
            "po_item_id": item.po_item_id,
            "grn_line_id": item.grn_line_id,
            "batch_number": item.batch_number,
            "billed_quantity": item.billed_quantity,
            "received_quantity": item.received_quantity,
            "ordered_quantity": item.ordered_quantity,
            "billed_unit_price": str(item.billed_unit_price),
            "po_unit_price": str(item.po_unit_price),
            "line_subtotal": str(item.line_subtotal),
            "line_tax_percentage": str(item.line_tax_percentage),
            "line_discount_percentage": str(item.line_discount_percentage),
            "line_total": str(item.line_total),
            "match_status": item.match_status,
            "price_variance": str(item.price_variance),
            "quantity_variance": item.quantity_variance,
            "notes": item.notes,
        }

    @classmethod
    def serialize_supplier_invoice(cls, invoice: SupplierInvoice) -> Dict[str, Any]:
        return {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "tenant_id": invoice.tenant_id,
            "branch_id": invoice.branch_id,
            "supplier_id": invoice.supplier_id,
            "supplier_name": invoice.supplier_name or (invoice.supplier.name if invoice.supplier else ""),
            "purchase_order_id": invoice.purchase_order_id,
            "purchase_order_number": invoice.purchase_order_number,
            "goods_receipt_id": invoice.goods_receipt_id,
            "goods_receipt_number": invoice.goods_receipt_number,
            "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "currency": invoice.currency,
            "subtotal_amount": str(invoice.subtotal_amount),
            "tax_amount": str(invoice.tax_amount),
            "discount_amount": str(invoice.discount_amount),
            "total_amount": str(invoice.total_amount),
            "matching_status": invoice.matching_status,
            "has_discrepancy": invoice.has_discrepancy,
            "discrepancy_reasons": invoice.discrepancy_reasons or [],
            "price_variance_amount": str(invoice.price_variance_amount),
            "quantity_variance_units": invoice.quantity_variance_units,
            "total_variance_amount": str(invoice.total_variance_amount),
            "tolerated_price_variance_percentage": str(invoice.tolerated_price_variance_percentage),
            "tolerated_amount_variance_bdt": str(invoice.tolerated_amount_variance_bdt),
            "exception_approved_by": str(getattr(invoice.exception_approved_by, "user_id", invoice.exception_approved_by_id or "")) if invoice.exception_approved_by_id else None,
            "exception_approved_at": invoice.exception_approved_at.isoformat() if invoice.exception_approved_at else None,
            "exception_justification": invoice.exception_justification,
            "rejection_reason": invoice.rejection_reason,
            "rejected_by": str(getattr(invoice.rejected_by, "user_id", invoice.rejected_by_id or "")) if invoice.rejected_by_id else None,
            "rejected_at": invoice.rejected_at.isoformat() if invoice.rejected_at else None,
            "posted_to_ap_at": invoice.posted_to_ap_at.isoformat() if invoice.posted_to_ap_at else None,
            "ap_ledger_entry_id": invoice.ap_ledger_entry_id,
            "proof_attachment_reference": invoice.proof_attachment_reference,
            "notes": invoice.notes,
            "is_active": invoice.is_active,
            "items": [cls.serialize_invoice_item(item) for item in invoice.items.all().select_related("product", "po_item", "grn_line")],
            "created_at": invoice.created_at.isoformat() if invoice.created_at else None,
            "updated_at": invoice.updated_at.isoformat() if invoice.updated_at else None,
        }

    @classmethod
    def create_supplier_invoice(
        cls,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
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

        invoice_number = str(data.get("invoice_number", "")).strip()
        if not invoice_number:
            raise ValidationError({"error_code": "ERR_MISSING_INVOICE_NUMBER", "detail": "Supplier invoice number is required."})

        # Anti-duplicate invoice guard
        if SupplierInvoice.objects.filter(tenant_id=tenant_id, supplier=supplier, invoice_number=invoice_number).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_INVOICE",
                    "detail": f"Invoice number {invoice_number} already exists for supplier {supplier.name}.",
                }
            )

        invoice_date_raw = data.get("invoice_date")
        if not invoice_date_raw:
            raise ValidationError({"error_code": "ERR_MISSING_INVOICE_DATE", "detail": "Invoice date is required."})

        # Branch resolution
        branch_id = data.get("branch_id") or getattr(actor_user, "branch_id", None)
        branch = None
        if branch_id:
            branch = tenant.branches.filter(branch_id=branch_id).first()

        # Link PO if provided
        po = None
        po_id = data.get("purchase_order_id")
        if po_id:
            po = PurchaseOrder.objects.filter(po_id=po_id, tenant_id=tenant_id).first()
            if not po:
                raise NotFound({"error_code": "ERR_PO_NOT_FOUND", "detail": f"Purchase order {po_id} not found for this tenant."})

        # Link GRN if provided
        grn = None
        grn_id = data.get("goods_receipt_id")
        if grn_id:
            grn = GoodsReceiptNote.objects.filter(grn_id=grn_id, tenant_id=tenant_id).first()
            if not grn:
                raise NotFound({"error_code": "ERR_GRN_NOT_FOUND", "detail": f"Goods receipt {grn_id} not found for this tenant."})

        items_payload = data.get("items", [])
        if not items_payload or not isinstance(items_payload, list):
            raise ValidationError({"error_code": "ERR_EMPTY_INVOICE", "detail": "Invoice must contain at least one line item."})

        tolerated_price_pct = Decimal(str(data.get("tolerated_price_variance_percentage", "1.50")))
        tolerated_amount_bdt = Decimal(str(data.get("tolerated_amount_variance_bdt", "50.0000")))

        # Validate lines and products
        validated_items: List[Dict[str, Any]] = []
        calc_subtotal = Decimal("0.0000")
        calc_tax = Decimal("0.0000")
        calc_discount = Decimal("0.0000")
        calc_total = Decimal("0.0000")

        for item_data in items_payload:
            prod_id = str(item_data.get("product_id", "")).strip()
            if not prod_id:
                raise ValidationError({"error_code": "ERR_MISSING_PRODUCT", "detail": "Product ID is required on each line."})
            product = MedicineProduct.objects.filter(product_id=prod_id).first()
            if not product:
                raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product {prod_id} not found."})

            billed_qty = int(item_data.get("billed_quantity", 0))
            if billed_qty <= 0:
                raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Billed quantity must be positive."})

            billed_unit_price = Decimal(str(item_data.get("billed_unit_price", "0.0000")))
            if billed_unit_price <= Decimal("0.0000"):
                raise ValidationError({"error_code": "ERR_INVALID_UNIT_PRICE", "detail": "Billed unit price must be positive."})

            tax_pct = Decimal(str(item_data.get("line_tax_percentage", "0.00")))
            disc_pct = Decimal(str(item_data.get("line_discount_percentage", "0.00")))

            disc_factor = Decimal("1.0000") - (disc_pct / Decimal("100.00"))
            net_unit_price = (billed_unit_price * disc_factor).quantize(Decimal("0.0001"))
            line_subtotal = (net_unit_price * Decimal(str(billed_qty))).quantize(Decimal("0.0001"))
            line_tax_amount = (line_subtotal * (tax_pct / Decimal("100.00"))).quantize(Decimal("0.0001"))
            line_total = (line_subtotal + line_tax_amount).quantize(Decimal("0.0001"))

            po_item = None
            po_unit_price = Decimal("0.0000")
            ordered_qty = 0
            po_item_id = item_data.get("po_item_id")
            if po_item_id:
                po_item = PurchaseOrderItem.objects.filter(item_id=po_item_id).first()
                if po_item:
                    po_unit_price = po_item.unit_cost_price
                    ordered_qty = po_item.ordered_quantity
            elif po:
                po_item = po.items.filter(product=product).first()
                if po_item:
                    po_unit_price = po_item.unit_cost_price
                    ordered_qty = po_item.ordered_quantity

            grn_line = None
            received_qty = 0
            batch_num = str(item_data.get("batch_number", "")).strip()
            grn_line_id = item_data.get("grn_line_id")
            if grn_line_id:
                grn_line = GoodsReceiptLine.objects.filter(line_id=grn_line_id).first()
                if grn_line:
                    received_qty = grn_line.received_quantity_base_units
                    if not batch_num:
                        batch_num = grn_line.batch_number
            elif grn:
                grn_line = grn.lines.filter(product=product).first()
                if grn_line:
                    received_qty = grn_line.received_quantity_base_units
                    if not batch_num:
                        batch_num = grn_line.batch_number

            calc_subtotal += (billed_unit_price * Decimal(str(billed_qty))).quantize(Decimal("0.0001"))
            calc_tax += line_tax_amount
            calc_discount += ((billed_unit_price * (disc_pct / Decimal("100.00"))) * Decimal(str(billed_qty))).quantize(Decimal("0.0001"))
            calc_total += line_total

            validated_items.append(
                {
                    "product": product,
                    "po_item": po_item,
                    "grn_line": grn_line,
                    "batch_number": batch_num,
                    "billed_quantity": billed_qty,
                    "received_quantity": received_qty,
                    "ordered_quantity": ordered_qty,
                    "billed_unit_price": billed_unit_price,
                    "po_unit_price": po_unit_price,
                    "line_subtotal": line_subtotal,
                    "line_tax_percentage": tax_pct,
                    "line_discount_percentage": disc_pct,
                    "line_total": line_total,
                    "notes": str(item_data.get("notes", "")).strip(),
                }
            )

        invoice_id = f"si-{uuid.uuid4().hex[:12]}"
        due_date = data.get("due_date")

        with transaction.atomic():
            invoice = SupplierInvoice.objects.create(
                invoice_id=invoice_id,
                invoice_number=invoice_number,
                tenant=tenant,
                branch=branch,
                supplier=supplier,
                supplier_name=supplier.name,
                purchase_order=po,
                purchase_order_number=po.po_number if po else "",
                goods_receipt=grn,
                goods_receipt_number=grn.grn_number if grn else "",
                invoice_date=invoice_date_raw,
                due_date=due_date,
                currency=str(data.get("currency", "BDT")).strip().upper(),
                subtotal_amount=calc_subtotal,
                tax_amount=calc_tax,
                discount_amount=calc_discount,
                total_amount=calc_total,
                matching_status=InvoiceMatchingStatus.DRAFT,
                tolerated_price_variance_percentage=tolerated_price_pct,
                tolerated_amount_variance_bdt=tolerated_amount_bdt,
                proof_attachment_reference=str(data.get("proof_attachment_reference", "")).strip(),
                notes=str(data.get("notes", "")).strip(),
                created_by=actor_user if hasattr(actor_user, "user_id") else None,
                is_active=True,
            )

            for vi in validated_items:
                SupplierInvoiceItem.objects.create(
                    item_id=f"sii-{uuid.uuid4().hex[:12]}",
                    invoice=invoice,
                    tenant=tenant,
                    product=vi["product"],
                    po_item=vi["po_item"],
                    grn_line=vi["grn_line"],
                    batch_number=vi["batch_number"],
                    billed_quantity=vi["billed_quantity"],
                    received_quantity=vi["received_quantity"],
                    ordered_quantity=vi["ordered_quantity"],
                    billed_unit_price=vi["billed_unit_price"],
                    po_unit_price=vi["po_unit_price"],
                    line_subtotal=vi["line_subtotal"],
                    line_tax_percentage=vi["line_tax_percentage"],
                    line_discount_percentage=vi["line_discount_percentage"],
                    line_total=vi["line_total"],
                    match_status=ItemMatchStatus.MATCHED,
                    price_variance=Decimal("0.0000"),
                    quantity_variance=0,
                    notes=vi["notes"],
                )

        # Run automated 3-way match
        return cls.execute_three_way_match(tenant_id, actor_user, invoice.invoice_id)

    @classmethod
    def execute_three_way_match(
        cls,
        tenant_id: str,
        actor_user: Any,
        invoice_id: str,
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            invoice = SupplierInvoice.objects.select_for_update().get(invoice_id=invoice_id, tenant_id=tenant_id)
        except SupplierInvoice.DoesNotExist:
            raise NotFound({"error_code": "ERR_INVOICE_NOT_FOUND", "detail": f"Supplier invoice {invoice_id} not found."})

        if invoice.matching_status in [InvoiceMatchingStatus.POSTED_TO_AP, InvoiceMatchingStatus.REJECTED]:
            raise ValidationError(
                {
                    "error_code": "ERR_INVOICE_IMMUTABLE",
                    "detail": f"Cannot execute 3-way match on invoice in {invoice.matching_status} status.",
                }
            )

        items = list(invoice.items.all().select_related("product", "po_item", "grn_line"))
        discrepancy_reasons: List[str] = []
        total_price_variance = Decimal("0.0000")
        total_quantity_variance = 0
        has_blocking_discrepancy = False

        price_pct_threshold = invoice.tolerated_price_variance_percentage
        amount_threshold = invoice.tolerated_amount_variance_bdt

        with transaction.atomic():
            for item in items:
                item_has_variance = False

                # 1. Price Verification vs Purchase Order
                if item.po_item:
                    po_price = item.po_item.unit_cost_price
                    item.po_unit_price = po_price
                    price_diff = item.billed_unit_price - po_price
                    item.price_variance = price_diff
                    total_price_variance += (price_diff * Decimal(str(item.billed_quantity))).quantize(Decimal("0.0001"))

                    if po_price > Decimal("0.0000") and price_diff > Decimal("0.0000"):
                        pct_diff = ((price_diff / po_price) * Decimal("100.00")).quantize(Decimal("0.01"))
                        if pct_diff > price_pct_threshold and price_diff > Decimal("1.0000"):
                            item.match_status = ItemMatchStatus.PRICE_VARIANCE
                            item_has_variance = True
                            has_blocking_discrepancy = True
                            reason_msg = f"PRICE_VARIANCE: {item.product.brand_name} billed ৳{item.billed_unit_price} exceeds PO price ৳{po_price} by {pct_diff}% (tolerance {price_pct_threshold}%)"
                            if reason_msg not in discrepancy_reasons:
                                discrepancy_reasons.append(reason_msg)

                # 2. Quantity Verification vs Goods Receipt Note
                if item.grn_line:
                    rec_qty = item.grn_line.received_quantity_base_units
                    item.received_quantity = rec_qty
                    qty_diff = item.billed_quantity - rec_qty
                    item.quantity_variance = qty_diff

                    if qty_diff > 0:
                        # Physical over-billing: Vendor is charging for goods that were never received!
                        total_quantity_variance += qty_diff
                        item.match_status = ItemMatchStatus.QUANTITY_OVER_BILLING
                        item_has_variance = True
                        has_blocking_discrepancy = True
                        reason_msg = f"QUANTITY_OVER_BILLING: {item.product.brand_name} billed {item.billed_quantity} units exceeds GRN received {rec_qty} units"
                        if reason_msg not in discrepancy_reasons:
                            discrepancy_reasons.append(reason_msg)
                    elif qty_diff < 0:
                        item.match_status = ItemMatchStatus.QUANTITY_UNDER_BILLING
                        # Under-billing is permitted/tolerated (e.g. partial invoice)
                elif invoice.goods_receipt:
                    # Item billed but missing from GRN delivery!
                    item.match_status = ItemMatchStatus.UNMATCHED_ITEM
                    item_has_variance = True
                    has_blocking_discrepancy = True
                    reason_msg = f"UNMATCHED_ITEM: {item.product.brand_name} billed on invoice but not found in GRN shipment"
                    if reason_msg not in discrepancy_reasons:
                        discrepancy_reasons.append(reason_msg)

                if not item_has_variance:
                    item.match_status = ItemMatchStatus.MATCHED

                item.save(update_fields=["po_unit_price", "received_quantity", "match_status", "price_variance", "quantity_variance"])

            # 3. Overall Amount Variance vs PO/GRN expected total
            if abs(total_price_variance) > amount_threshold and total_price_variance > Decimal("0.0000"):
                has_blocking_discrepancy = True
                reason_msg = f"AMOUNT_VARIANCE: Net invoice variance ৳{total_price_variance} exceeds tolerated threshold ৳{amount_threshold}"
                if reason_msg not in discrepancy_reasons:
                    discrepancy_reasons.append(reason_msg)

            # 4. Route status
            if has_blocking_discrepancy:
                invoice.matching_status = InvoiceMatchingStatus.DISCREPANCY_QUEUE
                invoice.has_discrepancy = True
                invoice.discrepancy_reasons = discrepancy_reasons
            else:
                invoice.matching_status = InvoiceMatchingStatus.MATCHED
                invoice.has_discrepancy = False
                invoice.discrepancy_reasons = []

            invoice.price_variance_amount = total_price_variance
            invoice.quantity_variance_units = total_quantity_variance
            invoice.total_variance_amount = total_price_variance
            invoice.save(
                update_fields=[
                    "matching_status",
                    "has_discrepancy",
                    "discrepancy_reasons",
                    "price_variance_amount",
                    "quantity_variance_units",
                    "total_variance_amount",
                    "updated_at",
                ]
            )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "system@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_INVOICE_THREE_WAY_MATCHED",
            resource_type="SupplierInvoice",
            resource_id=invoice.invoice_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_id": invoice.invoice_id,
                "invoice_number": invoice.invoice_number,
                "matching_status": invoice.matching_status,
                "has_discrepancy": invoice.has_discrepancy,
                "discrepancy_reasons": invoice.discrepancy_reasons,
            },
        )

        return cls.serialize_supplier_invoice(invoice)

    @classmethod
    def approve_exception(
        cls,
        tenant_id: str,
        actor_user: Any,
        invoice_id: str,
        justification: str,
        notes: str = "",
    ) -> Dict[str, Any]:
        cls._assert_exception_approver_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            invoice = SupplierInvoice.objects.select_for_update().get(invoice_id=invoice_id, tenant_id=tenant_id)
        except SupplierInvoice.DoesNotExist:
            raise NotFound({"error_code": "ERR_INVOICE_NOT_FOUND", "detail": f"Supplier invoice {invoice_id} not found."})

        if invoice.matching_status != InvoiceMatchingStatus.DISCREPANCY_QUEUE:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_INVOICE_STATUS",
                    "detail": f"Invoice is not in exception queue (current status: {invoice.matching_status}).",
                }
            )

        just = str(justification).strip()
        if not just:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_JUSTIFICATION",
                    "detail": "Documented managerial justification is required to override invoice discrepancies.",
                }
            )

        now = timezone.now()
        with transaction.atomic():
            invoice.matching_status = InvoiceMatchingStatus.APPROVED_EXCEPTION
            invoice.exception_approved_by = actor_user if hasattr(actor_user, "user_id") else None
            invoice.exception_approved_at = now
            invoice.exception_justification = just if not notes.strip() else f"{just} | {notes.strip()}"
            invoice.save(
                update_fields=[
                    "matching_status",
                    "exception_approved_by",
                    "exception_approved_at",
                    "exception_justification",
                    "updated_at",
                ]
            )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_INVOICE_EXCEPTION_APPROVED",
            resource_type="SupplierInvoice",
            resource_id=invoice.invoice_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_id": invoice.invoice_id,
                "invoice_number": invoice.invoice_number,
                "justification": just,
                "previous_discrepancies": invoice.discrepancy_reasons,
            },
        )

        return cls.serialize_supplier_invoice(invoice)

    @classmethod
    def reject_invoice(
        cls,
        tenant_id: str,
        actor_user: Any,
        invoice_id: str,
        rejection_reason: str,
        notes: str = "",
    ) -> Dict[str, Any]:
        cls._assert_exception_approver_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            invoice = SupplierInvoice.objects.select_for_update().get(invoice_id=invoice_id, tenant_id=tenant_id)
        except SupplierInvoice.DoesNotExist:
            raise NotFound({"error_code": "ERR_INVOICE_NOT_FOUND", "detail": f"Supplier invoice {invoice_id} not found."})

        if invoice.matching_status == InvoiceMatchingStatus.POSTED_TO_AP:
            raise ValidationError(
                {
                    "error_code": "ERR_INVOICE_ALREADY_POSTED",
                    "detail": "Cannot reject an invoice that is already posted to Accounts Payable.",
                }
            )

        reason = str(rejection_reason).strip()
        if not reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REJECTION_REASON",
                    "detail": "Documented rejection reason is mandatory to reject a supplier invoice.",
                }
            )

        now = timezone.now()
        with transaction.atomic():
            invoice.matching_status = InvoiceMatchingStatus.REJECTED
            invoice.rejected_by = actor_user if hasattr(actor_user, "user_id") else None
            invoice.rejected_at = now
            invoice.rejection_reason = reason if not notes.strip() else f"{reason} | {notes.strip()}"
            invoice.save(
                update_fields=[
                    "matching_status",
                    "rejected_by",
                    "rejected_at",
                    "rejection_reason",
                    "updated_at",
                ]
            )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_INVOICE_REJECTED",
            resource_type="SupplierInvoice",
            resource_id=invoice.invoice_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_id": invoice.invoice_id,
                "invoice_number": invoice.invoice_number,
                "rejection_reason": reason,
            },
        )

        return cls.serialize_supplier_invoice(invoice)

    @classmethod
    def post_invoice_to_ap(
        cls,
        tenant_id: str,
        actor_user: Any,
        invoice_id: str,
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            invoice = SupplierInvoice.objects.select_for_update().get(invoice_id=invoice_id, tenant_id=tenant_id)
        except SupplierInvoice.DoesNotExist:
            raise NotFound({"error_code": "ERR_INVOICE_NOT_FOUND", "detail": f"Supplier invoice {invoice_id} not found."})

        if invoice.matching_status == InvoiceMatchingStatus.POSTED_TO_AP:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_POSTED",
                    "detail": "Supplier invoice is already posted to Accounts Payable.",
                }
            )

        if invoice.matching_status == InvoiceMatchingStatus.REJECTED:
            raise ValidationError(
                {
                    "error_code": "ERR_INVOICE_REJECTED",
                    "detail": "Cannot post a rejected supplier invoice to Accounts Payable.",
                }
            )

        if invoice.matching_status != InvoiceMatchingStatus.MATCHED and invoice.matching_status != InvoiceMatchingStatus.APPROVED_EXCEPTION:
            raise ValidationError(
                {
                    "error_code": "ERR_INVOICE_NOT_MATCHED",
                    "detail": f"Cannot post invoice in {invoice.matching_status} status. Only MATCHED or APPROVED_EXCEPTION invoices can be posted to AP.",
                }
            )

        now = timezone.now()
        with transaction.atomic():
            # Calculate running AP balance for this supplier
            last_entry = (
                SupplierLedgerEntry.objects.filter(tenant=tenant, supplier=invoice.supplier)
                .order_by("-posted_at", "-posting_id")
                .first()
            )
            prev_balance = last_entry.balance_after if last_entry else Decimal("0.0000")
            new_balance = prev_balance + invoice.total_amount

            posting_id = f"sup-post-{uuid.uuid4().hex[:12]}"
            ledger_entry = SupplierLedgerEntry.objects.create(
                posting_id=posting_id,
                tenant=tenant,
                branch=invoice.branch,
                supplier=invoice.supplier,
                posting_type=SupplierPostingType.GRN_INVOICE,
                reference_number=invoice.invoice_number,
                debit_amount=Decimal("0.0000"),
                credit_amount=invoice.total_amount,
                balance_after=new_balance,
                currency=invoice.currency,
                notes=f"Commercial Invoice Matched: {invoice.invoice_number} (PO: {invoice.purchase_order_number}, GRN: {invoice.goods_receipt_number})",
                posted_by=actor_user if hasattr(actor_user, "user_id") else None,
            )

            invoice.matching_status = InvoiceMatchingStatus.POSTED_TO_AP
            invoice.posted_to_ap_at = now
            invoice.ap_ledger_entry = ledger_entry
            invoice.save(update_fields=["matching_status", "posted_to_ap_at", "ap_ledger_entry", "updated_at"])

            # If GRN is linked, update its AP confirmation
            if invoice.goods_receipt:
                grn = invoice.goods_receipt
                grn.ap_posting_status = "POSTED"
                grn.ap_invoice_amount = invoice.total_amount
                grn.save(update_fields=["ap_posting_status", "ap_invoice_amount", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "ap@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_INVOICE_POSTED_TO_AP",
            resource_type="SupplierInvoice",
            resource_id=invoice.invoice_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_id": invoice.invoice_id,
                "invoice_number": invoice.invoice_number,
                "posting_id": ledger_entry.posting_id,
                "credit_amount": str(invoice.total_amount),
                "balance_after": str(new_balance),
            },
        )

        return cls.serialize_supplier_invoice(invoice)

    @classmethod
    def get_exception_queue(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = SupplierInvoice.objects.filter(
            tenant_id=tenant_id,
            matching_status=InvoiceMatchingStatus.DISCREPANCY_QUEUE,
        ).select_related("supplier", "purchase_order", "goods_receipt", "exception_approved_by")

        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        return [cls.serialize_supplier_invoice(inv) for inv in qs]

    @classmethod
    def get_supplier_invoice(cls, tenant_id: str, invoice_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        try:
            invoice = SupplierInvoice.objects.select_related(
                "supplier", "purchase_order", "goods_receipt", "exception_approved_by", "rejected_by", "ap_ledger_entry"
            ).get(invoice_id=invoice_id, tenant_id=tenant_id)
        except SupplierInvoice.DoesNotExist:
            raise NotFound({"error_code": "ERR_INVOICE_NOT_FOUND", "detail": f"Supplier invoice {invoice_id} not found."})

        return cls.serialize_supplier_invoice(invoice)

    @classmethod
    def list_supplier_invoices(
        cls,
        tenant_id: str,
        supplier_id: Optional[str] = None,
        matching_status: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = SupplierInvoice.objects.filter(tenant_id=tenant_id).select_related(
            "supplier", "purchase_order", "goods_receipt", "exception_approved_by", "rejected_by", "ap_ledger_entry"
        )

        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if matching_status:
            qs = qs.filter(matching_status=matching_status.upper())

        return [cls.serialize_supplier_invoice(inv) for inv in qs]
