"""
backend/apps/core/customer_partial_collection_service.py
Architecture Decision: ADR-0266 (P266).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
- Applying partial collections directly to original invoices / due records
- Strict overpayment prevention (ERR_DUE_OVERPAYMENT)
- Duplicate settlement prevention (ERR_DUE_ALREADY_SETTLED)
- Itemized invoice allocations (CustomerDueInvoiceAllocation)
- Multi-invoice FIFO and targeted collections with exact penny parity
- Full synchronization with double-entry AR ledger (CustomerReceivableLedgerEntry)
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    CustomerDueInvoiceAllocation,
    CustomerDueRecord,
    DueStatus,
    PaymentMethod,
    PosCustomer,
    PosSaleOrder,
    SaleInvoice,
)
from apps.core.pos_customer_due_service import PosCustomerDueService
from apps.core.pos_domain_service import PosDomainService

MONEY_Q = Decimal("0.01")


def q2(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0.00")).quantize(
            MONEY_Q, rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(
            {"error_code": "ERR_INVALID_PAYMENT_AMOUNT", "detail": f"Invalid monetary amount '{value}'."}
        ) from exc


class CustomerPartialCollectionService:
    """
    Manages partial customer payments applied against specific original sales invoices.
    """

    @classmethod
    def resolve_due_record(
        cls, tenant: Any, identifier: str
    ) -> Optional[CustomerDueRecord]:
        """
        Resolves a CustomerDueRecord by due_id, invoice_id, invoice_number,
        sale_id, or order_number within tenant.
        """
        clean_id = str(identifier or "").strip()
        if not clean_id:
            return None

        # 1. Direct due_id
        due = CustomerDueRecord.objects.filter(tenant=tenant, due_id=clean_id).first()
        if due:
            return due

        # 2. SaleInvoice by invoice_id or invoice_number
        inv = SaleInvoice.objects.filter(
            Q(tenant=tenant) & (Q(invoice_id=clean_id) | Q(invoice_number=clean_id))
        ).first()
        if inv:
            due = CustomerDueRecord.objects.filter(tenant=tenant, invoice=inv).first()
            if due:
                return due

        # 3. PosSaleOrder by sale_id or order_number
        sale = PosSaleOrder.objects.filter(
            Q(tenant=tenant) & (Q(sale_id=clean_id) | Q(order_number=clean_id))
        ).first()
        if sale:
            due = CustomerDueRecord.objects.filter(tenant=tenant, sale=sale).first()
            if due:
                return due

        return None

    @classmethod
    @transaction.atomic
    def apply_partial_collection_to_invoice(
        cls,
        *,
        user: Any,
        invoice_identifier: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Applies a partial collection directly against an original invoice or due record.
        Strictly prevents overpayments (ERR_DUE_OVERPAYMENT) and duplicate settlement
        (ERR_DUE_ALREADY_SETTLED).
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=data.get("branch_id"),
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES | PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        PosDomainService.assert_tenant_active(tenant)
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "partial collections")

        if bool(data.get("is_offline", False)):
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_UNAVAILABLE",
                    "detail": "Receivable collection requires online connectivity to verify live AR ledger state.",
                }
            )

        clean_id = str(invoice_identifier or "").strip()
        due_rec = CustomerDueRecord.objects.select_for_update().filter(
            Q(tenant=tenant)
            & (
                Q(due_id=clean_id)
                | Q(invoice__invoice_id=clean_id)
                | Q(invoice__invoice_number=clean_id)
                | Q(sale__sale_id=clean_id)
                | Q(sale__order_number=clean_id)
            )
        ).first()

        if not due_rec:
            raise NotFound(
                {
                    "error_code": "ERR_INVOICE_DUE_NOT_FOUND",
                    "invoice_identifier": f"No open customer receivable found for identifier '{clean_id}'.",
                }
            )

        # 1. Duplicate settlement guard
        if due_rec.status in (DueStatus.SETTLED, DueStatus.WRITTEN_OFF) or due_rec.remaining_due_bdt <= Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_ALREADY_SETTLED",
                    "due_id": f"Invoice / due '{due_rec.due_id}' is already {due_rec.status} (remaining 0.00 BDT).",
                }
            )

        # 2. Amount validation
        amount_bdt = q2(data.get("amount_bdt"))
        if amount_bdt <= Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                    "amount_bdt": "Partial collection amount must be strictly greater than 0.00 BDT.",
                }
            )

        # 3. Source-specific overpayment guard
        if amount_bdt > due_rec.remaining_due_bdt:
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_OVERPAYMENT",
                    "amount_bdt": (
                        f"Collection amount ({amount_bdt:.2f} BDT) exceeds remaining due on invoice "
                        f"({due_rec.remaining_due_bdt:.2f} BDT). Overpayments are strictly prohibited."
                    ),
                }
            )

        # 4. Delegate core payment & journal posting to PosCustomerDueService
        col, created = PosCustomerDueService.collect_customer_due(
            user=user,
            due_record=due_rec,
            data=data,
        )
        due_rec.refresh_from_db()

        # Retrieve the created invoice allocation record
        allocation = CustomerDueInvoiceAllocation.objects.filter(
            collection=col, due_record=due_rec
        ).first()

        invoice_no = (
            due_rec.invoice.invoice_number if due_rec.invoice else (
                due_rec.sale.order_number if due_rec.sale else due_rec.due_id
            )
        )

        return {
            "success": True,
            "created": created,
            "idempotent_replay": not created,
            "collection_id": col.collection_id,
            "tender_type": col.tender_type,
            "amount_bdt": f"{col.amount_bdt:.2f}",
            "cash_tendered_bdt": f"{col.cash_tendered_bdt:.2f}",
            "change_given_bdt": f"{col.change_given_bdt:.2f}",
            "due_record_id": due_rec.due_id,
            "invoice_number": invoice_no,
            "status": due_rec.status,
            "is_settled": due_rec.status == DueStatus.SETTLED,
            "remaining_due_bdt": f"{due_rec.remaining_due_bdt:.2f}",
            "total_due_bdt": f"{due_rec.total_due_bdt:.2f}",
            "paid_amount_bdt": f"{due_rec.paid_amount_bdt:.2f}",
            "customer_id": due_rec.customer_id,
            "customer_current_due_bdt": f"{due_rec.customer.current_due_bdt:.2f}" if due_rec.customer else "0.00",
            "allocation_id": allocation.allocation_id if allocation else "",
            "message_en": (
                f"Applied partial collection of {col.amount_bdt:.2f} BDT to invoice {invoice_no} "
                f"(Remaining: {due_rec.remaining_due_bdt:.2f} BDT, Status: {due_rec.status})."
                if created
                else "Idempotent payment collection replayed."
            ),
            "message_bn": (
                f"ইনভয়েস {invoice_no}-এ {col.amount_bdt:.2f} টাকা আংশিক আদায় সফল হয়েছে "
                f"(অবশিষ্ট: {due_rec.remaining_due_bdt:.2f} টাকা, স্ট্যাটাস: {due_rec.status})।"
                if created
                else "আইডেম্পোটেন্ট পেমেন্ট কালেকশন পুনঃপ্রদর্শন করা হয়েছে।"
            ),
        }

    @classmethod
    @transaction.atomic
    def apply_multi_invoice_collection(
        cls,
        *,
        user: Any,
        customer_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Applies a collection across multiple outstanding invoices for a customer
        using either explicit allocations or automated FIFO (oldest first).
        Ensures atomic execution, penny-exact parity, and zero overpayments.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=data.get("branch_id"),
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES | PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        PosDomainService.assert_tenant_active(tenant)
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "multi-invoice collections")

        if bool(data.get("is_offline", False)):
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_UNAVAILABLE",
                    "detail": "Multi-invoice collection requires online ledger synchronization.",
                }
            )

        customer = PosCustomer.objects.select_for_update().filter(
            tenant=tenant, customer_id=customer_id
        ).first()
        if not customer:
            raise NotFound(
                {
                    "error_code": "ERR_CUSTOMER_NOT_FOUND",
                    "customer_id": f"Customer '{customer_id}' not found.",
                }
            )

        total_amount_bdt = q2(data.get("amount_bdt"))
        if total_amount_bdt <= Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                    "amount_bdt": "Payment amount must be greater than 0.00 BDT.",
                }
            )

        raw_allocations = data.get("allocations")
        allocation_plan: List[Tuple[CustomerDueRecord, Decimal]] = []

        if raw_allocations:
            # Explicit allocation list
            sum_alloc = Decimal("0.00")
            for item in raw_allocations:
                rec_id = str(item.get("invoice_identifier") or item.get("due_id") or "").strip()
                amt = q2(item.get("amount_bdt"))
                if amt <= Decimal("0.00"):
                    raise ValidationError(
                        {"error_code": "ERR_INVALID_PAYMENT_AMOUNT", "detail": "Allocation amount must be > 0.00."}
                    )
                due_rec = CustomerDueRecord.objects.select_for_update().filter(
                    Q(tenant=tenant, customer=customer)
                    & (
                        Q(due_id=rec_id)
                        | Q(invoice__invoice_id=rec_id)
                        | Q(invoice__invoice_number=rec_id)
                        | Q(sale__sale_id=rec_id)
                        | Q(sale__order_number=rec_id)
                    )
                ).first()
                if not due_rec:
                    raise NotFound(
                        {"error_code": "ERR_INVOICE_DUE_NOT_FOUND", "detail": f"Due record '{rec_id}' not found for customer."}
                    )
                if due_rec.status in (DueStatus.SETTLED, DueStatus.WRITTEN_OFF) or due_rec.remaining_due_bdt <= Decimal("0.00"):
                    raise ValidationError(
                        {"error_code": "ERR_DUE_ALREADY_SETTLED", "detail": f"Invoice '{rec_id}' is already settled."}
                    )
                if amt > due_rec.remaining_due_bdt:
                    raise ValidationError(
                        {
                            "error_code": "ERR_DUE_OVERPAYMENT",
                            "detail": f"Allocation {amt:.2f} BDT exceeds remaining due {due_rec.remaining_due_bdt:.2f} BDT on {rec_id}.",
                        }
                    )
                allocation_plan.append((due_rec, amt))
                sum_alloc = q2(sum_alloc + amt)

            if sum_alloc != total_amount_bdt:
                raise ValidationError(
                    {
                        "error_code": "ERR_ALLOCATION_SUM_MISMATCH",
                        "detail": f"Sum of invoice allocations ({sum_alloc:.2f} BDT) does not match total payment amount ({total_amount_bdt:.2f} BDT).",
                    }
                )
        else:
            # Automated FIFO allocation across oldest open due records
            open_dues = CustomerDueRecord.objects.select_for_update().filter(
                tenant=tenant,
                customer=customer,
                remaining_due_bdt__gt=Decimal("0.00"),
                status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
            ).order_by("created_at", "due_id")

            if not open_dues.exists():
                raise ValidationError(
                    {
                        "error_code": "ERR_NO_OUTSTANDING_DUES",
                        "customer_id": f"Customer '{customer.full_name}' has no outstanding due invoices.",
                    }
                )

            total_open = sum((d.remaining_due_bdt for d in open_dues), Decimal("0.00"))
            if total_amount_bdt > total_open:
                raise ValidationError(
                    {
                        "error_code": "ERR_DUE_OVERPAYMENT",
                        "amount_bdt": f"Payment amount ({total_amount_bdt:.2f} BDT) exceeds total outstanding due ({total_open:.2f} BDT).",
                    }
                )

            remaining_to_allocate = total_amount_bdt
            for due_rec in open_dues:
                if remaining_to_allocate <= Decimal("0.00"):
                    break
                alloc_amt = min(due_rec.remaining_due_bdt, remaining_to_allocate)
                allocation_plan.append((due_rec, alloc_amt))
                remaining_to_allocate = q2(remaining_to_allocate - alloc_amt)

        # Apply allocations
        applied_results: List[Dict[str, Any]] = []
        base_idem = str(data.get("idempotency_key") or "").strip()

        for idx, (due_rec, amt) in enumerate(allocation_plan):
            item_data = dict(data)
            item_data["amount_bdt"] = str(amt)
            if base_idem:
                item_data["idempotency_key"] = f"{base_idem}-alloc-{idx+1}"
            res = cls.apply_partial_collection_to_invoice(
                user=user,
                invoice_identifier=due_rec.due_id,
                data=item_data,
            )
            applied_results.append(res)

        customer.refresh_from_db()

        return {
            "success": True,
            "customer_id": customer.customer_id,
            "total_collected_bdt": f"{total_amount_bdt:.2f}",
            "customer_remaining_due_bdt": f"{customer.current_due_bdt:.2f}",
            "allocations_count": len(applied_results),
            "allocations": applied_results,
            "message_en": f"Applied {total_amount_bdt:.2f} BDT across {len(applied_results)} invoice(s).",
            "message_bn": f"{len(applied_results)}টি ইনভয়েসে সর্বমোট {total_amount_bdt:.2f} টাকা সফলভাবে আদায় করা হয়েছে।",
        }

    @classmethod
    def get_invoice_collection_history(
        cls,
        *,
        user: Any,
        invoice_identifier: str,
    ) -> Dict[str, Any]:
        """
        Retrieves all historical collection events and allocations applied to an invoice.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        PosDomainService.assert_tenant_active(tenant)

        clean_id = str(invoice_identifier or "").strip()
        due_rec = CustomerDueRecord.objects.filter(
            Q(tenant=tenant)
            & (
                Q(due_id=clean_id)
                | Q(invoice__invoice_id=clean_id)
                | Q(invoice__invoice_number=clean_id)
                | Q(sale__sale_id=clean_id)
                | Q(sale__order_number=clean_id)
            )
        ).first()

        if not due_rec:
            raise NotFound(
                {
                    "error_code": "ERR_INVOICE_DUE_NOT_FOUND",
                    "invoice_identifier": f"No due record found for '{clean_id}'.",
                }
            )

        allocations = CustomerDueInvoiceAllocation.objects.filter(
            tenant=tenant, due_record=due_rec
        ).select_related("collection", "collection__collected_by").order_by("-created_at")

        history = []
        for alloc in allocations:
            col = alloc.collection
            history.append(
                {
                    "allocation_id": alloc.allocation_id,
                    "collection_id": col.collection_id,
                    "tender_type": col.tender_type,
                    "allocated_amount_bdt": f"{alloc.allocated_amount_bdt:.2f}",
                    "previous_remaining_due_bdt": f"{alloc.previous_remaining_due_bdt:.2f}",
                    "new_remaining_due_bdt": f"{alloc.new_remaining_due_bdt:.2f}",
                    "is_reversed": alloc.is_reversed,
                    "collected_at": col.collected_at.isoformat() if col.collected_at else "",
                    "collected_by": col.collected_by.username if col.collected_by else "",
                    "gateway_reference": col.gateway_reference,
                    "status": col.status,
                }
            )

        return {
            "due_id": due_rec.due_id,
            "invoice_number": due_rec.invoice.invoice_number if due_rec.invoice else (due_rec.sale.order_number if due_rec.sale else ""),
            "customer_id": due_rec.customer_id,
            "customer_name": due_rec.customer_name,
            "total_due_bdt": f"{due_rec.total_due_bdt:.2f}",
            "paid_amount_bdt": f"{due_rec.paid_amount_bdt:.2f}",
            "remaining_due_bdt": f"{due_rec.remaining_due_bdt:.2f}",
            "status": due_rec.status,
            "allocations": history,
        }
