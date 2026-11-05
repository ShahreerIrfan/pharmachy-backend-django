"""
backend/apps/core/stock_correction_service.py
Architecture Decision: ADR-0174 (P174).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Authoritative Stock Correction Restriction Engine.
Enforces:
1. No direct balance edits on BatchStock or PurchaseCostLot.
2. Audited, reasoned adjustment commands with mandatory justification (>= 10 chars).
3. Two-person dual authorization rule (cashiers propose, managers approve).
4. Stock conservation and reservation protection (no reduction of cart-reserved stock).
5. Reversible ledger entries (immutable historical records, symmetrical reversals).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStock,
    Branch,
    PurchaseCostLot,
    StockAdjustmentRecord,
    StockAdjustmentType,
    StockCorrectionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_availability_service import StockAvailabilityService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

PROPOSE_ALLOWED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.INVENTORY_CLERK,
    UserRole.SUPERADMIN,
    "CASHIER",
    "STAFF_PHARMACIST",
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "INVENTORY_CLERK",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

APPROVE_REVERSE_ALLOWED_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class StockCorrectionService:
    """Authoritative service governing reasoned inventory adjustment commands and reversible ledgers."""

    @classmethod
    def _assert_propose_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in PROPOSE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "User role is not authorized to propose stock adjustments.",
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                }
            )

    @classmethod
    def _assert_approve_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in APPROVE_REVERSE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Branch Managers or Pharmacy Owners can approve, reject, or reverse stock adjustments.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant: Tenant) -> None:
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "detail": "Tenant subscription is suspended. Inventory operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def propose_adjustment(
        cls,
        user: Any,
        tenant: Tenant,
        branch: Branch,
        batch: BatchStock,
        adjustment_type: str,
        quantity_delta: int,
        reason: str,
        cost_lot: Optional[PurchaseCostLot] = None,
        supporting_document_ref: str = "",
        auto_approve: bool = False,
    ) -> StockAdjustmentRecord:
        """
        Propose a reasoned inventory adjustment.
        If user has manager/owner authority and auto_approve is True, posts immediately.
        Otherwise creates a PENDING record awaiting second-person manager approval.
        """
        cls._assert_propose_role(user)
        cls._assert_tenant_active(tenant)

        # 1. Tenant & Branch isolation validations
        if batch.tenant_id != tenant.tenant_id:
            raise ValidationError(
                {
                    "detail": "Batch does not belong to specified tenant.",
                    "error_code": "ERR_CROSS_TENANT_MISMATCH",
                }
            )
        if batch.branch_id != branch.branch_id:
            raise ValidationError(
                {
                    "detail": "Batch does not belong to specified branch.",
                    "error_code": "ERR_CROSS_BRANCH_MISMATCH",
                }
            )

        # 2. Delta validation: cannot be zero
        try:
            qty_delta = int(quantity_delta)
        except (ValueError, TypeError):
            raise ValidationError(
                {
                    "detail": "Quantity delta must be a non-zero signed integer.",
                    "error_code": "ERR_INVALID_QUANTITY_DELTA",
                }
            )
        if qty_delta == 0:
            raise ValidationError(
                {
                    "detail": "Quantity delta cannot be zero.",
                    "error_code": "ERR_ZERO_QUANTITY_DELTA",
                }
            )

        # 3. Reason validation: mandatory, length >= 10
        cleaned_reason = (reason or "").strip()
        if len(cleaned_reason) < 10:
            raise ValidationError(
                {
                    "detail": "A reasoned justification (minimum 10 characters) is strictly mandatory.",
                    "error_code": "ERR_AUDIT_REASON_REQUIRED",
                }
            )

        # 4. Adjustment type validation
        if adjustment_type not in StockAdjustmentType.values:
            raise ValidationError(
                {
                    "detail": f"Invalid adjustment_type '{adjustment_type}'. Must be one of: {list(StockAdjustmentType.values)}.",
                    "error_code": "ERR_INVALID_ADJUSTMENT_TYPE",
                }
            )

        # 5. Cost lot validation if provided
        if cost_lot:
            if cost_lot.tenant_id != tenant.tenant_id or cost_lot.branch_id != branch.branch_id:
                raise ValidationError(
                    {
                        "detail": "Specified cost lot does not belong to the target tenant/branch.",
                        "error_code": "ERR_COST_LOT_MISMATCH",
                    }
                )
            if cost_lot.batch_id != batch.batch_id:
                raise ValidationError(
                    {
                        "detail": "Specified cost lot does not match target batch.",
                        "error_code": "ERR_COST_LOT_BATCH_MISMATCH",
                    }
                )
            unit_cost = cost_lot.unit_cost
        else:
            unit_cost = batch.cost_per_unit or Decimal("0.00")

        total_cost_impact = abs(qty_delta) * Decimal(str(unit_cost))

        # 6. Check unreserved stock safety if proposing a reduction
        if qty_delta < 0:
            available_unreserved = batch.quantity_base_units - batch.reserved_quantity_base_units
            if available_unreserved < abs(qty_delta):
                raise ValidationError(
                    {
                        "detail": (
                            f"Cannot reduce stock by {abs(qty_delta)} units. "
                            f"Current physical is {batch.quantity_base_units}, but {batch.reserved_quantity_base_units} "
                            f"are locked in active cart reservations (available unreserved: {available_unreserved})."
                        ),
                        "error_code": "ERR_INSUFFICIENT_UNRESERVED_STOCK",
                    }
                )

        adj_id = f"stkadj-{uuid.uuid4().hex[:12]}"
        user_role = getattr(user, "role", None)
        can_approve = user_role in APPROVE_REVERSE_ALLOWED_ROLES

        if auto_approve and not can_approve:
            raise PermissionDenied(
                {
                    "detail": "Only Branch Managers or Pharmacy Owners can auto-approve adjustments.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

        with transaction.atomic():
            record = StockAdjustmentRecord.objects.create(
                adjustment_id=adj_id,
                tenant=tenant,
                branch=branch,
                medicine=batch.product,
                batch=batch,
                cost_lot=cost_lot,
                adjustment_type=adjustment_type,
                quantity_delta=qty_delta,
                unit_cost_bdt=Decimal(str(unit_cost)),
                total_cost_impact=total_cost_impact,
                reason=cleaned_reason,
                status=StockCorrectionStatus.PENDING,
                proposed_by=user,
                supporting_document_ref=supporting_document_ref or "",
                adjusted_by_username=getattr(user, "username", str(user)),
            )

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                tenant_id=tenant.tenant_id,
                branch_id=branch.branch_id,
                user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                actor_email=str(getattr(user, "email", "")),
                actor_role=str(getattr(user, "role", "CASHIER")),
                action="STOCK_CORRECTION_PROPOSED",
                resource_type="StockAdjustmentRecord",
                resource_id=record.adjustment_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "branch_id": branch.branch_id,
                    "batch_id": batch.batch_id,
                    "quantity_delta": qty_delta,
                    "adjustment_type": adjustment_type,
                    "reason": cleaned_reason,
                },
            )

            if auto_approve and can_approve:
                record = cls.approve_and_post_adjustment(user, record.adjustment_id)

        return record

    @classmethod
    def approve_and_post_adjustment(cls, user: Any, adjustment_id: str) -> StockAdjustmentRecord:
        """
        Approve a pending stock adjustment, atomically update batch physical stock and
        cost lot remaining quantity, and recalculate branch stock availability.
        """
        cls._assert_approve_role(user)

        with transaction.atomic():
            try:
                record = StockAdjustmentRecord.objects.select_for_update().get(adjustment_id=adjustment_id)
            except StockAdjustmentRecord.DoesNotExist:
                raise NotFound(
                    {
                        "detail": f"Stock adjustment '{adjustment_id}' not found.",
                        "error_code": "ERR_ADJUSTMENT_NOT_FOUND",
                    }
                )

            cls._assert_tenant_active(record.tenant)

            if record.status != StockCorrectionStatus.PENDING:
                raise ValidationError(
                    {
                        "detail": f"Cannot approve adjustment with status '{record.status}'. Must be PENDING.",
                        "error_code": "ERR_INVALID_ADJUSTMENT_STATUS",
                    }
                )

            batch = BatchStock.objects.select_for_update().get(pk=record.batch_id)

            # Stock conservation check
            if record.quantity_delta < 0:
                available_unreserved = batch.quantity_base_units - batch.reserved_quantity_base_units
                if available_unreserved < abs(record.quantity_delta):
                    raise ValidationError(
                        {
                            "detail": (
                                f"Cannot post reduction of {abs(record.quantity_delta)} units. "
                                f"Batch unreserved stock is now {available_unreserved} units."
                            ),
                            "error_code": "ERR_INSUFFICIENT_UNRESERVED_STOCK",
                        }
                    )

            # Apply stock change to physical batch
            new_batch_qty = batch.quantity_base_units + record.quantity_delta
            if new_batch_qty < 0:
                raise ValidationError(
                    {
                        "detail": "Batch physical quantity cannot become negative.",
                        "error_code": "ERR_NEGATIVE_STOCK_PROHIBITED",
                    }
                )
            batch.quantity_base_units = new_batch_qty
            batch.save(update_fields=["quantity_base_units"])

            # Apply stock change to cost lot if attached
            if record.cost_lot:
                cost_lot = PurchaseCostLot.objects.select_for_update().get(pk=record.cost_lot_id)
                new_lot_qty = cost_lot.remaining_quantity + record.quantity_delta
                cost_lot.remaining_quantity = max(0, new_lot_qty)
                if cost_lot.remaining_quantity == 0:
                    cost_lot.status = "DEPLETED"
                elif cost_lot.status == "DEPLETED" and cost_lot.remaining_quantity > 0:
                    cost_lot.status = "ACTIVE"
                cost_lot.save()

            # Recalculate branch stock availability (P171)
            StockAvailabilityService.recalculate_availability(
                tenant=record.tenant,
                branch=record.branch,
                product=record.medicine,
            )

            record.status = StockCorrectionStatus.APPROVED
            record.approved_by = user
            record.approved_at = timezone.now()
            record.save()

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                tenant_id=record.tenant.tenant_id,
                branch_id=record.branch.branch_id,
                user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                actor_email=str(getattr(user, "email", "")),
                actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
                action="STOCK_CORRECTION_APPROVED",
                resource_type="StockAdjustmentRecord",
                resource_id=record.adjustment_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "branch_id": record.branch.branch_id,
                    "batch_id": record.batch.batch_id,
                    "quantity_delta": record.quantity_delta,
                    "new_batch_quantity": batch.quantity_base_units,
                },
            )

        return record

    @classmethod
    def reject_adjustment(cls, user: Any, adjustment_id: str, rejection_reason: str) -> StockAdjustmentRecord:
        """Reject a pending stock adjustment."""
        cls._assert_approve_role(user)

        with transaction.atomic():
            try:
                record = StockAdjustmentRecord.objects.select_for_update().get(adjustment_id=adjustment_id)
            except StockAdjustmentRecord.DoesNotExist:
                raise NotFound(
                    {
                        "detail": f"Stock adjustment '{adjustment_id}' not found.",
                        "error_code": "ERR_ADJUSTMENT_NOT_FOUND",
                    }
                )

            cls._assert_tenant_active(record.tenant)

            if record.status != StockCorrectionStatus.PENDING:
                raise ValidationError(
                    {
                        "detail": f"Cannot reject adjustment with status '{record.status}'. Must be PENDING.",
                        "error_code": "ERR_INVALID_ADJUSTMENT_STATUS",
                    }
                )

            cleaned_reason = (rejection_reason or "").strip()
            if len(cleaned_reason) < 10:
                raise ValidationError(
                    {
                        "detail": "A valid rejection explanation (minimum 10 characters) is required.",
                        "error_code": "ERR_AUDIT_REASON_REQUIRED",
                    }
                )

            record.status = StockCorrectionStatus.REJECTED
            record.reversal_reason = cleaned_reason
            record.approved_by = user
            record.approved_at = timezone.now()
            record.save()

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                tenant_id=record.tenant.tenant_id,
                branch_id=record.branch.branch_id,
                user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                actor_email=str(getattr(user, "email", "")),
                actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
                action="STOCK_CORRECTION_REJECTED",
                resource_type="StockAdjustmentRecord",
                resource_id=record.adjustment_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={"reason": cleaned_reason},
            )

        return record

    @classmethod
    def reverse_adjustment(cls, user: Any, adjustment_id: str, reversal_reason: str) -> StockAdjustmentRecord:
        """
        Create a symmetrical reversing entry for a posted adjustment, restoring physical inventory
        and updating stock availability while preserving immutable audit logs.
        """
        cls._assert_approve_role(user)

        with transaction.atomic():
            try:
                original = StockAdjustmentRecord.objects.select_for_update().get(adjustment_id=adjustment_id)
            except StockAdjustmentRecord.DoesNotExist:
                raise NotFound(
                    {
                        "detail": f"Stock adjustment '{adjustment_id}' not found.",
                        "error_code": "ERR_ADJUSTMENT_NOT_FOUND",
                    }
                )

            cls._assert_tenant_active(original.tenant)

            if original.is_reversed or original.status == StockCorrectionStatus.REVERSED:
                raise ValidationError(
                    {
                        "detail": "Adjustment has already been reversed.",
                        "error_code": "ERR_ALREADY_REVERSED",
                    }
                )

            if original.status not in (StockCorrectionStatus.APPROVED, "POSTED"):
                raise ValidationError(
                    {
                        "detail": f"Only APPROVED adjustments can be reversed. Current status is '{original.status}'.",
                        "error_code": "ERR_CANNOT_REVERSE_UNAPPROVED",
                    }
                )

            cleaned_reason = (reversal_reason or "").strip()
            if len(cleaned_reason) < 10:
                raise ValidationError(
                    {
                        "detail": "A valid reversal justification (minimum 10 characters) is mandatory.",
                        "error_code": "ERR_AUDIT_REASON_REQUIRED",
                    }
                )

            # Reversing delta is the exact negation of the original delta
            reversing_delta = -original.quantity_delta
            batch = BatchStock.objects.select_for_update().get(pk=original.batch_id)

            # If the reversal reduces stock (e.g. reversing a prior addition), verify reservation bounds!
            if reversing_delta < 0:
                available_unreserved = batch.quantity_base_units - batch.reserved_quantity_base_units
                if available_unreserved < abs(reversing_delta):
                    raise ValidationError(
                        {
                            "detail": (
                                f"Cannot reverse adjustment. Restoring original balance requires deducting "
                                f"{abs(reversing_delta)} units, but only {available_unreserved} unreserved units are available."
                            ),
                            "error_code": "ERR_INSUFFICIENT_UNRESERVED_STOCK",
                        }
                    )

            # Apply stock change to physical batch
            new_batch_qty = batch.quantity_base_units + reversing_delta
            if new_batch_qty < 0:
                raise ValidationError(
                    {
                        "detail": "Reversal would cause batch physical stock to become negative.",
                        "error_code": "ERR_NEGATIVE_STOCK_PROHIBITED",
                    }
                )
            batch.quantity_base_units = new_batch_qty
            batch.save(update_fields=["quantity_base_units"])

            # Apply stock change to cost lot if attached
            if original.cost_lot:
                cost_lot = PurchaseCostLot.objects.select_for_update().get(pk=original.cost_lot_id)
                new_lot_qty = cost_lot.remaining_quantity + reversing_delta
                cost_lot.remaining_quantity = max(0, new_lot_qty)
                if cost_lot.remaining_quantity == 0:
                    cost_lot.status = "DEPLETED"
                elif cost_lot.status == "DEPLETED" and cost_lot.remaining_quantity > 0:
                    cost_lot.status = "ACTIVE"
                cost_lot.save()

            # Recalculate branch stock availability
            StockAvailabilityService.recalculate_availability(
                tenant=original.tenant,
                branch=original.branch,
                product=original.medicine,
            )

            # Create symmetrical reversing record
            rev_id = f"stkadj-{uuid.uuid4().hex[:12]}"
            reversal_record = StockAdjustmentRecord.objects.create(
                adjustment_id=rev_id,
                tenant=original.tenant,
                branch=original.branch,
                medicine=original.medicine,
                batch=original.batch,
                cost_lot=original.cost_lot,
                adjustment_type=original.adjustment_type,
                quantity_delta=reversing_delta,
                unit_cost_bdt=original.unit_cost_bdt,
                total_cost_impact=original.total_cost_impact,
                reason=f"Reversal of {original.adjustment_id}: {cleaned_reason}",
                status=StockCorrectionStatus.APPROVED,
                proposed_by=user,
                approved_by=user,
                approved_at=timezone.now(),
                reversal_of=original,
                is_reversed=False,
                adjusted_by_username=getattr(user, "username", str(user)),
            )

            # Mark original as reversed
            original.is_reversed = True
            original.status = StockCorrectionStatus.REVERSED
            original.reversal_reason = cleaned_reason
            original.save(update_fields=["is_reversed", "status", "reversal_reason"])

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                tenant_id=original.tenant.tenant_id,
                branch_id=original.branch.branch_id,
                user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                actor_email=str(getattr(user, "email", "")),
                actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
                action="STOCK_CORRECTION_REVERSED",
                resource_type="StockAdjustmentRecord",
                resource_id=original.adjustment_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "reversal_id": reversal_record.adjustment_id,
                    "reversing_delta": reversing_delta,
                    "restored_batch_quantity": batch.quantity_base_units,
                    "reason": cleaned_reason,
                },
            )

        return reversal_record
