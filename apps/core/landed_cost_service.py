"""
backend/apps/core/landed_cost_service.py
Architecture Decision: ADR-0176 (P176).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Authoritative Landed Cost Allocation & Inventory Valuation Engine:
1. Inward transport, courier, freight, handling, and cold-chain cost capitalization.
2. Configurable allocation policies: BY_VALUE, BY_QUANTITY, MANUAL_EQUAL.
3. Strict exact paisa / penny conservation (sum of allocations strictly equals total expense).
4. Updating PurchaseCostLot.unit_cost and BatchStock.cost_per_unit for accurate GAAP / IAS 2 COGS.
5. Audited, non-destructive ledger reversals restoring original acquisition unit costs.
"""

from __future__ import annotations

import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    GoodsReceiptNote,
    LandedCostAllocationLine,
    LandedCostAllocationMethod,
    LandedCostExpense,
    LandedCostExpenseType,
    LandedCostStatus,
    PurchaseCostLot,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

LANDED_COST_ALLOWED_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

PAISA_QUANTIZE = Decimal("0.0001")


class LandedCostService:
    """Authoritative service governing landed freight and handling cost allocations."""

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in LANDED_COST_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Branch Managers or Pharmacy Owners can record and capitalize landed costs.",
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
                    "detail": "Tenant subscription is suspended. Landed costing operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def calculate_allocations(
        cls,
        total_expense: Decimal,
        allocation_method: str,
        cost_lots: List[PurchaseCostLot],
    ) -> List[Dict[str, Any]]:
        """
        Pure calculation engine allocating total landed expense across cost lots
        with strict exact penny / paisa conservation.
        """
        if total_expense <= Decimal("0.0000"):
            raise ValidationError(
                {
                    "detail": "Total landed expense must be strictly positive.",
                    "error_code": "ERR_INVALID_EXPENSE_AMOUNT",
                }
            )

        if not cost_lots:
            raise ValidationError(
                {
                    "detail": "At least one cost lot is required for landed cost allocation.",
                    "error_code": "ERR_NO_COST_LOTS",
                }
            )

        if allocation_method not in LandedCostAllocationMethod.values:
            raise ValidationError(
                {
                    "detail": f"Invalid allocation method '{allocation_method}'. Must be one of: {list(LandedCostAllocationMethod.values)}",
                    "error_code": "ERR_INVALID_ALLOCATION_METHOD",
                }
            )

        num_lots = len(cost_lots)
        provisional_allocations: List[Dict[str, Any]] = []

        if allocation_method == LandedCostAllocationMethod.BY_VALUE:
            lot_values = [
                Decimal(str(lot.initial_quantity)) * Decimal(str(lot.unit_cost))
                for lot in cost_lots
            ]
            total_value = sum(lot_values)
            if total_value <= Decimal("0.0000"):
                weights = [Decimal("1") / Decimal(str(num_lots)) for _ in cost_lots]
            else:
                weights = [val / total_value for val in lot_values]

        elif allocation_method == LandedCostAllocationMethod.BY_QUANTITY:
            lot_quantities = [Decimal(str(lot.initial_quantity)) for lot in cost_lots]
            total_quantity = sum(lot_quantities)
            if total_quantity <= Decimal("0"):
                weights = [Decimal("1") / Decimal(str(num_lots)) for _ in cost_lots]
            else:
                weights = [qty / total_quantity for qty in lot_quantities]

        else:  # MANUAL_EQUAL
            weights = [Decimal("1") / Decimal(str(num_lots)) for _ in cost_lots]

        # First pass: calculate fractional amounts
        allocated_amounts = []
        for w in weights:
            amount = (total_expense * w).quantize(PAISA_QUANTIZE, rounding=ROUND_HALF_UP)
            allocated_amounts.append(amount)

        # Penny / Paisa Conservation Guard: ensure sum(allocated_amounts) == total_expense exactly!
        allocated_sum = sum(allocated_amounts)
        discrepancy = total_expense - allocated_sum

        if discrepancy != Decimal("0.0000"):
            # Add or deduct remainder from the highest-weighted lot
            max_idx = max(range(num_lots), key=lambda i: weights[i])
            allocated_amounts[max_idx] += discrepancy

        # Verify conservation invariant
        assert sum(allocated_amounts) == total_expense, "Conservation invariant failed: sum != total"

        # Build detailed allocation breakdown
        for idx, lot in enumerate(cost_lots):
            allocated_amt = allocated_amounts[idx]
            qty = Decimal(str(lot.initial_quantity))
            allocated_per_unit = (allocated_amt / qty).quantize(PAISA_QUANTIZE, rounding=ROUND_HALF_UP)
            base_unit_cost = Decimal(str(lot.unit_cost)).quantize(PAISA_QUANTIZE)
            landed_unit_cost = base_unit_cost + allocated_per_unit

            provisional_allocations.append(
                {
                    "cost_lot": lot,
                    "cost_lot_id": lot.lot_id,
                    "batch_id": lot.batch_id,
                    "product_id": lot.product_id,
                    "product_name": lot.product.brand_name if hasattr(lot, "product") and lot.product else "",
                    "initial_quantity": lot.initial_quantity,
                    "lot_base_unit_cost_before": base_unit_cost,
                    "allocated_expense_amount": allocated_amt,
                    "allocated_cost_per_unit": allocated_per_unit,
                    "lot_landed_unit_cost_after": landed_unit_cost,
                }
            )

        return provisional_allocations

    @classmethod
    def create_and_post_landed_cost(
        cls,
        user: Any,
        tenant: Tenant,
        branch: Branch,
        expense_type: str,
        allocation_method: str,
        total_expense_bdt: Decimal,
        reason: str,
        cost_lot_ids: List[str],
        grn_id: Optional[str] = None,
        invoice_ref: str = "",
    ) -> LandedCostExpense:
        """
        Calculates and posts a landed cost expense, capitalizing transport/freight
        into target PurchaseCostLot and BatchStock records.
        """
        cls._assert_manage_role(user)
        cls._assert_tenant_active(tenant)

        cleaned_reason = (reason or "").strip()
        if len(cleaned_reason) < 10:
            raise ValidationError(
                {
                    "detail": "A reasoned justification (minimum 10 characters) is strictly mandatory.",
                    "error_code": "ERR_AUDIT_REASON_REQUIRED",
                }
            )

        if expense_type not in LandedCostExpenseType.values:
            raise ValidationError(
                {
                    "detail": f"Invalid expense type '{expense_type}'. Must be one of: {list(LandedCostExpenseType.values)}",
                    "error_code": "ERR_INVALID_EXPENSE_TYPE",
                }
            )

        grn = None
        if grn_id:
            try:
                grn = GoodsReceiptNote.objects.get(tenant=tenant, branch=branch, grn_id=grn_id)
            except GoodsReceiptNote.DoesNotExist:
                raise NotFound(
                    {"detail": f"GRN '{grn_id}' not found in branch '{branch.branch_id}'.", "error_code": "ERR_GRN_NOT_FOUND"}
                )

        if not cost_lot_ids:
            # If no lot IDs explicitly provided, try to auto-populate from GRN
            if grn:
                cost_lot_ids = list(
                    PurchaseCostLot.objects.filter(
                        tenant=tenant, branch=branch, grn_number=grn.grn_number
                    ).values_list("lot_id", flat=True)
                )
            if not cost_lot_ids:
                raise ValidationError(
                    {
                        "detail": "No cost lots specified or linked to the selected GRN.",
                        "error_code": "ERR_NO_COST_LOTS",
                    }
                )

        with transaction.atomic():
            cost_lots = list(
                PurchaseCostLot.objects.select_for_update()
                .select_related("product", "batch")
                .filter(tenant=tenant, branch=branch, lot_id__in=cost_lot_ids)
            )

            if len(cost_lots) != len(cost_lot_ids):
                # Bar cross-branch cost lot allocations explicitly
                cross_branch_lots = PurchaseCostLot.objects.filter(
                    tenant=tenant, lot_id__in=cost_lot_ids
                ).exclude(branch=branch)
                if cross_branch_lots.exists():
                    raise ValidationError(
                        {
                            "detail": "Cross-branch cost lot allocation is strictly prohibited. All cost lots must belong to the active branch.",
                            "error_code": "ERR_CROSS_BRANCH_LOT",
                        }
                    )
                raise NotFound(
                    {
                        "detail": "One or more cost lots not found in the specified branch/tenant.",
                        "error_code": "ERR_COST_LOT_NOT_FOUND",
                    }
                )

            allocations = cls.calculate_allocations(
                total_expense=total_expense_bdt,
                allocation_method=allocation_method,
                cost_lots=cost_lots,
            )

            expense_id = f"landed-{uuid.uuid4().hex[:12]}"
            expense = LandedCostExpense.objects.create(
                expense_id=expense_id,
                tenant=tenant,
                branch=branch,
                grn=grn,
                expense_type=expense_type,
                allocation_method=allocation_method,
                total_expense_bdt=total_expense_bdt,
                invoice_ref=invoice_ref or "",
                reason=cleaned_reason,
                status=LandedCostStatus.POSTED,
                recorded_by=user,
                posted_at=timezone.now(),
            )

            for alloc in allocations:
                lot = alloc["cost_lot"]
                batch = lot.batch

                alloc_line_id = f"lcalloc-{uuid.uuid4().hex[:12]}"
                LandedCostAllocationLine.objects.create(
                    allocation_id=alloc_line_id,
                    expense=expense,
                    cost_lot=lot,
                    batch=batch,
                    product=lot.product,
                    lot_base_unit_cost_before=alloc["lot_base_unit_cost_before"],
                    allocated_expense_amount=alloc["allocated_expense_amount"],
                    allocated_cost_per_unit=alloc["allocated_cost_per_unit"],
                    lot_landed_unit_cost_after=alloc["lot_landed_unit_cost_after"],
                )

                # Capitalize to PurchaseCostLot
                lot.unit_cost = alloc["lot_landed_unit_cost_after"]
                lot.save(update_fields=["unit_cost"])

                # Update primary BatchStock valuation
                batch.cost_per_unit = alloc["lot_landed_unit_cost_after"]
                batch.save(update_fields=["cost_per_unit"])

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                tenant_id=tenant.tenant_id,
                branch_id=branch.branch_id,
                user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                actor_email=str(getattr(user, "email", "")),
                actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
                action="LANDED_COST_POSTED",
                resource_type="LandedCostExpense",
                resource_id=expense.expense_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "total_expense_bdt": str(total_expense_bdt),
                    "allocation_method": allocation_method,
                    "cost_lot_count": len(cost_lots),
                    "reason": cleaned_reason,
                },
            )

        return expense

    @classmethod
    def reverse_landed_cost(
        cls,
        user: Any,
        expense_id: str,
        reversal_reason: str,
    ) -> LandedCostExpense:
        """
        Reverses a posted landed cost allocation, restoring each lot and batch
        back to their original acquisition unit cost.
        """
        cls._assert_manage_role(user)

        with transaction.atomic():
            try:
                expense = LandedCostExpense.objects.select_for_update().get(expense_id=expense_id)
            except LandedCostExpense.DoesNotExist:
                raise NotFound(
                    {"detail": f"Landed cost expense '{expense_id}' not found.", "error_code": "ERR_EXPENSE_NOT_FOUND"}
                )

            cls._assert_tenant_active(expense.tenant)

            if expense.status == LandedCostStatus.REVERSED:
                raise ValidationError(
                    {
                        "detail": "Landed cost expense has already been reversed.",
                        "error_code": "ERR_ALREADY_REVERSED",
                    }
                )

            if expense.status != LandedCostStatus.POSTED:
                raise ValidationError(
                    {
                        "detail": f"Only POSTED landed cost expenses can be reversed. Current status: '{expense.status}'.",
                        "error_code": "ERR_INVALID_STATUS",
                    }
                )

            cleaned_reason = (reversal_reason or "").strip()
            if len(cleaned_reason) < 10:
                raise ValidationError(
                    {
                        "detail": "A reasoned justification (minimum 10 characters) is mandatory.",
                        "error_code": "ERR_AUDIT_REASON_REQUIRED",
                    }
                )

            # Restore each lot and batch
            for line in expense.lines.select_related("cost_lot", "batch").all():
                lot = line.cost_lot
                batch = line.batch

                lot.unit_cost = line.lot_base_unit_cost_before
                lot.save(update_fields=["unit_cost"])

                batch.cost_per_unit = line.lot_base_unit_cost_before
                batch.save(update_fields=["cost_per_unit"])

            expense.status = LandedCostStatus.REVERSED
            expense.reversal_reason = cleaned_reason
            expense.save(update_fields=["status", "reversal_reason"])

            AuditLogEntry.objects.create(
                request_id=f"req-{uuid.uuid4().hex[:12]}",
                tenant_id=expense.tenant.tenant_id,
                branch_id=expense.branch.branch_id,
                user_id=str(getattr(user, "user_id", getattr(user, "id", user))),
                actor_email=str(getattr(user, "email", "")),
                actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
                action="LANDED_COST_REVERSED",
                resource_type="LandedCostExpense",
                resource_id=expense.expense_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "total_expense_bdt": str(expense.total_expense_bdt),
                    "reversal_reason": cleaned_reason,
                },
            )

        return expense

