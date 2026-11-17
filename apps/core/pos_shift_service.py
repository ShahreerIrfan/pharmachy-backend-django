"""
Architecture Decision: ADR-0233 (P233).
POS Cashier Shift Service managing shift open-close, drawer balance,
mid-shift payouts, physical cash counting, and variance approvals.
"""

from decimal import Decimal
from typing import Any, Dict, Optional
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    Branch,
    PosCashierShift,
    PosShiftCashMovement,
    PosShiftMovementType,
    PosShiftStatus,
    Tenant,
)
from apps.identity.models import UserRole


class PosShiftService:
    """
    Core business logic for POS cashier shifts, drawer cash management,
    payouts, and manager close variance reconciliation.
    """

    ALLOWED_SHIFT_ROLES = {
        UserRole.CASHIER,
        UserRole.STAFF_PHARMACIST,
        UserRole.BRANCH_MANAGER,
        UserRole.PHARMACY_OWNER,
        UserRole.SUPERADMIN,
    }

    MANAGER_APPROVAL_ROLES = {
        UserRole.BRANCH_MANAGER,
        UserRole.PHARMACY_OWNER,
        UserRole.SUPERADMIN,
    }

    @classmethod
    @transaction.atomic
    def open_shift(
        cls,
        *,
        user: Any,
        branch_id: Optional[str] = None,
        device_code: str = "DEFAULT",
        opening_float_bdt: str = "0.00",
    ) -> PosCashierShift:
        """
        Opens a new cashier shift declaring starting drawer cash (float).
        Enforces one open shift per cashier/device rule.
        """
        role = getattr(user, "role", None)
        if role not in cls.ALLOWED_SHIFT_ROLES:
            raise ValidationError({"user": "User role is not authorized to open POS shifts."})

        tenant_id = getattr(user, "tenant_id", None)
        user_branch_id = branch_id or getattr(user, "branch_id", None)
        if not tenant_id or not user_branch_id:
            raise ValidationError({"branch": "Tenant and branch are required to open a POS shift."})

        tenant = Tenant.objects.get(tenant_id=tenant_id)
        branch = Branch.objects.get(tenant=tenant, branch_id=user_branch_id)

        device = str(device_code or "DEFAULT").strip().upper()

        # Check existing open shift for this cashier
        active_shift = PosCashierShift.objects.filter(
            tenant=tenant,
            branch=branch,
            cashier_user=user,
            status=PosShiftStatus.OPEN,
        ).first()

        if active_shift:
            raise ValidationError(
                {"shift": f"Cashier already has an active open shift ({active_shift.shift_number}). Close it before opening a new one."}
            )

        float_val = Decimal(str(opening_float_bdt or "0.00")).quantize(Decimal("0.01"))
        if float_val < Decimal("0.00"):
            raise ValidationError({"opening_float_bdt": "Opening float cannot be negative."})

        date_str = timezone.now().strftime("%Y%m%d")
        br_code = getattr(branch, "branch_code", "BR01") or "BR01"
        shift_num = f"SHF-{br_code}-{date_str}-{uuid.uuid4().hex[:4].upper()}"

        shift = PosCashierShift.objects.create(
            shift_id=f"shf-{uuid.uuid4().hex[:12]}",
            shift_number=shift_num,
            tenant=tenant,
            branch=branch,
            cashier_user=user,
            device_code=device,
            status=PosShiftStatus.OPEN,
            opening_float_bdt=float_val,
            expected_drawer_cash_bdt=float_val,
            opened_at=timezone.now(),
        )
        return shift

    @classmethod
    @transaction.atomic
    def record_cash_movement(
        cls,
        *,
        user: Any,
        shift_id: str,
        movement_type: str,
        amount_bdt: str,
        reason: str,
        recipient_or_source: str = "",
    ) -> PosShiftCashMovement:
        """
        Records a mid-shift drawer cash movement (payout expense, cash drop, float addition, cash in).
        """
        tenant_id = getattr(user, "tenant_id", None)
        shift = PosCashierShift.objects.select_for_update().get(
            tenant_id=tenant_id, shift_id=shift_id
        )

        if shift.status != PosShiftStatus.OPEN:
            raise ValidationError({"shift": "Cannot record cash movement on a closed or reconciled shift."})

        amt = Decimal(str(amount_bdt or "0.00")).quantize(Decimal("0.01"))
        if amt <= Decimal("0.00"):
            raise ValidationError({"amount_bdt": "Cash movement amount must be greater than zero."})

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError({"reason": "Reason is mandatory for drawer cash movements."})

        # Validate movement type
        if movement_type not in PosShiftMovementType.values:
            raise ValidationError({"movement_type": f"Invalid movement type: {movement_type}."})

        # Check sufficiency if cash is leaving the drawer
        if movement_type in (PosShiftMovementType.PAYOUT_EXPENSE, PosShiftMovementType.CASH_DROP):
            if amt > shift.expected_drawer_cash_bdt:
                raise ValidationError(
                    {"amount_bdt": f"Insufficient drawer cash ({shift.expected_drawer_cash_bdt} BDT) for withdrawal of {amt} BDT."}
                )
            shift.total_payouts_bdt = (shift.total_payouts_bdt + amt).quantize(Decimal("0.01"))
        else:
            shift.total_cash_in_bdt = (shift.total_cash_in_bdt + amt).quantize(Decimal("0.01"))

        shift.expected_drawer_cash_bdt = shift.calculate_expected_drawer()
        shift.save(update_fields=["total_payouts_bdt", "total_cash_in_bdt", "expected_drawer_cash_bdt"])

        movement = PosShiftCashMovement.objects.create(
            movement_id=f"shm-{uuid.uuid4().hex[:12]}",
            tenant=shift.tenant,
            branch=shift.branch,
            shift=shift,
            movement_type=movement_type,
            amount_bdt=amt,
            reason=clean_reason,
            recipient_or_source=str(recipient_or_source or "").strip(),
            created_by=user,
            created_at=timezone.now(),
        )
        return movement

    @classmethod
    @transaction.atomic
    def close_shift(
        cls,
        *,
        user: Any,
        shift_id: str,
        closing_cash_counted_bdt: str,
        variance_reason: str = "",
    ) -> PosCashierShift:
        """
        Closes an open shift comparing physical cash against expected drawer cash.
        If variance is non-zero, flags shift for manager approval.
        """
        tenant_id = getattr(user, "tenant_id", None)
        shift = PosCashierShift.objects.select_for_update().get(
            tenant_id=tenant_id, shift_id=shift_id
        )

        if shift.status != PosShiftStatus.OPEN:
            raise ValidationError({"shift": f"Shift is not open (current status: {shift.status})."})

        # Only the shift cashier or branch manager/owner can close the shift
        user_role = getattr(user, "role", None)
        if shift.cashier_user_id != user.pk and user_role not in cls.MANAGER_APPROVAL_ROLES:
            raise ValidationError({"user": "You are not authorized to close another cashier's shift."})

        counted = Decimal(str(closing_cash_counted_bdt or "0.00")).quantize(Decimal("0.01"))
        if counted < Decimal("0.00"):
            raise ValidationError({"closing_cash_counted_bdt": "Counted cash cannot be negative."})

        expected = shift.calculate_expected_drawer()
        shift.expected_drawer_cash_bdt = expected
        shift.closing_cash_counted_bdt = counted
        variance = (counted - expected).quantize(Decimal("0.01"))
        shift.variance_bdt = variance

        clean_reason = str(variance_reason or "").strip()
        if variance != Decimal("0.00"):
            if not clean_reason:
                raise ValidationError(
                    {"variance_reason": "Variance explanation is mandatory when counted cash does not match expected drawer."}
                )
            shift.variance_reason = clean_reason
            shift.requires_manager_approval = True
            shift.status = PosShiftStatus.PENDING_MANAGER_APPROVAL
        else:
            shift.variance_reason = clean_reason
            shift.requires_manager_approval = False
            shift.status = PosShiftStatus.CLOSED

        shift.closed_at = timezone.now()
        shift.save()
        return shift

    @classmethod
    @transaction.atomic
    def approve_variance(
        cls,
        *,
        user: Any,
        shift_id: str,
        manager_notes: str = "",
    ) -> PosCashierShift:
        """
        Manager reviews and reconciles shift close variance.
        Enforces separation of duties: cashier cannot approve their own shift variance.
        """
        role = getattr(user, "role", None)
        if role not in cls.MANAGER_APPROVAL_ROLES:
            raise ValidationError({"user": "Only Branch Managers or Pharmacy Owners can approve shift variances."})

        tenant_id = getattr(user, "tenant_id", None)
        shift = PosCashierShift.objects.select_for_update().get(
            tenant_id=tenant_id, shift_id=shift_id
        )

        if shift.status not in (PosShiftStatus.PENDING_MANAGER_APPROVAL, PosShiftStatus.CLOSED):
            raise ValidationError({"shift": f"Shift is not eligible for variance approval (current status: {shift.status})."})

        # Separation of duties invariant
        if shift.cashier_user_id == user.pk and role != UserRole.PHARMACY_OWNER:
            raise ValidationError({"user": "Separation of duties violation: Cashier cannot approve their own shift variance."})

        shift.approved_by = user
        shift.manager_approval_notes = str(manager_notes or "").strip()
        shift.status = PosShiftStatus.RECONCILED
        shift.reconciled_at = timezone.now()
        shift.save()
        return shift
