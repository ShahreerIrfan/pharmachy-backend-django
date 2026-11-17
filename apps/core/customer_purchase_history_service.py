"""
backend/apps/core/customer_purchase_history_service.py
Architecture Decision: ADR-0263 (P263).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
- Building unified customer purchase history timelines:
  * Sales Orders (FINALIZED, REVERSED, RETURNED states)
  * Sales Returns & Refunds (PosSaleReturn)
  * Due Collections & Repayments (CustomerDueCollection)
- Enforcing strict tenant and branch scoping.
- Calculating comprehensive financial summary (purchases, returns, collections, net spend, current due).
- Role-based PHI / PII privacy masking (cashier vs licensed pharmacist/owner).
- Clean pagination, date-range filtering, and event type filtering.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional, Tuple

from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    PosCustomer,
    PosRefundMethod,
    PosReturnStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleReturn,
)
from apps.identity.models import UserRole

MONEY_ZERO = Decimal("0.00")
PRIVILEGED_ROLES = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.STAFF_PHARMACIST,
}


def mask_phone_number(phone: str) -> str:
    """Mask phone number preserving country/area code and last 3-4 digits."""
    raw = (phone or "").strip()
    if not raw:
        return ""
    if len(raw) <= 6:
        return "*" * len(raw)
    if raw.startswith("+880"):
        # +88017****5678
        prefix = raw[:6]
        suffix = raw[-4:]
        middle_count = max(2, len(raw) - 10)
        return f"{prefix}{'*' * middle_count}{suffix}"
    elif raw.startswith("01"):
        # 017****5678
        prefix = raw[:3]
        suffix = raw[-4:]
        middle_count = max(2, len(raw) - 7)
        return f"{prefix}{'*' * middle_count}{suffix}"
    else:
        prefix = raw[:2]
        suffix = raw[-2:]
        return f"{prefix}****{suffix}"


class CustomerPurchaseHistoryService:
    """Domain service for generating and serializing unified customer purchase history."""

    @classmethod
    def get_customer_purchase_history(
        cls,
        user: Any,
        customer_id: str,
        *,
        branch_id: Optional[str] = None,
        event_type: Optional[str] = "ALL",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        Retrieves paginated timeline of customer sales, returns, and collections
        with role-aware data masking and summary analytics.
        """
        # 1. Resolve user tenant & branch
        user_tenant_id = getattr(user, "tenant_id", None)
        if hasattr(user_tenant_id, "tenant_id"):
            user_tenant_id = user_tenant_id.tenant_id
        user_role = str(getattr(user, "role", "")).upper()
        actor_branch_id = getattr(user, "branch_id", None)
        if hasattr(actor_branch_id, "branch_id"):
            actor_branch_id = actor_branch_id.branch_id

        # 2. Fetch customer with strict tenant scoping
        try:
            customer = PosCustomer.objects.select_related("primary_branch").get(
                tenant_id=user_tenant_id,
                customer_id=customer_id,
            )
        except PosCustomer.DoesNotExist:
            raise NotFound({"customer_id": f"Customer '{customer_id}' not found in active tenant."})

        # Branch isolation check: if caller is non-tenant-wide and customer is assigned to another branch
        is_privileged = user_role in PRIVILEGED_ROLES
        if not is_privileged and actor_branch_id:
            if customer.primary_branch_id and customer.primary_branch_id != actor_branch_id:
                raise PermissionDenied({"detail": "Customer belongs to another branch."})

        # 3. Validate pagination parameters
        try:
            page = int(page)
            if page < 1:
                raise ValueError
        except (ValueError, TypeError):
            raise ValidationError({"page": "Page number must be an integer >= 1."})

        try:
            page_size = int(page_size)
            if page_size < 1 or page_size > 100:
                raise ValueError
        except (ValueError, TypeError):
            raise ValidationError({"page_size": "Page size must be an integer between 1 and 100."})

        # 4. Validate event_type filter
        resolved_event_type = (event_type or "ALL").upper().strip()
        allowed_event_types = {"ALL", "SALE", "RETURN", "COLLECTION"}
        if resolved_event_type not in allowed_event_types:
            raise ValidationError({"event_type": f"Invalid event_type. Allowed choices: {', '.join(sorted(allowed_event_types))}."})

        # 5. Validate and parse date range filters
        start_dt = None
        end_dt = None
        if start_date:
            try:
                parsed_start = datetime.date.fromisoformat(str(start_date).strip())
                start_dt = timezone.make_aware(datetime.datetime.combine(parsed_start, datetime.time.min))
            except (ValueError, TypeError):
                raise ValidationError({"start_date": "Invalid start_date format. Expected YYYY-MM-DD."})

        if end_date:
            try:
                parsed_end = datetime.date.fromisoformat(str(end_date).strip())
                end_dt = timezone.make_aware(datetime.datetime.combine(parsed_end, datetime.time.max))
            except (ValueError, TypeError):
                raise ValidationError({"end_date": "Invalid end_date format. Expected YYYY-MM-DD."})

        if start_dt and end_dt and start_dt > end_dt:
            raise ValidationError({"start_date": "start_date cannot be later than end_date."})

        # 6. Branch scoping
        effective_branch_id = branch_id or (actor_branch_id if not is_privileged else None)
        if effective_branch_id:
            if not Branch.objects.filter(tenant_id=user_tenant_id, branch_id=effective_branch_id).exists():
                raise ValidationError({"branch": f"Branch '{effective_branch_id}' does not belong to active tenant."})

        # 7. Collect Timeline Events
        events: List[Dict[str, Any]] = []

        # A. Sales Orders
        if resolved_event_type in ("ALL", "SALE"):
            sales_qs = (
                PosSaleOrder.objects.filter(
                    tenant_id=user_tenant_id,
                    customer=customer,
                )
                .exclude(lifecycle_state__in=[PosSaleLifecycleState.DRAFT, PosSaleLifecycleState.VOIDED, PosSaleLifecycleState.HELD])
                .select_related("branch", "prescription")
                .prefetch_related("lines__product")
            )
            if effective_branch_id:
                sales_qs = sales_qs.filter(branch_id=effective_branch_id)
            if start_dt:
                sales_qs = sales_qs.filter(created_at__gte=start_dt)
            if end_dt:
                sales_qs = sales_qs.filter(created_at__lte=end_dt)

            for sale in sales_qs:
                timestamp = sale.finalized_at or sale.created_at
                line_items = []
                for line in sale.lines.all():
                    line_items.append({
                        "line_id": line.line_id,
                        "product_id": line.product_id,
                        "product_name": line.product.brand_name if line.product else "",
                        "generic_name": line.product.generic_name if line.product else "",
                        "quantity": line.quantity_units,
                        "unit_price_bdt": f"{line.unit_price_bdt:.2f}",
                        "line_total_bdt": f"{line.line_total_bdt:.2f}",
                        "dosage_form": getattr(line.product, "dosage_form", "") if line.product else "",
                    })

                has_rx = bool(sale.prescription_id)
                prescriber_summary = None
                if has_rx and sale.prescription:
                    rx = sale.prescription
                    bmdc_val = getattr(rx, "doctor_bmdc_no", None) or getattr(rx, "bmdc_reg_number", "") or ""
                    if is_privileged:
                        prescriber_summary = {
                            "doctor_name": getattr(rx, "doctor_name", "") or "",
                            "doctor_bmdc_no": bmdc_val,
                            "bmdc_reg_number": bmdc_val,
                            "is_masked": False,
                        }
                    else:
                        prescriber_summary = {
                            "doctor_name": "Dr. *** (Masked)",
                            "doctor_bmdc_no": "****",
                            "bmdc_reg_number": "****",
                            "is_masked": True,
                        }

                ref_no = getattr(sale, "order_number", "") or getattr(sale, "sale_number", "")
                inv_no = sale.linked_invoice.invoice_number if getattr(sale, "linked_invoice", None) else ""
                disc_bdt = getattr(sale, "discount_amount_bdt", None) or getattr(sale, "discount_bdt", Decimal("0.00"))
                tax_bdt = getattr(sale, "tax_amount_bdt", None) or getattr(sale, "tax_bdt", Decimal("0.00"))
                tender = getattr(sale, "payment_method", None) or "CASH"

                events.append({
                    "event_type": "SALE",
                    "event_id": sale.sale_id,
                    "reference_number": ref_no,
                    "invoice_number": inv_no,
                    "timestamp": timestamp.isoformat(),
                    "sort_dt": timestamp,
                    "branch_id": str(sale.branch_id),
                    "branch_name": sale.branch.name if sale.branch else "",
                    "gross_amount_bdt": f"{sale.subtotal_bdt:.2f}",
                    "discount_bdt": f"{disc_bdt:.2f}",
                    "tax_bdt": f"{tax_bdt:.2f}",
                    "net_amount_bdt": f"{sale.grand_total_bdt:.2f}",
                    "paid_amount_bdt": f"{sale.paid_amount_bdt:.2f}",
                    "due_amount_bdt": f"{sale.due_amount_bdt:.2f}",
                    "tender_type": tender,
                    "status": sale.lifecycle_state,
                    "is_prescription_sale": has_rx,
                    "prescriber_info": prescriber_summary,
                    "items_summary": line_items,
                    "items_count": len(line_items),
                })

        # B. Sales Returns
        if resolved_event_type in ("ALL", "RETURN"):
            returns_qs = (
                PosSaleReturn.objects.filter(
                    tenant_id=user_tenant_id,
                    original_sale__customer=customer,
                )
                .select_related("branch", "original_sale")
                .prefetch_related("lines__product")
            )
            if effective_branch_id:
                returns_qs = returns_qs.filter(branch_id=effective_branch_id)
            if start_dt:
                returns_qs = returns_qs.filter(created_at__gte=start_dt)
            if end_dt:
                returns_qs = returns_qs.filter(created_at__lte=end_dt)

            for ret in returns_qs:
                ret_lines = []
                for rline in ret.lines.all():
                    ret_lines.append({
                        "return_line_id": rline.return_line_id,
                        "product_id": rline.product_id,
                        "product_name": rline.product.brand_name if rline.product else "",
                        "quantity": rline.quantity_units,
                        "refund_amount_bdt": f"{rline.refund_amount_bdt:.2f}",
                        "safety_disposition": rline.safety_disposition,
                    })

                events.append({
                    "event_type": "RETURN",
                    "event_id": ret.return_id,
                    "reference_number": ret.return_number,
                    "original_sale_number": getattr(ret.original_sale, "order_number", "") or getattr(ret.original_sale, "sale_number", ""),
                    "timestamp": ret.created_at.isoformat(),
                    "sort_dt": ret.created_at,
                    "branch_id": str(ret.branch_id),
                    "branch_name": ret.branch.name if ret.branch else "",
                    "gross_amount_bdt": f"{ret.total_refund_amount_bdt:.2f}",
                    "discount_bdt": "0.00",
                    "tax_bdt": "0.00",
                    "net_amount_bdt": f"{ret.total_refund_amount_bdt:.2f}",
                    "paid_amount_bdt": f"{ret.total_refund_amount_bdt:.2f}",
                    "due_amount_bdt": "0.00",
                    "tender_type": ret.refund_method,
                    "status": ret.status,
                    "is_prescription_sale": False,
                    "prescriber_info": None,
                    "reason": ret.reason,
                    "items_summary": ret_lines,
                    "items_count": len(ret_lines),
                })

        # C. Due Collections
        if resolved_event_type in ("ALL", "COLLECTION"):
            collections_qs = (
                CustomerDueCollection.objects.filter(
                    tenant_id=user_tenant_id,
                    customer=customer,
                )
                .select_related("branch", "due_record")
            )
            if effective_branch_id:
                collections_qs = collections_qs.filter(branch_id=effective_branch_id)
            if start_dt:
                collections_qs = collections_qs.filter(collected_at__gte=start_dt)
            if end_dt:
                collections_qs = collections_qs.filter(collected_at__lte=end_dt)

            for col in collections_qs:
                col_item = {
                    "collection_id": col.collection_id,
                    "due_record_id": col.due_record_id,
                    "description": f"Customer Due Repayment ({col.tender_type})",
                    "amount_bdt": f"{col.amount_bdt:.2f}",
                }
                events.append({
                    "event_type": "COLLECTION",
                    "event_id": col.collection_id,
                    "reference_number": col.collection_id,
                    "due_record_id": col.due_record_id,
                    "timestamp": col.collected_at.isoformat(),
                    "sort_dt": col.collected_at,
                    "branch_id": str(col.branch_id),
                    "branch_name": col.branch.name if col.branch else "",
                    "gross_amount_bdt": f"{col.amount_bdt:.2f}",
                    "discount_bdt": "0.00",
                    "tax_bdt": "0.00",
                    "net_amount_bdt": f"{col.amount_bdt:.2f}",
                    "paid_amount_bdt": f"{col.amount_bdt:.2f}",
                    "due_amount_bdt": "0.00",
                    "tender_type": col.tender_type,
                    "status": col.status,
                    "is_prescription_sale": False,
                    "prescriber_info": None,
                    "items_summary": [col_item],
                    "items_count": 1,
                })

        # 8. Sort chronological descending
        events.sort(key=lambda x: x["sort_dt"], reverse=True)

        # 9. Strip internal sort helper from payloads
        clean_events = []
        for e in events:
            item = dict(e)
            item.pop("sort_dt", None)
            clean_events.append(item)

        # 10. Financial Totals (Lifetime or tenant-scoped)
        all_sales = PosSaleOrder.objects.filter(
            tenant_id=user_tenant_id,
            customer=customer,
        ).exclude(lifecycle_state__in=[PosSaleLifecycleState.DRAFT, PosSaleLifecycleState.VOIDED])
        total_purchases_bdt = all_sales.aggregate(s=Sum("grand_total_bdt"))["s"] or MONEY_ZERO

        all_returns = PosSaleReturn.objects.filter(
            tenant_id=user_tenant_id,
            original_sale__customer=customer,
            status=PosReturnStatus.COMPLETED,
        )
        total_returns_bdt = all_returns.aggregate(s=Sum("total_refund_amount_bdt"))["s"] or MONEY_ZERO

        all_collections = CustomerDueCollection.objects.filter(
            tenant_id=user_tenant_id,
            customer=customer,
            status=CustomerDueCollectionStatus.CAPTURED,
        )
        total_collections_bdt = all_collections.aggregate(s=Sum("amount_bdt"))["s"] or MONEY_ZERO

        net_spend_bdt = max(MONEY_ZERO, total_purchases_bdt - total_returns_bdt)

        # 11. Customer Privacy Masking
        prefs = customer.privacy_preferences or {}
        should_mask_phone = not is_privileged
        display_phone = mask_phone_number(customer.phone) if should_mask_phone else customer.phone

        customer_summary = {
            "customer_id": customer.customer_id,
            "full_name": customer.full_name,
            "phone": display_phone,
            "is_phone_masked": should_mask_phone,
            "credit_status": customer.credit_status,
            "credit_limit_bdt": f"{customer.credit_limit_bdt:.2f}",
            "current_due_bdt": f"{customer.current_due_bdt:.2f}",
            "total_purchases_bdt": f"{total_purchases_bdt:.2f}",
            "total_returns_bdt": f"{total_returns_bdt:.2f}",
            "total_collections_bdt": f"{total_collections_bdt:.2f}",
            "net_spend_bdt": f"{net_spend_bdt:.2f}",
        }

        # 12. Paginate Results
        total_count = len(clean_events)
        total_pages = max(1, math.ceil(total_count / page_size)) if total_count > 0 else 1
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_items = clean_events[start_idx:end_idx]

        return {
            "customer": customer_summary,
            "pagination": {
                "count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1,
            },
            "results": paginated_items,
        }
