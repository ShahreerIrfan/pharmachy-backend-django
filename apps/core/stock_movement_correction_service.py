"""
backend/apps/core/stock_movement_correction_service.py
Architecture Decision: ADR-0217 (P217).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Authoritative domain service executing auditable stock movement corrections:
- Strictly prohibits direct database row mutation or deletion on posted ledger entries.
- Generates immutable compensating StockLedgerEntry records with inverse quantity deltas.
- Enforces double-reversal prevention and negative batch stock guards.
- Cryptographically signs compensating records and links them to the original entry.
- Restricts cashier execution with role-based access control (RBAC).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStock,
    Branch,
    MedicineProduct,
    MovementCorrectionReason,
    MovementCorrectionStatus,
    StockLedgerEntry,
    StockMovementCorrection,
    StockMovementType,
    Tenant,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_CORRECTION_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.STAFF_PHARMACIST,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "STAFF_PHARMACIST",
    "PROCUREMENT_OFFICER",
    "CHIEF_PHARMACIST",
    "AUDITOR",
}


class StockMovementCorrectionService:
    """
    Authoritative service executing compensating stock movement reversals.
    """

    @classmethod
    def assert_correction_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role == "CASHIER" or role == UserRole.CASHIER or role not in ALLOWED_CORRECTION_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Cashiers are strictly forbidden from performing inventory movement corrections.",
                    "error_code": "ERR_CORRECTION_PERMISSION_DENIED",
                }
            )

    @classmethod
    @transaction.atomic
    def execute_compensating_correction(
        cls,
        tenant: Tenant,
        branch: Branch,
        original_entry_id: str,
        reason: str,
        explanation: str,
        user: Any = None,
    ) -> StockMovementCorrection:
        """
        Executes an audited compensating reversal for an erroneous posted StockLedgerEntry.
        """
        if user:
            cls.assert_correction_role(user)

        if not explanation or len(explanation.strip()) < 5:
            raise ValidationError(
                {
                    "explanation": "A descriptive explanation of at least 5 characters is required.",
                    "error_code": "ERR_EXPLANATION_REQUIRED",
                }
            )

        if reason not in MovementCorrectionReason.values:
            raise ValidationError(
                {
                    "reason": f"Invalid correction reason '{reason}'. Allowed: {MovementCorrectionReason.values}",
                    "error_code": "ERR_INVALID_CORRECTION_REASON",
                }
            )

        # 1. Retrieve and lock original StockLedgerEntry
        try:
            original_entry = (
                StockLedgerEntry.objects.select_for_update()
                .select_related("product", "batch")
                .get(entry_id=original_entry_id, tenant=tenant, branch=branch)
            )
        except StockLedgerEntry.DoesNotExist:
            raise NotFound(
                {
                    "detail": f"Stock ledger entry '{original_entry_id}' not found for this tenant and branch.",
                    "error_code": "ERR_ORIGINAL_ENTRY_NOT_FOUND",
                }
            )

        # 2. Prevent Double-Reversal
        if StockMovementCorrection.objects.filter(original_entry=original_entry).exists():
            raise ValidationError(
                {
                    "detail": f"Stock ledger entry '{original_entry_id}' has already been reversed.",
                    "error_code": "ERR_ALREADY_REVERSED",
                }
            )

        # 3. Retrieve and lock BatchStock
        batch = original_entry.batch
        if batch:
            batch = BatchStock.objects.select_for_update().get(pk=batch.pk)
            current_balance = batch.quantity_base_units
        else:
            current_balance = 0

        # Calculate compensating delta and balance
        reversed_delta = -original_entry.quantity_delta
        new_balance = current_balance + reversed_delta

        # 4. Invariant: Negative physical stock is prohibited
        if new_balance < 0:
            raise ValidationError(
                {
                    "detail": (
                        f"Compensating reversal of {reversed_delta:+d} units would cause negative batch stock "
                        f"(current: {current_balance}, required: {abs(reversed_delta)})."
                    ),
                    "error_code": "ERR_INSUFFICIENT_STOCK_FOR_REVERSAL",
                }
            )

        # 5. Determine compensating movement type
        if reversed_delta > 0:
            movement_type = StockMovementType.STOCK_ADJUSTMENT_IN
        else:
            movement_type = StockMovementType.STOCK_ADJUSTMENT_OUT

        unit_cost = original_entry.unit_cost or Decimal("0.00")
        total_cost = (abs(Decimal(reversed_delta)) * unit_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        correction_id = f"correc-{uuid.uuid4().hex[:12]}"
        reversal_entry_id = f"stkld-rev-{uuid.uuid4().hex[:10]}"

        # 6. Create immutable compensating StockLedgerEntry
        compensating_entry = StockLedgerEntry(
            entry_id=reversal_entry_id,
            tenant=tenant,
            branch=branch,
            product=original_entry.product,
            batch=batch,
            batch_number=original_entry.batch_number,
            cost_lot=original_entry.cost_lot,
            movement_type=movement_type,
            quantity_delta=reversed_delta,
            balance_before=current_balance,
            balance_after=new_balance,
            unit_cost=unit_cost,
            total_cost=total_cost,
            source_entity_type="STOCK_MOVEMENT_CORRECTION",
            source_entity_id=correction_id,
            source_reference=f"REV-{original_entry.entry_id}",
            is_cold_chain=original_entry.is_cold_chain,
            storage_location=original_entry.storage_location,
            notes=f"Compensating reversal for {original_entry.entry_id}. Reason: {reason}. {explanation}",
            posted_by=user if hasattr(user, "pk") and user.is_authenticated else None,
        )
        compensating_entry.immutable_verification_hash = compensating_entry.compute_hash()
        compensating_entry.save()

        # 7. Update physical batch stock
        if batch:
            batch.quantity_base_units = new_balance
            batch.save(update_fields=["quantity_base_units"])

        # 8. Create StockMovementCorrection record
        correction = StockMovementCorrection.objects.create(
            correction_id=correction_id,
            tenant=tenant,
            branch=branch,
            original_entry=original_entry,
            compensating_entry=compensating_entry,
            batch=batch,
            product=original_entry.product,
            correction_reason=reason,
            explanation=explanation,
            reversed_quantity_delta=reversed_delta,
            unit_cost_bdt=unit_cost,
            total_cost_impact_bdt=total_cost,
            status=MovementCorrectionStatus.COMPLETED,
            executed_by=user if hasattr(user, "pk") and user.is_authenticated else None,
        )

        logger.info(
            "Executed stock movement correction %s: original %s reversed by %s (%+d units)",
            correction_id,
            original_entry.entry_id,
            reversal_entry_id,
            reversed_delta,
        )

        return correction
