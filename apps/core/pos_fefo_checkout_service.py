"""
backend/apps/core/pos_fefo_checkout_service.py
Architecture Decision: ADR-0228 (P228).
Phase 12: Atomic FEFO & FIFO Cost-Lot Allocation at POS Checkout.

Single authoritative engine for:
- Read-only checkout FEFO lot planning & blocked-batch diagnostics
- Atomic reservation & reconciliation (`SELECT FOR UPDATE`) across multiple batches
- Unblended FIFO PurchaseCostLot splitting within each batch (`Decimal(12,4)` / `Decimal(14,4)`)
- Strict rejection of expired, recalled, quarantined, cross-tenant/branch, or insufficient stock
- Elevated Pharmacist/Manager override governance (`ERR_FEFO_OVERRIDE_REQUIRES_APPROVAL`)
- Append-only SHA-256 `PosCheckoutAllocationAudit` trail (including rejected attempts outside rollback)
"""

from __future__ import annotations

import datetime
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    ClassificationType,
    MedicineClassificationSnapshot,
    MedicineCurationStatus,
    MedicineProduct,
    PosAllocationMode,
    PosAllocationStatus,
    PosCheckoutAllocationAudit,
    PosFefoAllocationStatus,
    PosSaleAllocation,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    SegregationStatus,
    StockSegregationRecord,
)
from apps.identity.models import UserRole

COST_Q = Decimal("0.0001")

ELEVATED_FEFO_OVERRIDE_ROLES = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.STAFF_PHARMACIST,
    "SUPERADMIN",
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "STAFF_PHARMACIST",
}


def q4(value: Any) -> Decimal:
    return Decimal(str(value or "0.0000")).quantize(COST_Q, rounding=ROUND_HALF_UP)


class _FefoRejectionAbort(Exception):
    """
    Internal sentinel exception carrying audit metadata and the DRF exception so a rolled-back
    atomic block still yields an immutable `PosCheckoutAllocationAudit(was_rejected=True)` record.
    """

    def __init__(self, audit_kwargs: Dict[str, Any], exc: APIException):
        self.audit_kwargs = audit_kwargs
        self.exc = exc
        super().__init__(str(exc))


class PosFefoCheckoutService:
    """
    Coordinates FEFO batch planning, atomic reservation, checkout reconciliation,
    explicit safety rejections, and audit logging across P221-P228 POS flows.
    """

    @classmethod
    def _is_product_globally_recalled(cls, product: MedicineProduct) -> bool:
        if str(getattr(product, "curation_status", "") or "").upper() == MedicineCurationStatus.RECALLED:
            return True
        snap = (
            MedicineClassificationSnapshot.objects.filter(product=product, is_current=True)
            .order_by("-version_number")
            .first()
        )
        return bool(snap and str(snap.classification_type or "").upper() in (ClassificationType.RECALLED, ClassificationType.BANNED))

    @classmethod
    def _quarantined_segregation_batch_ids(
        cls,
        tenant_id: str,
        branch_id: str,
        batch_ids: Iterable[str],
    ) -> set[str]:
        bids = list(batch_ids)
        if not bids:
            return set()
        return set(
            StockSegregationRecord.objects.filter(
                tenant_id=tenant_id,
                branch_id=branch_id,
                batch_id__in=bids,
                status=SegregationStatus.MOVED_TO_QUARANTINE,
            ).values_list("batch_id", flat=True)
        )

    @classmethod
    def _classify_batch_safety(
        cls,
        batch: BatchStock,
        *,
        today: datetime.date,
        product_recalled: bool,
        quarantined_seg_ids: set[str],
    ) -> Tuple[bool, Optional[str], str, str]:
        """
        Returns `(is_safe, block_category, error_code, reason)`.
        Precedence matches DGDA safety severity: RECALLED -> QUARANTINED -> EXPIRED -> INACTIVE.
        """
        b_status = str(batch.status or "").upper()
        if product_recalled or b_status == BatchStatus.RECALLED or batch.recall_notice_id is not None:
            reason_txt = batch.recall_reason or f"Batch '{batch.batch_number}' is under regulatory recall."
            return False, "RECALLED", "ERR_BATCH_RECALLED", reason_txt

        if batch.is_quarantined or b_status == BatchStatus.QUARANTINED or batch.batch_id in quarantined_seg_ids:
            return (
                False,
                "QUARANTINED",
                "ERR_BATCH_QUARANTINED",
                f"Batch '{batch.batch_number}' is quarantined and blocked from dispensing.",
            )

        if batch.expiry_date <= today or b_status == BatchStatus.EXPIRED:
            return (
                False,
                "EXPIRED",
                "ERR_BATCH_EXPIRED",
                f"Batch '{batch.batch_number}' expired on {batch.expiry_date.isoformat()} and cannot be dispensed.",
            )

        if not batch.is_active or b_status in (BatchStatus.DEPLETED, BatchStatus.ARCHIVED) or b_status != BatchStatus.ACTIVE:
            return (
                False,
                "INACTIVE",
                "ERR_INSUFFICIENT_DISPENSABLE_STOCK",
                f"Batch '{batch.batch_number}' is not active/dispensable (status={b_status}).",
            )

        return True, None, "", ""

    @staticmethod
    def _normalize_batch_overrides(raw_overrides: Any) -> Dict[str, Dict[str, Any]]:
        """Normalizes batch_overrides from `{line_id: batch_id}` or list of override dicts."""
        out: Dict[str, Dict[str, Any]] = {}
        if not raw_overrides:
            return out
        if isinstance(raw_overrides, dict):
            for k, v in raw_overrides.items():
                line_key = str(k or "").strip()
                if not line_key:
                    continue
                if isinstance(v, dict):
                    bid = str(v.get("batch_id") or "").strip()
                    if bid:
                        out[line_key] = {
                            "batch_id": bid,
                            "reason": str(v.get("reason") or "").strip(),
                            "strict": bool(v.get("strict", False)),
                        }
                elif v:
                    out[line_key] = {"batch_id": str(v).strip(), "reason": "", "strict": False}
        elif isinstance(raw_overrides, list):
            for item in raw_overrides:
                if not isinstance(item, dict):
                    continue
                line_key = str(item.get("line_id") or "").strip()
                bid = str(item.get("batch_id") or "").strip()
                if line_key and bid:
                    out[line_key] = {
                        "batch_id": bid,
                        "reason": str(item.get("reason") or "").strip(),
                        "strict": bool(item.get("strict", False)),
                    }
        return out

    @classmethod
    def record_audit_safely(cls, kwargs: Dict[str, Any]) -> Optional[PosCheckoutAllocationAudit]:
        if not kwargs or not kwargs.get("sale"):
            return None
        sale_obj = kwargs["sale"]
        actor_user = kwargs.get("actor_user")
        actor_role = str(getattr(actor_user, "role", "") or kwargs.get("actor_role") or "")[:48]
        return PosCheckoutAllocationAudit.objects.create(
            alloc_audit_id=f"pfaud-{uuid.uuid4().hex[:12]}",
            tenant=sale_obj.tenant,
            branch=sale_obj.branch,
            sale=sale_obj,
            sale_line=kwargs.get("sale_line"),
            batch=kwargs.get("batch"),
            action=str(kwargs.get("action") or "ALLOCATION_EVENT")[:48],
            allocation_mode=str(kwargs.get("allocation_mode") or "")[:24],
            requested_base_units=max(0, int(kwargs.get("requested_base_units") or 0)),
            allocated_base_units=max(0, int(kwargs.get("allocated_base_units") or 0)),
            batch_number_snapshot=str(kwargs.get("batch_number_snapshot") or "")[:64],
            expiry_date_snapshot=kwargs.get("expiry_date_snapshot"),
            was_rejected=bool(kwargs.get("was_rejected", False)),
            rejection_code=str(kwargs.get("rejection_code") or "")[:64],
            reason=str(kwargs.get("reason") or "")[:255],
            actor_user=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            actor_role=actor_role,
            is_offline_sync=bool(kwargs.get("is_offline_sync", False)),
            metadata_json=kwargs.get("metadata_json") or {},
        )

    @classmethod
    def _split_cost_lots_for_take(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        batch: BatchStock,
        take_units: int,
        exclude_sale_id: str,
        lock_rows: bool,
        lot_reserved_remaining_cache: Dict[str, int],
    ) -> List[Dict[str, Any]]:
        """
        Splits `take_units` across active FIFO PurchaseCostLots within a single BatchStock
        without ever overflowing or clamping a cost lot's remaining_quantity.
        """
        lot_qs = PurchaseCostLot.objects.filter(
            tenant_id=tenant_id,
            branch_id=branch_id,
            batch=batch,
            status=PurchaseCostLotStatus.ACTIVE,
            remaining_quantity__gt=0,
        ).order_by("purchase_date", "created_at", "lot_id")
        if lock_rows:
            lot_qs = lot_qs.select_for_update()
        lots = list(lot_qs)

        slices: List[Dict[str, Any]] = []
        rem = take_units
        for lot in lots:
            if rem <= 0:
                break
            if lot.lot_id not in lot_reserved_remaining_cache:
                other_reserved = (
                    PosSaleAllocation.objects.filter(
                        cost_lot=lot,
                        status=PosAllocationStatus.RESERVED,
                    )
                    .exclude(sale_id=exclude_sale_id)
                    .aggregate(tot=Sum("allocated_base_units"))
                    .get("tot")
                    or 0
                )
                lot_reserved_remaining_cache[lot.lot_id] = max(0, int(lot.remaining_quantity) - int(other_reserved))

            lot_avail = lot_reserved_remaining_cache[lot.lot_id]
            if lot_avail <= 0:
                continue
            lot_take = min(rem, lot_avail)
            lot_reserved_remaining_cache[lot.lot_id] -= lot_take
            unit_cost = q4(lot.unit_cost)
            slices.append(
                {
                    "cost_lot": lot,
                    "cost_lot_id": lot.lot_id,
                    "allocated_base_units": lot_take,
                    "unit_cost_bdt": unit_cost,
                    "total_cost_bdt": q4(unit_cost * Decimal(lot_take)),
                }
            )
            rem -= lot_take

        if rem > 0:
            fallback_cost = q4(batch.cost_per_unit)
            slices.append(
                {
                    "cost_lot": None,
                    "cost_lot_id": None,
                    "allocated_base_units": rem,
                    "unit_cost_bdt": fallback_cost,
                    "total_cost_bdt": q4(fallback_cost * Decimal(rem)),
                }
            )
        return slices

    @classmethod
    def _plan_line_fefo(
        cls,
        *,
        user: Any,
        sale: PosSaleOrder,
        line: PosSaleOrderLine,
        all_batches: List[BatchStock],
        today: datetime.date,
        product_recalled: bool,
        quarantined_seg_ids: set[str],
        batch_available_override: Dict[str, int],
        lot_reserved_remaining_cache: Dict[str, int],
        override_spec: Optional[Dict[str, Any]] = None,
        global_override_reason: str = "",
        lock_cost_lots: bool = False,
        raise_on_block: bool = True,
    ) -> Dict[str, Any]:
        """
        Pure deterministic FEFO + cost-lot planning core shared across preview and atomic commit.
        Tracks remaining batch availability mutably in `batch_available_override` across multi-line orders.
        """
        needed = int(line.base_units_total)
        safe_batches: List[Tuple[BatchStock, int]] = []
        blocked_lots: List[Dict[str, Any]] = []
        total_phys_recalled = 0
        total_phys_quarantined = 0
        total_phys_expired = 0

        for b in all_batches:
            is_safe, blk_cat, _, blk_reason = cls._classify_batch_safety(
                b,
                today=today,
                product_recalled=product_recalled,
                quarantined_seg_ids=quarantined_seg_ids,
            )
            avail = batch_available_override.get(b.batch_id, b.available_quantity_base_units)
            if not is_safe and blk_cat:
                qty_val = int(b.quantity_base_units)
                if blk_cat == "RECALLED":
                    total_phys_recalled += qty_val
                elif blk_cat == "QUARANTINED":
                    total_phys_quarantined += qty_val
                elif blk_cat == "EXPIRED":
                    total_phys_expired += qty_val
                blocked_lots.append(
                    {
                        "batch_id": b.batch_id,
                        "batch_number": b.batch_number,
                        "expiry_date": b.expiry_date.isoformat(),
                        "block_category": blk_cat,
                        "quantity_base_units": qty_val,
                        "reason": blk_reason,
                    }
                )
            elif avail > 0:
                safe_batches.append((b, avail))
            elif b.reserved_quantity_base_units > 0:
                blocked_lots.append(
                    {
                        "batch_id": b.batch_id,
                        "batch_number": b.batch_number,
                        "expiry_date": b.expiry_date.isoformat(),
                        "block_category": "RESERVED_OTHER",
                        "quantity_base_units": int(b.reserved_quantity_base_units),
                        "reason": f"Batch '{b.batch_number}' has units reserved by other open carts/orders.",
                    }
                )

        # Evaluate explicit preferred batch or manual override
        override_batch_id = (
            override_spec.get("batch_id")
            if override_spec and override_spec.get("batch_id")
            else (str(line.preferred_batch_id) if line.preferred_batch_id else "")
        )
        strict_preferred = bool(override_spec.get("strict", False)) if override_spec else False
        eff_reason = (
            str(override_spec.get("reason") or global_override_reason or "").strip()
            if override_spec
            else str(global_override_reason or "").strip()
        )
        line_mode = PosAllocationMode.FEFO_AUTO
        override_approved_user = None
        override_batch_obj: Optional[BatchStock] = None

        if override_batch_id:
            override_batch_obj = next((b for b in all_batches if b.batch_id == override_batch_id), None)
            if override_batch_obj is None:
                # Fetch directly to diagnose cross-tenant / cross-branch / wrong-product clearly
                foreign = BatchStock.objects.filter(pk=override_batch_id).first()
                if not foreign:
                    err = ValidationError(
                        {
                            "error_code": "ERR_BATCH_NOT_FOUND",
                            "batch_id": override_batch_id,
                            "message_en": f"Selected batch '{override_batch_id}' was not found.",
                            "message_bn": f"নির্বাচিত ব্যাচ '{override_batch_id}' পাওয়া যায়নি।",
                        }
                    )
                    if raise_on_block:
                        raise _FefoRejectionAbort(
                            {
                                "sale": sale,
                                "sale_line": line,
                                "action": "BLOCKED_BATCH_NOT_FOUND",
                                "requested_base_units": needed,
                                "was_rejected": True,
                                "rejection_code": "ERR_BATCH_NOT_FOUND",
                                "reason": f"Batch '{override_batch_id}' not found",
                                "actor_user": user,
                            },
                            err,
                        )
                    return cls._blocked_line_plan(line, needed, [], blocked_lots, "ERR_BATCH_NOT_FOUND", str(err.detail["message_en"]), str(err.detail["message_bn"]))
                if foreign.tenant_id != sale.tenant_id or foreign.branch_id != sale.branch_id:
                    err = PermissionDenied(
                        {
                            "error_code": "ERR_CROSS_TENANT_OR_BRANCH_BATCH",
                            "batch_id": override_batch_id,
                            "message_en": "Selected batch belongs to another pharmacy tenant or branch.",
                            "message_bn": "অন্য টেন্যান্ট বা শাখার ব্যাচ থেকে স্টক নেওয়া যাবে না।",
                        }
                    )
                    if raise_on_block:
                        raise _FefoRejectionAbort(
                            {
                                "sale": sale,
                                "sale_line": line,
                                "action": "BLOCKED_CROSS_TENANT_BATCH",
                                "requested_base_units": needed,
                                "batch_number_snapshot": foreign.batch_number,
                                "was_rejected": True,
                                "rejection_code": "ERR_CROSS_TENANT_OR_BRANCH_BATCH",
                                "reason": "Cross-tenant or cross-branch batch override rejected",
                                "actor_user": user,
                            },
                            err,
                        )
                    return cls._blocked_line_plan(line, needed, [], blocked_lots, "ERR_CROSS_TENANT_OR_BRANCH_BATCH", str(err.detail["message_en"]), str(err.detail["message_bn"]))
                err = ValidationError(
                    {
                        "error_code": "ERR_BATCH_PRODUCT_MISMATCH",
                        "batch_id": override_batch_id,
                        "message_en": f"Batch '{foreign.batch_number}' does not belong to product '{line.product.brand_name}'.",
                        "message_bn": f"ব্যাচ '{foreign.batch_number}' নির্বাচিত ওষুধের সাথে মিলছে না।",
                    }
                )
                if raise_on_block:
                    raise _FefoRejectionAbort(
                        {
                            "sale": sale,
                            "sale_line": line,
                            "batch": foreign,
                            "action": "BLOCKED_PRODUCT_MISMATCH",
                            "requested_base_units": needed,
                            "batch_number_snapshot": foreign.batch_number,
                            "was_rejected": True,
                            "rejection_code": "ERR_BATCH_PRODUCT_MISMATCH",
                            "reason": "Selected batch product mismatch",
                            "actor_user": user,
                        },
                        err,
                    )
                return cls._blocked_line_plan(line, needed, [], blocked_lots, "ERR_BATCH_PRODUCT_MISMATCH", str(err.detail["message_en"]), str(err.detail["message_bn"]))

            is_safe, blk_cat, err_code, blk_reason = cls._classify_batch_safety(
                override_batch_obj,
                today=today,
                product_recalled=product_recalled,
                quarantined_seg_ids=quarantined_seg_ids,
            )
            if not is_safe:
                bn_msg_map = {
                    "ERR_BATCH_RECALLED": f"ব্যাচ '{override_batch_obj.batch_number}' প্রত্যাহৃত (RECALLED) — বিক্রি সম্পূর্ণ নিষিদ্ধ।",
                    "ERR_BATCH_QUARANTINED": f"ব্যাচ '{override_batch_obj.batch_number}' কোয়ারেন্টিনে রয়েছে — ডিস্পেন্স নিষিদ্ধ।",
                    "ERR_BATCH_EXPIRED": f"ব্যাচ '{override_batch_obj.batch_number}' মেয়াদোত্তীর্ণ ({override_batch_obj.expiry_date}) — বিক্রি নিষিদ্ধ।",
                }
                err = ValidationError(
                    {
                        "error_code": err_code,
                        "batch_id": override_batch_obj.batch_id,
                        "batch_number": override_batch_obj.batch_number,
                        "message_en": blk_reason,
                        "message_bn": bn_msg_map.get(err_code, "নির্বাচিত ব্যাচটি নিরাপদ নয়।"),
                        "detail": blk_reason,
                    }
                )
                if raise_on_block:
                    raise _FefoRejectionAbort(
                        {
                            "sale": sale,
                            "sale_line": line,
                            "batch": override_batch_obj,
                            "action": f"BLOCKED_{blk_cat}",
                            "requested_base_units": needed,
                            "batch_number_snapshot": override_batch_obj.batch_number,
                            "expiry_date_snapshot": override_batch_obj.expiry_date,
                            "was_rejected": True,
                            "rejection_code": err_code,
                            "reason": blk_reason,
                            "actor_user": user,
                        },
                        err,
                    )
                return cls._blocked_line_plan(line, needed, [], blocked_lots, err_code, blk_reason, bn_msg_map.get(err_code, "ব্যাচ নিরাপদ নয়।"))

            earliest_safe = safe_batches[0][0] if safe_batches else None
            is_non_fefo = bool(earliest_safe and override_batch_obj.expiry_date > earliest_safe.expiry_date)
            if is_non_fefo:
                role = str(getattr(user, "role", "") or "").upper()
                if role not in ELEVATED_FEFO_OVERRIDE_ROLES:
                    err = PermissionDenied(
                        {
                            "error_code": "ERR_FEFO_OVERRIDE_REQUIRES_APPROVAL",
                            "earliest_batch_id": earliest_safe.batch_id,
                            "earliest_batch_number": earliest_safe.batch_number,
                            "message_en": (
                                f"Bypassing earlier FEFO batch '{earliest_safe.batch_number}' (exp {earliest_safe.expiry_date}) "
                                f"requires Pharmacist, Branch Manager, or Owner approval."
                            ),
                            "message_bn": (
                                f"আগে মেয়াদ শেষ হওয়া FEFO ব্যাচ '{earliest_safe.batch_number}' বাদ দিয়ে অন্য ব্যাচ নিতে "
                                f"ফার্মাসিস্ট বা ম্যানেজারের অনুমোদন প্রয়োজন।"
                            ),
                        }
                    )
                    if raise_on_block:
                        raise _FefoRejectionAbort(
                            {
                                "sale": sale,
                                "sale_line": line,
                                "batch": override_batch_obj,
                                "action": "BLOCKED_OVERRIDE_UNAUTHORIZED",
                                "allocation_mode": PosAllocationMode.MANUAL_OVERRIDE,
                                "requested_base_units": needed,
                                "batch_number_snapshot": override_batch_obj.batch_number,
                                "expiry_date_snapshot": override_batch_obj.expiry_date,
                                "was_rejected": True,
                                "rejection_code": "ERR_FEFO_OVERRIDE_REQUIRES_APPROVAL",
                                "reason": f"Role {role} denied non-FEFO override",
                                "actor_user": user,
                            },
                            err,
                        )
                    return cls._blocked_line_plan(line, needed, [], blocked_lots, "ERR_FEFO_OVERRIDE_REQUIRES_APPROVAL", str(err.detail["message_en"]), str(err.detail["message_bn"]))
                if len(eff_reason) < 3:
                    err = ValidationError(
                        {
                            "error_code": "ERR_FEFO_OVERRIDE_REASON_REQUIRED",
                            "message_en": "A documented reason (>= 3 chars) is mandatory when overriding FEFO expiry order.",
                            "message_bn": "FEFO ক্রম পরিবর্তনের জন্য কমপক্ষে ৩ অক্ষরের কারণ উল্লেখ করা আবশ্যক।",
                        }
                    )
                    if raise_on_block:
                        raise _FefoRejectionAbort(
                            {
                                "sale": sale,
                                "sale_line": line,
                                "batch": override_batch_obj,
                                "action": "BLOCKED_OVERRIDE_NO_REASON",
                                "allocation_mode": PosAllocationMode.MANUAL_OVERRIDE,
                                "requested_base_units": needed,
                                "batch_number_snapshot": override_batch_obj.batch_number,
                                "expiry_date_snapshot": override_batch_obj.expiry_date,
                                "was_rejected": True,
                                "rejection_code": "ERR_FEFO_OVERRIDE_REASON_REQUIRED",
                                "reason": "FEFO override missing reason",
                                "actor_user": user,
                            },
                            err,
                        )
                    return cls._blocked_line_plan(line, needed, [], blocked_lots, "ERR_FEFO_OVERRIDE_REASON_REQUIRED", str(err.detail["message_en"]), str(err.detail["message_bn"]))
                line_mode = PosAllocationMode.MANUAL_OVERRIDE
                override_approved_user = user
            else:
                line_mode = PosAllocationMode.FEFO_PREFERRED_BATCH

            # Move override batch to the front of candidate order
            prioritized: List[Tuple[BatchStock, int]] = [
                (b, a) for (b, a) in safe_batches if b.batch_id == override_batch_obj.batch_id
            ]
            if not strict_preferred:
                prioritized.extend([(b, a) for (b, a) in safe_batches if b.batch_id != override_batch_obj.batch_id])
            safe_batches = prioritized

        planned_slices: List[Dict[str, Any]] = []
        remaining = needed
        seq = 1
        for b, _initial_avail in safe_batches:
            if remaining <= 0:
                break
            cur_avail = batch_available_override.get(b.batch_id, b.available_quantity_base_units)
            if cur_avail <= 0:
                continue
            take = min(remaining, cur_avail)
            batch_available_override[b.batch_id] = cur_avail - take

            cost_slices = cls._split_cost_lots_for_take(
                tenant_id=sale.tenant_id,
                branch_id=sale.branch_id,
                batch=b,
                take_units=take,
                exclude_sale_id=sale.sale_id,
                lock_rows=lock_cost_lots,
                lot_reserved_remaining_cache=lot_reserved_remaining_cache,
            )
            slice_mode = (
                line_mode
                if (override_batch_obj and b.batch_id == override_batch_obj.batch_id)
                else PosAllocationMode.FEFO_AUTO
            )
            for sl in cost_slices:
                planned_slices.append(
                    {
                        "fefo_sequence": seq,
                        "batch": b,
                        "batch_id": b.batch_id,
                        "batch_number": b.batch_number,
                        "expiry_date": b.expiry_date,
                        "purchase_date": b.purchase_date,
                        "cost_lot": sl["cost_lot"],
                        "cost_lot_id": sl["cost_lot_id"],
                        "allocated_base_units": sl["allocated_base_units"],
                        "unit_cost_bdt": sl["unit_cost_bdt"],
                        "total_cost_bdt": sl["total_cost_bdt"],
                        "allocation_mode": slice_mode,
                        "override_reason": eff_reason if slice_mode == PosAllocationMode.MANUAL_OVERRIDE else "",
                        "override_approved_by": override_approved_user if slice_mode == PosAllocationMode.MANUAL_OVERRIDE else None,
                    }
                )
                seq += 1
            remaining -= take

        if remaining > 0:
            total_safe_planned = needed - remaining
            if total_safe_planned == 0 and total_phys_recalled > 0:
                code = "ERR_BATCH_RECALLED"
                audit_action = "BLOCKED_RECALLED"
                msg_en = f"Cannot sell '{line.product.brand_name}': only recalled batches exist in branch."
                msg_bn = f"'{line.product.brand_name}' বিক্রি নিষিদ্ধ: শাখায় শুধুমাত্র প্রত্যাহৃত (RECALLED) ব্যাচ রয়েছে।"
            elif total_safe_planned == 0 and total_phys_quarantined > 0:
                code = "ERR_BATCH_QUARANTINED"
                audit_action = "BLOCKED_QUARANTINED"
                msg_en = f"Cannot sell '{line.product.brand_name}': all remaining stock is quarantined."
                msg_bn = f"'{line.product.brand_name}' বিক্রি নিষিদ্ধ: অবশিষ্ট সব স্টক কোয়ারেন্টিনে রয়েছে।"
            elif total_safe_planned == 0 and total_phys_expired > 0:
                code = "ERR_BATCH_EXPIRED"
                audit_action = "BLOCKED_EXPIRED"
                msg_en = f"Cannot sell '{line.product.brand_name}': available stock in branch is expired."
                msg_bn = f"'{line.product.brand_name}' বিক্রি নিষিদ্ধ: শাখার মজুদ মেয়াদোত্তীর্ণ।"
            else:
                code = "ERR_INSUFFICIENT_DISPENSABLE_STOCK"
                audit_action = "BLOCKED_INSUFFICIENT_STOCK"
                msg_en = (
                    f"Insufficient safe FEFO stock for '{line.product.brand_name}': "
                    f"needed {needed} units, short by {remaining} units."
                )
                msg_bn = (
                    f"'{line.product.brand_name}'-এর পর্যাপ্ত নিরাপদ FEFO স্টক নেই "
                    f"(প্রয়োজন {needed}টি, ঘাটতি {remaining}টি)।"
                )

            if raise_on_block:
                raise _FefoRejectionAbort(
                    {
                        "sale": sale,
                        "sale_line": line,
                        "action": audit_action,
                        "allocation_mode": line_mode,
                        "requested_base_units": needed,
                        "allocated_base_units": total_safe_planned,
                        "was_rejected": True,
                        "rejection_code": code,
                        "reason": msg_en,
                        "actor_user": user,
                        "metadata_json": {
                            "short_by": remaining,
                            "blocked_lots_count": len(blocked_lots),
                            "expired_units": total_phys_expired,
                            "quarantined_units": total_phys_quarantined,
                            "recalled_units": total_phys_recalled,
                        },
                    },
                    ValidationError(
                        {
                            "error_code": code,
                            "product_id": line.product_id,
                            "needed_base_units": needed,
                            "available_safe_units": total_safe_planned,
                            "short_by": remaining,
                            "blocked_lots": blocked_lots,
                            "message_en": msg_en,
                            "message_bn": msg_bn,
                            "detail": msg_en,
                        }
                    ),
                )
            return cls._blocked_line_plan(line, needed, planned_slices, blocked_lots, code, msg_en, msg_bn)

        return {
            "line_id": line.line_id,
            "line_number": line.line_number,
            "product_id": line.product_id,
            "brand_name": line.product.brand_name,
            "packaging_unit": line.packaging_unit,
            "requested_base_units": needed,
            "planned_base_units": needed,
            "short_base_units": 0,
            "is_fulfilled": True,
            "allocation_mode": line_mode,
            "blocking_error_code": "",
            "blocking_reason_en": "",
            "blocking_reason_bn": "",
            "planned_slices": planned_slices,
            "blocked_lots": blocked_lots,
        }

    @staticmethod
    def _blocked_line_plan(
        line: PosSaleOrderLine,
        needed: int,
        planned_slices: List[Dict[str, Any]],
        blocked_lots: List[Dict[str, Any]],
        code: str,
        msg_en: str,
        msg_bn: str,
    ) -> Dict[str, Any]:
        planned_units = sum(int(s["allocated_base_units"]) for s in planned_slices)
        return {
            "line_id": line.line_id,
            "line_number": line.line_number,
            "product_id": line.product_id,
            "brand_name": line.product.brand_name,
            "packaging_unit": line.packaging_unit,
            "requested_base_units": needed,
            "planned_base_units": planned_units,
            "short_base_units": max(0, needed - planned_units),
            "is_fulfilled": False,
            "allocation_mode": PosAllocationMode.FEFO_AUTO,
            "blocking_error_code": code,
            "blocking_reason_en": msg_en,
            "blocking_reason_bn": msg_bn,
            "planned_slices": planned_slices,
            "blocked_lots": blocked_lots,
        }

    @classmethod
    def _serialize_plan_payload(
        cls,
        *,
        user: Any,
        sale: PosSaleOrder,
        line_plans: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        from apps.core.pos_domain_service import PosDomainService

        show_cost = PosDomainService.can_view_cost(user)
        lines_out: List[Dict[str, Any]] = []
        total_req = 0
        total_planned = 0
        total_cost = Decimal("0.0000")
        first_err_code = ""
        first_err_en = ""
        first_err_bn = ""
        has_exp = False
        has_rec = False
        has_quar = False

        for lp in line_plans:
            total_req += int(lp["requested_base_units"])
            total_planned += int(lp["planned_base_units"])
            if not lp["is_fulfilled"] and not first_err_code:
                first_err_code = lp["blocking_error_code"]
                first_err_en = lp["blocking_reason_en"]
                first_err_bn = lp["blocking_reason_bn"]
            for b in lp["blocked_lots"]:
                cat = b.get("block_category")
                if cat == "EXPIRED":
                    has_exp = True
                elif cat == "RECALLED":
                    has_rec = True
                elif cat == "QUARANTINED":
                    has_quar = True

            lots_out = []
            for sl in lp["planned_slices"]:
                total_cost += q4(sl["total_cost_bdt"])
                lots_out.append(
                    {
                        "fefo_sequence": sl["fefo_sequence"],
                        "batch_id": sl["batch_id"],
                        "batch_number": sl["batch_number"],
                        "expiry_date": sl["expiry_date"].isoformat()
                        if isinstance(sl["expiry_date"], datetime.date)
                        else str(sl["expiry_date"]),
                        "purchase_date": sl["purchase_date"].isoformat()
                        if isinstance(sl["purchase_date"], datetime.date)
                        else str(sl["purchase_date"]),
                        "cost_lot_id": sl["cost_lot_id"],
                        "allocated_base_units": int(sl["allocated_base_units"]),
                        "allocation_mode": sl["allocation_mode"],
                        "unit_cost_bdt": f"{sl['unit_cost_bdt']:.4f}" if show_cost else None,
                        "total_cost_bdt": f"{sl['total_cost_bdt']:.4f}" if show_cost else None,
                    }
                )
            lines_out.append(
                {
                    "line_id": lp["line_id"],
                    "line_number": lp["line_number"],
                    "product_id": lp["product_id"],
                    "brand_name": lp["brand_name"],
                    "packaging_unit": lp["packaging_unit"],
                    "requested_base_units": lp["requested_base_units"],
                    "planned_base_units": lp["planned_base_units"],
                    "short_base_units": lp["short_base_units"],
                    "is_fulfilled": lp["is_fulfilled"],
                    "allocation_mode": lp["allocation_mode"],
                    "blocking_error_code": lp["blocking_error_code"],
                    "blocking_reason_en": lp["blocking_reason_en"],
                    "blocking_reason_bn": lp["blocking_reason_bn"],
                    "planned_lots": lots_out,
                    "blocked_lots": lp["blocked_lots"],
                }
            )

        fefo_ready = len(lines_out) > 0 and all(lp["is_fulfilled"] for lp in line_plans)
        if len(lines_out) == 0:
            first_err_code = "ERR_EMPTY_CART"
            first_err_en = "Cart has no lines to allocate."
            first_err_bn = "কার্টে কোনো ওষুধ নেই।"

        return {
            "sale_id": sale.sale_id,
            "tenant_id": sale.tenant_id,
            "branch_id": sale.branch_id,
            "order_number": sale.order_number,
            "lifecycle_state": sale.lifecycle_state,
            "fefo_allocation_status": str(sale.fefo_allocation_status or PosFefoAllocationStatus.PENDING),
            "fefo_ready": fefo_ready,
            "blocking_error_code": first_err_code,
            "blocking_reason_en": first_err_en,
            "blocking_reason_bn": first_err_bn,
            "has_expired_block": has_exp,
            "has_recalled_block": has_rec,
            "has_quarantined_block": has_quar,
            "total_requested_base_units": total_req,
            "total_planned_base_units": total_planned,
            "total_estimated_cost_bdt": f"{q4(total_cost):.4f}" if show_cost else None,
            "version": int(sale.version),
            "lines": lines_out,
        }

    @classmethod
    def build_fefo_plan(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        batch_overrides: Any = None,
        override_reason: str = "",
    ) -> Dict[str, Any]:
        """Read-only FEFO lot plan preview with blocked-lot diagnostics and role-filtered cost precision."""
        from apps.core.pos_domain_service import PosDomainService

        PosDomainService.resolve_user_tenant_and_branch(
            user, branch_id=sale.branch_id, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )

        # If sale is already finalized/reversed, summarize committed/released allocations directly
        if sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.PARTIALLY_RETURNED,
            PosSaleLifecycleState.FULLY_RETURNED,
            PosSaleLifecycleState.REVERSED,
        ):
            lines = list(sale.lines.select_related("product").order_by("line_number"))
            existing_by_line: Dict[str, List[PosSaleAllocation]] = {}
            for a in sale.allocations.select_related("batch", "cost_lot").order_by("fefo_sequence", "allocation_id"):
                existing_by_line.setdefault(a.sale_line_id, []).append(a)
            line_plans = []
            for ln in lines:
                allocs = existing_by_line.get(ln.line_id, [])
                slices = [
                    {
                        "fefo_sequence": a.fefo_sequence,
                        "batch": a.batch,
                        "batch_id": a.batch_id,
                        "batch_number": a.batch.batch_number,
                        "expiry_date": a.expiry_date_snapshot or a.batch.expiry_date,
                        "purchase_date": a.batch.purchase_date,
                        "cost_lot_id": a.cost_lot_id,
                        "allocated_base_units": int(a.allocated_base_units),
                        "unit_cost_bdt": a.unit_cost_bdt,
                        "total_cost_bdt": a.total_cost_bdt,
                        "allocation_mode": a.allocation_mode,
                    }
                    for a in allocs
                ]
                planned_u = sum(s["allocated_base_units"] for s in slices)
                line_plans.append(
                    {
                        "line_id": ln.line_id,
                        "line_number": ln.line_number,
                        "product_id": ln.product_id,
                        "brand_name": ln.product.brand_name,
                        "packaging_unit": ln.packaging_unit,
                        "requested_base_units": int(ln.base_units_total),
                        "planned_base_units": planned_u,
                        "short_base_units": max(0, int(ln.base_units_total) - planned_u),
                        "is_fulfilled": planned_u >= int(ln.base_units_total),
                        "allocation_mode": slices[0]["allocation_mode"] if slices else PosAllocationMode.FEFO_AUTO,
                        "blocking_error_code": "",
                        "blocking_reason_en": "",
                        "blocking_reason_bn": "",
                        "planned_slices": slices,
                        "blocked_lots": [],
                    }
                )
            return cls._serialize_plan_payload(user=user, sale=sale, line_plans=line_plans)

        today = timezone.now().date()
        overrides = cls._normalize_batch_overrides(batch_overrides)
        lines = list(sale.lines.select_related("product", "preferred_batch").order_by("line_number"))

        # Add back THIS sale's active RESERVED allocations so read-only preview doesn't self-starve
        this_reserved = list(sale.allocations.filter(status=PosAllocationStatus.RESERVED))
        self_batch_credit: Dict[str, int] = {}
        self_lot_credit: Dict[str, int] = {}
        for r in this_reserved:
            self_batch_credit[r.batch_id] = self_batch_credit.get(r.batch_id, 0) + int(r.allocated_base_units)
            if r.cost_lot_id:
                self_lot_credit[r.cost_lot_id] = self_lot_credit.get(r.cost_lot_id, 0) + int(r.allocated_base_units)

        product_ids = {ln.product_id for ln in lines}
        all_branch_batches = list(
            BatchStock.objects.filter(
                tenant=sale.tenant,
                branch=sale.branch,
                product_id__in=product_ids,
            ).order_by("expiry_date", "purchase_date", "batch_number", "batch_id")
        )
        batches_by_product: Dict[str, List[BatchStock]] = {}
        batch_available_override: Dict[str, int] = {}
        for b in all_branch_batches:
            batches_by_product.setdefault(b.product_id, []).append(b)
            batch_available_override[b.batch_id] = b.available_quantity_base_units + self_batch_credit.get(b.batch_id, 0)

        quarantined_seg_ids = cls._quarantined_segregation_batch_ids(
            sale.tenant_id, sale.branch_id, [b.batch_id for b in all_branch_batches]
        )
        lot_cache: Dict[str, int] = {}
        if self_lot_credit:
            for lot in PurchaseCostLot.objects.filter(lot_id__in=list(self_lot_credit.keys())):
                other_reserved = (
                    PosSaleAllocation.objects.filter(cost_lot=lot, status=PosAllocationStatus.RESERVED)
                    .exclude(sale_id=sale.sale_id)
                    .aggregate(tot=Sum("allocated_base_units"))
                    .get("tot")
                    or 0
                )
                lot_cache[lot.lot_id] = max(0, int(lot.remaining_quantity) - int(other_reserved))

        recalled_product_cache: Dict[str, bool] = {}
        line_plans: List[Dict[str, Any]] = []
        for ln in lines:
            if ln.product_id not in recalled_product_cache:
                recalled_product_cache[ln.product_id] = cls._is_product_globally_recalled(ln.product)
            lp = cls._plan_line_fefo(
                user=user,
                sale=sale,
                line=ln,
                all_batches=batches_by_product.get(ln.product_id, []),
                today=today,
                product_recalled=recalled_product_cache[ln.product_id],
                quarantined_seg_ids=quarantined_seg_ids,
                batch_available_override=batch_available_override,
                lot_reserved_remaining_cache=lot_cache,
                override_spec=overrides.get(ln.line_id),
                global_override_reason=override_reason,
                lock_cost_lots=False,
                raise_on_block=False,
            )
            line_plans.append(lp)

        return cls._serialize_plan_payload(user=user, sale=sale, line_plans=line_plans)

    @classmethod
    def _release_reserved_allocations_in_tx(
        cls,
        sale: PosSaleOrder,
        *,
        target_status: str = PosAllocationStatus.RELEASED,
        record_audit: bool = False,
        actor_user: Any = None,
        reason: str = "",
    ) -> int:
        reserved_allocs = list(
            sale.allocations.filter(status=PosAllocationStatus.RESERVED).select_related("batch")
        )
        now = timezone.now()
        released_units = 0
        for alloc in reserved_allocs:
            batch = BatchStock.objects.select_for_update().get(pk=alloc.batch_id)
            batch.reserved_quantity_base_units = max(
                0, int(batch.reserved_quantity_base_units) - int(alloc.allocated_base_units)
            )
            batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])
            alloc.status = target_status
            alloc.released_at = now
            alloc.save(update_fields=["status", "released_at"])
            released_units += int(alloc.allocated_base_units)
        if record_audit and released_units > 0:
            cls.record_audit_safely(
                {
                    "sale": sale,
                    "action": "ALLOCATION_RELEASED",
                    "allocated_base_units": released_units,
                    "reason": reason or "Released reserved FEFO allocations",
                    "actor_user": actor_user,
                }
            )
        return released_units

    @classmethod
    def allocate_sale_fefo_in_tx(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        batch_overrides: Any = None,
        override_reason: str = "",
        expected_version: Optional[int] = None,
        is_offline_sync: bool = False,
        transition_lifecycle: bool = True,
    ) -> PosSaleOrder:
        """
        Core transactional FEFO + cost-lot reservation engine. Must be executed inside an active
        `transaction.atomic()` block (caller catches `_FefoRejectionAbort` outside atomic rollback).
        """
        from apps.core.pos_domain_service import PosDomainService

        locked_sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(user, branch_id=locked_sale.branch_id, require_write=True)

        if locked_sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
            PosSaleLifecycleState.REVERSED,
            PosSaleLifecycleState.FULLY_RETURNED,
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot allocate stock for sale in '{locked_sale.lifecycle_state}' state.",
                }
            )

        if expected_version is not None:
            try:
                exp_v = int(expected_version)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"expected_version": "expected_version must be an integer."}) from exc
            if exp_v != int(locked_sale.version):
                raise _FefoRejectionAbort(
                    {
                        "sale": locked_sale,
                        "action": "BLOCKED_VERSION_CONFLICT",
                        "was_rejected": True,
                        "rejection_code": "ERR_CART_VERSION_CONFLICT",
                        "reason": f"Expected version {exp_v}, got {locked_sale.version}",
                        "actor_user": user,
                        "is_offline_sync": is_offline_sync,
                    },
                    ValidationError(
                        {
                            "error_code": "ERR_CART_VERSION_CONFLICT",
                            "expected_version": exp_v,
                            "current_version": locked_sale.version,
                            "message_en": "Cart changed concurrently; refresh before allocating FEFO lots.",
                            "message_bn": "কার্ট পরিবর্তিত হয়েছে; রিফ্রেশ করে FEFO বরাদ্দ করুন।",
                        }
                    ),
                )

        # Release this sale's existing active reservations before locking and reallocating
        cls._release_reserved_allocations_in_tx(locked_sale)

        lines = list(locked_sale.lines.select_related("product", "preferred_batch").order_by("line_number"))
        if not lines:
            raise _FefoRejectionAbort(
                {
                    "sale": locked_sale,
                    "action": "BLOCKED_EMPTY_CART",
                    "was_rejected": True,
                    "rejection_code": "ERR_EMPTY_CART",
                    "reason": "Cannot allocate stock or finalize an empty POS cart",
                    "actor_user": user,
                    "is_offline_sync": is_offline_sync,
                },
                ValidationError(
                    {
                        "error_code": "ERR_EMPTY_CART",
                        "lines": "Cannot allocate stock or finalize an empty POS cart.",
                        "message_en": "Cart is empty; add medicines before checkout.",
                        "message_bn": "কার্ট খালি; চেকআউটের আগে ওষুধ যোগ করুন।",
                    }
                ),
            )

        product_ids = sorted({ln.product_id for ln in lines})
        # Lock all candidate branch batches in deterministic pk order to prevent multi-thread deadlocks
        all_batches = list(
            BatchStock.objects.select_for_update()
            .filter(
                tenant=locked_sale.tenant,
                branch=locked_sale.branch,
                product_id__in=product_ids,
            )
            .order_by("batch_id")
        )
        # Re-sort in deterministic FEFO order after acquiring pessimistic locks
        all_batches.sort(key=lambda b: (b.expiry_date, b.purchase_date, b.batch_number, b.batch_id))

        batches_by_product: Dict[str, List[BatchStock]] = {}
        batch_available_override: Dict[str, int] = {}
        for b in all_batches:
            batches_by_product.setdefault(b.product_id, []).append(b)
            batch_available_override[b.batch_id] = b.available_quantity_base_units

        quarantined_seg_ids = cls._quarantined_segregation_batch_ids(
            locked_sale.tenant_id, locked_sale.branch_id, [b.batch_id for b in all_batches]
        )
        today = timezone.now().date()
        overrides = cls._normalize_batch_overrides(batch_overrides)
        lot_cache: Dict[str, int] = {}
        recalled_product_cache: Dict[str, bool] = {}

        line_plans: List[Dict[str, Any]] = []
        for ln in lines:
            if ln.product_id not in recalled_product_cache:
                recalled_product_cache[ln.product_id] = cls._is_product_globally_recalled(ln.product)
            lp = cls._plan_line_fefo(
                user=user,
                sale=locked_sale,
                line=ln,
                all_batches=batches_by_product.get(ln.product_id, []),
                today=today,
                product_recalled=recalled_product_cache[ln.product_id],
                quarantined_seg_ids=quarantined_seg_ids,
                batch_available_override=batch_available_override,
                lot_reserved_remaining_cache=lot_cache,
                override_spec=overrides.get(ln.line_id),
                global_override_reason=override_reason,
                lock_cost_lots=True,
                raise_on_block=True,
            )
            line_plans.append((ln, lp))

        # Commit atomic reservations & allocation rows
        now = timezone.now()
        batch_take_totals: Dict[str, int] = {}
        for _ln, lp in line_plans:
            for sl in lp["planned_slices"]:
                batch_take_totals[sl["batch_id"]] = batch_take_totals.get(sl["batch_id"], 0) + int(
                    sl["allocated_base_units"]
                )

        batch_lookup = {b.batch_id: b for b in all_batches}
        for bid, qty_taken in batch_take_totals.items():
            b = batch_lookup[bid]
            b.reserved_quantity_base_units = int(b.reserved_quantity_base_units) + qty_taken
            b.save(update_fields=["reserved_quantity_base_units", "updated_at"])

        for ln, lp in line_plans:
            ov = overrides.get(ln.line_id)
            if ov and ov.get("batch_id") and ln.preferred_batch_id != ov["batch_id"]:
                ln.preferred_batch_id = ov["batch_id"]
                ln.save(update_fields=["preferred_batch", "updated_at"])

            for sl in lp["planned_slices"]:
                mode = PosAllocationMode.OFFLINE_RECONCILED if is_offline_sync else sl["allocation_mode"]
                PosSaleAllocation.objects.create(
                    allocation_id=f"palloc-{uuid.uuid4().hex[:12]}",
                    tenant=locked_sale.tenant,
                    branch=locked_sale.branch,
                    sale=locked_sale,
                    sale_line=ln,
                    product=ln.product,
                    batch=sl["batch"],
                    cost_lot=sl["cost_lot"],
                    allocated_base_units=int(sl["allocated_base_units"]),
                    unit_cost_bdt=sl["unit_cost_bdt"],
                    total_cost_bdt=sl["total_cost_bdt"],
                    status=PosAllocationStatus.RESERVED,
                    fefo_sequence=int(sl["fefo_sequence"]),
                    allocation_mode=mode,
                    expiry_date_snapshot=sl["expiry_date"],
                    override_reason=sl.get("override_reason", ""),
                    override_approved_by=sl.get("override_approved_by"),
                    allocated_by=user if getattr(user, "is_authenticated", False) else None,
                    allocated_at=now,
                )
                audit_act = (
                    "FEFO_OVERRIDE_APPROVED"
                    if sl["allocation_mode"] == PosAllocationMode.MANUAL_OVERRIDE
                    else ("OFFLINE_LOT_RECONCILED" if is_offline_sync else "FEFO_RESERVED")
                )
                cls.record_audit_safely(
                    {
                        "sale": locked_sale,
                        "sale_line": ln,
                        "batch": sl["batch"],
                        "action": audit_act,
                        "allocation_mode": mode,
                        "requested_base_units": int(ln.base_units_total),
                        "allocated_base_units": int(sl["allocated_base_units"]),
                        "batch_number_snapshot": sl["batch_number"],
                        "expiry_date_snapshot": sl["expiry_date"],
                        "was_rejected": False,
                        "reason": sl.get("override_reason") or "Deterministic FEFO batch stock reserved",
                        "actor_user": user,
                        "is_offline_sync": is_offline_sync,
                    }
                )

        old_state = locked_sale.lifecycle_state
        locked_sale.fefo_allocation_status = PosFefoAllocationStatus.RESERVED
        update_fields = ["fefo_allocation_status", "version", "updated_by", "updated_at"]
        if transition_lifecycle:
            if locked_sale.requires_rx_verification and not locked_sale.rx_verified_at:
                locked_sale.lifecycle_state = PosSaleLifecycleState.PENDING_RX_REVIEW
            elif locked_sale.paid_amount_bdt > Decimal("0.00") and locked_sale.paid_amount_bdt < locked_sale.grand_total_bdt:
                locked_sale.lifecycle_state = PosSaleLifecycleState.PARTIALLY_PAID
            else:
                locked_sale.lifecycle_state = PosSaleLifecycleState.CONFIRMED
            update_fields.append("lifecycle_state")

        locked_sale.version += 1
        locked_sale.updated_by = user
        locked_sale.save(update_fields=list(dict.fromkeys(update_fields)))

        if transition_lifecycle:
            PosDomainService._record_lifecycle_audit(
                sale=locked_sale,
                from_state=old_state,
                to_state=locked_sale.lifecycle_state,
                action="STOCK_ALLOCATED",
                user=user,
                reason="FEFO batch stock reserved for POS sale order",
            )
        return locked_sale

    @classmethod
    def allocate_sale_fefo(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        batch_overrides: Any = None,
        override_reason: str = "",
        expected_version: Optional[int] = None,
        is_offline_sync: bool = False,
        transition_lifecycle: bool = True,
    ) -> PosSaleOrder:
        """
        Public wrapper managing transaction/savepoint boundary and persisting rejection audits
        outside the rolled-back atomic block.
        """
        try:
            with transaction.atomic():
                return cls.allocate_sale_fefo_in_tx(
                    user,
                    sale,
                    batch_overrides=batch_overrides,
                    override_reason=override_reason,
                    expected_version=expected_version,
                    is_offline_sync=is_offline_sync,
                    transition_lifecycle=transition_lifecycle,
                )
        except _FefoRejectionAbort as abort:
            cls.record_audit_safely(abort.audit_kwargs)
            raise abort.exc

    @classmethod
    def ensure_and_validate_checkout_allocations_in_tx(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        batch_overrides: Any = None,
        override_reason: str = "",
        is_offline_sync: bool = False,
    ) -> PosSaleOrder:
        """
        Executed inside `PosDomainService.finalize_sale`:
        1. Releases expired hold reservations (P225 parity).
        2. Checks whether active `RESERVED` allocations already cover EVERY cart line's exact
           `base_units_total`, cost lots haven't drained below allocated quantities, AND all
           allocated batches remain safe (not expired, recalled, or quarantined).
        3. Otherwise re-runs `allocate_sale_fefo_in_tx(...)` atomically right before invoice posting!
        """
        if sale.hold_stock_reserved and sale.hold_reservation_expires_at and timezone.now() > sale.hold_reservation_expires_at:
            from apps.core.pos_held_cart_service import PosHeldCartService

            PosHeldCartService._release_sale_reservations(
                sale, target_status=PosAllocationStatus.EXPIRED, only_if_expired_or_unsafe=False
            )

        overrides = cls._normalize_batch_overrides(batch_overrides)
        lines = list(sale.lines.select_related("product").order_by("line_number"))
        if not lines or overrides:
            return cls.allocate_sale_fefo_in_tx(
                user,
                sale,
                batch_overrides=batch_overrides,
                override_reason=override_reason,
                is_offline_sync=is_offline_sync,
                transition_lifecycle=False,
            )

        active_allocs = list(
            sale.allocations.filter(status=PosAllocationStatus.RESERVED)
            .select_related("batch", "cost_lot", "product")
            .order_by("fefo_sequence", "allocation_id")
        )
        if not active_allocs:
            return cls.allocate_sale_fefo_in_tx(
                user,
                sale,
                is_offline_sync=is_offline_sync,
                transition_lifecycle=False,
            )

        # Verify exact coverage per line AND live safety of every reserved batch
        alloc_qty_by_line: Dict[str, int] = {}
        today = timezone.now().date()
        quarantined_seg_ids = cls._quarantined_segregation_batch_ids(
            sale.tenant_id, sale.branch_id, {a.batch_id for a in active_allocs}
        )
        recalled_prod_cache: Dict[str, bool] = {}

        all_valid = True
        for ln in lines:
            if ln.product_id not in recalled_prod_cache:
                recalled_prod_cache[ln.product_id] = cls._is_product_globally_recalled(ln.product)

        for a in active_allocs:
            alloc_qty_by_line[a.sale_line_id] = alloc_qty_by_line.get(a.sale_line_id, 0) + int(a.allocated_base_units)
            locked_batch = BatchStock.objects.select_for_update().get(pk=a.batch_id)
            is_safe, blk_cat, err_code, blk_reason = cls._classify_batch_safety(
                locked_batch,
                today=today,
                product_recalled=recalled_prod_cache.get(a.product_id, False),
                quarantined_seg_ids=quarantined_seg_ids,
            )
            if not is_safe:
                # If caller had pinned this specific preferred_batch on line and it turned unsafe, reject explicitly!
                line_obj = next((x for x in lines if x.line_id == a.sale_line_id), None)
                if line_obj and line_obj.preferred_batch_id == locked_batch.batch_id:
                    raise _FefoRejectionAbort(
                        {
                            "sale": sale,
                            "sale_line": line_obj,
                            "batch": locked_batch,
                            "action": f"BLOCKED_{blk_cat}",
                            "requested_base_units": int(line_obj.base_units_total),
                            "batch_number_snapshot": locked_batch.batch_number,
                            "expiry_date_snapshot": locked_batch.expiry_date,
                            "was_rejected": True,
                            "rejection_code": err_code,
                            "reason": blk_reason,
                            "actor_user": user,
                            "is_offline_sync": is_offline_sync,
                        },
                        ValidationError(
                            {
                                "error_code": err_code,
                                "batch_id": locked_batch.batch_id,
                                "batch_number": locked_batch.batch_number,
                                "message_en": blk_reason,
                                "message_bn": "সংরক্ষিত ব্যাচটি মেয়াদোত্তীর্ণ, কোয়ারেন্টিনড বা প্রত্যাহৃত হওয়ায় চেকআউট বাতিল করা হয়েছে।",
                                "detail": blk_reason,
                            }
                        ),
                    )
                all_valid = False
                break

            if a.cost_lot_id:
                locked_lot = PurchaseCostLot.objects.select_for_update().get(pk=a.cost_lot_id)
                if locked_lot.remaining_quantity < a.allocated_base_units:
                    all_valid = False
                    break

        if all_valid:
            for ln in lines:
                if alloc_qty_by_line.get(ln.line_id, 0) != int(ln.base_units_total):
                    all_valid = False
                    break

        if not all_valid:
            return cls.allocate_sale_fefo_in_tx(
                user,
                sale,
                is_offline_sync=is_offline_sync,
                transition_lifecycle=False,
            )

        return sale
