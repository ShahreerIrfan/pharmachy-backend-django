"""
Architecture Decision: ADR-0232 (P232).
POS Receipt Printing & Reprint Service (58mm, 80mm Thermal, A4, Printer Status & Watermarking).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.models import (
    PosPaymentAttempt,
    PosPrintJobType,
    PosPrinterStatus,
    PosReceiptPrintFormat,
    PosReceiptPrintLog,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSyncState,
    SaleInvoice,
    Tenant,
)


class PosReceiptService:
    """Canonical service for POS receipt generation, formatting, thermal rendering, and audited reprints."""

    WIDTH_58MM = 32
    WIDTH_80MM = 48
    WIDTH_A4 = 80

    @classmethod
    def get_column_width(cls, print_format: str) -> int:
        fmt = str(print_format or PosReceiptPrintFormat.THERMAL_80MM).upper()
        if fmt == PosReceiptPrintFormat.THERMAL_58MM:
            return cls.WIDTH_58MM
        elif fmt == PosReceiptPrintFormat.A4_INVOICE:
            return cls.WIDTH_A4
        return cls.WIDTH_80MM

    @classmethod
    def determine_watermark(
        cls,
        sale: PosSaleOrder,
        reprint_count: int = 0,
    ) -> str:
        # Check offline / unsynchronized state first
        if (
            sale.sync_status != PosSyncState.CLOUD_CONFIRMED
            or sale.lifecycle_state == PosSaleLifecycleState.OFFLINE_PENDING_SYNC
        ):
            return "*** PENDING CLOUD CONFIRMATION / ক্লাউড কনফার্মেশন অপেক্ষমান ***"

        # Check duplicate reprint state
        if reprint_count > 0:
            return f"*** DUPLICATE RECEIPT / পুনর্মুদ্রণ কপি #{reprint_count} ***"

        return ""

    @classmethod
    def build_receipt_payload(
        cls,
        sale: PosSaleOrder,
        reprint_count: int = 0,
        reprint_reason: str = "",
        printer_status: str = PosPrinterStatus.READY,
        print_format: str = PosReceiptPrintFormat.THERMAL_80MM,
    ) -> Dict[str, Any]:
        tenant = sale.tenant
        branch = sale.branch

        watermark = cls.determine_watermark(sale, reprint_count=reprint_count)
        is_duplicate = reprint_count > 0 or bool(watermark and "DUPLICATE" in watermark)

        # Lines data
        items: List[Dict[str, Any]] = []
        for line in sale.lines.select_related("product").prefetch_related("allocations__batch").order_by("line_number"):
            prod = line.product
            batch_alloc = line.allocations.first()
            batch_num = batch_alloc.batch.batch_number if batch_alloc and batch_alloc.batch else "N/A"
            exp_date = (
                batch_alloc.batch.expiry_date.strftime("%m/%y")
                if batch_alloc and batch_alloc.batch and batch_alloc.batch.expiry_date
                else ""
            )

            items.append({
                "line_number": line.line_number,
                "item_name": prod.brand_name,
                "generic_name": prod.generic_name or "",
                "dosage_form": prod.dosage_form or "Unit",
                "batch_number": batch_num,
                "expiry_date": exp_date,
                "quantity": line.quantity_units,
                "packaging_unit": line.packaging_unit,
                "unit_price_bdt": str(line.unit_price_bdt),
                "line_discount_bdt": str(getattr(line, "discount_amount_bdt", "0.00")),
                "subtotal_bdt": str(getattr(line, "line_subtotal_bdt", getattr(line, "line_total_bdt", "0.00"))),
            })

        # Payment tender attempts
        tenders: List[Dict[str, Any]] = []
        for att in sale.payment_attempts.order_by("attempt_number"):
            tenders.append({
                "attempt_number": att.attempt_number,
                "tender_type": att.tender_type,
                "amount_bdt": str(att.amount_bdt),
                "cash_tendered_bdt": str(getattr(att, "cash_tendered_bdt", att.amount_bdt)),
                "change_given_bdt": str(getattr(att, "change_given_bdt", "0.00")),
                "card_last4": att.card_last4 or "",
                "gateway_reference": att.gateway_reference or "",
                "status": att.status,
            })

        cashier_name = ""
        if sale.cashier_user:
            cashier_name = getattr(sale.cashier_user, "full_name", "") or getattr(sale.cashier_user, "email", "")

        inv_num = sale.order_number
        if sale.linked_invoice:
            inv_num = sale.linked_invoice.invoice_number

        created_dt = sale.created_at or timezone.now()

        payload = {
            "sale_id": sale.sale_id,
            "invoice_number": inv_num,
            "order_number": sale.order_number,
            "tenant_name": tenant.name,
            "branch_name": branch.name if branch else "Main Branch",
            "branch_code": getattr(branch, "branch_code", "BR01"),
            "customer_name": sale.customer_name or "Walk-in Customer",
            "customer_phone": sale.customer_phone or "",
            "cashier_name": cashier_name,
            "date_time": created_dt.strftime("%Y-%m-%d %I:%M %p"),
            "sync_status": sale.sync_status,
            "lifecycle_state": sale.lifecycle_state,
            "subtotal_bdt": str(sale.subtotal_bdt),
            "discount_amount_bdt": str(sale.discount_amount_bdt),
            "tax_amount_bdt": str(sale.tax_amount_bdt),
            "grand_total_bdt": str(sale.grand_total_bdt),
            "paid_amount_bdt": str(sale.paid_amount_bdt),
            "due_amount_bdt": str(sale.due_amount_bdt),
            "change_due_bdt": str(getattr(sale, "change_returned_bdt", getattr(sale, "change_due_bdt", "0.00"))),
            "items": items,
            "tenders": tenders,
            "reprint_count": reprint_count,
            "is_duplicate": is_duplicate,
            "reprint_reason": reprint_reason,
            "watermark_text": watermark,
            "printer_status": printer_status,
            "print_format": print_format,
            "return_policy_en": "Returns accepted within 7 days with valid receipt.",
            "return_policy_bn": "বিক্রিত ঔষধ ও সামগ্রী ৭ দিনের মধ্যে ক্যাশ মেমোসহ ফেরতযোগ্য।",
        }
        return payload

    @classmethod
    def render_thermal_text(cls, payload: Dict[str, Any], print_format: str) -> str:
        width = cls.get_column_width(print_format)
        sep_line = "=" * width
        thin_line = "-" * width

        lines: List[str] = []

        def center(text: str) -> str:
            return text.center(width)[:width]

        def l_r(left: str, right: str) -> str:
            gap = width - len(left) - len(right)
            if gap < 1:
                return f"{left[:width-len(right)-1]} {right}"
            return f"{left}{' ' * gap}{right}"

        # 1. Watermark top banner (if present)
        watermark = payload.get("watermark_text", "")
        if watermark:
            lines.append(sep_line)
            lines.append(center(watermark))
            lines.append(sep_line)

        # 2. Pharmacy Header
        lines.append(center(payload.get("tenant_name", "Pharmacy SaaS")))
        lines.append(center(payload.get("branch_name", "Main Branch")))
        lines.append(center(f"Hotline: {payload.get('customer_phone') or '01700-000000'}"))
        lines.append(thin_line)

        # 3. Invoice & Metadata
        lines.append(l_r(f"Inv: {payload.get('invoice_number')}", f"Date: {payload.get('date_time', '')[:10]}"))
        lines.append(l_r(f"Cashier: {payload.get('cashier_name')[:14]}", f"Time: {payload.get('date_time', '')[11:]}"))
        lines.append(l_r(f"Customer: {payload.get('customer_name')[:16]}", f"Tel: {payload.get('customer_phone')[:11]}"))
        lines.append(sep_line)

        # 4. Item Headers
        if width <= cls.WIDTH_58MM:
            # 32 columns: Item / Qty x Price / Total
            lines.append(l_r("Item (Batch)", "Qty x Price"))
            lines.append(thin_line)
            for itm in payload.get("items", []):
                name_batch = f"{itm['item_name']} ({itm['batch_number']})"
                price_str = f"{itm['quantity']} x {itm['unit_price_bdt']}"
                lines.append(name_batch[:width])
                lines.append(l_r(f"  {price_str}", f"৳{itm['subtotal_bdt']}"))
        else:
            # 48+ columns: Name Batch Qty Price Total
            lines.append(f"{'Item (Batch)':<24} {'Qty':>4} {'Price':>8} {'Total':>9}")
            lines.append(thin_line)
            for itm in payload.get("items", []):
                name_b = f"{itm['item_name']} ({itm['batch_number']})"[:24]
                lines.append(
                    f"{name_b:<24} {str(itm['quantity']):>4} {itm['unit_price_bdt']:>8} {itm['subtotal_bdt']:>9}"
                )

        lines.append(thin_line)

        # 5. Financial Summary
        lines.append(l_r("Subtotal / উপমোট:", f"BDT {payload.get('subtotal_bdt')}"))
        if Decimal(payload.get("discount_amount_bdt", "0.00")) > Decimal("0.00"):
            lines.append(l_r("Discount / ছাড়:", f"-BDT {payload.get('discount_amount_bdt')}"))
        if Decimal(payload.get("tax_amount_bdt", "0.00")) > Decimal("0.00"):
            lines.append(l_r("VAT / ভ্যাট:", f"+BDT {payload.get('tax_amount_bdt')}"))
        lines.append(sep_line)
        lines.append(l_r("NET TOTAL / সর্বমোট:", f"BDT {payload.get('grand_total_bdt')}"))
        lines.append(sep_line)

        # 6. Tenders
        for t in payload.get("tenders", []):
            lines.append(l_r(f"Paid ({t['tender_type']}):", f"BDT {t['amount_bdt']}"))
            if Decimal(t.get("change_given_bdt", "0.00")) > Decimal("0.00"):
                lines.append(l_r("Change Returned:", f"BDT {t['change_given_bdt']}"))

        if Decimal(payload.get("due_amount_bdt", "0.00")) > Decimal("0.00"):
            lines.append(l_r("DUE BALANCE / বকেয়া:", f"BDT {payload.get('due_amount_bdt')}"))

        lines.append(thin_line)

        # 7. Policy & Footer
        lines.append(center(payload.get("return_policy_en", "")))
        lines.append(center(payload.get("return_policy_bn", "")))

        # 8. Watermark bottom banner
        if watermark:
            lines.append(sep_line)
            lines.append(center(watermark))
            lines.append(sep_line)

        return "\n".join(lines)

    @classmethod
    def record_print_event(
        cls,
        user: Any,
        sale: PosSaleOrder,
        print_format: str = PosReceiptPrintFormat.THERMAL_80MM,
        printer_status: str = PosPrinterStatus.READY,
        reprint_reason: str = "",
        printer_name: str = "Default POS Thermal",
    ) -> Dict[str, Any]:
        """
        Validates reprint permissions and records an audited print/reprint log.
        """
        tenant = sale.tenant
        branch = sale.branch

        # Count prior prints for this sale
        past_prints_count = PosReceiptPrintLog.objects.filter(tenant=tenant, sale=sale).count()
        reprint_count = past_prints_count
        is_duplicate = reprint_count > 0

        # Mandatory reprint reason rule
        clean_reason = str(reprint_reason or "").strip()
        if is_duplicate and not clean_reason:
            raise ValidationError(
                {"reprint_reason": "Reprint reason is mandatory when printing a duplicate receipt."}
            )

        payload = cls.build_receipt_payload(
            sale=sale,
            reprint_count=reprint_count,
            reprint_reason=clean_reason,
            printer_status=printer_status,
            print_format=print_format,
        )

        rendered_text = cls.render_thermal_text(payload, print_format)

        job_type = (
            PosPrintJobType.REPRINT_DUPLICATE
            if is_duplicate
            else PosPrintJobType.ORIGINAL_RECEIPT
        )

        log = PosReceiptPrintLog.objects.create(
            print_id=f"prn-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            sale=sale,
            invoice=sale.linked_invoice,
            print_format=print_format,
            job_type=job_type,
            reprint_count=reprint_count,
            is_duplicate=is_duplicate,
            watermark_text=payload["watermark_text"],
            printer_name=printer_name,
            printer_status=printer_status,
            reprint_reason=clean_reason,
            raw_receipt_text=rendered_text,
            receipt_payload_json=payload,
            printed_by=user,
            printed_at=timezone.now(),
        )

        return {
            "print_id": log.print_id,
            "sale_id": sale.sale_id,
            "invoice_number": payload["invoice_number"],
            "job_type": log.job_type,
            "print_format": log.print_format,
            "format": log.print_format,
            "reprint_count": log.reprint_count,
            "reprint_sequence": log.reprint_count,
            "is_duplicate": log.is_duplicate,
            "printer_status": log.printer_status,
            "watermark_text": log.watermark_text,
            "reprint_reason": log.reprint_reason,
            "raw_receipt_text": log.raw_receipt_text,
            "raw_text": log.raw_receipt_text,
            "receipt_payload": log.receipt_payload_json,
            "payload": log.receipt_payload_json,
            "printed_at": log.printed_at.isoformat(),
        }

