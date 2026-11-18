"""
backend/apps/core/customer_receivable_ledger_service.py
Architecture Decision: ADR-0265 (P265).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
- Posting immutable double-entry accounts receivable (AR) ledger entries:
  * DUE_SALE: Initial credit sale due posting (Debit AR asset).
  * PAYMENT_COLLECTION: Customer due repayment (Credit AR asset).
  * SALES_RETURN_CREDIT: Return of goods on credit (Credit AR asset).
  * ADJUSTMENT: Audited balance correction (Debit/Credit).
  * REVERSAL: Compensating reversal posting.
  * WRITE_OFF: Bad debt receivable write-off.
- Atomic concurrency safety with `select_for_update` on PosCustomer.
- Idempotency replay protection for all ledger postings.
- Full reconciliation parity: opening balance + debits - credits == closing balance == customer.current_due_bdt.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueRecord,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    PosCustomer,
    PosSaleOrder,
    PosSaleReturn,
    SaleInvoice,
)
from apps.identity.models import UserRole

MONEY_ZERO = Decimal("0.00")
LEDGER_ADMIN_ROLES = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
}


def q2(val: Any) -> Decimal:
    """Quantize to 2 decimal places strictly."""
    if val is None:
        return MONEY_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"))
    except Exception:
        return MONEY_ZERO


class CustomerReceivableLedgerService:
    """Domain service for managing customer accounts receivable ledger entries."""

    @classmethod
    @transaction.atomic
    def post_entry_in_tx(
        cls,
        *,
        tenant: Any,
        customer: PosCustomer,
        entry_type: str,
        reference_number: str,
        debit_amount_bdt: Decimal = MONEY_ZERO,
        credit_amount_bdt: Decimal = MONEY_ZERO,
        branch: Optional[Any] = None,
        source_sale: Optional[PosSaleOrder] = None,
        source_invoice: Optional[SaleInvoice] = None,
        source_due_record: Optional[CustomerDueRecord] = None,
        source_collection: Optional[CustomerDueCollection] = None,
        source_return: Optional[PosSaleReturn] = None,
        narration: str = "",
        idempotency_key: str = "",
        posted_by: Optional[Any] = None,
        skip_customer_balance_update: bool = False,
    ) -> CustomerReceivableLedgerEntry:
        """
        Atomically records an immutable AR ledger entry and updates the customer's
        `current_due_bdt` running balance (unless skip_customer_balance_update=True).
        """
        tenant_id = getattr(tenant, "tenant_id", tenant)

        # 1. Idempotency Check
        clean_idem = (idempotency_key or "").strip()
        if clean_idem:
            existing = CustomerReceivableLedgerEntry.objects.filter(
                tenant_id=tenant_id,
                idempotency_key=clean_idem,
            ).first()
            if existing:
                return existing

        # 2. Lock customer row for balance integrity
        cust = PosCustomer.objects.select_for_update().get(pk=customer.pk)
        if str(cust.tenant_id) != str(tenant_id):
            raise ValidationError({"customer": "Customer does not belong to active tenant."})

        # 3. Validate amounts and reference
        debit = q2(debit_amount_bdt)
        credit = q2(credit_amount_bdt)
        if debit < MONEY_ZERO or credit < MONEY_ZERO:
            raise ValidationError({"amounts": "Debit and credit amounts cannot be negative."})
        if debit == MONEY_ZERO and credit == MONEY_ZERO:
            raise ValidationError({"amounts": "Either debit or credit amount must be greater than 0.00 BDT."})

        clean_ref = str(reference_number or "").strip()
        if not clean_ref:
            raise ValidationError({"reference_number": "reference_number is required for ledger posting."})

        # 4. Calculate new running balance
        if not skip_customer_balance_update:
            new_balance = max(MONEY_ZERO, q2(cust.current_due_bdt + debit - credit))
            cust.current_due_bdt = new_balance
            cust.save(update_fields=["current_due_bdt", "updated_at"])
        else:
            new_balance = q2(cust.current_due_bdt)

        branch_obj = None
        if branch:
            branch_id = getattr(branch, "branch_id", branch)
            branch_obj = Branch.objects.filter(tenant_id=tenant_id, branch_id=branch_id).first()

        # 5. Create immutable entry
        entry = CustomerReceivableLedgerEntry.objects.create(
            tenant_id=tenant_id,
            customer=cust,
            branch=branch_obj or cust.primary_branch,
            entry_type=entry_type,
            reference_number=clean_ref,
            source_sale=source_sale,
            source_invoice=source_invoice,
            source_due_record=source_due_record,
            source_collection=source_collection,
            source_return=source_return,
            debit_amount_bdt=debit,
            credit_amount_bdt=credit,
            balance_after_bdt=new_balance,
            narration=narration or f"Receivable {entry_type} posting for ref {clean_ref}",
            idempotency_key=clean_idem,
            posted_by=posted_by,
        )
        return entry

    @classmethod
    @transaction.atomic
    def post_adjustment_in_tx(
        cls,
        user: Any,
        customer_id: str,
        *,
        adjustment_direction: str,  # "DEBIT" or "CREDIT"
        amount_bdt: Decimal,
        reason: str,
        branch_id: Optional[str] = None,
        idempotency_key: str = "",
    ) -> CustomerReceivableLedgerEntry:
        """
        Posts an audited manual receivable adjustment.
        Restricted to Owner, Manager, or Finance Officer.
        """
        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in LEDGER_ADMIN_ROLES:
            raise PermissionDenied({
                "error_code": "ERR_PERMISSION_DENIED",
                "message_en": "Only Pharmacy Owner, Branch Manager, or Finance Officer can post receivable adjustments.",
                "message_bn": "শুধুমাত্র ফার্মেসি মালিক, শাখা ব্যবস্থাপক বা ফাইন্যান্স অফিসার রিসিভেবল অ্যাডজাস্টমেন্ট করতে পারেন।",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if hasattr(user_tenant_id, "tenant_id"):
            user_tenant_id = user_tenant_id.tenant_id

        try:
            customer = PosCustomer.objects.select_related("primary_branch").get(
                tenant_id=user_tenant_id,
                customer_id=customer_id,
            )
        except PosCustomer.DoesNotExist:
            raise NotFound({"customer_id": f"Customer '{customer_id}' not found in tenant."})

        amt = q2(amount_bdt)
        if amt <= MONEY_ZERO:
            raise ValidationError({"amount_bdt": "Adjustment amount must be strictly greater than 0.00 BDT."})

        direction = (adjustment_direction or "").strip().upper()
        if direction not in ("DEBIT", "CREDIT"):
            raise ValidationError({"adjustment_direction": "Must be either 'DEBIT' or 'CREDIT'."})

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "A documented reason is required for manual balance adjustment."})

        debit = amt if direction == "DEBIT" else MONEY_ZERO
        credit = amt if direction == "CREDIT" else MONEY_ZERO
        ref_no = f"ADJ-{timezone.now().strftime('%Y%m%d%H%M%S')}"

        return cls.post_entry_in_tx(
            tenant=user_tenant_id,
            customer=customer,
            entry_type=CustomerReceivableEntryType.ADJUSTMENT,
            reference_number=ref_no,
            debit_amount_bdt=debit,
            credit_amount_bdt=credit,
            branch=branch_id or customer.primary_branch_id,
            narration=f"Manual AR Adjustment ({direction}): {clean_reason}",
            idempotency_key=idempotency_key,
            posted_by=user,
        )

    @classmethod
    @transaction.atomic
    def post_reversal_in_tx(
        cls,
        user: Any,
        original_entry_id: str,
        reason: str,
        idempotency_key: str = "",
    ) -> CustomerReceivableLedgerEntry:
        """
        Posts a compensating reversal for an existing receivable ledger entry.
        """
        user_role = str(getattr(user, "role", "")).upper()
        if user_role not in LEDGER_ADMIN_ROLES:
            raise PermissionDenied({
                "error_code": "ERR_PERMISSION_DENIED",
                "message_en": "Only Pharmacy Owner, Branch Manager, or Finance Officer can reverse receivable entries.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if hasattr(user_tenant_id, "tenant_id"):
            user_tenant_id = user_tenant_id.tenant_id

        try:
            orig = CustomerReceivableLedgerEntry.objects.select_related("customer").get(
                tenant_id=user_tenant_id,
                entry_id=original_entry_id,
            )
        except CustomerReceivableLedgerEntry.DoesNotExist:
            raise NotFound({"entry_id": f"Ledger entry '{original_entry_id}' not found in active tenant."})

        if orig.entry_type == CustomerReceivableEntryType.REVERSAL:
            raise ValidationError({"entry_id": "Cannot reverse a reversal entry."})

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "Reason is required to reverse a ledger entry."})

        # Compensating entry: swap debit and credit
        comp_debit = orig.credit_amount_bdt
        comp_credit = orig.debit_amount_bdt
        ref_no = f"REV-{orig.reference_number}"

        return cls.post_entry_in_tx(
            tenant=user_tenant_id,
            customer=orig.customer,
            entry_type=CustomerReceivableEntryType.REVERSAL,
            reference_number=ref_no,
            debit_amount_bdt=comp_debit,
            credit_amount_bdt=comp_credit,
            branch=orig.branch_id,
            source_sale=orig.source_sale,
            source_invoice=orig.source_invoice,
            source_due_record=orig.source_due_record,
            source_collection=orig.source_collection,
            source_return=orig.source_return,
            narration=f"Reversal of {orig.entry_id} ({orig.reference_number}): {clean_reason}",
            idempotency_key=idempotency_key,
            posted_by=user,
        )

    @classmethod
    def get_customer_receivable_statement(
        cls,
        user: Any,
        customer_id: str,
        *,
        branch_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        Retrieves paginated accounts receivable ledger statement with opening balance,
        running debits/credits, and closing parity reconciliation.
        """
        user_tenant_id = getattr(user, "tenant_id", None)
        if hasattr(user_tenant_id, "tenant_id"):
            user_tenant_id = user_tenant_id.tenant_id

        try:
            customer = PosCustomer.objects.select_related("primary_branch").get(
                tenant_id=user_tenant_id,
                customer_id=customer_id,
            )
        except PosCustomer.DoesNotExist:
            raise NotFound({"customer_id": f"Customer '{customer_id}' not found in active tenant."})

        # Pagination validation
        try:
            page = int(page)
            if page < 1:
                raise ValueError
        except (ValueError, TypeError):
            raise ValidationError({"page": "Page must be an integer >= 1."})

        try:
            page_size = int(page_size)
            if page_size < 1 or page_size > 100:
                raise ValueError
        except (ValueError, TypeError):
            raise ValidationError({"page_size": "Page size must be between 1 and 100."})

        # Parse date filters
        start_dt = None
        end_dt = None
        if start_date:
            try:
                parsed_s = datetime.date.fromisoformat(str(start_date).strip())
                start_dt = timezone.make_aware(datetime.datetime.combine(parsed_s, datetime.time.min))
            except (ValueError, TypeError):
                raise ValidationError({"start_date": "Invalid start_date format. Expected YYYY-MM-DD."})

        if end_date:
            try:
                parsed_e = datetime.date.fromisoformat(str(end_date).strip())
                end_dt = timezone.make_aware(datetime.datetime.combine(parsed_e, datetime.time.max))
            except (ValueError, TypeError):
                raise ValidationError({"end_date": "Invalid end_date format. Expected YYYY-MM-DD."})

        if start_dt and end_dt and start_dt > end_dt:
            raise ValidationError({"start_date": "start_date cannot be later than end_date."})

        # Base query
        base_qs = CustomerReceivableLedgerEntry.objects.filter(
            tenant_id=user_tenant_id,
            customer=customer,
        )
        if branch_id:
            base_qs = base_qs.filter(branch_id=branch_id)

        # Opening balance before start_date
        opening_balance = MONEY_ZERO
        if start_dt:
            prior_entries = base_qs.filter(posted_at__lt=start_dt)
            prior_debits = prior_entries.aggregate(s=Sum("debit_amount_bdt"))["s"] or MONEY_ZERO
            prior_credits = prior_entries.aggregate(s=Sum("credit_amount_bdt"))["s"] or MONEY_ZERO
            opening_balance = max(MONEY_ZERO, q2(prior_debits - prior_credits))

        # Filtered period entries
        period_qs = base_qs
        if start_dt:
            period_qs = period_qs.filter(posted_at__gte=start_dt)
        if end_dt:
            period_qs = period_qs.filter(posted_at__lte=end_dt)

        period_debits = period_qs.aggregate(s=Sum("debit_amount_bdt"))["s"] or MONEY_ZERO
        period_credits = period_qs.aggregate(s=Sum("credit_amount_bdt"))["s"] or MONEY_ZERO
        closing_balance = max(MONEY_ZERO, q2(opening_balance + period_debits - period_credits))

        # Check reconciliation parity
        is_parity_reconciled = (not end_dt and not start_dt) and (closing_balance == customer.current_due_bdt)

        # Pagination
        total_count = period_qs.count()
        total_pages = max(1, math.ceil(total_count / page_size)) if total_count > 0 else 1
        start_idx = (page - 1) * page_size
        paginated_entries = period_qs.select_related("branch", "posted_by").order_by("-posted_at")[start_idx : start_idx + page_size]

        results = []
        for e in paginated_entries:
            results.append({
                "entry_id": e.entry_id,
                "entry_type": e.entry_type,
                "reference_number": e.reference_number,
                "branch_id": str(e.branch_id) if e.branch_id else None,
                "branch_name": e.branch.name if e.branch else "",
                "debit_amount_bdt": f"{e.debit_amount_bdt:.2f}",
                "credit_amount_bdt": f"{e.credit_amount_bdt:.2f}",
                "balance_after_bdt": f"{e.balance_after_bdt:.2f}",
                "narration": e.narration,
                "posted_by": e.posted_by.email if e.posted_by else None,
                "posted_at": e.posted_at.isoformat(),
            })

        return {
            "customer": {
                "customer_id": customer.customer_id,
                "full_name": customer.full_name,
                "phone": customer.phone,
                "credit_limit_bdt": f"{customer.credit_limit_bdt:.2f}",
                "current_due_bdt": f"{customer.current_due_bdt:.2f}",
            },
            "statement_summary": {
                "opening_balance_bdt": f"{opening_balance:.2f}",
                "total_debits_bdt": f"{period_debits:.2f}",
                "total_credits_bdt": f"{period_credits:.2f}",
                "closing_balance_bdt": f"{closing_balance:.2f}",
                "is_parity_reconciled": is_parity_reconciled or (closing_balance == customer.current_due_bdt),
            },
            "pagination": {
                "count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1,
            },
            "results": results,
        }
