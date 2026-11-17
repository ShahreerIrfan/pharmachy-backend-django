"""
backend/apps/core/pos_held_cart_service.py
Architecture Decision: ADR-0225 (P225).
Phase 12: POS, Sales Orders, Checkout & Returns.

Domain Service for Held & Resumed Carts:
- Parks active non-empty POS carts (`HELD`) with reference, note, TTLs, canonical
  pricing snapshot hash/total, and optional row-locked FEFO BatchStock reservations.
- Detects and reports stale held carts safely (`GET /api/v2/pos/sales/held/`):
  * Hold TTL expiration (`HOLD_TTL_EXPIRED`)
  * Stock reservation timeout (`RESERVATION_EXPIRED`)
  * Catalog/tenant MRP or cart discount/tax drift (`PRICE_CHANGED`)
  * Live sellable inventory shortfall (`STOCK_SHORTAGE`)
  * Expired/quarantined/recalled reserved batches (`UNSAFE_BATCH`)
- Requires explicit cashier acknowledgement (`acknowledge_stale=True`) with clear EN/BN
  prompts before resuming a stale cart, releases expired/unsafe reservations back to
  BatchStock cleanly with zero conservation drift, and logs immutable PosHeldCartAudit rows.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchOperatingSettings,
    PosAllocationStatus,
    PosHeldCartAudit,
    PosSaleAllocation,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PurchaseCostLot,
    PurchaseCostLotStatus,
)
from apps.core.pos_cart_pricing_service import PosCartPricingService, q2
from apps.core.pos_cart_unit_service import PosCartUnitSelectionService
from apps.core.pos_domain_service import PosDomainService, q4

logger = logging.getLogger("pharmacy.core.pos_held_cart")

DEFAULT_HOLD_TTL_MINUTES = 30
DEFAULT_RESERVATION_TTL_MINUTES = 15
MIN_TTL_MINUTES = 1
MAX_TTL_MINUTES = 1440


class HeldCartDomainError(ValidationError):
    """Structured domain validation error preserving raw Python JSON types (bool/int/list/dict)."""

    def __init__(self, payload: Dict[str, Any], code: str | None = None) -> None:
        super().__init__(payload, code=code)
        self.raw_detail = payload


class _HoldCartAbort(Exception):
    """Signals atomic rollback while preserving outside-transaction PosHeldCartAudit records."""

    def __init__(self, audit_kwargs: Dict[str, Any], exc: APIException) -> None:
        super().__init__(str(exc))
        self.audit_kwargs = audit_kwargs
        self.exc = exc


class PosHeldCartService:
    """Authoritative service for holding, inspecting, expiring, cancelling, and safely resuming POS baskets."""

    HOLDABLE_STATES = {
        PosSaleLifecycleState.DRAFT,
        PosSaleLifecycleState.PENDING_RX_REVIEW,
        PosSaleLifecycleState.HELD,
    }

    @classmethod
    def parse_strict_ttl_minutes(
        cls,
        raw_value: Any,
        field_name: str,
        *,
        default_minutes: int,
    ) -> int:
        if raw_value is None or raw_value == "":
            return default_minutes
        if isinstance(raw_value, bool) or isinstance(raw_value, float):
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_INVALID_HOLD_TTL",
                    field_name: f"'{field_name}' must be a whole integer number of minutes between {MIN_TTL_MINUTES} and {MAX_TTL_MINUTES}.",
                    "message_en": f"'{field_name}' must be a whole integer ({MIN_TTL_MINUTES}-{MAX_TTL_MINUTES} min).",
                    "message_bn": f"'{field_name}'-এর মেয়াদ {MIN_TTL_MINUTES} থেকে {MAX_TTL_MINUTES} মিনিটের মধ্যে পূর্ণসংখ্যায় দিন।",
                }
            )
        if isinstance(raw_value, int):
            val = raw_value
        elif isinstance(raw_value, str):
            clean = raw_value.strip()
            if not re.fullmatch(r"[+-]?\d+", clean):
                raise HeldCartDomainError(
                    {
                        "error_code": "ERR_INVALID_HOLD_TTL",
                        field_name: f"'{field_name}' ('{raw_value}') must be a whole integer number of minutes.",
                        "message_en": f"'{field_name}' must be a whole integer ({MIN_TTL_MINUTES}-{MAX_TTL_MINUTES} min).",
                        "message_bn": f"'{field_name}'-এর মেয়াদ অবশ্যই পূর্ণসংখ্যায় হতে হবে।",
                    }
                )
            val = int(clean)
        else:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_INVALID_HOLD_TTL",
                    field_name: f"Invalid '{field_name}' type.",
                    "message_en": f"Invalid '{field_name}' value.",
                    "message_bn": "মেয়াদ নির্ধারণের মান সঠিক নয়।",
                }
            )

        if val < MIN_TTL_MINUTES or val > MAX_TTL_MINUTES:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_INVALID_HOLD_TTL",
                    field_name: f"'{field_name}' ({val}) must be between {MIN_TTL_MINUTES} and {MAX_TTL_MINUTES} minutes.",
                    "message_en": f"'{field_name}' must be between {MIN_TTL_MINUTES} and {MAX_TTL_MINUTES} minutes.",
                    "message_bn": f"মেয়াদ {MIN_TTL_MINUTES} থেকে {MAX_TTL_MINUTES} মিনিটের মধ্যে হতে হবে।",
                }
            )
        return val

    @classmethod
    def parse_strict_expected_version(cls, raw_value: Any) -> Optional[int]:
        if raw_value is None or raw_value == "":
            return None
        if isinstance(raw_value, bool) or isinstance(raw_value, float):
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_CART_VERSION_CONFLICT",
                    "expected_version": "expected_version must be a positive integer.",
                    "message_en": "Invalid cart version token.",
                    "message_bn": "কার্টের ভার্সন নম্বর সঠিক নয়।",
                }
            )
        try:
            clean = str(raw_value).strip()
            if not re.fullmatch(r"\d+", clean):
                raise ValueError("non-int")
            parsed = int(clean)
        except Exception:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_CART_VERSION_CONFLICT",
                    "expected_version": "expected_version must be a positive integer.",
                    "message_en": "Invalid cart version token.",
                    "message_bn": "কার্টের ভার্সন নম্বর সঠিক নয়।",
                }
            )
        if parsed < 1:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_CART_VERSION_CONFLICT",
                    "expected_version": "expected_version must be >= 1.",
                    "message_en": "Invalid cart version token.",
                    "message_bn": "কার্টের ভার্সন নম্বর ১ বা তার বেশি হতে হবে।",
                }
            )
        return parsed

    @classmethod
    def resolve_branch_hold_defaults(cls, branch: Branch) -> Tuple[int, int]:
        settings_obj = BranchOperatingSettings.objects.filter(
            tenant_id=branch.tenant_id, branch_id=branch.branch_id
        ).first()
        policies = getattr(settings_obj, "device_policies", None) if settings_obj else None
        if not isinstance(policies, dict):
            policies = {}
        try:
            hold_ttl = int(policies.get("hold_cart_ttl_minutes", DEFAULT_HOLD_TTL_MINUTES))
        except Exception:
            hold_ttl = DEFAULT_HOLD_TTL_MINUTES
        if hold_ttl < MIN_TTL_MINUTES or hold_ttl > MAX_TTL_MINUTES:
            hold_ttl = DEFAULT_HOLD_TTL_MINUTES

        try:
            res_ttl = int(policies.get("hold_reservation_ttl_minutes", min(hold_ttl, DEFAULT_RESERVATION_TTL_MINUTES)))
        except Exception:
            res_ttl = min(hold_ttl, DEFAULT_RESERVATION_TTL_MINUTES)
        if res_ttl < MIN_TTL_MINUTES or res_ttl > hold_ttl:
            res_ttl = min(hold_ttl, DEFAULT_RESERVATION_TTL_MINUTES)
        return hold_ttl, res_ttl

    @classmethod
    def _record_hold_audit(
        cls,
        *,
        sale: PosSaleOrder,
        action: str,
        user: Any,
        hold_reference: str = "",
        hold_note: str = "",
        reserve_stock_requested: bool = False,
        released_reserved_units: int = 0,
        held_grand_total_bdt: Optional[Decimal] = None,
        live_grand_total_bdt: Optional[Decimal] = None,
        held_pricing_hash: str = "",
        live_pricing_hash: str = "",
        was_stale: bool = False,
        stale_reasons: Optional[List[str]] = None,
        was_rejected: bool = False,
        rejection_code: str = "",
        rejection_message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PosHeldCartAudit:
        actor_role = str(getattr(user, "role", "SYSTEM") or "SYSTEM").upper()
        held_total = q2(held_grand_total_bdt if held_grand_total_bdt is not None else (sale.hold_snapshot_grand_total_bdt or sale.grand_total_bdt))
        live_total = q2(live_grand_total_bdt if live_grand_total_bdt is not None else sale.grand_total_bdt)
        audit = PosHeldCartAudit.objects.create(
            tenant=sale.tenant,
            branch=sale.branch,
            sale=sale,
            action=action,
            hold_reference=(hold_reference or sale.hold_reference or "")[:128],
            hold_note=hold_note or sale.hold_note or "",
            reserve_stock_requested=bool(reserve_stock_requested),
            released_reserved_units=max(0, int(released_reserved_units)),
            held_grand_total_bdt=held_total,
            live_grand_total_bdt=live_total,
            held_pricing_hash=(held_pricing_hash or sale.hold_snapshot_pricing_hash or "")[:64],
            live_pricing_hash=(live_pricing_hash or "")[:64],
            was_stale=bool(was_stale),
            stale_reasons=list(stale_reasons or []),
            was_rejected=bool(was_rejected),
            rejection_code=rejection_code[:64],
            rejection_message=rejection_message[:255],
            actor_user=user if getattr(user, "is_authenticated", False) else None,
            actor_role=actor_role,
            metadata_json=metadata or {},
        )
        AuditLogEntry.objects.create(
            tenant_id=sale.tenant_id,
            branch_id=sale.branch_id,
            user_id=str(getattr(user, "user_id", "") or getattr(user, "pk", "")),
            actor_email=str(getattr(user, "email", "")),
            actor_role=actor_role,
            request_id=f"hc-{uuid.uuid4().hex[:10]}",
            action=f"POS_HELD_{action}",
            resource_type="POS_SALE_ORDER",
            resource_id=sale.sale_id,
            status=AuditActionStatus.DENIED if was_rejected else AuditActionStatus.SUCCESS,
            before_state={
                "lifecycle_state": sale.lifecycle_state,
                "held_grand_total_bdt": str(held_total),
                "hold_stock_reserved": bool(sale.hold_stock_reserved),
            },
            after_state={
                "live_grand_total_bdt": str(live_total),
                "released_reserved_units": max(0, int(released_reserved_units)),
                "stale_reasons": list(stale_reasons or []),
            },
            metadata_json={
                "rejection_code": rejection_code,
                "rejection_message": rejection_message,
                **(metadata or {}),
            },
        )
        return audit

    @classmethod
    def _refresh_read_model_safe(cls, user: Any, branch_id: str, product_ids: List[str]) -> None:
        if not product_ids:
            return
        try:
            from apps.core.pos_search_cart_service import PosSearchCartService

            PosSearchCartService.refresh_branch_sku_read_model(
                user,
                branch_id=branch_id,
                product_ids=list(set(product_ids)),
                enforce_snapshot_role=False,
            )
        except Exception as exc:
            logger.warning("Non-fatal read model refresh failure after held reservation change: %s", exc)

    @classmethod
    def _release_sale_reservations(
        cls,
        sale: PosSaleOrder,
        *,
        target_status: str = PosAllocationStatus.RELEASED,
        only_if_expired_or_unsafe: bool = False,
        now: Optional[Any] = None,
    ) -> Tuple[int, List[str]]:
        now = now or timezone.now()
        reserved_allocs = list(
            sale.allocations.filter(status=PosAllocationStatus.RESERVED).select_related("batch")
        )
        released_units = 0
        affected_products: List[str] = []
        reservation_timed_out = bool(
            (sale.hold_reservation_expires_at and now > sale.hold_reservation_expires_at)
            or (sale.hold_expires_at and now > sale.hold_expires_at)
        )

        for alloc in reserved_allocs:
            batch = BatchStock.objects.select_for_update().get(pk=alloc.batch_id)
            batch_unsafe = bool(
                batch.is_expired or batch.status != BatchStatus.ACTIVE or batch.is_quarantined or batch.is_recalled
            )
            if only_if_expired_or_unsafe and not reservation_timed_out and not batch_unsafe:
                continue
            batch.reserved_quantity_base_units = max(0, batch.reserved_quantity_base_units - alloc.allocated_base_units)
            batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])
            alloc.status = target_status
            alloc.released_at = now
            alloc.save(update_fields=["status", "released_at"])
            released_units += int(alloc.allocated_base_units)
            affected_products.append(alloc.product_id)

        remaining_reserved = sale.allocations.filter(status=PosAllocationStatus.RESERVED).exists()
        if not remaining_reserved and sale.hold_stock_reserved:
            sale.hold_stock_reserved = False
            sale.save(update_fields=["hold_stock_reserved", "updated_at"])

        return released_units, affected_products

    @classmethod
    def _allocate_fefo_reserved_stock(
        cls,
        user: Any,
        sale: PosSaleOrder,
        lines: List[PosSaleOrderLine],
    ) -> Tuple[int, List[str]]:
        cls._release_sale_reservations(sale, target_status=PosAllocationStatus.RELEASED)
        total_reserved = 0
        affected_products: List[str] = []

        for line in lines:
            needed = int(line.base_units_total)
            candidate_batches: List[BatchStock] = []
            if line.preferred_batch_id:
                pb = BatchStock.objects.select_for_update().get(pk=line.preferred_batch_id)
                candidate_batches.append(pb)

            fefo_others = list(
                BatchStock.objects.select_for_update()
                .filter(
                    tenant=sale.tenant,
                    branch=sale.branch,
                    product=line.product,
                    status=BatchStatus.ACTIVE,
                    is_active=True,
                    is_quarantined=False,
                    recall_notice__isnull=True,
                )
                .exclude(pk=line.preferred_batch_id)
                .order_by("expiry_date", "batch_id")
            )
            candidate_batches.extend(fefo_others)

            remaining = needed
            for batch in candidate_batches:
                if remaining <= 0:
                    break
                if batch.is_expired or batch.status != BatchStatus.ACTIVE or batch.is_quarantined or batch.is_recalled:
                    if line.preferred_batch_id and batch.batch_id == line.preferred_batch_id:
                        raise _HoldCartAbort(
                            audit_kwargs={
                                "sale": sale,
                                "action": "HOLD_REJECTED",
                                "user": user,
                                "reserve_stock_requested": True,
                                "was_rejected": True,
                                "rejection_code": "ERR_UNSAFE_BATCH_BLOCKED",
                                "rejection_message": f"Preferred batch '{batch.batch_number}' is unsafe.",
                            },
                            exc=HeldCartDomainError(
                                {
                                    "error_code": "ERR_UNSAFE_BATCH_BLOCKED",
                                    "detail": (
                                        f"Preferred batch '{batch.batch_number}' cannot be reserved: "
                                        f"expired={batch.is_expired}, quarantined={batch.is_quarantined}, status={batch.status}."
                                    ),
                                    "message_en": f"Preferred batch '{batch.batch_number}' cannot be reserved due to expiry/quarantine/recall.",
                                    "message_bn": f"নির্বাচিত ব্যাচ '{batch.batch_number}' মেয়াদোত্তীর্ণ/কোয়ারেন্টাইন/রিকল অবস্থায় থাকায় রিজার্ভ করা যাবে না।",
                                }
                            ),
                        )
                    continue

                avail = batch.available_quantity_base_units
                if avail <= 0:
                    continue

                take = min(remaining, avail)
                batch.reserved_quantity_base_units += take
                batch.save(update_fields=["reserved_quantity_base_units", "updated_at"])

                cost_lot = (
                    PurchaseCostLot.objects.filter(
                        tenant=sale.tenant,
                        branch=sale.branch,
                        batch=batch,
                        status=PurchaseCostLotStatus.ACTIVE,
                        remaining_quantity__gt=0,
                    )
                    .order_by("purchase_date", "lot_id")
                    .first()
                )
                unit_cost = q4(cost_lot.unit_cost if cost_lot else batch.cost_per_unit)

                PosSaleAllocation.objects.create(
                    allocation_id=f"palloc-{uuid.uuid4().hex[:12]}",
                    tenant=sale.tenant,
                    branch=sale.branch,
                    sale=sale,
                    sale_line=line,
                    product=line.product,
                    batch=batch,
                    cost_lot=cost_lot,
                    allocated_base_units=take,
                    unit_cost_bdt=unit_cost,
                    total_cost_bdt=q4(unit_cost * Decimal(take)),
                    status=PosAllocationStatus.RESERVED,
                    allocated_by=user,
                )
                total_reserved += take
                affected_products.append(line.product_id)
                remaining -= take

            if remaining > 0:
                raise _HoldCartAbort(
                    audit_kwargs={
                        "sale": sale,
                        "action": "HOLD_REJECTED",
                        "user": user,
                        "reserve_stock_requested": True,
                        "was_rejected": True,
                        "rejection_code": "ERR_INSUFFICIENT_DISPENSABLE_STOCK",
                        "rejection_message": f"Insufficient safe FEFO stock for '{line.product.brand_name}': short by {remaining}.",
                    },
                    exc=HeldCartDomainError(
                        {
                            "error_code": "ERR_INSUFFICIENT_DISPENSABLE_STOCK",
                            "detail": (
                                f"Insufficient safe FEFO stock to reserve '{line.product.brand_name}': "
                                f"needed {needed} base units, short by {remaining} base units."
                            ),
                            "message_en": (
                                f"Insufficient sellable stock to reserve '{line.product.brand_name}' "
                                f"(needed {needed}, short by {remaining})."
                            ),
                            "message_bn": (
                                f"'{line.product.brand_name}'-এর পর্যাপ্ত বিক্রয়যোগ্য স্টক নেই "
                                f"(প্রয়োজন {needed}, ঘাটতি {remaining})।"
                            ),
                        }
                    ),
                )

        return total_reserved, list(set(affected_products))

    @classmethod
    def _simulate_live_grand_total(
        cls,
        sale: PosSaleOrder,
        lines: List[PosSaleOrderLine],
        live_prices: Dict[str, Decimal],
    ) -> Decimal:
        gross_subtotal = Decimal("0.00")
        line_discounts_sum = Decimal("0.00")
        lines_total_sum = Decimal("0.00")
        line_tax_sum = Decimal("0.00")
        sale_tax_mode = str(sale.tax_mode or "EXCLUSIVE").upper()

        for line in lines:
            qty = int(line.quantity_units)
            unit_price = q2(live_prices.get(line.line_id, line.unit_price_bdt))
            line_gross = q2(Decimal(qty) * unit_price)

            disc_type = str(line.discount_type or "NONE").upper()
            if disc_type == "PERCENTAGE":
                line_disc = q2(line_gross * q2(line.discount_rate_pct) / Decimal("100.00"))
            else:
                line_disc = min(line_gross, q2(line.discount_amount_bdt))

            after_disc = q2(line_gross - line_disc)
            eff_mode = str(line.tax_mode or sale_tax_mode).upper()
            tax_rate = q2(line.tax_rate_pct)
            if tax_rate == Decimal("0.00"):
                line_tax = Decimal("0.00")
                line_total = after_disc
            elif eff_mode == "INCLUSIVE":
                line_total = after_disc
                divisor = Decimal("1.00") + (tax_rate / Decimal("100.00"))
                net_bt = (line_total / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                line_tax = q2(line_total - net_bt)
            else:
                line_tax = q2(after_disc * tax_rate / Decimal("100.00"))
                line_total = q2(after_disc + line_tax)

            gross_subtotal = q2(gross_subtotal + line_gross)
            line_discounts_sum = q2(line_discounts_sum + line_disc)
            line_tax_sum = q2(line_tax_sum + line_tax)
            lines_total_sum = q2(lines_total_sum + line_total)

        after_line_discounts = q2(gross_subtotal - line_discounts_sum)
        hdr_disc_type = str(sale.discount_type or "NONE").upper()
        if hdr_disc_type == "PERCENTAGE":
            hdr_discount = q2(after_line_discounts * q2(sale.discount_rate_pct) / Decimal("100.00"))
        elif hdr_disc_type in ("FLAT_BDT", "PROMO_POLICY"):
            hdr_discount = min(after_line_discounts, q2(sale.header_discount_amount_bdt or sale.discount_amount_bdt))
        else:
            hdr_discount = Decimal("0.00")

        total_discount = q2(line_discounts_sum + hdr_discount)
        hdr_tax_rate = q2(sale.tax_rate_pct)
        taxable = max(Decimal("0.00"), q2(gross_subtotal - total_discount))
        if hdr_tax_rate > Decimal("0.00") and line_tax_sum == Decimal("0.00"):
            if sale_tax_mode == "INCLUSIVE":
                total_before_rounding = taxable
            else:
                extra_tax = q2(taxable * hdr_tax_rate / Decimal("100.00"))
                total_before_rounding = q2(taxable + extra_tax)
        else:
            total_before_rounding = max(Decimal("0.00"), q2(lines_total_sum - hdr_discount))

        rounding_mode = str(sale.rounding_mode or "NONE").upper()
        if rounding_mode == "NONE":
            return q2(total_before_rounding)
        if rounding_mode == "HALF_UP_WHOLE_BDT":
            return total_before_rounding.quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(Decimal("0.01"))
        if rounding_mode == "FLOOR_WHOLE_BDT":
            from decimal import ROUND_FLOOR

            return total_before_rounding.quantize(Decimal("1"), rounding=ROUND_FLOOR).quantize(Decimal("0.01"))
        if rounding_mode == "NEAREST_FIVE_BDT":
            units_five = (total_before_rounding / Decimal("5.00")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            return q2(units_five * Decimal("5.00"))
        return max(Decimal("0.00"), q2(total_before_rounding + q2(sale.rounding_adjustment_bdt)))

    @classmethod
    def inspect_held_cart_staleness(
        cls,
        sale: PosSaleOrder,
        *,
        now: Optional[Any] = None,
        extra_unsafe_product_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        """Non-mutating staleness check evaluating hold TTL, reservation TTL, price/MRP drift, stock & unsafe batches."""
        now = now or timezone.now()
        lines = list(sale.lines.select_related("product", "variant").order_by("line_number"))
        current_breakdown = PosCartPricingService.build_canonical_breakdown(sale, lines)
        current_hash = current_breakdown["calculated_pricing_hash"]

        held_total = q2(
            sale.hold_snapshot_grand_total_bdt
            if sale.hold_snapshot_grand_total_bdt is not None
            else sale.grand_total_bdt
        )
        snapshot_hash = sale.hold_snapshot_pricing_hash or current_hash

        is_hold_expired = bool(
            sale.lifecycle_state == PosSaleLifecycleState.HELD
            and sale.hold_expires_at is not None
            and now > sale.hold_expires_at
        )

        reserved_allocs = list(
            sale.allocations.filter(status=PosAllocationStatus.RESERVED).select_related("batch")
        )
        expired_allocs_exist = sale.allocations.filter(status=PosAllocationStatus.EXPIRED).exists()
        is_reservation_expired = bool(
            (
                sale.hold_stock_reserved
                and sale.hold_reservation_expires_at is not None
                and now > sale.hold_reservation_expires_at
            )
            or (
                sale.hold_reservation_expires_at is not None
                and now > sale.hold_reservation_expires_at
                and expired_allocs_exist
            )
        )

        # Evaluate unsafe batches & reserved quantities held by this sale
        unsafe_batch_products: set[str] = set(extra_unsafe_product_ids or ())
        reserved_by_product: Dict[str, int] = {}
        for alloc in reserved_allocs:
            b = alloc.batch
            unsafe = bool(b.is_expired or b.status != BatchStatus.ACTIVE or b.is_quarantined or b.is_recalled)
            if unsafe:
                unsafe_batch_products.add(alloc.product_id)
            elif not is_reservation_expired and not is_hold_expired:
                # Active valid reservation belongs to this cart
                reserved_by_product[alloc.product_id] = reserved_by_product.get(alloc.product_id, 0) + int(alloc.allocated_base_units)
            else:
                # If reservation timed out, its reserved qty is about to be released back to sellable pool
                reserved_by_product[alloc.product_id] = reserved_by_product.get(alloc.product_id, 0) + int(alloc.allocated_base_units)
        if sale.held_at is not None:
            for exp_alloc in sale.allocations.filter(
                status=PosAllocationStatus.EXPIRED,
                released_at__gte=sale.held_at,
            ).select_related("batch"):
                b = exp_alloc.batch
                if b.is_expired or b.status != BatchStatus.ACTIVE or b.is_quarantined or b.is_recalled:
                    unsafe_batch_products.add(exp_alloc.product_id)

        # Evaluate catalog/tenant MRP drift for CANONICAL_MRP lines
        price_drift_items: List[Dict[str, Any]] = []
        live_prices: Dict[str, Decimal] = {}
        for line in lines:
            held_unit_price = q2(line.unit_price_bdt)
            live_unit_price = held_unit_price
            if str(line.price_source or "") == "CANONICAL_MRP":
                pkg_ctx = PosCartUnitSelectionService.resolve_packaging_context(
                    sale.tenant, sale.branch, line.product, variant=line.variant
                )
                live_unit_price = PosCartUnitSelectionService.compute_unit_price_bdt(
                    tenant=sale.tenant,
                    product=line.product,
                    variant=line.variant,
                    packaging_unit=line.packaging_unit,
                    conversion_factor=int(line.conversion_factor),
                    pkg_ctx=pkg_ctx,
                )
                if live_unit_price != held_unit_price:
                    price_drift_items.append(
                        {
                            "line_id": line.line_id,
                            "product_id": line.product_id,
                            "product_brand_name": line.product.brand_name,
                            "packaging_unit": line.packaging_unit,
                            "held_unit_price_bdt": f"{held_unit_price:.2f}",
                            "live_unit_price_bdt": f"{live_unit_price:.2f}",
                        }
                    )
            live_prices[line.line_id] = live_unit_price

        live_grand_total = (
            cls._simulate_live_grand_total(sale, lines, live_prices)
            if price_drift_items
            else q2(sale.grand_total_bdt)
        )
        hash_drifted = bool(snapshot_hash and current_hash != snapshot_hash)
        has_price_change = bool(price_drift_items or hash_drifted or live_grand_total != held_total)

        # Live batch stock availability per line product
        today = now.date()
        product_ids = list({l.product_id for l in lines})
        avail_by_product: Dict[str, int] = {pid: reserved_by_product.get(pid, 0) for pid in product_ids}
        if product_ids:
            for b in BatchStock.objects.filter(
                tenant=sale.tenant,
                branch=sale.branch,
                product_id__in=product_ids,
                status=BatchStatus.ACTIVE,
                is_active=True,
                is_quarantined=False,
                recall_notice__isnull=True,
                expiry_date__gt=today,
            ):
                avail_by_product[b.product_id] = avail_by_product.get(b.product_id, 0) + max(0, int(b.available_quantity_base_units))

        shortfall_items: List[Dict[str, Any]] = []
        req_by_product: Dict[str, int] = {}
        for line in lines:
            req_by_product[line.product_id] = req_by_product.get(line.product_id, 0) + int(line.base_units_total)

        seen_short_pids: set[str] = set()
        for line in lines:
            pid = line.product_id
            if pid in seen_short_pids:
                continue
            seen_short_pids.add(pid)
            needed_total = req_by_product.get(pid, int(line.base_units_total))
            sellable_avail = avail_by_product.get(pid, 0)
            unsafe_flag = pid in unsafe_batch_products
            if sellable_avail < needed_total or unsafe_flag:
                shortfall_items.append(
                    {
                        "line_id": line.line_id,
                        "product_id": pid,
                        "product_brand_name": line.product.brand_name,
                        "required_base_units": needed_total,
                        "sellable_available_base_units": sellable_avail,
                        "shortfall_base_units": max(0, needed_total - sellable_avail),
                        "unsafe_batch_detected": unsafe_flag,
                    }
                )

        has_unsafe_batch = bool(unsafe_batch_products)
        has_stock_shortage = any(item["shortfall_base_units"] > 0 for item in shortfall_items)

        stale_reasons: List[str] = []
        en_parts: List[str] = []
        bn_parts: List[str] = []

        if is_hold_expired:
            stale_reasons.append("HOLD_TTL_EXPIRED")
            en_parts.append("Hold time limit expired.")
            bn_parts.append("হোল্ড করার সময়সীমা শেষ হয়েছে।")
        if is_reservation_expired:
            stale_reasons.append("RESERVATION_EXPIRED")
            en_parts.append("Stock reservation timed out and was released.")
            bn_parts.append("স্টক রিজার্ভেশনের মেয়াদ শেষ হয়ে রিলিজ হয়ে গেছে।")
        if has_price_change:
            stale_reasons.append("PRICE_CHANGED")
            en_parts.append(f"Cart price changed ({held_total:.2f} -> {live_grand_total:.2f} BDT).")
            bn_parts.append(f"কার্টের মূল্য পরিবর্তিত হয়েছে ({held_total:.2f} -> {live_grand_total:.2f} টাকা)।")
        if has_unsafe_batch:
            stale_reasons.append("UNSAFE_BATCH")
            en_parts.append("Reserved batch became expired, quarantined, or recalled.")
            bn_parts.append("রিজার্ভকৃত ব্যাচ মেয়াদোত্তীর্ণ, কোয়ারেন্টাইন বা রিকল হয়েছে।")
        if has_stock_shortage:
            stale_reasons.append("STOCK_SHORTAGE")
            en_parts.append("Insufficient sellable stock remains for one or more items.")
            bn_parts.append("এক বা একাধিক ওষুধের পর্যাপ্ত বিক্রয়যোগ্য স্টক বর্তমানে নেই।")

        is_stale = bool(stale_reasons)
        prompt_en = " ".join(en_parts) if is_stale else "Held basket pricing and stock are up to date."
        prompt_bn = " ".join(bn_parts) if is_stale else "হোল্ড করা কার্টের মূল্য এবং স্টক হালনাগাদ রয়েছে।"

        return {
            "is_stale": is_stale,
            "is_hold_expired": is_hold_expired,
            "is_reservation_expired": is_reservation_expired,
            "has_price_change": has_price_change,
            "has_stock_shortage": has_stock_shortage,
            "has_unsafe_batch": has_unsafe_batch,
            "stale_reasons": stale_reasons,
            "held_grand_total_bdt": f"{held_total:.2f}",
            "live_grand_total_bdt": f"{live_grand_total:.2f}",
            "price_delta_bdt": f"{q2(live_grand_total - held_total):.2f}",
            "price_drift_items": price_drift_items,
            "shortfall_items": shortfall_items,
            "held_pricing_hash": snapshot_hash,
            "live_pricing_hash": current_hash,
            "user_prompt_en": prompt_en,
            "user_prompt_bn": prompt_bn,
        }

    @classmethod
    def hold_cart(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Optional[Dict[str, Any]] = None,
    ) -> PosSaleOrder:
        data = data or {}
        expected_version = cls.parse_strict_expected_version(data.get("expected_version"))
        try:
            updated, affected_pids = cls._hold_cart_atomic(
                user=user,
                sale=sale,
                data=data,
                expected_version=expected_version,
            )
            cls._refresh_read_model_safe(user, updated.branch_id, affected_pids)
            return updated
        except _HoldCartAbort as abort:
            cls._record_hold_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _hold_cart_atomic(
        cls,
        *,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
        expected_version: Optional[int],
    ) -> Tuple[PosSaleOrder, List[str]]:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        _, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=sale.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )

        if expected_version is not None and int(sale.version) != expected_version:
            raise _HoldCartAbort(
                audit_kwargs={
                    "sale": sale,
                    "action": "HOLD_REJECTED",
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_CART_VERSION_CONFLICT",
                    "rejection_message": f"Expected version {expected_version}, got {sale.version}.",
                },
                exc=HeldCartDomainError(
                    {
                        "error_code": "ERR_CART_VERSION_CONFLICT",
                        "expected_version": expected_version,
                        "actual_version": int(sale.version),
                        "detail": f"Cart was modified concurrently (expected v{expected_version}, current v{sale.version}).",
                        "message_en": "Cart changed on another terminal. Refresh before holding.",
                        "message_bn": "অন্য টার্মিনাল থেকে কার্টটি পরিবর্তিত হয়েছে। অনুগ্রহ করে রিফ্রেশ করুন।",
                    }
                ),
            )

        old_state = sale.lifecycle_state
        if old_state not in cls.HOLDABLE_STATES:
            raise _HoldCartAbort(
                audit_kwargs={
                    "sale": sale,
                    "action": "HOLD_REJECTED",
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "rejection_message": f"Cannot hold POS sale in '{old_state}' state.",
                },
                exc=HeldCartDomainError(
                    {
                        "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                        "detail": f"Cannot transition POS sale from '{old_state}' to 'HELD'.",
                        "message_en": f"Cart in '{old_state}' state cannot be placed on hold.",
                        "message_bn": f"'{old_state}' অবস্থার কার্ট হোল্ড করা যাবে না।",
                    }
                ),
            )

        lines = list(sale.lines.select_related("product", "variant", "preferred_batch").order_by("line_number"))
        if not lines:
            raise _HoldCartAbort(
                audit_kwargs={
                    "sale": sale,
                    "action": "HOLD_REJECTED",
                    "user": user,
                    "was_rejected": True,
                    "rejection_code": "ERR_EMPTY_CART",
                    "rejection_message": "Cannot hold an empty cart.",
                },
                exc=HeldCartDomainError(
                    {
                        "error_code": "ERR_EMPTY_CART",
                        "lines": "Cannot hold an empty POS cart.",
                        "message_en": "Add at least one medicine before holding the cart.",
                        "message_bn": "কার্ট হোল্ড করার আগে কমপক্ষে একটি ওষুধ যোগ করুন।",
                    }
                ),
            )

        branch_default_hold, branch_default_res = cls.resolve_branch_hold_defaults(branch or sale.branch)
        hold_ttl_minutes = cls.parse_strict_ttl_minutes(
            data.get("hold_ttl_minutes"), "hold_ttl_minutes", default_minutes=branch_default_hold
        )
        reserve_stock = bool(data.get("reserve_stock", False))
        default_res_for_hold = min(hold_ttl_minutes, branch_default_res)
        res_ttl_minutes = cls.parse_strict_ttl_minutes(
            data.get("reservation_ttl_minutes"), "reservation_ttl_minutes", default_minutes=default_res_for_hold
        )
        if res_ttl_minutes > hold_ttl_minutes:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_INVALID_HOLD_TTL",
                    "reservation_ttl_minutes": "reservation_ttl_minutes cannot exceed hold_ttl_minutes.",
                    "message_en": "Stock reservation TTL cannot exceed overall cart hold TTL.",
                    "message_bn": "স্টক রিজার্ভেশনের সময় হোল্ডের মোট সময়ের চেয়ে বেশি হতে পারবে না।",
                }
            )

        # Ensure canonical prices & snapshot hash before holding
        sale, breakdown = PosCartPricingService.recompute_sale_totals(sale, user, allow_empty=False)
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
            sale, actor_user=user, mutate=True, reason="Held cart checked against live Rx rules"
        )
        sale.refresh_from_db()
        now = timezone.now()

        hold_ref = str(data.get("hold_reference") or sale.hold_reference or "").strip()[:128]
        hold_note = str(data.get("hold_note") if "hold_note" in data else (sale.hold_note or "")).strip()[:500]
        if not hold_ref:
            hold_ref = f"HOLD-{sale.order_number[-6:]}"

        affected_products: List[str] = []
        if reserve_stock:
            _, affected_products = cls._allocate_fefo_reserved_stock(user, sale, lines)
            sale.hold_stock_reserved = True
            sale.hold_reservation_expires_at = now + timedelta(minutes=res_ttl_minutes)
        else:
            released_units, rel_pids = cls._release_sale_reservations(
                sale, target_status=PosAllocationStatus.RELEASED, now=now
            )
            if released_units > 0:
                affected_products.extend(rel_pids)
            sale.hold_stock_reserved = False
            sale.hold_reservation_expires_at = None

        sale.lifecycle_state = PosSaleLifecycleState.HELD
        sale.hold_reference = hold_ref
        sale.hold_note = hold_note
        sale.held_at = now
        sale.held_by = user
        sale.hold_expires_at = now + timedelta(minutes=hold_ttl_minutes)
        sale.hold_snapshot_pricing_hash = breakdown["calculated_pricing_hash"]
        sale.hold_snapshot_grand_total_bdt = q2(sale.grand_total_bdt)
        sale.version += 1
        sale.updated_by = user
        sale.save()

        cls._record_hold_audit(
            sale=sale,
            action="HELD",
            user=user,
            hold_reference=hold_ref,
            hold_note=hold_note,
            reserve_stock_requested=reserve_stock,
            held_grand_total_bdt=q2(sale.grand_total_bdt),
            live_grand_total_bdt=q2(sale.grand_total_bdt),
            held_pricing_hash=sale.hold_snapshot_pricing_hash,
            live_pricing_hash=sale.hold_snapshot_pricing_hash,
            metadata={
                "hold_ttl_minutes": hold_ttl_minutes,
                "reservation_ttl_minutes": res_ttl_minutes if reserve_stock else 0,
            },
        )
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=PosSaleLifecycleState.HELD,
            action="HOLD_CART",
            user=user,
            reason=hold_ref,
            metadata={
                "reserve_stock": reserve_stock,
                "hold_expires_at": sale.hold_expires_at.isoformat(),
                "hold_reservation_expires_at": (
                    sale.hold_reservation_expires_at.isoformat() if sale.hold_reservation_expires_at else None
                ),
            },
        )
        return sale, affected_products

    @classmethod
    def _release_expired_before_resume(
        cls,
        user: Any,
        sale_id: str,
        expected_version: Optional[int],
    ) -> Tuple[int, List[str], set[str]]:
        """Atomically validates state/version and frees already-expired or unsafe stock locks prior to resume checks."""
        with transaction.atomic():
            sale = PosSaleOrder.objects.select_for_update().get(pk=sale_id)
            PosDomainService.resolve_user_tenant_and_branch(
                user,
                branch_id=sale.branch_id,
                require_write=True,
                allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
            )

            if expected_version is not None and int(sale.version) != expected_version:
                raise _HoldCartAbort(
                    audit_kwargs={
                        "sale": sale,
                        "action": "RESUME_REJECTED",
                        "user": user,
                        "was_rejected": True,
                        "rejection_code": "ERR_CART_VERSION_CONFLICT",
                        "rejection_message": f"Expected version {expected_version}, got {sale.version}.",
                    },
                    exc=HeldCartDomainError(
                        {
                            "error_code": "ERR_CART_VERSION_CONFLICT",
                            "expected_version": expected_version,
                            "actual_version": int(sale.version),
                            "detail": f"Held cart was modified concurrently (expected v{expected_version}, current v{sale.version}).",
                            "message_en": "Held cart changed on another terminal. Refresh held baskets.",
                            "message_bn": "হোল্ড করা কার্টটি অন্য টার্মিনাল থেকে পরিবর্তিত হয়েছে। অনুগ্রহ করে রিফ্রেশ করুন।",
                        }
                    ),
                )

            if sale.lifecycle_state != PosSaleLifecycleState.HELD:
                raise _HoldCartAbort(
                    audit_kwargs={
                        "sale": sale,
                        "action": "RESUME_REJECTED",
                        "user": user,
                        "was_rejected": True,
                        "rejection_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                        "rejection_message": f"Cannot resume sale in '{sale.lifecycle_state}' state.",
                    },
                    exc=HeldCartDomainError(
                        {
                            "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                            "detail": f"Cannot resume POS sale from '{sale.lifecycle_state}' state (must be HELD).",
                            "message_en": f"Only HELD carts can be resumed (current state: '{sale.lifecycle_state}').",
                            "message_bn": f"শুধুমাত্র HELD অবস্থার কার্ট রিজিউম করা যায় (বর্তমান: '{sale.lifecycle_state}')।",
                        }
                    ),
                )

            now = timezone.now()
            unsafe_pids = {
                alloc.product_id
                for alloc in sale.allocations.filter(status=PosAllocationStatus.RESERVED).select_related("batch")
                if (
                    alloc.batch.is_expired
                    or alloc.batch.status != BatchStatus.ACTIVE
                    or alloc.batch.is_quarantined
                    or alloc.batch.is_recalled
                )
            }
            timed_out = bool(
                (sale.hold_reservation_expires_at and now > sale.hold_reservation_expires_at)
                or (sale.hold_expires_at and now > sale.hold_expires_at)
            )
            released_units, pids = cls._release_sale_reservations(
                sale,
                target_status=PosAllocationStatus.EXPIRED,
                only_if_expired_or_unsafe=True,
                now=now,
            )
            if released_units > 0:
                reasons: List[str] = []
                if timed_out:
                    reasons.append("RESERVATION_EXPIRED")
                if unsafe_pids:
                    reasons.append("UNSAFE_BATCH")
                cls._record_hold_audit(
                    sale=sale,
                    action="UNSAFE_BATCH_RELEASED" if (unsafe_pids and not timed_out) else "RESERVATION_EXPIRED",
                    user=user,
                    reserve_stock_requested=True,
                    released_reserved_units=released_units,
                    was_stale=True,
                    stale_reasons=reasons or ["RESERVATION_EXPIRED"],
                    metadata={"released_before_resume": True},
                )
            return released_units, pids, unsafe_pids

    @classmethod
    def resume_cart(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[PosSaleOrder, Dict[str, Any]]:
        data = data or {}
        expected_version = cls.parse_strict_expected_version(data.get("expected_version"))
        acknowledge_stale = bool(data.get("acknowledge_stale", False))
        reprice_to_live = bool(data.get("reprice_to_live", True))
        re_reserve_stock = bool(data.get("re_reserve_stock", False))
        allow_unreserved_shortage = bool(data.get("allow_unreserved_shortage", False))
        resume_note = str(data.get("note") or "").strip()[:500]

        try:
            released_units, released_pids, unsafe_pids = cls._release_expired_before_resume(
                user=user,
                sale_id=sale.sale_id,
                expected_version=expected_version,
            )
            if released_pids:
                cls._refresh_read_model_safe(user, sale.branch_id, released_pids)

            resumed, staleness, reserved_pids = cls._resume_cart_atomic(
                user=user,
                sale_id=sale.sale_id,
                acknowledge_stale=acknowledge_stale,
                reprice_to_live=reprice_to_live,
                re_reserve_stock=re_reserve_stock,
                allow_unreserved_shortage=allow_unreserved_shortage,
                resume_note=resume_note,
                released_units_prior=released_units,
                prior_unsafe_product_ids=unsafe_pids,
            )
            if reserved_pids:
                cls._refresh_read_model_safe(user, resumed.branch_id, reserved_pids)
            return resumed, staleness
        except _HoldCartAbort as abort:
            cls._record_hold_audit(**abort.audit_kwargs)
            raise abort.exc

    @classmethod
    @transaction.atomic
    def _resume_cart_atomic(
        cls,
        *,
        user: Any,
        sale_id: str,
        acknowledge_stale: bool,
        reprice_to_live: bool,
        re_reserve_stock: bool,
        allow_unreserved_shortage: bool,
        resume_note: str,
        released_units_prior: int,
        prior_unsafe_product_ids: Optional[set[str]] = None,
    ) -> Tuple[PosSaleOrder, Dict[str, Any], List[str]]:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale_id)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=sale.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )

        now = timezone.now()
        staleness = cls.inspect_held_cart_staleness(
            sale, now=now, extra_unsafe_product_ids=prior_unsafe_product_ids
        )
        if staleness["is_stale"] and not acknowledge_stale:
            raise _HoldCartAbort(
                audit_kwargs={
                    "sale": sale,
                    "action": "RESUME_STALE_PROMPTED",
                    "user": user,
                    "was_stale": True,
                    "stale_reasons": staleness["stale_reasons"],
                    "held_grand_total_bdt": Decimal(staleness["held_grand_total_bdt"]),
                    "live_grand_total_bdt": Decimal(staleness["live_grand_total_bdt"]),
                    "held_pricing_hash": staleness["held_pricing_hash"],
                    "live_pricing_hash": staleness["live_pricing_hash"],
                    "released_reserved_units": released_units_prior,
                    "was_rejected": True,
                    "rejection_code": "ERR_HELD_CART_REQUIRES_CONFIRMATION",
                    "rejection_message": staleness["user_prompt_en"][:255],
                    "metadata": {"price_drift_items": staleness["price_drift_items"], "shortfall_items": staleness["shortfall_items"]},
                },
                exc=HeldCartDomainError(
                    {
                        "error_code": "ERR_HELD_CART_REQUIRES_CONFIRMATION",
                        "requires_confirmation": True,
                        "message_en": (
                            f"{staleness['user_prompt_en']} Confirm with acknowledge_stale=true to resume with updated prices/stock."
                        ),
                        "message_bn": (
                            f"{staleness['user_prompt_bn']} হালনাগাদ মূল্য/স্টক সহ রিজিউম করতে নিশ্চিত করুন।"
                        ),
                        **staleness,
                    }
                ),
            )

        lines = list(sale.lines.select_related("product", "variant", "preferred_batch").order_by("line_number"))
        if not lines:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_EMPTY_CART",
                    "lines": "Held cart contains no line items.",
                    "message_en": "Held cart is empty and cannot be resumed.",
                    "message_bn": "হোল্ড করা কার্টটি খালি; রিজিউম করা যাবে না।",
                }
            )

        # Reprice CANONICAL_MRP lines when stale price drift occurred
        repriced_lines = False
        if reprice_to_live and staleness["price_drift_items"]:
            drift_map = {item["line_id"]: Decimal(item["live_unit_price_bdt"]) for item in staleness["price_drift_items"]}
            for line in lines:
                if line.line_id in drift_map:
                    line.unit_price_bdt = drift_map[line.line_id]
                    line.save(update_fields=["unit_price_bdt", "updated_at"])
                    repriced_lines = True

        if repriced_lines or staleness["has_price_change"]:
            sale.pricing_confirmed_at = None
            sale.pricing_confirmed_by = None
            sale.pricing_confirmation_hash = ""
            sale.save(update_fields=["pricing_confirmed_at", "pricing_confirmed_by", "pricing_confirmation_hash", "updated_at"])

        sale, breakdown = PosCartPricingService.recompute_sale_totals(sale, user, allow_empty=False)
        from apps.core.pos_rx_safety_service import PosRxSafetyService

        PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
            sale, actor_user=user, mutate=True, reason="Held cart checked against live Rx rules"
        )
        sale.refresh_from_db()

        reserved_pids: List[str] = []
        if re_reserve_stock:
            if not staleness["has_stock_shortage"] or not allow_unreserved_shortage:
                _, reserved_pids = cls._allocate_fefo_reserved_stock(user, sale, lines)
                sale.hold_stock_reserved = True

        target_state = (
            PosSaleLifecycleState.PENDING_RX_REVIEW
            if sale.requires_rx_verification and not sale.rx_verified_at
            else PosSaleLifecycleState.DRAFT
        )
        old_state = sale.lifecycle_state
        sale.lifecycle_state = target_state
        sale.resumed_at = now
        sale.resumed_by = user
        if not re_reserve_stock and not sale.allocations.filter(status=PosAllocationStatus.RESERVED).exists():
            sale.hold_stock_reserved = False
        if resume_note:
            sale.notes = f"{sale.notes}\n{resume_note}".strip() if sale.notes else resume_note
        sale.version += 1
        sale.updated_by = user
        sale.save()

        cls._record_hold_audit(
            sale=sale,
            action="RESUMED",
            user=user,
            reserve_stock_requested=re_reserve_stock,
            released_reserved_units=released_units_prior,
            held_grand_total_bdt=Decimal(staleness["held_grand_total_bdt"]),
            live_grand_total_bdt=q2(sale.grand_total_bdt),
            held_pricing_hash=staleness["held_pricing_hash"],
            live_pricing_hash=breakdown["calculated_pricing_hash"],
            was_stale=staleness["is_stale"],
            stale_reasons=staleness["stale_reasons"],
            metadata={"repriced_lines": repriced_lines, "re_reserve_stock": re_reserve_stock},
        )
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=target_state,
            action="RESUME_CART",
            user=user,
            reason=resume_note or f"Resumed held cart ({'stale acknowledged' if staleness['is_stale'] else 'fresh'})",
            metadata={"stale_reasons": staleness["stale_reasons"], "repriced_lines": repriced_lines},
        )
        return sale, staleness, reserved_pids

    @classmethod
    @transaction.atomic
    def expire_single_hold(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        void_hold: bool = False,
        reason: str = "",
    ) -> Tuple[PosSaleOrder, Dict[str, Any]]:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=sale.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        if sale.lifecycle_state != PosSaleLifecycleState.HELD:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Only HELD carts can have hold reservations expired (current state: '{sale.lifecycle_state}').",
                    "message_en": "Only held baskets can be expired cleanly.",
                    "message_bn": "শুধুমাত্র হোল্ড করা কার্টের মেয়াদ শেষ করা যায়।",
                }
            )

        now = timezone.now()
        released_units, pids = cls._release_sale_reservations(
            sale, target_status=PosAllocationStatus.EXPIRED, only_if_expired_or_unsafe=False, now=now
        )
        if sale.hold_reservation_expires_at is None or sale.hold_reservation_expires_at > now:
            sale.hold_reservation_expires_at = now
        if void_hold and (sale.hold_expires_at is None or sale.hold_expires_at > now):
            sale.hold_expires_at = now

        old_state = sale.lifecycle_state
        action = "HOLD_EXPIRED"
        if void_hold:
            sale.lifecycle_state = PosSaleLifecycleState.VOIDED
            sale.voided_at = now
            sale.void_reason = reason or "Held cart expired cleanly"
            sale.is_archived = True
            action = "HOLD_VOIDED_EXPIRED"
        else:
            action = "RESERVATION_EXPIRED"

        sale.hold_stock_reserved = False
        sale.version += 1
        sale.updated_by = user
        sale.save()

        staleness = cls.inspect_held_cart_staleness(sale, now=now)
        cls._record_hold_audit(
            sale=sale,
            action=action,
            user=user,
            released_reserved_units=released_units,
            was_stale=True,
            stale_reasons=staleness["stale_reasons"] or ["RESERVATION_EXPIRED"],
            metadata={"void_hold": void_hold, "reason": reason},
        )
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action=action,
            user=user,
            reason=reason or "Expired held cart reservation released cleanly",
            metadata={"released_base_units": released_units, "void_hold": void_hold},
        )
        cls._refresh_read_model_safe(user, sale.branch_id, pids)
        return sale, {
            "sale_id": sale.sale_id,
            "order_number": sale.order_number,
            "released_base_units": released_units,
            "voided": void_hold,
            "staleness": staleness,
            "message_en": f"Released {released_units} reserved base unit(s) for {sale.order_number}.",
            "message_bn": f"{sale.order_number}-এর {released_units}টি রিজার্ভকৃত ইউনিট মুক্ত করা হয়েছে।",
        }

    @classmethod
    @transaction.atomic
    def expire_stale_holds(
        cls,
        user: Any,
        *,
        branch_id: Optional[str] = None,
        void_expired_holds: bool = False,
        reason: str = "",
    ) -> Dict[str, Any]:
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        now = timezone.now()
        qs = PosSaleOrder.objects.select_for_update().filter(
            tenant=tenant,
            lifecycle_state=PosSaleLifecycleState.HELD,
            is_archived=False,
        )
        role = str(getattr(user, "role", "")).upper()
        if branch:
            qs = qs.filter(branch=branch)
        elif role not in PosDomainService.TENANT_WIDE_ROLES and getattr(user, "branch_id", None):
            qs = qs.filter(branch_id=user.branch_id)

        expired_sales: List[PosSaleOrder] = []
        total_released_units = 0
        voided_count = 0
        affected_pids: List[str] = []
        target_branch_ids: set[str] = set()

        for sale in qs.order_by("held_at", "sale_id"):
            hold_expired = bool(sale.hold_expires_at and now > sale.hold_expires_at)
            res_expired = bool(sale.hold_stock_reserved and sale.hold_reservation_expires_at and now > sale.hold_reservation_expires_at)
            has_active_res = sale.allocations.filter(status=PosAllocationStatus.RESERVED).exists()
            has_unsafe = any(
                (a.batch.is_expired or a.batch.status != BatchStatus.ACTIVE or a.batch.is_quarantined or a.batch.is_recalled)
                for a in sale.allocations.filter(status=PosAllocationStatus.RESERVED).select_related("batch")
            )
            if not (hold_expired or res_expired or has_unsafe):
                continue

            released_units, pids = cls._release_sale_reservations(
                sale,
                target_status=PosAllocationStatus.EXPIRED,
                only_if_expired_or_unsafe=False,
                now=now,
            )
            affected_pids.extend(pids)
            target_branch_ids.add(sale.branch_id)
            total_released_units += released_units

            old_state = sale.lifecycle_state
            did_void = False
            if void_expired_holds and hold_expired:
                sale.lifecycle_state = PosSaleLifecycleState.VOIDED
                sale.voided_at = now
                sale.void_reason = reason or "Held cart expired during stale hold sweep"
                sale.is_archived = True
                did_void = True
                voided_count += 1

            sale.hold_stock_reserved = False
            sale.version += 1
            sale.updated_by = user
            sale.save()

            if released_units > 0 or did_void or has_active_res or hold_expired:
                reasons = []
                if hold_expired:
                    reasons.append("HOLD_TTL_EXPIRED")
                if res_expired:
                    reasons.append("RESERVATION_EXPIRED")
                if has_unsafe:
                    reasons.append("UNSAFE_BATCH")
                cls._record_hold_audit(
                    sale=sale,
                    action="STALE_SWEEP_EXPIRED",
                    user=user,
                    released_reserved_units=released_units,
                    was_stale=True,
                    stale_reasons=reasons,
                    metadata={"voided": did_void, "reason": reason},
                )
                PosDomainService._record_lifecycle_audit(
                    sale=sale,
                    from_state=old_state,
                    to_state=sale.lifecycle_state,
                    action="STALE_SWEEP_EXPIRED",
                    user=user,
                    reason=reason or "Automated/operator stale held cart expiry sweep",
                    metadata={"released_base_units": released_units, "voided": did_void, "stale_reasons": reasons},
                )
                expired_sales.append(sale)

        for bid in target_branch_ids:
            cls._refresh_read_model_safe(user, bid, affected_pids)

        return {
            "expired_count": len(expired_sales),
            "released_base_units": total_released_units,
            "voided_count": voided_count,
            "affected_sale_ids": [s.sale_id for s in expired_sales],
            "message_en": (
                f"Processed {len(expired_sales)} stale held cart(s); released {total_released_units} reserved unit(s)."
            ),
            "message_bn": (
                f"{len(expired_sales)}টি মেয়াদোত্তীর্ণ হোল্ড কার্ট প্রসেস হয়েছে এবং {total_released_units}টি রিজার্ভকৃত ইউনিট মুক্ত হয়েছে।"
            ),
        }

    @classmethod
    @transaction.atomic
    def cancel_hold(
        cls,
        user: Any,
        sale: PosSaleOrder,
        *,
        reason: str = "",
        expected_version: Optional[int] = None,
    ) -> PosSaleOrder:
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=sale.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
        )
        if expected_version is not None and int(sale.version) != expected_version:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_CART_VERSION_CONFLICT",
                    "expected_version": expected_version,
                    "actual_version": int(sale.version),
                    "detail": f"Held cart was modified concurrently (expected v{expected_version}, current v{sale.version}).",
                    "message_en": "Held cart changed on another terminal. Refresh before cancelling.",
                    "message_bn": "অন্য টার্মিনাল থেকে কার্টটি পরিবর্তিত হয়েছে। অনুগ্রহ করে রিফ্রেশ করুন।",
                }
            )

        if sale.lifecycle_state != PosSaleLifecycleState.HELD:
            raise HeldCartDomainError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Cannot cancel hold on sale in '{sale.lifecycle_state}' state.",
                    "message_en": "Only HELD baskets can be cancelled via cancel-hold.",
                    "message_bn": "শুধুমাত্র HELD অবস্থার কার্ট cancel-hold করা যায়।",
                }
            )

        released_units, pids = cls._release_sale_reservations(
            sale, target_status=PosAllocationStatus.RELEASED, only_if_expired_or_unsafe=False
        )
        old_state = sale.lifecycle_state
        now = timezone.now()
        sale.lifecycle_state = PosSaleLifecycleState.VOIDED
        sale.hold_stock_reserved = False
        sale.voided_at = now
        sale.void_reason = reason or "Held cart cancelled by operator"
        sale.is_archived = True
        sale.version += 1
        sale.updated_by = user
        sale.save()

        cls._record_hold_audit(
            sale=sale,
            action="HOLD_CANCELLED",
            user=user,
            released_reserved_units=released_units,
            metadata={"reason": sale.void_reason},
        )
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=PosSaleLifecycleState.VOIDED,
            action="HOLD_CANCELLED",
            user=user,
            reason=sale.void_reason,
            metadata={"released_base_units": released_units},
        )
        cls._refresh_read_model_safe(user, sale.branch_id, pids)
        return sale

    @classmethod
    def list_held_carts(
        cls,
        user: Any,
        *,
        branch_id: Optional[str] = None,
        include_expired: bool = True,
    ) -> List[Tuple[PosSaleOrder, Dict[str, Any]]]:
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        qs = (
            PosSaleOrder.objects.filter(
                tenant=tenant,
                lifecycle_state=PosSaleLifecycleState.HELD,
                is_archived=False,
            )
            .select_related("branch", "cashier_user", "held_by", "resumed_by", "customer")
            .prefetch_related(
                "lines__product",
                "lines__variant",
                "lines__allocations__batch",
                "allocations__batch",
                "payment_attempts",
                "lifecycle_audits__actor_user",
            )
        )
        role = str(getattr(user, "role", "")).upper()
        if branch:
            qs = qs.filter(branch=branch)
        elif role not in PosDomainService.TENANT_WIDE_ROLES and getattr(user, "branch_id", None):
            qs = qs.filter(branch_id=user.branch_id)

        now = timezone.now()
        results: List[Tuple[PosSaleOrder, Dict[str, Any]]] = []
        for sale in qs.order_by("-held_at", "-created_at", "-sale_id"):
            staleness = cls.inspect_held_cart_staleness(sale, now=now)
            if not include_expired and (staleness["is_hold_expired"] or staleness["is_reservation_expired"]):
                continue
            results.append((sale, staleness))
        return results
