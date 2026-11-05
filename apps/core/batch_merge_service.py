"""
backend/apps/core/batch_merge_service.py
Architecture Decision: ADR-0178 (P178).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service enforcing clinical and regulatory safety guards against merging
incompatible pharmaceutical batches, managing weighted cost reconciliation,
and auditing batch consolidation lifecycles.
"""

from __future__ import annotations

import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchMergeRecord,
    BatchStatus,
    BatchStock,
    PurchaseCostLot,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

ALLOWED_MERGE_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class BatchMergeService:
    """
    Authoritative service governing pharmaceutical batch merges.
    Hard-blocks incompatible expiry dates, mismatched manufacturer lots,
    and contaminated recall/quarantine states.
    """

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_MERGE_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Batch consolidation and merge authorization requires a branch manager or owner.",
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
                    "detail": "Tenant subscription is suspended. Batch operations are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def serialize_merge_record(cls, record: BatchMergeRecord) -> Dict[str, Any]:
        """Serializes a BatchMergeRecord matching JSON schema."""
        return {
            "merge_id": record.merge_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id),
            "product_id": str(record.product_id),
            "product_name": record.product.brand_name if record.product else "",
            "source_batch_id": str(record.source_batch_id),
            "target_batch_id": str(record.target_batch_id),
            "batch_number": record.target_batch.batch_number if record.target_batch else "",
            "expiry_date": (
                record.target_batch.expiry_date.isoformat()
                if record.target_batch and record.target_batch.expiry_date
                else None
            ),
            "merged_quantity_base_units": record.merged_quantity_base_units,
            "source_cost_per_unit_before": str(record.source_cost_per_unit_before.quantize(Decimal("0.0001"))),
            "target_cost_per_unit_before": str(record.target_cost_per_unit_before.quantize(Decimal("0.0001"))),
            "target_cost_per_unit_after": str(record.target_cost_per_unit_after.quantize(Decimal("0.0001"))),
            "reason": record.reason,
            "merged_by_id": str(record.merged_by_id) if record.merged_by_id else None,
            "merged_by_name": (
                getattr(record.merged_by, "full_name", "") or getattr(record.merged_by, "email", "")
                if record.merged_by
                else ""
            ),
            "is_reversed": record.is_reversed,
            "reversal_reason": record.reversal_reason or None,
            "reversed_at": record.reversed_at.isoformat() if record.reversed_at else None,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @classmethod
    def validate_merge_compatibility(
        cls,
        source_batch: BatchStock,
        target_batch: BatchStock,
        raise_exception: bool = True,
    ) -> Dict[str, Any]:
        """
        Pure validation engine inspecting all clinical, manufacturer, and branch compatibility constraints.
        """
        blockers: List[Dict[str, str]] = []

        # 1. Self merge guard
        if source_batch.batch_id == target_batch.batch_id:
            blockers.append(
                {
                    "error_code": "ERR_CANNOT_MERGE_SAME_BATCH",
                    "detail": "Cannot merge a batch into itself.",
                }
            )

        # 2. Cross-tenant check
        if source_batch.tenant_id != target_batch.tenant_id:
            blockers.append(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant batch merging is strictly barred.",
                }
            )

        # 3. Branch ownership check (ADR-0175)
        if source_batch.branch_id != target_batch.branch_id:
            blockers.append(
                {
                    "error_code": "ERR_BRANCH_MISMATCH",
                    "detail": f"Batches belong to different physical branches ('{source_batch.branch_id}' vs '{target_batch.branch_id}'). Cross-branch merging violates branch inventory sovereignty.",
                }
            )

        # 4. Product identity check
        if source_batch.product_id != target_batch.product_id:
            blockers.append(
                {
                    "error_code": "ERR_PRODUCT_MISMATCH",
                    "detail": "Cannot merge batches of different pharmaceutical products.",
                }
            )

        # 5. Manufacturer batch and lot reference compatibility check
        source_mfr = (source_batch.lot_reference or source_batch.batch_number or "").strip().upper()
        target_mfr = (target_batch.lot_reference or target_batch.batch_number or "").strip().upper()
        if source_mfr != target_mfr:
            blockers.append(
                {
                    "error_code": "ERR_MANUFACTURER_BATCH_MISMATCH",
                    "detail": f"Manufacturer batch references differ ('{source_mfr}' vs '{target_mfr}'). Merging different manufacturer lots destroys statutory DGDA recall traceability.",
                }
            )

        source_mfr_name = (source_batch.manufacturer_name or "").strip().upper()
        target_mfr_name = (target_batch.manufacturer_name or "").strip().upper()
        if source_mfr_name and target_mfr_name and source_mfr_name != target_mfr_name:
            blockers.append(
                {
                    "error_code": "ERR_MANUFACTURER_BATCH_MISMATCH",
                    "detail": f"Manufacturer names differ ('{source_mfr_name}' vs '{target_mfr_name}'). Merging batches from different manufacturers is prohibited.",
                }
            )

        # 6. Expiry date identity check (FEFO guard)
        if source_batch.expiry_date != target_batch.expiry_date:
            blockers.append(
                {
                    "error_code": "ERR_EXPIRY_DATE_MISMATCH",
                    "detail": f"Batch expiry dates differ ('{source_batch.expiry_date}' vs '{target_batch.expiry_date}'). Merging batches with different expiry dates compromises clinical FEFO dispensing.",
                }
            )

        today = timezone.now().date()

        # 7. Expiry cutoff guard
        if source_batch.expiry_date <= today:
            blockers.append(
                {
                    "error_code": "ERR_SOURCE_BATCH_EXPIRED",
                    "detail": f"Source batch is expired ({source_batch.expiry_date}). Expired stock cannot be consolidated into active inventory.",
                }
            )
        if target_batch.expiry_date <= today:
            blockers.append(
                {
                    "error_code": "ERR_TARGET_BATCH_EXPIRED",
                    "detail": f"Target batch is expired ({target_batch.expiry_date}). Cannot merge stock into an expired target.",
                }
            )

        # 8. Recall status guard
        if source_batch.status == BatchStatus.RECALLED or target_batch.status == BatchStatus.RECALLED:
            blockers.append(
                {
                    "error_code": "ERR_BATCH_RECALLED",
                    "detail": "Cannot merge recalled batches. Recalled stock must remain isolated in quarantine.",
                }
            )

        # 9. Quarantine status guard
        if (
            source_batch.is_quarantined
            or target_batch.is_quarantined
            or source_batch.status == BatchStatus.QUARANTINED
            or target_batch.status == BatchStatus.QUARANTINED
        ):
            blockers.append(
                {
                    "error_code": "ERR_BATCH_QUARANTINED",
                    "detail": "Cannot merge quarantined batches. Quarantined stock is clinical non-dispensable.",
                }
            )

        # 10. Source batch quantity check
        if source_batch.quantity_base_units <= 0:
            blockers.append(
                {
                    "error_code": "ERR_SOURCE_BATCH_EMPTY",
                    "detail": "Source batch has zero quantity available to merge.",
                }
            )

        # 11. Source batch active reservation check
        if source_batch.reserved_quantity_base_units > 0:
            blockers.append(
                {
                    "error_code": "ERR_SOURCE_BATCH_HAS_RESERVATIONS",
                    "detail": f"Source batch has {source_batch.reserved_quantity_base_units} units actively reserved in cashier checkout carts. Release reservations before merging.",
                }
            )

        can_merge = len(blockers) == 0

        if not can_merge and raise_exception:
            first_blocker = blockers[0]
            raise ValidationError(
                {
                    "detail": first_blocker["detail"],
                    "error_code": first_blocker["error_code"],
                    "all_blockers": blockers,
                }
            )

        # Calculate projected weighted average cost
        target_qty = Decimal(str(target_batch.quantity_base_units))
        source_qty = Decimal(str(source_batch.quantity_base_units))
        target_cost = Decimal(str(target_batch.cost_per_unit))
        source_cost = Decimal(str(source_batch.cost_per_unit))

        total_qty = target_qty + source_qty
        if total_qty > Decimal("0"):
            projected_cost = ((target_qty * target_cost) + (source_qty * source_cost)) / total_qty
            projected_cost = projected_cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        else:
            projected_cost = target_cost.quantize(Decimal("0.0001"))

        return {
            "can_merge": can_merge,
            "blockers": blockers,
            "source_batch_id": source_batch.batch_id,
            "target_batch_id": target_batch.batch_id,
            "source_quantity": source_batch.quantity_base_units,
            "target_quantity_before": target_batch.quantity_base_units,
            "projected_target_quantity": target_batch.quantity_base_units + source_batch.quantity_base_units,
            "source_cost_per_unit": str(source_cost.quantize(Decimal("0.0001"))),
            "target_cost_per_unit_before": str(target_cost.quantize(Decimal("0.0001"))),
            "projected_target_cost_per_unit": str(projected_cost),
        }

    @classmethod
    @transaction.atomic
    def execute_merge(
        cls,
        user: Any,
        source_batch_id: str,
        target_batch_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Executes an audited batch merge, enforcing compatibility guards,
        updating stock quantities and weighted cost, and transferring purchase cost lots.
        """
        cls._assert_manage_role(user)

        cleaned_reason = (reason or "").strip()
        if len(cleaned_reason) < 10:
            raise ValidationError(
                {
                    "detail": "A reasoned justification (minimum 10 characters) is strictly mandatory for batch consolidation.",
                    "error_code": "ERR_AUDIT_REASON_REQUIRED",
                }
            )

        source_batch = (
            BatchStock.objects.select_for_update()
            .select_related("tenant", "branch", "product")
            .filter(batch_id=source_batch_id)
            .first()
        )
        if not source_batch:
            raise NotFound(
                {
                    "detail": f"Source batch '{source_batch_id}' not found.",
                    "error_code": "ERR_SOURCE_BATCH_NOT_FOUND",
                }
            )

        target_batch = (
            BatchStock.objects.select_for_update()
            .select_related("tenant", "branch", "product")
            .filter(batch_id=target_batch_id)
            .first()
        )
        if not target_batch:
            raise NotFound(
                {
                    "detail": f"Target batch '{target_batch_id}' not found.",
                    "error_code": "ERR_TARGET_BATCH_NOT_FOUND",
                }
            )

        cls._assert_tenant_active(source_batch.tenant)

        # Cross-tenant check against actor
        user_tenant_id = getattr(user, "tenant_id", None)
        if (
            user_tenant_id
            and str(user_tenant_id) != str(source_batch.tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {
                    "detail": "Cross-tenant batch merging is strictly barred.",
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                }
            )

        # Validate all compatibility guards (raises ValidationError if any guard fails)
        validation_res = cls.validate_merge_compatibility(source_batch, target_batch, raise_exception=True)

        merged_qty = source_batch.quantity_base_units
        source_cost_before = Decimal(str(source_batch.cost_per_unit)).quantize(Decimal("0.0001"))
        target_cost_before = Decimal(str(target_batch.cost_per_unit)).quantize(Decimal("0.0001"))
        target_cost_after = Decimal(str(validation_res["projected_target_cost_per_unit"]))

        # 1. Update Target Batch
        target_batch.quantity_base_units += merged_qty
        target_batch.cost_per_unit = target_cost_after
        target_batch.save(update_fields=["quantity_base_units", "cost_per_unit", "updated_at"])

        # 2. Update Source Batch
        source_batch.quantity_base_units = 0
        source_batch.status = BatchStatus.DEPLETED
        source_batch.save(update_fields=["quantity_base_units", "status", "updated_at"])

        # 3. Transfer/Relink linked PurchaseCostLots to target batch so FIFO accounting is preserved
        PurchaseCostLot.objects.filter(batch=source_batch).update(batch=target_batch)

        # 4. Create BatchMergeRecord
        merge_id = f"bmerge-{uuid.uuid4().hex[:12]}"
        record = BatchMergeRecord.objects.create(
            merge_id=merge_id,
            tenant=source_batch.tenant,
            branch=source_batch.branch,
            product=source_batch.product,
            source_batch=source_batch,
            target_batch=target_batch,
            merged_quantity_base_units=merged_qty,
            source_cost_per_unit_before=source_cost_before,
            target_cost_per_unit_before=target_cost_before,
            target_cost_per_unit_after=target_cost_after,
            reason=cleaned_reason,
            merged_by=user,
        )

        # 5. Audit Log Entry
        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=source_batch.tenant_id,
            branch_id=source_batch.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "manager@pharmacy.com"),
            actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
            action="BATCH_MERGED",
            resource_type="BatchMergeRecord",
            resource_id=record.merge_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "source_batch_id": source_batch.batch_id,
                "target_batch_id": target_batch.batch_id,
                "merged_quantity": merged_qty,
                "target_cost_before": str(target_cost_before),
                "target_cost_after": str(target_cost_after),
                "reason": cleaned_reason,
            },
        )

        return cls.serialize_merge_record(record)

    @classmethod
    @transaction.atomic
    def reverse_merge(
        cls,
        user: Any,
        merge_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Reverses an executed batch merge, restoring original quantities,
        reverting target unit cost, and un-linking transferred cost lots.
        """
        cls._assert_manage_role(user)

        cleaned_reason = (reason or "").strip()
        if len(cleaned_reason) < 10:
            raise ValidationError(
                {
                    "detail": "A reasoned justification (minimum 10 characters) is required for merge reversal.",
                    "error_code": "ERR_AUDIT_REASON_REQUIRED",
                }
            )

        record = (
            BatchMergeRecord.objects.select_for_update()
            .select_related("tenant", "branch", "source_batch", "target_batch")
            .filter(merge_id=merge_id)
            .first()
        )
        if not record:
            raise NotFound(
                {
                    "detail": f"Batch merge record '{merge_id}' not found.",
                    "error_code": "ERR_MERGE_RECORD_NOT_FOUND",
                }
            )

        user_tenant_id = getattr(user, "tenant_id", None)
        if (
            user_tenant_id
            and str(user_tenant_id) != str(record.tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {
                    "detail": "Cross-tenant merge reversal is strictly barred.",
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                }
            )

        cls._assert_tenant_active(record.tenant)

        if record.is_reversed:
            raise ValidationError(
                {
                    "detail": "This batch merge has already been reversed.",
                    "error_code": "ERR_ALREADY_REVERSED",
                }
            )

        target_batch = BatchStock.objects.select_for_update().get(batch_id=record.target_batch_id)
        source_batch = BatchStock.objects.select_for_update().get(batch_id=record.source_batch_id)

        merged_qty = record.merged_quantity_base_units

        # Target batch must have sufficient unreserved units to return to source
        available_in_target = target_batch.quantity_base_units - target_batch.reserved_quantity_base_units
        if available_in_target < merged_qty:
            raise ValidationError(
                {
                    "detail": f"Target batch only has {available_in_target} unreserved units available. Cannot reverse merge of {merged_qty} units.",
                    "error_code": "ERR_INSUFFICIENT_STOCK_FOR_REVERSAL",
                }
            )

        # 1. Decrement Target Batch & Revert Cost
        target_batch.quantity_base_units -= merged_qty
        target_batch.cost_per_unit = record.target_cost_per_unit_before
        target_batch.save(update_fields=["quantity_base_units", "cost_per_unit", "updated_at"])

        # 2. Restore Source Batch
        source_batch.quantity_base_units = merged_qty
        source_batch.status = BatchStatus.ACTIVE
        source_batch.cost_per_unit = record.source_cost_per_unit_before
        source_batch.save(update_fields=["quantity_base_units", "status", "cost_per_unit", "updated_at"])

        # 3. Mark Record Reversed
        record.is_reversed = True
        record.reversal_reason = cleaned_reason
        record.reversed_by = user
        record.reversed_at = timezone.now()
        record.save(update_fields=["is_reversed", "reversal_reason", "reversed_by", "reversed_at", "updated_at"])

        # 4. Audit Log Entry
        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=record.tenant_id,
            branch_id=record.branch_id,
            user_id=getattr(user, "user_id", str(user)),
            actor_email=getattr(user, "email", "manager@pharmacy.com"),
            actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
            action="BATCH_MERGE_REVERSED",
            resource_type="BatchMergeRecord",
            resource_id=record.merge_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "reversal_reason": cleaned_reason,
                "merged_quantity": merged_qty,
                "source_batch_id": source_batch.batch_id,
                "target_batch_id": target_batch.batch_id,
            },
        )

        return cls.serialize_merge_record(record)

