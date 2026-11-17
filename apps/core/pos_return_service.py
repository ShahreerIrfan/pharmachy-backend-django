"""
Architecture Decision: ADR-0234 (P234).
POS Sales Refund, Item Returns, Lot Traceability & Safety Disposition Service.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    BatchStock,
    PosCashierShift,
    PosPackagingUnit,
    PosRefundMethod,
    PosReturnSafetyDisposition,
    PosReturnStatus,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleReturn,
    PosSaleReturnLine,
    PosShiftStatus,
)
from apps.identity.models import UserRole


class PosReturnService:
    """
    Business service for processing item returns, original lot tracking,
    safety dispositions, and refund financial reconciliations.
    """

    ALLOWED_RETURN_ROLES = {
        UserRole.CASHIER,
        UserRole.STAFF_PHARMACIST,
        UserRole.BRANCH_MANAGER,
        UserRole.PHARMACY_OWNER,
        UserRole.SUPERADMIN,
    }

    @classmethod
    @transaction.atomic
    def process_return(
        cls,
        *,
        user: Any,
        sale_id: str,
        lines_data: List[Dict[str, Any]],
        refund_method: str = PosRefundMethod.CASH,
        reason: str = "",
        customer_name: str = "",
        customer_phone: str = "",
    ) -> PosSaleReturn:
        """
        Processes a customer return with safety disposition, lot reference, and refund ledger updates.
        """
        role = getattr(user, "role", None)
        if role not in cls.ALLOWED_RETURN_ROLES:
            raise ValidationError({"user": "User role is not authorized to process POS returns."})

        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "Authenticated tenant context is required."})

        sale = (
            PosSaleOrder.objects.select_for_update()
            .prefetch_related("lines__allocations__batch", "lines__product")
            .filter(tenant_id=tenant_id, sale_id=sale_id)
            .first()
        )
        if not sale:
            raise ValidationError({"sale": f"Sale order not found: {sale_id}."})

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "Return reason is mandatory."})

        if not lines_data:
            raise ValidationError({"lines": "At least one return item line is required."})

        if refund_method not in PosRefundMethod.values:
            raise ValidationError({"refund_method": f"Invalid refund method: {refund_method}."})

        tenant = sale.tenant
        branch = sale.branch

        # Active cashier shift check for drawer cash refund
        active_shift = PosCashierShift.objects.filter(
            tenant=tenant,
            branch=branch,
            cashier_user=user,
            status=PosShiftStatus.OPEN,
        ).first()

        date_str = timezone.now().strftime("%Y%m%d")
        br_code = getattr(branch, "branch_code", "BR01") or "BR01"
        ret_num = f"RET-{br_code}-{date_str}-{uuid.uuid4().hex[:4].upper()}"

        sale_return = PosSaleReturn(
            return_id=f"ret-{uuid.uuid4().hex[:12]}",
            return_number=ret_num,
            tenant=tenant,
            branch=branch,
            original_sale=sale,
            shift=active_shift,
            processed_by=user,
            status=PosReturnStatus.COMPLETED,
            refund_method=refund_method,
            total_refund_amount_bdt=Decimal("0.00"),
            reason=clean_reason,
            customer_name=customer_name or sale.customer_name or "Walk-in Customer",
            customer_phone=customer_phone or sale.customer_phone or "",
            created_at=timezone.now(),
        )

        total_refund = Decimal("0.00")
        return_lines: List[PosSaleReturnLine] = []

        # Map sale lines for quick lookup
        sale_lines_map = {l.line_id: l for l in sale.lines.all()}

        for itm in lines_data:
            line_id = itm.get("sale_line_id") or itm.get("line_id")
            if not line_id or line_id not in sale_lines_map:
                raise ValidationError({"sale_line_id": f"Sale line {line_id} does not belong to original sale."})

            s_line = sale_lines_map[line_id]
            qty_units = int(itm.get("quantity_units") or 1)
            if qty_units < 1:
                raise ValidationError({"quantity_units": "Return quantity must be at least 1 unit."})

            conversion = s_line.conversion_factor or 1
            requested_base_units = qty_units * conversion

            # Check quantity availability against remaining sold units
            available_base_units = s_line.base_units_total - s_line.returned_base_units
            if requested_base_units > available_base_units:
                max_returnable_units = available_base_units // conversion
                raise ValidationError(
                    {"quantity_units": f"Cannot return {qty_units} units for {s_line.product.brand_name}. Maximum returnable: {max_returnable_units} units."}
                )

            # Update returned units on original sale line
            s_line.returned_base_units += requested_base_units
            s_line.save(update_fields=["returned_base_units"])

            # Determine original batch / lot reference
            batch_alloc = s_line.allocations.first()
            orig_batch = batch_alloc.batch if batch_alloc else None
            batch_num = (
                orig_batch.batch_number
                if orig_batch
                else str(itm.get("batch_number") or s_line.scanned_barcode or "ORIGINAL-LOT")
            )
            if not orig_batch and batch_num:
                orig_batch = BatchStock.objects.filter(
                    tenant=tenant,
                    branch=branch,
                    product=s_line.product,
                    batch_number=batch_num,
                ).first()

            disposition = str(itm.get("safety_disposition") or PosReturnSafetyDisposition.RESTOCKABLE).upper()
            if disposition not in PosReturnSafetyDisposition.values:
                disposition = PosReturnSafetyDisposition.RESTOCKABLE

            # Compute line refund
            line_refund = (s_line.unit_price_bdt * Decimal(qty_units)).quantize(Decimal("0.01"))
            total_refund += line_refund

            # If RESTOCKABLE, restore base units to BatchStock quantity_base_units
            if disposition == PosReturnSafetyDisposition.RESTOCKABLE and orig_batch:
                orig_batch.quantity_base_units = orig_batch.quantity_base_units + requested_base_units
                orig_batch.save(update_fields=["quantity_base_units"])

            ret_line = PosSaleReturnLine(
                return_line_id=f"rtl-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                branch=branch,
                sale_return=sale_return,
                sale_line=s_line,
                product=s_line.product,
                original_batch=orig_batch,
                batch_number=batch_num,
                packaging_unit=s_line.packaging_unit or PosPackagingUnit.TABLET,
                conversion_factor=conversion,
                quantity_units=qty_units,
                base_units_returned=requested_base_units,
                unit_price_bdt=s_line.unit_price_bdt,
                refund_amount_bdt=line_refund,
                safety_disposition=disposition,
                disposition_notes=str(itm.get("disposition_notes") or "").strip(),
                created_at=timezone.now(),
            )
            return_lines.append(ret_line)

        sale_return.total_refund_amount_bdt = total_refund
        sale_return.save()

        for rl in return_lines:
            rl.sale_return = sale_return
            rl.save()

        # Update active shift drawer if refund was given in CASH
        if refund_method == PosRefundMethod.CASH and active_shift:
            active_shift.total_cash_refunds_bdt = (
                active_shift.total_cash_refunds_bdt + total_refund
            ).quantize(Decimal("0.01"))
            active_shift.expected_drawer_cash_bdt = active_shift.calculate_expected_drawer()
            active_shift.save(update_fields=["total_cash_refunds_bdt", "expected_drawer_cash_bdt"])

        return sale_return
