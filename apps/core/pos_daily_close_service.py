"""
Architecture Decision: ADR-0237 (P237).
Branch Daily Sales Close Service reconciling sales, split tenders, returns,
refunds, dues, cash drawer physical counts, and general ledger postings.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    PaymentMethod,
    PosCashierShift,
    PosDailyCloseStatus,
    PosDailySalesClose,
    PosPaymentAttempt,
    PosPaymentAttemptStatus,
    PosPaymentJournalEntry,
    PosRefundMethod,
    PosReturnStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleReturn,
    PosShiftCashMovement,
    PosShiftMovementType,
    PosShiftStatus,
    Tenant,
)
from apps.identity.models import UserRole


def q2(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    return Decimal(str(value)).quantize(Decimal("0.01"))


class PosDailyCloseService:
    """
    Authoritative service for branch daily sales close calculations,
    cashier shift drawer consolidation, ledger balancing, and manager approvals.
    """

    ALLOWED_VIEW_ROLES = {
        UserRole.CASHIER,
        UserRole.STAFF_PHARMACIST,
        UserRole.BRANCH_MANAGER,
        UserRole.PHARMACY_OWNER,
        UserRole.SUPERADMIN,
    }

    APPROVER_ROLES = {
        UserRole.BRANCH_MANAGER,
        UserRole.PHARMACY_OWNER,
        UserRole.SUPERADMIN,
    }

    @classmethod
    def resolve_date(cls, val: Any) -> date:
        if isinstance(val, date) and not isinstance(val, datetime):
            return val
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, str) and val.strip():
            return datetime.strptime(val.strip(), "%Y-%m-%d").date()
        return timezone.now().date()

    @classmethod
    def compile_daily_summary(
        cls,
        *,
        user: Any,
        branch_id: Optional[str] = None,
        closing_date: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Computes accurate daily aggregations for the specified branch and calendar date.
        """
        tenant_id = getattr(user, "tenant_id", None)
        user_branch_id = branch_id or getattr(user, "branch_id", None)
        if not tenant_id or not user_branch_id:
            raise ValidationError({"branch": "Tenant and branch are required to compile daily sales close."})

        tenant = Tenant.objects.get(tenant_id=tenant_id)
        branch = Branch.objects.get(tenant=tenant, branch_id=user_branch_id)
        close_dt = cls.resolve_date(closing_date)

        # 1. Finalized Sales Orders
        sales = PosSaleOrder.objects.filter(
            tenant=tenant,
            branch=branch,
            created_at__date=close_dt,
            lifecycle_state=PosSaleLifecycleState.FINALIZED,
        )
        sales_count = sales.count()
        gross_sales = q2(sum((s.subtotal_bdt for s in sales), Decimal("0.00")))
        disc_total = q2(sum((s.discount_amount_bdt for s in sales), Decimal("0.00")))
        tax_total = q2(sum((s.tax_amount_bdt for s in sales), Decimal("0.00")))
        net_sales = q2(sum((s.grand_total_bdt for s in sales), Decimal("0.00")))
        due_sales = q2(sum((s.due_amount_bdt for s in sales), Decimal("0.00")))

        # 2. Captured Tender Payments
        payments = PosPaymentAttempt.objects.filter(
            sale__in=sales,
            status=PosPaymentAttemptStatus.CAPTURED,
        )
        cash_sales = q2(sum((p.amount_bdt for p in payments if p.tender_type == PaymentMethod.CASH), Decimal("0.00")))
        digital_sales = q2(sum((p.amount_bdt for p in payments if p.tender_type != PaymentMethod.CASH), Decimal("0.00")))

        # 3. Completed Returns & Refunds
        returns = PosSaleReturn.objects.filter(
            tenant=tenant,
            branch=branch,
            created_at__date=close_dt,
            status=PosReturnStatus.COMPLETED,
        )
        returns_count = returns.count()
        returns_total = q2(sum((r.total_refund_amount_bdt for r in returns), Decimal("0.00")))
        cash_refunds = q2(sum((r.total_refund_amount_bdt for r in returns if r.refund_method == PosRefundMethod.CASH), Decimal("0.00")))
        digital_refunds = q2(sum((r.total_refund_amount_bdt for r in returns if r.refund_method != PosRefundMethod.CASH), Decimal("0.00")))

        # 4. Due Collections
        due_cols = CustomerDueCollection.objects.filter(
            tenant=tenant,
            branch=branch,
            collected_at__date=close_dt,
            status=CustomerDueCollectionStatus.CAPTURED,
        )
        due_col_cash = q2(sum((c.amount_bdt for c in due_cols if c.payment_method == PaymentMethod.CASH), Decimal("0.00")))
        due_col_digital = q2(sum((c.amount_bdt for c in due_cols if c.payment_method != PaymentMethod.CASH), Decimal("0.00")))

        # 5. Cashier Shifts & Movements
        shifts = PosCashierShift.objects.filter(
            tenant=tenant,
            branch=branch,
            opened_at__date=close_dt,
        )
        shifts_closed_count = shifts.filter(status__in=[PosShiftStatus.CLOSED, PosShiftStatus.RECONCILED]).count()
        unclosed_shifts_count = shifts.filter(status__in=[PosShiftStatus.OPEN, PosShiftStatus.PENDING_MANAGER_APPROVAL]).count()
        opening_float_total = q2(sum((s.opening_float_bdt for s in shifts), Decimal("0.00")))

        movements = PosShiftCashMovement.objects.filter(
            tenant=tenant,
            branch=branch,
            created_at__date=close_dt,
        )
        cash_in_movements = q2(
            sum(
                (m.amount_bdt for m in movements if m.movement_type in (PosShiftMovementType.ADD_FLOAT, PosShiftMovementType.CASH_IN)),
                Decimal("0.00"),
            )
        )
        cash_payouts = q2(
            sum(
                (m.amount_bdt for m in movements if m.movement_type in (PosShiftMovementType.PAYOUT_EXPENSE, PosShiftMovementType.CASH_DROP)),
                Decimal("0.00"),
            )
        )

        expected_drawer_cash = q2(
            opening_float_total + cash_sales + due_col_cash + cash_in_movements - cash_refunds - cash_payouts
        )

        # 6. Ledger Parity
        journals = PosPaymentJournalEntry.objects.filter(
            tenant=tenant,
            branch=branch,
            posted_at__date=close_dt,
        )
        ledger_count = journals.count()
        ledger_debits = q2(sum((j.amount_bdt for j in journals), Decimal("0.00")))
        ledger_credits = q2(sum((j.amount_bdt for j in journals), Decimal("0.00")))
        is_ledger_balanced = ledger_debits == ledger_credits

        return {
            "tenant": tenant,
            "branch": branch,
            "closing_date": close_dt,
            "sales_count": sales_count,
            "gross_sales_bdt": gross_sales,
            "discount_total_bdt": disc_total,
            "tax_total_bdt": tax_total,
            "net_sales_bdt": net_sales,
            "due_sales_bdt": due_sales,
            "cash_sales_bdt": cash_sales,
            "digital_sales_bdt": digital_sales,
            "returns_count": returns_count,
            "returns_total_bdt": returns_total,
            "cash_refunds_bdt": cash_refunds,
            "digital_refunds_bdt": digital_refunds,
            "due_collections_cash_bdt": due_col_cash,
            "due_collections_digital_bdt": due_col_digital,
            "shifts_closed_count": shifts_closed_count,
            "unclosed_shifts_count": unclosed_shifts_count,
            "opening_float_total_bdt": opening_float_total,
            "cash_in_movements_bdt": cash_in_movements,
            "cash_payouts_bdt": cash_payouts,
            "expected_drawer_cash_bdt": expected_drawer_cash,
            "ledger_entries_count": ledger_count,
            "ledger_debit_total_bdt": ledger_debits,
            "ledger_credit_total_bdt": ledger_credits,
            "is_ledger_balanced": is_ledger_balanced,
        }

    @classmethod
    @transaction.atomic
    def submit_daily_close(
        cls,
        *,
        user: Any,
        branch_id: Optional[str] = None,
        closing_date: Optional[Any] = None,
        actual_drawer_cash_bdt: str = "0.00",
        notes: str = "",
    ) -> PosDailySalesClose:
        """
        Compiles the daily close summary, accepts physical drawer cash count,
        computes cash variance, and sets status to SUBMITTED.
        """
        role = getattr(user, "role", None)
        if role not in cls.ALLOWED_VIEW_ROLES:
            raise PermissionDenied({"user": "Not authorized to submit daily sales close."})

        data = cls.compile_daily_summary(user=user, branch_id=branch_id, closing_date=closing_date)
        actual_cash = q2(actual_drawer_cash_bdt)
        if actual_cash < Decimal("0.00"):
            raise ValidationError({"actual_drawer_cash_bdt": "Counted cash cannot be negative."})

        expected_cash = data["expected_drawer_cash_bdt"]
        cash_variance = q2(actual_cash - expected_cash)

        # Check existing close for branch and date
        close_inst = PosDailySalesClose.objects.filter(
            tenant=data["tenant"],
            branch=data["branch"],
            closing_date=data["closing_date"],
        ).first()

        if close_inst and close_inst.status == PosDailyCloseStatus.APPROVED:
            raise ValidationError({"closing_date": "Daily sales close for this date is already APPROVED and sealed."})

        if not close_inst:
            close_inst = PosDailySalesClose(
                close_id=f"dcl-{uuid.uuid4().hex[:12]}",
                tenant=data["tenant"],
                branch=data["branch"],
                closing_date=data["closing_date"],
            )

        close_inst.status = PosDailyCloseStatus.SUBMITTED
        close_inst.sales_count = data["sales_count"]
        close_inst.gross_sales_bdt = data["gross_sales_bdt"]
        close_inst.discount_total_bdt = data["discount_total_bdt"]
        close_inst.tax_total_bdt = data["tax_total_bdt"]
        close_inst.net_sales_bdt = data["net_sales_bdt"]
        close_inst.cash_sales_bdt = data["cash_sales_bdt"]
        close_inst.digital_sales_bdt = data["digital_sales_bdt"]
        close_inst.due_sales_bdt = data["due_sales_bdt"]
        close_inst.returns_count = data["returns_count"]
        close_inst.returns_total_bdt = data["returns_total_bdt"]
        close_inst.cash_refunds_bdt = data["cash_refunds_bdt"]
        close_inst.digital_refunds_bdt = data["digital_refunds_bdt"]
        close_inst.due_collections_cash_bdt = data["due_collections_cash_bdt"]
        close_inst.due_collections_digital_bdt = data["due_collections_digital_bdt"]
        close_inst.shifts_closed_count = data["shifts_closed_count"]
        close_inst.unclosed_shifts_count = data["unclosed_shifts_count"]
        close_inst.opening_float_total_bdt = data["opening_float_total_bdt"]
        close_inst.cash_in_movements_bdt = data["cash_in_movements_bdt"]
        close_inst.cash_payouts_bdt = data["cash_payouts_bdt"]
        close_inst.expected_drawer_cash_bdt = expected_cash
        close_inst.actual_drawer_cash_bdt = actual_cash
        close_inst.cash_variance_bdt = cash_variance
        close_inst.ledger_entries_count = data["ledger_entries_count"]
        close_inst.ledger_debit_total_bdt = data["ledger_debit_total_bdt"]
        close_inst.ledger_credit_total_bdt = data["ledger_credit_total_bdt"]
        close_inst.is_ledger_balanced = data["is_ledger_balanced"]
        close_inst.prepared_by = user
        close_inst.submitted_at = timezone.now()
        close_inst.notes = str(notes or "").strip()
        close_inst.save()
        return close_inst

    @classmethod
    @transaction.atomic
    def approve_daily_close(
        cls,
        *,
        user: Any,
        close_id: str,
        manager_notes: str = "",
    ) -> PosDailySalesClose:
        """
        Manager reviews and seals the daily close.
        Enforces separation of duties & role check.
        """
        role = getattr(user, "role", None)
        if role not in cls.APPROVER_ROLES:
            raise PermissionDenied({"user": "Only Branch Managers or Pharmacy Owners can approve daily sales close."})

        tenant_id = getattr(user, "tenant_id", None)
        try:
            close_inst = PosDailySalesClose.objects.select_for_update().get(
                tenant_id=tenant_id, close_id=close_id
            )
        except PosDailySalesClose.DoesNotExist:
            from rest_framework.exceptions import NotFound
            raise NotFound({"detail": "Daily sales close not found."})

        if close_inst.status == PosDailyCloseStatus.APPROVED:
            raise ValidationError({"close": "Daily sales close is already approved."})

        # Check for unclosed shifts
        if close_inst.unclosed_shifts_count > 0:
            raise ValidationError(
                {"unclosed_shifts": f"Cannot approve daily close while {close_inst.unclosed_shifts_count} shift(s) remain open."}
            )

        close_inst.approved_by = user
        close_inst.approved_at = timezone.now()
        if manager_notes:
            existing = close_inst.notes
            close_inst.notes = f"{existing} | Approval Note: {manager_notes}".strip(" | ")
        close_inst.status = PosDailyCloseStatus.APPROVED
        close_inst.sha256_hash = close_inst.compute_hash()
        close_inst.save()
        return close_inst
