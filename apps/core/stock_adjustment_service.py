"""
backend/apps/core/stock_adjustment_service.py
Architecture Decision: ADR-0205 (P205).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Business Service for Nontrivial Inventory Adjustment Approval Flow:
- Nontrivial adjustment threshold governance (quantity >= 10, cost >= 500 BDT, or controlled/cold chain).
- Mandatory documented reason codes and detailed explanations.
- Strict Maker-Checker anti-self-approval rule (Maker != Checker).
- Immutable difference events with SHA-256 cryptographic hashes.
- Audited reversal workflow.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStock,
    Branch,
    MedicineProduct,
    StockAdjustmentDifferenceEvent,
    StockAdjustmentReasonCode,
    StockAdjustmentRecord,
    StockAdjustmentType,
    StockCorrectionStatus,
    StockMovementType,
    Tenant,
)
from apps.core.stock_ledger_service import StockLedgerService

logger = logging.getLogger(__name__)


class StockAdjustmentService:
    """
    Core business service for proposed, reviewed, approved, and reversed
    inventory adjustments with immutable difference events.
    """

    NONTRIVIAL_QTY_THRESHOLD = 10
    NONTRIVIAL_COST_THRESHOLD = Decimal("500.0000")

    _PROPOSER_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "STAFF_PHARMACIST",
        "PHARMACIST",
        "INVENTORY_CLERK",
        "TENANT_OWNER",
    }

    _APPROVER_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "PLATFORM_SUPER_ADMIN",
        "TENANT_OWNER",
    }

    @classmethod
    def assert_proposer_role(cls, actor_user: Any) -> None:
        """Enforces that Cashiers cannot propose stock adjustments."""
        if not actor_user:
            return
        role = getattr(actor_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str == "CASHIER" or (role_str and role_str not in cls._PROPOSER_ROLES):
            raise PermissionDenied(
                "Cashiers are not authorized to propose inventory stock adjustments (ERR_STAFF_ROLE_DENIED)."
            )

    @classmethod
    def assert_approver_role(cls, approver_user: Any) -> None:
        """Enforces that only managers or owners can approve adjustments."""
        if not approver_user or not getattr(approver_user, "is_authenticated", False):
            raise PermissionDenied("Approver must be an authenticated manager or owner.")
        role = getattr(approver_user, "role", None)
        role_str = str(role).upper() if role else ""
        if role_str not in cls._APPROVER_ROLES:
            raise PermissionDenied(
                f"Role '{role_str}' is not authorized to approve inventory adjustments (ERR_APPROVER_ROLE_DENIED)."
            )

    @classmethod
    def is_nontrivial_adjustment(
        cls, quantity_delta: int, total_cost: Decimal, product: MedicineProduct, batch: Optional[BatchStock] = None
    ) -> bool:
        """
        Determines if an adjustment is nontrivial:
        - Absolute quantity >= 10
        - Absolute cost impact >= 500 BDT
        - Product is a narcotic / controlled substance
        - Product requires cold chain
        - Batch is quarantined
        """
        abs_qty = abs(quantity_delta)
        abs_cost = abs(total_cost)

        if abs_qty >= cls.NONTRIVIAL_QTY_THRESHOLD:
            return True
        if abs_cost >= cls.NONTRIVIAL_COST_THRESHOLD:
            return True
        if getattr(product, "is_narcotic", False):
            return True
        if getattr(product, "requires_cold_chain", False):
            return True
        if batch and (getattr(batch, "is_quarantined", False) or getattr(batch, "status", "") == "QUARANTINED"):
            return True
        return False

    @classmethod
    @transaction.atomic
    def propose_adjustment(
        cls,
        tenant_id: str,
        branch_id: str,
        batch_id: str,
        quantity_delta: int,
        reason_code: str,
        reason_notes: str,
        supporting_document_ref: str = "",
        actor_user: Any = None,
    ) -> StockAdjustmentRecord:
        """
        Proposes an inventory stock adjustment. Requires documented reason code and explanation.
        """
        cls.assert_proposer_role(actor_user)

        if quantity_delta == 0:
            raise ValidationError({"quantity_delta": "Stock adjustment quantity delta cannot be zero (ERR_ZERO_DELTA)."})

        clean_notes = reason_notes.strip()
        if len(clean_notes) < 10:
            raise ValidationError(
                {"reason": "Documented reason must be provided with specific explanation of at least 10 characters (ERR_MANDATORY_REASON_REQUIRED)."}
            )

        if reason_code not in StockAdjustmentReasonCode.values:
            raise ValidationError({"reason_code": f"Invalid reason code '{reason_code}'."})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound(f"Tenant '{tenant_id}' not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound(f"Branch '{branch_id}' not found under tenant '{tenant_id}'.")

        batch = (
            BatchStock.objects.select_for_update()
            .filter(tenant=tenant, branch=branch, batch_id=batch_id)
            .select_related("product")
            .first()
        )
        if not batch:
            raise NotFound(f"Batch '{batch_id}' not found in branch inventory.")

        # If reducing stock, cannot reduce below 0
        if quantity_delta < 0 and (batch.quantity_base_units + quantity_delta) < 0:
            raise ValidationError(
                {"quantity_delta": f"Cannot reduce {abs(quantity_delta)} units. Batch only has {batch.quantity_base_units} available (ERR_INSUFFICIENT_STOCK)."}
            )

        product = batch.product
        unit_cost = batch.cost_per_unit
        total_cost = (Decimal(str(abs(quantity_delta))) * unit_cost).quantize(Decimal("0.0001"))

        is_nontrivial = cls.is_nontrivial_adjustment(quantity_delta, total_cost, product, batch)

        # Map adjustment type
        if quantity_delta > 0:
            adj_type = StockAdjustmentType.FOUND_SURPLUS
        else:
            if reason_code == StockAdjustmentReasonCode.BREAKAGE_SPILLAGE:
                adj_type = StockAdjustmentType.DAMAGE
            elif reason_code == StockAdjustmentReasonCode.EXPIRY_SPOILAGE:
                adj_type = StockAdjustmentType.EXPIRY_REMOVAL
            elif reason_code == StockAdjustmentReasonCode.THEFT_PILFERAGE:
                adj_type = StockAdjustmentType.THEFT_LOSS
            else:
                adj_type = StockAdjustmentType.COUNT_CORRECTION

        adjustment = StockAdjustmentRecord.objects.create(
            adjustment_id=f"adj-{uuid.uuid4().hex[:10]}",
            tenant=tenant,
            branch=branch,
            medicine=product,
            batch=batch,
            adjustment_type=adj_type,
            quantity_delta=quantity_delta,
            unit_cost_bdt=unit_cost,
            total_cost_impact=total_cost,
            reason=clean_notes,
            reason_code=reason_code,
            is_nontrivial=is_nontrivial,
            status=StockCorrectionStatus.PENDING,
            proposed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            supporting_document_ref=supporting_document_ref.strip(),
            adjusted_by_username=getattr(actor_user, "username", "") or getattr(actor_user, "email", "system"),
        )
        return adjustment

    @classmethod
    @transaction.atomic
    def approve_and_post_adjustment(
        cls,
        adjustment_id: str,
        tenant_id: str,
        checker_user: Any,
        checker_notes: str = "",
    ) -> StockAdjustmentRecord:
        """
        Checker approves adjustment and atomically posts it to the Stock Ledger.
        Enforces Maker-Checker separation of duties for nontrivial adjustments.
        Emits immutable StockAdjustmentDifferenceEvent.
        """
        cls.assert_approver_role(checker_user)

        adj = (
            StockAdjustmentRecord.objects.select_for_update()
            .filter(tenant_id=tenant_id, adjustment_id=adjustment_id)
            .select_related("medicine", "batch", "tenant", "branch")
            .first()
        )
        if not adj:
            raise NotFound(f"Stock adjustment '{adjustment_id}' not found.")

        if adj.status == StockCorrectionStatus.APPROVED:
            raise ValidationError({"status": "Adjustment has already been approved and posted (ERR_ALREADY_APPROVED)."})

        if adj.status != StockCorrectionStatus.PENDING:
            raise ValidationError({"status": f"Adjustment in status '{adj.status}' cannot be approved (must be PENDING)."})

        # Maker-Checker separation of duties for nontrivial adjustments
        if adj.is_nontrivial:
            checker_pk = getattr(checker_user, "pk", None)
            proposer_pk = getattr(adj.proposed_by, "pk", None)
            if checker_pk and proposer_pk and checker_pk == proposer_pk:
                raise ValidationError(
                    {"checker": "Maker-checker separation violated: you cannot approve your own nontrivial inventory adjustment (ERR_MAKER_CHECKER_SELF_APPROVAL)."}
                )

        batch = adj.batch
        pre_qty = batch.quantity_base_units if batch else 0

        # Post to Stock Movement Ledger (ADR-0201)
        movement_type = (
            StockMovementType.STOCK_ADJUSTMENT_IN
            if adj.quantity_delta > 0
            else StockMovementType.STOCK_ADJUSTMENT_OUT
        )

        ledger_entry = StockLedgerService.record_stock_movement(
            tenant=adj.tenant,
            branch=adj.branch,
            product=adj.medicine,
            movement_type=movement_type,
            quantity_delta=adj.quantity_delta,
            source_entity_type="STOCK_ADJUSTMENT",
            source_entity_id=adj.adjustment_id,
            source_reference=adj.adjustment_id,
            batch=batch,
            batch_number=batch.batch_number if batch else "",
            unit_cost=adj.unit_cost_bdt,
            storage_location=batch.storage_location if batch else "",
            notes=f"Adjustment {adj.adjustment_id} approved: {adj.reason}",
            posted_by=checker_user,
        )

        post_qty = pre_qty + adj.quantity_delta

        # Create immutable difference event
        diff_event = StockAdjustmentDifferenceEvent.objects.create(
            event_id=f"adjevt-{uuid.uuid4().hex[:10]}",
            tenant=adj.tenant,
            branch=adj.branch,
            adjustment=adj,
            product=adj.medicine,
            batch=batch,
            batch_number=batch.batch_number if batch else "",
            pre_adjustment_quantity=pre_qty,
            post_adjustment_quantity=post_qty,
            quantity_delta=adj.quantity_delta,
            unit_cost_bdt=adj.unit_cost_bdt,
            total_cost_impact_bdt=adj.total_cost_impact,
            is_nontrivial=adj.is_nontrivial,
            reason_code=adj.reason_code,
            reason_text=adj.reason,
            proposed_by=adj.proposed_by,
            approved_by=checker_user,
            ledger_entry_id=ledger_entry.entry_id,
        )

        adj.status = StockCorrectionStatus.APPROVED
        adj.approved_by = checker_user
        adj.approved_at = timezone.now()
        adj.ledger_entry_id = ledger_entry.entry_id
        if checker_notes:
            adj.supporting_document_ref = f"{adj.supporting_document_ref}; Checker: {checker_notes}".strip("; ")
        adj.save()

        logger.info(
            "Stock adjustment %s approved by %s, emitted ledger entry %s, difference event %s",
            adj.adjustment_id,
            checker_user,
            ledger_entry.entry_id,
            diff_event.event_id,
        )
        return adj

    @classmethod
    @transaction.atomic
    def reject_adjustment(
        cls,
        adjustment_id: str,
        tenant_id: str,
        rejection_reason: str,
        checker_user: Any,
    ) -> StockAdjustmentRecord:
        """Checker rejects proposed adjustment with documented explanation."""
        cls.assert_approver_role(checker_user)

        adj = (
            StockAdjustmentRecord.objects.select_for_update()
            .filter(tenant_id=tenant_id, adjustment_id=adjustment_id)
            .first()
        )
        if not adj:
            raise NotFound(f"Stock adjustment '{adjustment_id}' not found.")

        if adj.status != StockCorrectionStatus.PENDING:
            raise ValidationError({"status": f"Cannot reject adjustment in status '{adj.status}'."})

        clean_reason = rejection_reason.strip()
        if len(clean_reason) < 5:
            raise ValidationError({"rejection_reason": "Rejection reason must be provided with specific explanation."})

        adj.status = StockCorrectionStatus.REJECTED
        adj.reversal_reason = clean_reason
        adj.approved_by = checker_user
        adj.approved_at = timezone.now()
        adj.save(update_fields=["status", "reversal_reason", "approved_by", "approved_at", "updated_at"])
        return adj

    @classmethod
    @transaction.atomic
    def reverse_approved_adjustment(
        cls,
        adjustment_id: str,
        tenant_id: str,
        reversal_reason: str,
        actor_user: Any,
    ) -> StockAdjustmentRecord:
        """
        Creates an offsetting reversal adjustment for an already approved adjustment.
        Emits offsetting StockLedgerEntry.
        """
        cls.assert_approver_role(actor_user)

        orig = (
            StockAdjustmentRecord.objects.select_for_update()
            .filter(tenant_id=tenant_id, adjustment_id=adjustment_id)
            .select_related("medicine", "batch", "tenant", "branch")
            .first()
        )
        if not orig:
            raise NotFound(f"Adjustment '{adjustment_id}' not found.")

        if orig.status != StockCorrectionStatus.APPROVED:
            raise ValidationError({"status": "Only APPROVED adjustments can be reversed."})

        if orig.is_reversed:
            raise ValidationError({"status": "Adjustment has already been reversed (ERR_ALREADY_REVERSED)."})

        clean_reason = reversal_reason.strip()
        if len(clean_reason) < 10:
            raise ValidationError({"reversal_reason": "Reversal reason must be provided with explanation of at least 10 characters."})

        reversal_delta = -orig.quantity_delta
        movement_type = (
            StockMovementType.STOCK_ADJUSTMENT_IN
            if reversal_delta > 0
            else StockMovementType.STOCK_ADJUSTMENT_OUT
        )

        batch = orig.batch
        pre_qty = batch.quantity_base_units if batch else 0

        # Post offsetting ledger entry
        ledger_entry = StockLedgerService.record_stock_movement(
            tenant=orig.tenant,
            branch=orig.branch,
            product=orig.medicine,
            movement_type=movement_type,
            quantity_delta=reversal_delta,
            source_entity_type="STOCK_ADJUSTMENT_REVERSAL",
            source_entity_id=orig.adjustment_id,
            source_reference=f"REV-{orig.adjustment_id}",
            batch=batch,
            batch_number=batch.batch_number if batch else "",
            unit_cost=orig.unit_cost_bdt,
            storage_location=batch.storage_location if batch else "",
            notes=f"Reversal of {orig.adjustment_id}: {clean_reason}",
            posted_by=actor_user,
        )

        reversal_record = StockAdjustmentRecord.objects.create(
            adjustment_id=f"adj-rev-{uuid.uuid4().hex[:8]}",
            tenant=orig.tenant,
            branch=orig.branch,
            medicine=orig.medicine,
            batch=batch,
            adjustment_type=orig.adjustment_type,
            quantity_delta=reversal_delta,
            unit_cost_bdt=orig.unit_cost_bdt,
            total_cost_impact=orig.total_cost_impact,
            reason=f"Offset reversal of {orig.adjustment_id}: {clean_reason}",
            reason_code=orig.reason_code,
            is_nontrivial=orig.is_nontrivial,
            status=StockCorrectionStatus.APPROVED,
            proposed_by=actor_user,
            approved_by=actor_user,
            approved_at=timezone.now(),
            reversal_of=orig,
            ledger_entry_id=ledger_entry.entry_id,
        )

        orig.is_reversed = True
        orig.reversal_reason = clean_reason
        orig.save(update_fields=["is_reversed", "reversal_reason", "updated_at"])

        # Create reversal difference event
        StockAdjustmentDifferenceEvent.objects.create(
            event_id=f"adjevt-{uuid.uuid4().hex[:10]}",
            tenant=orig.tenant,
            branch=orig.branch,
            adjustment=reversal_record,
            product=orig.medicine,
            batch=batch,
            batch_number=batch.batch_number if batch else "",
            pre_adjustment_quantity=pre_qty,
            post_adjustment_quantity=pre_qty + reversal_delta,
            quantity_delta=reversal_delta,
            unit_cost_bdt=orig.unit_cost_bdt,
            total_cost_impact_bdt=orig.total_cost_impact,
            is_nontrivial=orig.is_nontrivial,
            reason_code=orig.reason_code,
            reason_text=clean_reason,
            proposed_by=actor_user,
            approved_by=actor_user,
            ledger_entry_id=ledger_entry.entry_id,
        )

        return reversal_record

