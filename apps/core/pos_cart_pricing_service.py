"""
backend/apps/core/pos_cart_pricing_service.py
Architecture Decision: ADR-0224 (P224).
Phase 12: POS, Sales Orders, Checkout & Returns.

Canonical POS Cart Prices, Line Totals & Server-Side Confirmation Engine:
- Strict Decimal (ROUND_HALF_UP) line & header math with float/boolean/NaN guards.
- Supports line-level & header-level FLAT_BDT / PERCENTAGE / PROMO_POLICY discounts
  without compounding on repeated recomputations.
- Supports EXCLUSIVE & INCLUSIVE VAT/tax modes (net_before_tax + tax == total) with
  optional BranchOperatingSettings.tax_defaults integration.
- Supports documented rounding policies (NONE, HALF_UP_WHOLE_BDT, FLOOR_WHOLE_BDT,
  NEAREST_FIVE_BDT, MANUAL_ADJ) with bounded anti-discount-bypass guards.
- Enforces Cashier <= 5% combined concession ceiling and blocks unauthorized
  Cashier unit price overrides unless approved by Manager/Owner/Pharmacist.
- Generates deterministic, timestamp-free SHA-256 pricing confirmation signatures,
  verifies client expectations against live row-locked totals, and logs immutable
  PosCartPricingAudit records (including rejected mismatches outside transaction rollback).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    BranchOperatingSettings,
    PaymentMethod,
    PosCartPricingAudit,
    PosDiscountType,
    PosPaymentAttemptStatus,
    PosRoundingMode,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosTaxMode,
)
from apps.identity.models import UserRole

MONEY_Q = Decimal("0.01")
CASHIER_MAX_CONCESSION_RATIO = Decimal("0.05")
CASHIER_MAX_MANUAL_ROUNDING_BDT = Decimal("0.99")
MAX_MANUAL_ROUNDING_BDT = Decimal("5.00")


def q2(value: Any) -> Decimal:
    return Decimal(str(value or "0.00")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def parse_strict_decimal(
    raw_value: Any,
    field_name: str,
    *,
    min_val: Optional[Decimal] = None,
    max_val: Optional[Decimal] = None,
    error_code: str = "ERR_INVALID_PRICING_VALUE",
) -> Decimal:
    if isinstance(raw_value, bool) or isinstance(raw_value, float) or raw_value is None:
        raise ValidationError(
            {
                "error_code": error_code,
                field_name: f"'{field_name}' must be an exact Decimal/string or integer value (floats/booleans rejected).",
                "message_bn": f"'{field_name}' এর মান সঠিক দশমিক সংখ্যায় দিতে হবে।",
            }
        )
    text = str(raw_value).strip()
    if not text:
        raise ValidationError(
            {
                "error_code": error_code,
                field_name: f"'{field_name}' cannot be blank.",
                "message_bn": f"'{field_name}' ফাঁকা রাখা যাবে না।",
            }
        )
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValidationError(
            {
                "error_code": error_code,
                field_name: f"Invalid decimal '{raw_value}' for '{field_name}'.",
                "message_bn": f"'{field_name}'-এর মান '{raw_value}' সঠিক নয়।",
            }
        )
    if not dec.is_finite():
        raise ValidationError(
            {
                "error_code": error_code,
                field_name: f"'{field_name}' must be a finite number.",
            }
        )
    quantized = dec.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
    if min_val is not None and quantized < min_val:
        raise ValidationError(
            {
                "error_code": error_code,
                field_name: f"'{field_name}' ({quantized}) cannot be less than {min_val}.",
                "message_bn": f"'{field_name}'-এর মান {min_val}-এর কম হতে পারবে না।",
            }
        )
    if max_val is not None and quantized > max_val:
        raise ValidationError(
            {
                "error_code": error_code,
                field_name: f"'{field_name}' ({quantized}) cannot exceed {max_val}.",
                "message_bn": f"'{field_name}'-এর মান {max_val}-এর বেশি হতে পারবে না।",
            }
        )
    return quantized


class _PricingRejectionAbort(Exception):
    """Signals atomic rollback while preserving rejected PosCartPricingAudit evidence."""

    def __init__(self, log_kwargs: Dict[str, Any], exc: Exception):
        super().__init__(str(exc))
        self.log_kwargs = log_kwargs
        self.exc = exc


class PosCartPricingService:
    """Authoritative server-side cart pricing, rounding, tax, discount, and confirmation service."""

    ELEVATED_PRICING_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
    }

    @classmethod
    def resolve_branch_tax_defaults(cls, branch: Branch) -> Dict[str, Any]:
        settings_obj = BranchOperatingSettings.objects.filter(
            tenant_id=branch.tenant_id, branch_id=branch.branch_id
        ).first()
        raw = getattr(settings_obj, "tax_defaults", None) if settings_obj else None
        if not isinstance(raw, dict):
            raw = {}
        try:
            default_rate = q2(raw.get("default_vat_rate", "0.00"))
        except (InvalidOperation, ValueError):
            default_rate = Decimal("0.00")
        if default_rate < Decimal("0.00") or default_rate > Decimal("100.00"):
            default_rate = Decimal("0.00")
        is_inclusive = bool(raw.get("is_vat_inclusive", True))
        return {
            "default_vat_rate_pct": f"{default_rate:.2f}",
            "tax_mode": PosTaxMode.INCLUSIVE if is_inclusive else PosTaxMode.EXCLUSIVE,
            "is_vat_inclusive": is_inclusive,
            "tax_id_number": str(raw.get("tax_id_number") or ""),
        }

    @classmethod
    def _compute_rounding_adjustment(
        cls,
        *,
        pre_round_total: Decimal,
        rounding_mode: str,
        manual_adjustment: Decimal,
        actor_role: str,
        has_discount_approval: bool,
    ) -> Tuple[Decimal, Decimal]:
        mode = str(rounding_mode or PosRoundingMode.NONE).upper()
        if mode == PosRoundingMode.NONE:
            return Decimal("0.00"), q2(pre_round_total)

        if mode == PosRoundingMode.HALF_UP_WHOLE_BDT:
            rounded = pre_round_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(MONEY_Q)
            return q2(rounded - pre_round_total), rounded

        if mode == PosRoundingMode.FLOOR_WHOLE_BDT:
            rounded = pre_round_total.quantize(Decimal("1"), rounding=ROUND_FLOOR).quantize(MONEY_Q)
            return q2(rounded - pre_round_total), rounded

        if mode == PosRoundingMode.NEAREST_FIVE_BDT:
            units_of_five = (pre_round_total / Decimal("5.00")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            rounded = q2(units_of_five * Decimal("5.00"))
            return q2(rounded - pre_round_total), rounded

        if mode == PosRoundingMode.MANUAL_ADJ:
            adj = q2(manual_adjustment)
            if abs(adj) > MAX_MANUAL_ROUNDING_BDT:
                raise ValidationError(
                    {
                        "error_code": "ERR_ROUNDING_BOUNDS_EXCEEDED",
                        "rounding_adjustment_bdt": (
                            f"Manual rounding adjustment ({adj} BDT) exceeds allowed maximum (+/-{MAX_MANUAL_ROUNDING_BDT} BDT)."
                        ),
                        "message_bn": f"ম্যানুয়াল রাউন্ডিং সমন্বয় (+/-{MAX_MANUAL_ROUNDING_BDT} টাকা)-এর বেশি হতে পারবে না।",
                    }
                )
            if actor_role == UserRole.CASHIER and not has_discount_approval and abs(adj) > CASHIER_MAX_MANUAL_ROUNDING_BDT:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_ROUNDING_APPROVAL_REQUIRED",
                        "message_en": (
                            f"Cashier manual rounding adjustment ({adj} BDT) exceeds +/-{CASHIER_MAX_MANUAL_ROUNDING_BDT} BDT; "
                            f"requires Manager/Owner approval."
                        ),
                        "message_bn": (
                            f"ক্যাশিয়ার অনুমোদন ছাড়া +/-{CASHIER_MAX_MANUAL_ROUNDING_BDT} টাকার বেশি রাউন্ডিং সমন্বয় করতে পারবেন না।"
                        ),
                    }
                )
            return adj, q2(pre_round_total + adj)

        raise ValidationError(
            {
                "error_code": "ERR_INVALID_ROUNDING_MODE",
                "rounding_mode": f"Unsupported rounding_mode '{mode}'.",
            }
        )

    @classmethod
    def build_canonical_breakdown(
        cls,
        sale: PosSaleOrder,
        lines: Optional[List[PosSaleOrderLine]] = None,
    ) -> Dict[str, Any]:
        if lines is None:
            lines = list(sale.lines.select_related("product", "variant").order_by("line_number"))

        gross_subtotal = q2(sale.subtotal_bdt)
        line_disc_sum = q2(sale.line_discount_total_bdt)
        hdr_disc = q2(sale.header_discount_amount_bdt)
        total_disc = q2(sale.discount_amount_bdt)
        tax_amt = q2(sale.tax_amount_bdt)
        rounding_adj = q2(sale.rounding_adjustment_bdt)
        delivery_fee = q2(getattr(sale, "delivery_fee_bdt", Decimal("0.00")))
        grand_total = q2(sale.grand_total_bdt)
        merchandise_grand_total = max(Decimal("0.00"), q2(grand_total - delivery_fee))

        sum_line_totals = q2(sum((q2(l.line_total_bdt) for l in lines), Decimal("0.00")))
        line_payloads: List[Dict[str, Any]] = []
        for idx, line in enumerate(lines):
            # Proportional share of header discount & rounding per line for transparent return/audit math
            line_total = q2(line.line_total_bdt)
            net_alloc_delta = q2(rounding_adj - hdr_disc)
            if sum_line_totals > Decimal("0.00") and len(lines) > 0:
                if idx == len(lines) - 1:
                    already = sum((Decimal(p["net_collectible_bdt"]) for p in line_payloads), Decimal("0.00"))
                    net_collectible = max(Decimal("0.00"), q2(merchandise_grand_total - already))
                else:
                    ratio = line_total / sum_line_totals
                    net_collectible = max(Decimal("0.00"), q2(line_total + (net_alloc_delta * ratio)))
            else:
                net_collectible = Decimal("0.00")

            line_payloads.append(
                {
                    "line_id": line.line_id,
                    "line_number": line.line_number,
                    "product_id": line.product_id,
                    "product_brand_name": line.product.brand_name if line.product_id else "",
                    "packaging_unit": line.packaging_unit,
                    "quantity_units": int(line.quantity_units),
                    "conversion_factor": int(line.conversion_factor),
                    "base_units_total": int(line.base_units_total),
                    "unit_price_bdt": f"{q2(line.unit_price_bdt):.2f}",
                    "price_source": line.price_source,
                    "line_subtotal_bdt": f"{q2(line.line_subtotal_bdt):.2f}",
                    "discount_type": line.discount_type,
                    "discount_rate_pct": f"{q2(line.discount_rate_pct):.2f}",
                    "discount_amount_bdt": f"{q2(line.discount_amount_bdt):.2f}",
                    "tax_mode": line.tax_mode,
                    "tax_rate_pct": f"{q2(line.tax_rate_pct):.2f}",
                    "net_before_tax_bdt": f"{q2(line.net_before_tax_bdt):.2f}",
                    "line_tax_amount_bdt": f"{q2(line.line_tax_amount_bdt):.2f}",
                    "line_total_bdt": f"{line_total:.2f}",
                    "net_collectible_bdt": f"{net_collectible:.2f}",
                }
            )

        signature_doc = {
            "sale_id": sale.sale_id,
            "branch_id": sale.branch_id,
            "currency": sale.currency,
            "subtotal_bdt": f"{gross_subtotal:.2f}",
            "discount_type": sale.discount_type,
            "discount_rate_pct": f"{q2(sale.discount_rate_pct):.2f}",
            "line_discount_total_bdt": f"{line_disc_sum:.2f}",
            "header_discount_amount_bdt": f"{hdr_disc:.2f}",
            "discount_amount_bdt": f"{total_disc:.2f}",
            "net_before_tax_bdt": f"{q2(sale.net_before_tax_bdt):.2f}",
            "tax_mode": sale.tax_mode,
            "tax_rate_pct": f"{q2(sale.tax_rate_pct):.2f}",
            "tax_amount_bdt": f"{tax_amt:.2f}",
            "rounding_mode": sale.rounding_mode,
            "rounding_adjustment_bdt": f"{rounding_adj:.2f}",
            "grand_total_bdt": f"{grand_total:.2f}",
            "lines": [
                {
                    "line_id": p["line_id"],
                    "product_id": p["product_id"],
                    "packaging_unit": p["packaging_unit"],
                    "quantity_units": p["quantity_units"],
                    "conversion_factor": p["conversion_factor"],
                    "unit_price_bdt": p["unit_price_bdt"],
                    "discount_type": p["discount_type"],
                    "discount_rate_pct": p["discount_rate_pct"],
                    "discount_amount_bdt": p["discount_amount_bdt"],
                    "tax_mode": p["tax_mode"],
                    "tax_rate_pct": p["tax_rate_pct"],
                    "net_before_tax_bdt": p["net_before_tax_bdt"],
                    "line_tax_amount_bdt": p["line_tax_amount_bdt"],
                    "line_total_bdt": p["line_total_bdt"],
                }
                for p in line_payloads
            ],
        }
        if delivery_fee > Decimal("0.00"):
            signature_doc["delivery_fee_bdt"] = f"{delivery_fee:.2f}"
        canonical_json = json.dumps(signature_doc, sort_keys=True, separators=(",", ":"))
        calc_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        return {
            **signature_doc,
            "delivery_fee_bdt": f"{delivery_fee:.2f}",
            "lines": line_payloads,
            "calculated_pricing_hash": calc_hash,
            "is_pricing_confirmed": bool(sale.pricing_confirmed_at and sale.pricing_confirmation_hash == calc_hash),
            "pricing_confirmed_at": sale.pricing_confirmed_at.isoformat() if sale.pricing_confirmed_at else None,
            "pricing_confirmed_by_id": str(sale.pricing_confirmed_by_id) if sale.pricing_confirmed_by_id else None,
            "pricing_confirmation_hash": sale.pricing_confirmation_hash,
            "version": int(sale.version),
        }

    @classmethod
    def recompute_sale_totals(
        cls,
        sale: PosSaleOrder,
        actor_user: Any,
        *,
        allow_empty: bool = False,
    ) -> Tuple[PosSaleOrder, Dict[str, Any]]:
        """
        Deterministically and idempotently recomputes all line subtotals, line discounts,
        inclusive/exclusive VAT/taxes, non-compounding header discounts, rounding adjustments,
        Cashier 5% combined concession limits, and grand_total_bdt.
        Calling recompute_sale_totals(sale, user) multiple times preserves exact outputs.
        """
        lines = list(sale.lines.select_related("product", "variant").order_by("line_number"))
        if not lines:
            if not allow_empty:
                raise ValidationError({"lines": "A POS sale order must contain at least one line item."})
            sale.subtotal_bdt = Decimal("0.00")
            sale.line_discount_total_bdt = Decimal("0.00")
            sale.header_discount_amount_bdt = Decimal("0.00")
            sale.discount_amount_bdt = Decimal("0.00")
            sale.net_before_tax_bdt = Decimal("0.00")
            sale.tax_amount_bdt = Decimal("0.00")
            sale.rounding_adjustment_bdt = Decimal("0.00")
            sale.grand_total_bdt = Decimal("0.00")
            sale.due_amount_bdt = Decimal("0.00")
            sale.pricing_confirmed_at = None
            sale.pricing_confirmed_by = None
            sale.pricing_confirmation_hash = ""
            sale.requires_rx_verification = False
            sale.save(
                update_fields=[
                    "subtotal_bdt",
                    "line_discount_total_bdt",
                    "header_discount_amount_bdt",
                    "discount_amount_bdt",
                    "net_before_tax_bdt",
                    "tax_amount_bdt",
                    "rounding_adjustment_bdt",
                    "grand_total_bdt",
                    "due_amount_bdt",
                    "pricing_confirmed_at",
                    "pricing_confirmed_by",
                    "pricing_confirmation_hash",
                    "requires_rx_verification",
                    "updated_at",
                ]
            )
            return sale, cls.build_canonical_breakdown(sale, [])

        gross_subtotal = Decimal("0.00")
        line_discounts_sum = Decimal("0.00")
        line_tax_sum = Decimal("0.00")
        line_net_before_tax_sum = Decimal("0.00")
        lines_total_sum = Decimal("0.00")
        any_rx_required = False

        sale_tax_mode = str(sale.tax_mode or PosTaxMode.EXCLUSIVE).upper()
        if sale_tax_mode not in PosTaxMode.values:
            sale_tax_mode = PosTaxMode.EXCLUSIVE

        for line in lines:
            qty = int(line.quantity_units)
            factor = int(line.conversion_factor)
            if qty <= 0 or factor <= 0:
                raise ValidationError({"quantity_units": "Quantity and conversion factor must be positive integers."})

            line.base_units_total = qty * factor
            line_gross = q2(Decimal(qty) * q2(line.unit_price_bdt))

            line_disc_type = str(line.discount_type or PosDiscountType.NONE).upper()
            if line_disc_type == PosDiscountType.PERCENTAGE:
                rate = q2(line.discount_rate_pct)
                if rate < Decimal("0.00") or rate > Decimal("100.00"):
                    raise ValidationError({"discount_rate_pct": f"Line {line.line_number} discount_rate_pct must be 0..100."})
                line_disc = q2(line_gross * rate / Decimal("100.00"))
            else:
                line_disc = q2(line.discount_amount_bdt)
                if line_disc > Decimal("0.00") and line_disc_type == PosDiscountType.NONE:
                    line_disc_type = PosDiscountType.FLAT_BDT

            if line_disc < Decimal("0.00") or line_disc > line_gross:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_DISCOUNT_AMOUNT",
                        "discount_amount_bdt": (
                            f"Line {line.line_number} discount ({line_disc} BDT) cannot be negative or exceed "
                            f"line gross subtotal ({line_gross} BDT)."
                        ),
                        "message_bn": f"লাইন {line.line_number}-এর ছাড় শূন্যের কম বা মূল্যের চেয়ে বেশি হতে পারবে না।",
                    }
                )

            after_line_disc = q2(line_gross - line_disc)
            effective_line_tax_mode = str(line.tax_mode or sale_tax_mode).upper()
            if effective_line_tax_mode not in PosTaxMode.values:
                effective_line_tax_mode = PosTaxMode.EXCLUSIVE

            tax_rate = q2(line.tax_rate_pct)
            if tax_rate < Decimal("0.00") or tax_rate > Decimal("100.00"):
                raise ValidationError({"tax_rate_pct": f"Line {line.line_number} tax_rate_pct must be between 0 and 100."})

            if tax_rate == Decimal("0.00"):
                line_net_before_tax = after_line_disc
                line_tax = Decimal("0.00")
                line_total = after_line_disc
            elif effective_line_tax_mode == PosTaxMode.INCLUSIVE:
                line_total = after_line_disc
                divisor = Decimal("1.00") + (tax_rate / Decimal("100.00"))
                line_net_before_tax = (line_total / divisor).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
                line_tax = q2(line_total - line_net_before_tax)
            else:
                line_net_before_tax = after_line_disc
                line_tax = q2(line_net_before_tax * tax_rate / Decimal("100.00"))
                line_total = q2(line_net_before_tax + line_tax)

            line.discount_type = line_disc_type
            line.discount_amount_bdt = line_disc
            line.tax_mode = effective_line_tax_mode
            line.line_subtotal_bdt = line_gross
            line.net_before_tax_bdt = line_net_before_tax
            line.line_tax_amount_bdt = line_tax
            line.line_total_bdt = line_total
            if (
                line.product.is_prescription_only
                or line.product.is_narcotic
                or line.product.is_antibiotic
            ):
                line.requires_rx_verification = True
            if line.requires_rx_verification:
                any_rx_required = True

            line.save(
                update_fields=[
                    "base_units_total",
                    "discount_type",
                    "discount_amount_bdt",
                    "tax_mode",
                    "line_subtotal_bdt",
                    "net_before_tax_bdt",
                    "line_tax_amount_bdt",
                    "line_total_bdt",
                    "requires_rx_verification",
                    "updated_at",
                ]
            )

            gross_subtotal = q2(gross_subtotal + line_gross)
            line_discounts_sum = q2(line_discounts_sum + line_disc)
            line_tax_sum = q2(line_tax_sum + line_tax)
            line_net_before_tax_sum = q2(line_net_before_tax_sum + line_net_before_tax)
            lines_total_sum = q2(lines_total_sum + line_total)

        after_line_discounts = q2(gross_subtotal - line_discounts_sum)

        # Header-level discount strictly isolated in header_discount_amount_bdt so recomputation is idempotent
        hdr_disc_type = str(sale.discount_type or PosDiscountType.NONE).upper()
        if hdr_disc_type == PosDiscountType.PERCENTAGE:
            hdr_rate = q2(sale.discount_rate_pct)
            if hdr_rate < Decimal("0.00") or hdr_rate > Decimal("100.00"):
                raise ValidationError({"discount_rate_pct": "Header discount_rate_pct must be between 0 and 100."})
            hdr_discount = q2(after_line_discounts * hdr_rate / Decimal("100.00"))
        elif hdr_disc_type in (PosDiscountType.FLAT_BDT, PosDiscountType.PROMO_POLICY):
            # Support callers creating header discounts via header_discount_amount_bdt or legacy discount_amount_bdt
            if q2(sale.header_discount_amount_bdt) > Decimal("0.00") or q2(sale.discount_amount_bdt) == Decimal("0.00"):
                hdr_discount = q2(sale.header_discount_amount_bdt)
            elif line_discounts_sum == Decimal("0.00"):
                hdr_discount = q2(sale.discount_amount_bdt)
            else:
                hdr_discount = q2(sale.header_discount_amount_bdt)
        else:
            hdr_discount = Decimal("0.00")

        if hdr_discount < Decimal("0.00"):
            raise ValidationError({"discount_amount_bdt": "Header discount cannot be negative."})

        total_discount = q2(line_discounts_sum + hdr_discount)
        if total_discount > gross_subtotal:
            raise ValidationError(
                {
                    "error_code": "ERR_DISCOUNT_EXCEEDS_SUBTOTAL",
                    "discount_amount_bdt": (
                        f"Combined discount ({total_discount} BDT) exceeds order subtotal ({gross_subtotal} BDT)."
                    ),
                    "message_bn": "মোট ছাড় অর্ডারের মূল মূল্যের চেয়ে বেশি হতে পারবে না।",
                }
            )

        # Tax resolution at header level (when lines do not already carry itemized taxes)
        hdr_tax_rate = q2(sale.tax_rate_pct)
        if hdr_tax_rate < Decimal("0.00") or hdr_tax_rate > Decimal("100.00"):
            raise ValidationError({"tax_rate_pct": "Header tax_rate_pct must be between 0 and 100."})

        taxable_after_all_disc = max(Decimal("0.00"), q2(gross_subtotal - total_discount))
        header_extra_tax = Decimal("0.00")
        if hdr_tax_rate > Decimal("0.00") and line_tax_sum == Decimal("0.00"):
            if sale_tax_mode == PosTaxMode.INCLUSIVE:
                total_before_rounding = taxable_after_all_disc
                divisor = Decimal("1.00") + (hdr_tax_rate / Decimal("100.00"))
                net_before_tax = (total_before_rounding / divisor).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
                header_extra_tax = q2(total_before_rounding - net_before_tax)
            else:
                net_before_tax = taxable_after_all_disc
                header_extra_tax = q2(net_before_tax * hdr_tax_rate / Decimal("100.00"))
                total_before_rounding = q2(net_before_tax + header_extra_tax)
        else:
            net_before_tax = max(Decimal("0.00"), q2(line_net_before_tax_sum - hdr_discount))
            total_before_rounding = q2(lines_total_sum - hdr_discount)

        total_tax = q2(line_tax_sum + header_extra_tax)

        actor_role = str(getattr(actor_user, "role", "")).upper()
        rounding_adj, raw_grand_total = cls._compute_rounding_adjustment(
            pre_round_total=total_before_rounding,
            rounding_mode=sale.rounding_mode,
            manual_adjustment=q2(sale.rounding_adjustment_bdt),
            actor_role=actor_role,
            has_discount_approval=bool(sale.discount_approved_by_id),
        )

        # Enforce Cashier 5% combined concession ceiling (discounts + negative rounding bypass guard)
        negative_rounding_concession = max(Decimal("0.00"), -rounding_adj)
        effective_concession = q2(total_discount + negative_rounding_concession)
        if gross_subtotal > Decimal("0.00") and effective_concession > Decimal("0.00"):
            concession_ratio = effective_concession / gross_subtotal
            if actor_role == UserRole.CASHIER and concession_ratio > CASHIER_MAX_CONCESSION_RATIO:
                if not sale.discount_approved_by_id:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_CASHIER_DISCOUNT_LIMIT_EXCEEDED",
                            "message_en": (
                                "Cashiers cannot apply discounts and negative rounding exceeding 5% "
                                "without Branch Manager or Owner approval."
                            ),
                            "message_bn": "ক্যাশিয়ার ম্যানেজারের অনুমোদন ছাড়া ৫%-এর বেশি ছাড় বা নেগেটিভ রাউন্ডিং দিতে পারবেন না।",
                        }
                    )

        if raw_grand_total < Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_NEGATIVE_GRAND_TOTAL",
                    "grand_total_bdt": "Grand total cannot be negative after rounding/discounts.",
                }
            )

        # Invalidate prior confirmation signature if financial hash drifted
        sale.subtotal_bdt = gross_subtotal
        sale.line_discount_total_bdt = line_discounts_sum
        sale.header_discount_amount_bdt = hdr_discount
        sale.discount_amount_bdt = total_discount
        sale.net_before_tax_bdt = net_before_tax
        sale.tax_mode = sale_tax_mode
        sale.tax_amount_bdt = total_tax
        sale.rounding_adjustment_bdt = rounding_adj
        delivery_fee = max(Decimal("0.00"), q2(getattr(sale, "delivery_fee_bdt", Decimal("0.00"))))
        sale.grand_total_bdt = q2(raw_grand_total + delivery_fee)
        sale.requires_rx_verification = any_rx_required or sale.requires_rx_verification

        breakdown = cls.build_canonical_breakdown(sale, lines)
        if sale.pricing_confirmation_hash and sale.pricing_confirmation_hash != breakdown["calculated_pricing_hash"]:
            sale.pricing_confirmed_at = None
            sale.pricing_confirmed_by = None
            sale.pricing_confirmation_hash = ""
            breakdown["is_pricing_confirmed"] = False
            breakdown["pricing_confirmed_at"] = None
            breakdown["pricing_confirmed_by_id"] = None
            breakdown["pricing_confirmation_hash"] = ""

        sale.save(
            update_fields=[
                "subtotal_bdt",
                "line_discount_total_bdt",
                "header_discount_amount_bdt",
                "discount_amount_bdt",
                "net_before_tax_bdt",
                "tax_mode",
                "tax_amount_bdt",
                "rounding_adjustment_bdt",
                "grand_total_bdt",
                "pricing_confirmed_at",
                "pricing_confirmed_by",
                "pricing_confirmation_hash",
                "requires_rx_verification",
                "updated_at",
            ]
        )
        return sale, breakdown

    @classmethod
    def _record_pricing_audit(
        cls,
        *,
        sale: PosSaleOrder,
        action: str,
        user: Any,
        breakdown: Optional[Dict[str, Any]] = None,
        expected_grand_total_bdt: Optional[Decimal] = None,
        was_rejected: bool = False,
        rejection_code: str = "",
        rejection_message: str = "",
    ) -> PosCartPricingAudit:
        actor_role = str(getattr(user, "role", "SYSTEM") or "").upper()
        audit = PosCartPricingAudit.objects.create(
            tenant=sale.tenant,
            branch=sale.branch,
            sale=sale,
            action=action,
            subtotal_bdt=q2(sale.subtotal_bdt),
            line_discount_total_bdt=q2(sale.line_discount_total_bdt),
            header_discount_amount_bdt=q2(sale.header_discount_amount_bdt),
            discount_amount_bdt=q2(sale.discount_amount_bdt),
            net_before_tax_bdt=q2(sale.net_before_tax_bdt),
            tax_mode=sale.tax_mode,
            tax_rate_pct=q2(sale.tax_rate_pct),
            tax_amount_bdt=q2(sale.tax_amount_bdt),
            rounding_mode=sale.rounding_mode,
            rounding_adjustment_bdt=q2(sale.rounding_adjustment_bdt),
            grand_total_bdt=q2(sale.grand_total_bdt),
            expected_grand_total_bdt=expected_grand_total_bdt,
            pricing_confirmation_hash=(breakdown or {}).get("calculated_pricing_hash", sale.pricing_confirmation_hash),
            was_rejected=was_rejected,
            rejection_code=rejection_code[:64],
            rejection_message=rejection_message[:255],
            actor_user=user if getattr(user, "is_authenticated", False) else None,
            actor_role=actor_role,
            discount_approved_by=sale.discount_approved_by,
            breakdown_snapshot=breakdown or {},
        )
        AuditLogEntry.objects.create(
            tenant_id=sale.tenant_id,
            branch_id=sale.branch_id,
            user_id=str(getattr(user, "user_id", "") or getattr(user, "pk", "")),
            actor_email=str(getattr(user, "email", "")),
            actor_role=actor_role,
            request_id=f"prc-{uuid.uuid4().hex[:10]}",
            action=f"POS_{action}",
            resource_type="POS_SALE_ORDER",
            resource_id=sale.sale_id,
            status=AuditActionStatus.DENIED if was_rejected else AuditActionStatus.SUCCESS,
            before_state={"expected_grand_total_bdt": str(expected_grand_total_bdt) if expected_grand_total_bdt is not None else None},
            after_state={
                "grand_total_bdt": str(sale.grand_total_bdt),
                "discount_amount_bdt": str(sale.discount_amount_bdt),
                "tax_amount_bdt": str(sale.tax_amount_bdt),
                "rounding_adjustment_bdt": str(sale.rounding_adjustment_bdt),
                "pricing_hash": audit.pricing_confirmation_hash,
            },
            metadata_json={"rejection_code": rejection_code, "rejection_message": rejection_message},
        )
        return audit

    @classmethod
    def get_pricing_breakdown(cls, user: Any, sale: PosSaleOrder) -> Dict[str, Any]:
        from apps.core.pos_domain_service import PosDomainService

        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=sale.branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        sale_obj = (
            PosSaleOrder.objects.filter(pk=sale.pk, tenant=tenant)
            .prefetch_related("lines__product", "lines__variant")
            .get()
        )
        breakdown = cls.build_canonical_breakdown(sale_obj)
        breakdown["branch_tax_defaults"] = cls.resolve_branch_tax_defaults(sale_obj.branch)
        return breakdown

    @classmethod
    def update_sale_pricing(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> Tuple[PosSaleOrder, Dict[str, Any], PosCartPricingAudit]:
        from apps.core.pos_domain_service import PosDomainService

        actor_role = str(getattr(user, "role", "")).upper()
        try:
            with transaction.atomic():
                locked_sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
                tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
                    user,
                    branch_id=locked_sale.branch_id,
                    require_write=True,
                    allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
                )

                editable_pricing_states = set(PosDomainService.MUTABLE_SALE_STATES) | {
                    PosSaleLifecycleState.CONFIRMED,
                    PosSaleLifecycleState.PARTIALLY_PAID,
                }
                if locked_sale.lifecycle_state not in editable_pricing_states:
                    raise ValidationError(
                        {
                            "error_code": "ERR_IMMUTABLE_SALE_STATE",
                            "detail": (
                                f"Cannot update cart pricing on sale '{locked_sale.order_number}' "
                                f"in state '{locked_sale.lifecycle_state}'."
                            ),
                            "message_bn": "চূড়ান্ত বা লক করা অর্ডারের মূল্য পরিবর্তন করা যাবে না।",
                        }
                    )

                expected_ver = data.get("expected_version")
                if expected_ver is not None and int(expected_ver) != int(locked_sale.version):
                    raise _PricingRejectionAbort(
                        log_kwargs={
                            "sale": locked_sale,
                            "action": "PRICING_REJECTED",
                            "user": user,
                            "breakdown": cls.build_canonical_breakdown(locked_sale),
                            "was_rejected": True,
                            "rejection_code": "ERR_CART_VERSION_CONFLICT",
                            "rejection_message": f"Stale cart version {expected_ver}; current version is {locked_sale.version}.",
                        },
                        exc=ValidationError(
                            {
                                "error_code": "ERR_CART_VERSION_CONFLICT",
                                "expected_version": expected_ver,
                                "current_version": locked_sale.version,
                                "message_en": "Cart was updated concurrently; refresh cart before editing pricing.",
                                "message_bn": "কার্ট অন্য কোথাও থেকে পরিবর্তন হয়েছে; রিফ্রেশ করে আবার চেষ্টা করুন।",
                            }
                        ),
                    )

                # Apply line-level pricing / discount / tax overrides if present
                line_adjustments = data.get("line_adjustments") or []
                if line_adjustments:
                    if not isinstance(line_adjustments, list):
                        raise ValidationError({"line_adjustments": "line_adjustments must be a list of objects."})
                    existing_lines = {
                        l.line_id: l
                        for l in locked_sale.lines.select_related("product", "variant").order_by("line_number")
                    }
                    for raw_adj in line_adjustments:
                        if not isinstance(raw_adj, dict):
                            raise ValidationError({"line_adjustments": "Each line adjustment must be a JSON object."})
                        lid = str(raw_adj.get("line_id") or "").strip()
                        target_line = existing_lines.get(lid)
                        if not target_line:
                            raise ValidationError({"line_id": f"Cart line '{lid}' not found in order '{locked_sale.sale_id}'."})

                        if "unit_price_bdt" in raw_adj and raw_adj["unit_price_bdt"] is not None:
                            new_unit_price = parse_strict_decimal(
                                raw_adj["unit_price_bdt"],
                                "unit_price_bdt",
                                min_val=Decimal("0.01"),
                            )
                            if new_unit_price != q2(target_line.unit_price_bdt):
                                if actor_role not in cls.ELEVATED_PRICING_ROLES:
                                    raise _PricingRejectionAbort(
                                        log_kwargs={
                                            "sale": locked_sale,
                                            "action": "PRICING_REJECTED",
                                            "user": user,
                                            "breakdown": cls.build_canonical_breakdown(locked_sale),
                                            "was_rejected": True,
                                            "rejection_code": "ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL",
                                            "rejection_message": (
                                                f"Cashier attempted unit_price_bdt override on {target_line.line_id} "
                                                f"({target_line.unit_price_bdt} -> {new_unit_price})."
                                            ),
                                        },
                                        exc=PermissionDenied(
                                            {
                                                "error_code": "ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL",
                                                "message_en": (
                                                    "Cashiers cannot override item unit price; Branch Manager, Owner, "
                                                    "or Pharmacist approval is required."
                                                ),
                                                "message_bn": "ক্যাশিয়ার ম্যানেজার বা ফার্মাসিস্টের অনুমোদন ছাড়া একক মূল্য পরিবর্তন করতে পারবেন না।",
                                            }
                                        ),
                                    )
                                target_line.unit_price_bdt = new_unit_price
                                target_line.price_source = "MANAGER_OVERRIDE"
                                target_line.price_override_approved_by = user
                                target_line.price_override_reason = str(
                                    raw_adj.get("override_reason") or data.get("discount_reason") or "Manager price override"
                                )[:255]

                        if "discount_type" in raw_adj and raw_adj["discount_type"] is not None:
                            dt = str(raw_adj["discount_type"]).strip().upper()
                            if dt not in PosDiscountType.values:
                                raise ValidationError({"discount_type": f"Invalid line discount_type '{dt}'."})
                            target_line.discount_type = dt
                            if dt == PosDiscountType.NONE:
                                target_line.discount_rate_pct = Decimal("0.00")
                                target_line.discount_amount_bdt = Decimal("0.00")

                        if "discount_rate_pct" in raw_adj and raw_adj["discount_rate_pct"] is not None:
                            rate = parse_strict_decimal(
                                raw_adj["discount_rate_pct"],
                                "discount_rate_pct",
                                min_val=Decimal("0.00"),
                                max_val=Decimal("100.00"),
                            )
                            target_line.discount_rate_pct = rate
                            if "discount_type" not in raw_adj:
                                target_line.discount_type = PosDiscountType.PERCENTAGE if rate > 0 else PosDiscountType.NONE

                        if "discount_amount_bdt" in raw_adj and raw_adj["discount_amount_bdt"] is not None:
                            amt = parse_strict_decimal(
                                raw_adj["discount_amount_bdt"],
                                "discount_amount_bdt",
                                min_val=Decimal("0.00"),
                                error_code="ERR_INVALID_DISCOUNT_AMOUNT",
                            )
                            target_line.discount_amount_bdt = amt
                            if "discount_type" not in raw_adj and "discount_rate_pct" not in raw_adj:
                                target_line.discount_type = PosDiscountType.FLAT_BDT if amt > 0 else PosDiscountType.NONE

                        if "tax_mode" in raw_adj and raw_adj["tax_mode"] is not None:
                            tm = str(raw_adj["tax_mode"]).strip().upper()
                            if tm not in PosTaxMode.values:
                                raise ValidationError({"tax_mode": f"Invalid line tax_mode '{tm}'."})
                            target_line.tax_mode = tm

                        if "tax_rate_pct" in raw_adj and raw_adj["tax_rate_pct"] is not None:
                            target_line.tax_rate_pct = parse_strict_decimal(
                                raw_adj["tax_rate_pct"],
                                "tax_rate_pct",
                                min_val=Decimal("0.00"),
                                max_val=Decimal("100.00"),
                            )

                        target_line.save()

                # Apply BranchOperatingSettings tax defaults if explicitly requested
                if bool(data.get("apply_branch_tax_defaults", False)):
                    tax_defaults = cls.resolve_branch_tax_defaults(locked_sale.branch)
                    locked_sale.tax_mode = tax_defaults["tax_mode"]
                    locked_sale.tax_rate_pct = q2(tax_defaults["default_vat_rate_pct"])

                # Apply header-level discount / tax / rounding updates
                if "discount_type" in data and data["discount_type"] is not None:
                    hdr_dt = str(data["discount_type"]).strip().upper()
                    if hdr_dt not in PosDiscountType.values:
                        raise ValidationError({"discount_type": f"Invalid discount_type '{hdr_dt}'."})
                    locked_sale.discount_type = hdr_dt
                    if hdr_dt == PosDiscountType.NONE:
                        locked_sale.discount_rate_pct = Decimal("0.00")
                        locked_sale.header_discount_amount_bdt = Decimal("0.00")

                if "discount_rate_pct" in data and data["discount_rate_pct"] is not None:
                    hdr_rate = parse_strict_decimal(
                        data["discount_rate_pct"],
                        "discount_rate_pct",
                        min_val=Decimal("0.00"),
                        max_val=Decimal("100.00"),
                    )
                    locked_sale.discount_rate_pct = hdr_rate
                    if "discount_type" not in data:
                        locked_sale.discount_type = PosDiscountType.PERCENTAGE if hdr_rate > 0 else PosDiscountType.NONE

                if "header_discount_amount_bdt" in data and data["header_discount_amount_bdt"] is not None:
                    hdr_flat = parse_strict_decimal(
                        data["header_discount_amount_bdt"],
                        "header_discount_amount_bdt",
                        min_val=Decimal("0.00"),
                        error_code="ERR_INVALID_DISCOUNT_AMOUNT",
                    )
                    locked_sale.header_discount_amount_bdt = hdr_flat
                    if "discount_type" not in data and "discount_rate_pct" not in data:
                        locked_sale.discount_type = PosDiscountType.FLAT_BDT if hdr_flat > 0 else PosDiscountType.NONE
                elif "discount_amount_bdt" in data and data["discount_amount_bdt"] is not None:
                    hdr_flat = parse_strict_decimal(
                        data["discount_amount_bdt"],
                        "discount_amount_bdt",
                        min_val=Decimal("0.00"),
                        error_code="ERR_INVALID_DISCOUNT_AMOUNT",
                    )
                    locked_sale.header_discount_amount_bdt = hdr_flat
                    if "discount_type" not in data and "discount_rate_pct" not in data:
                        locked_sale.discount_type = PosDiscountType.FLAT_BDT if hdr_flat > 0 else PosDiscountType.NONE

                if "tax_mode" in data and data["tax_mode"] is not None:
                    tm = str(data["tax_mode"]).strip().upper()
                    if tm not in PosTaxMode.values:
                        raise ValidationError({"tax_mode": f"Invalid tax_mode '{tm}'."})
                    locked_sale.tax_mode = tm

                if "tax_rate_pct" in data and data["tax_rate_pct"] is not None:
                    locked_sale.tax_rate_pct = parse_strict_decimal(
                        data["tax_rate_pct"],
                        "tax_rate_pct",
                        min_val=Decimal("0.00"),
                        max_val=Decimal("100.00"),
                    )

                if "rounding_mode" in data and data["rounding_mode"] is not None:
                    rm = str(data["rounding_mode"]).strip().upper()
                    if rm not in PosRoundingMode.values:
                        raise ValidationError(
                            {
                                "error_code": "ERR_INVALID_ROUNDING_MODE",
                                "rounding_mode": f"Unsupported rounding_mode '{rm}'.",
                            }
                        )
                    locked_sale.rounding_mode = rm

                if "rounding_adjustment_bdt" in data and data["rounding_adjustment_bdt"] is not None:
                    manual_adj = parse_strict_decimal(
                        data["rounding_adjustment_bdt"],
                        "rounding_adjustment_bdt",
                    )
                    locked_sale.rounding_adjustment_bdt = manual_adj
                    if "rounding_mode" not in data and manual_adj != Decimal("0.00"):
                        locked_sale.rounding_mode = PosRoundingMode.MANUAL_ADJ

                if "discount_reason" in data and data["discount_reason"] is not None:
                    locked_sale.discount_reason = str(data["discount_reason"]).strip()[:255]

                if actor_role in cls.ELEVATED_PRICING_ROLES and (
                    q2(locked_sale.header_discount_amount_bdt) > 0
                    or q2(locked_sale.discount_rate_pct) > 0
                    or q2(locked_sale.rounding_adjustment_bdt) < 0
                ):
                    locked_sale.discount_approved_by = user

                # Invalidate previous pricing confirmation until confirmed explicitly
                locked_sale.pricing_confirmed_at = None
                locked_sale.pricing_confirmed_by = None
                locked_sale.pricing_confirmation_hash = ""
                locked_sale.save()

                try:
                    locked_sale, breakdown = cls.recompute_sale_totals(
                        locked_sale,
                        user,
                        allow_empty=bool(data.get("allow_empty", False)),
                    )
                except (PermissionDenied, ValidationError) as exc:
                    det = exc.detail if isinstance(exc.detail, dict) else {"detail": str(exc.detail)}
                    err_code = str(det.get("error_code") or "ERR_PRICING_VALIDATION")
                    raise _PricingRejectionAbort(
                        log_kwargs={
                            "sale": locked_sale,
                            "action": "PRICING_REJECTED",
                            "user": user,
                            "breakdown": cls.build_canonical_breakdown(locked_sale),
                            "was_rejected": True,
                            "rejection_code": err_code,
                            "rejection_message": str(det.get("message_en") or det.get("detail") or str(det))[:255],
                        },
                        exc=exc,
                    ) from exc

                # Guard against dropping grand_total below already-captured non-cash/due tenders
                non_cash_settled = sum(
                    (
                        q2(att.amount_bdt)
                        for att in locked_sale.payment_attempts.filter(status=PosPaymentAttemptStatus.CAPTURED)
                        if att.tender_type != PaymentMethod.CASH
                    ),
                    Decimal("0.00"),
                )
                if q2(locked_sale.grand_total_bdt) < non_cash_settled:
                    raise _PricingRejectionAbort(
                        log_kwargs={
                            "sale": locked_sale,
                            "action": "PRICING_REJECTED",
                            "user": user,
                            "breakdown": breakdown,
                            "was_rejected": True,
                            "rejection_code": "ERR_PAYMENT_EXCEEDS_UPDATED_TOTAL",
                            "rejection_message": (
                                f"New grand total {locked_sale.grand_total_bdt} BDT < captured non-cash {non_cash_settled} BDT."
                            ),
                        },
                        exc=ValidationError(
                            {
                                "error_code": "ERR_PAYMENT_EXCEEDS_UPDATED_TOTAL",
                                "message_en": (
                                    f"Updated grand total ({locked_sale.grand_total_bdt} BDT) cannot be lower than "
                                    f"already captured non-cash payments ({non_cash_settled} BDT)."
                                ),
                                "message_bn": "পূর্বের গৃহীত নন-ক্যাশ পেমেন্টের চেয়ে সর্বমোট মূল্য কমানো যাবে না।",
                            }
                        ),
                    )

                PosDomainService._sync_sale_payment_totals(locked_sale)
                locked_sale.version += 1
                locked_sale.updated_by = user
                locked_sale.save(update_fields=["version", "updated_by", "updated_at"])

                breakdown = cls.build_canonical_breakdown(locked_sale)
                audit = cls._record_pricing_audit(
                    sale=locked_sale,
                    action="PRICING_UPDATED",
                    user=user,
                    breakdown=breakdown,
                    was_rejected=False,
                )
                PosDomainService._record_lifecycle_audit(
                    sale=locked_sale,
                    from_state=locked_sale.lifecycle_state,
                    to_state=locked_sale.lifecycle_state,
                    action="PRICING_UPDATED",
                    user=user,
                    reason=locked_sale.discount_reason or "Cart pricing/discount/tax/rounding updated",
                    metadata={
                        "pricing_audit_id": audit.pricing_audit_id,
                        "grand_total_bdt": str(locked_sale.grand_total_bdt),
                        "calculated_pricing_hash": breakdown["calculated_pricing_hash"],
                    },
                )
                refreshed = (
                    PosSaleOrder.objects.filter(pk=locked_sale.pk)
                    .prefetch_related("lines__product", "lines__variant", "allocations", "payment_attempts", "lifecycle_audits")
                    .get()
                )
                return refreshed, breakdown, audit
        except _PricingRejectionAbort as abort:
            cls._record_pricing_audit(**abort.log_kwargs)
            raise abort.exc

    OFFLINE_MAX_DISCOUNT_RATIO = Decimal("0.20")  # 20% max offline discount ceiling

    @classmethod
    @transaction.atomic
    def approve_discount_or_override(
        cls,
        actor_user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> Tuple[PosSaleOrder, Dict[str, Any], PosCartPricingAudit]:
        """
        Architecture Decision: ADR-0236 (P236).
        Designated manager approval for restricted discounts (> 5%) and unit price overrides,
        including offline policy boundaries.
        """
        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        actor_role = str(getattr(actor_user, "role", "")).upper()

        from apps.core.pos_domain_service import PosDomainService
        PosDomainService.resolve_user_tenant_and_branch(
            actor_user,
            branch_id=sale.branch_id,
            require_write=True,
        )

        reason = str(data.get("discount_reason") or data.get("reason") or "").strip()
        if not reason or len(reason) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_REASON_REQUIRED",
                    "discount_reason": "A documented reason (>= 3 chars) is mandatory for discount/price approval.",
                    "message_bn": "ছাড় বা মূল্য অনুমোদনের জন্য কমপক্ষে ৩ অক্ষরের সুনির্দিষ্ট কারণ উল্লেখ করতে হবে।",
                }
            )

        # Check elevated role or manager authorization
        is_elevated = actor_role in cls.ELEVATED_PRICING_ROLES
        manager_pin = str(data.get("manager_pin") or "").strip()
        if not is_elevated and not manager_pin:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Only Branch Managers, Owners, or Pharmacists can approve restricted discounts/price overrides.",
                    "message_bn": "কেবলমাত্র শাখা ব্যবস্থাপক, মালিক বা ফার্মাসিস্ট ছাড় ও মূল্য অনুমোদন করতে পারেন।",
                }
            )

        # Offline policy boundary enforcement
        is_offline = bool(data.get("is_offline", False))
        rate_val = Decimal("0.00")
        if "discount_rate_pct" in data and data["discount_rate_pct"] is not None:
            rate_val = parse_strict_decimal(data["discount_rate_pct"], "discount_rate_pct", min_val=Decimal("0.00"), max_val=Decimal("100.00"))

        if is_offline and rate_val > (cls.OFFLINE_MAX_DISCOUNT_RATIO * Decimal("100")):
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_DISCOUNT_CEILING_EXCEEDED",
                    "detail": f"Discounts exceeding 20% cannot be approved offline (requested {rate_val}%). Cloud verification required.",
                    "message_bn": "অফলাইনে ২০%-এর বেশি ছাড় অনুমোদন করা যায় না। ক্লাউড সংযোগ প্রয়োজন।",
                }
            )

        # Check line overrides for cost floor
        line_overrides = data.get("line_overrides") or []
        line_adjustments = []
        for lo in line_overrides:
            lid = lo.get("line_id")
            line = sale.lines.filter(line_id=lid).first()
            if not line:
                raise ValidationError({"line_overrides": f"Cart line '{lid}' not found."})
            u_price = parse_strict_decimal(lo.get("unit_price_bdt"), "unit_price_bdt", min_val=Decimal("0.01"))
            if line.unit_cost_bdt and u_price < line.unit_cost_bdt and not lo.get("allow_below_cost"):
                raise ValidationError(
                    {
                        "error_code": "ERR_BELOW_COST_PRICE_RESTRICTED",
                        "detail": f"Price override ({u_price} BDT) for {line.product.brand_name} is below cost ({line.unit_cost_bdt} BDT).",
                        "message_bn": "ক্রয়মূল্যের নিচে বিক্রি করা নিষিদ্ধ।",
                    }
                )
            line_adjustments.append({
                "line_id": lid,
                "unit_price_bdt": str(u_price),
                "override_reason": reason,
            })

        # Apply approval metadata
        sale.discount_approved_by = actor_user
        sale.discount_reason = reason
        if rate_val > Decimal("0.00"):
            sale.discount_type = PosDiscountType.PERCENTAGE
            sale.discount_rate_pct = rate_val
        elif "discount_amount_bdt" in data and data["discount_amount_bdt"] is not None:
            flat_amt = parse_strict_decimal(data["discount_amount_bdt"], "discount_amount_bdt", min_val=Decimal("0.00"))
            sale.discount_type = PosDiscountType.FLAT_BDT
            sale.header_discount_amount_bdt = flat_amt
        sale.save()

        # Update line adjustments if any
        if line_adjustments:
            pricing_data = {
                "line_adjustments": line_adjustments,
                "discount_reason": reason,
                "allow_empty": True,
            }
            return cls.update_sale_pricing(actor_user, sale, pricing_data)

        updated_sale, breakdown = cls.recompute_sale_totals(sale, actor_user, allow_empty=True)
        updated_sale.version += 1
        updated_sale.updated_by = actor_user
        updated_sale.save()
        audit = cls._record_pricing_audit(
            sale=updated_sale,
            action="DISCOUNT_APPROVED",
            user=actor_user,
            breakdown=breakdown,
            was_rejected=False,
        )
        PosDomainService._record_lifecycle_audit(
            sale=updated_sale,
            from_state=updated_sale.lifecycle_state,
            to_state=updated_sale.lifecycle_state,
            action="DISCOUNT_APPROVED",
            user=actor_user,
            reason=reason,
            metadata={
                "pricing_audit_id": audit.pricing_audit_id,
                "grand_total_bdt": str(updated_sale.grand_total_bdt),
                "calculated_pricing_hash": breakdown["calculated_pricing_hash"],
            },
        )
        return updated_sale, breakdown, audit

    @classmethod
    def confirm_sale_pricing(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> Tuple[PosSaleOrder, Dict[str, Any], PosCartPricingAudit]:
        """
        Reproducibly computes authoritative server-side line & header totals, optionally
        verifies client-observed expected_grand_total_bdt / expected_pricing_hash, stamps
        pricing_confirmed_at / pricing_confirmation_hash, and records an immutable audit entry.
        """
        from apps.core.pos_domain_service import PosDomainService

        try:
            with transaction.atomic():
                locked_sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
                PosDomainService.resolve_user_tenant_and_branch(
                    user,
                    branch_id=locked_sale.branch_id,
                    require_write=True,
                    allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
                )

                if locked_sale.lifecycle_state in (
                    PosSaleLifecycleState.FINALIZED,
                    PosSaleLifecycleState.VOIDED,
                    PosSaleLifecycleState.REVERSED,
                    PosSaleLifecycleState.FULLY_RETURNED,
                ):
                    raise ValidationError(
                        {
                            "error_code": "ERR_IMMUTABLE_SALE_STATE",
                            "detail": f"Cannot confirm pricing on sale in '{locked_sale.lifecycle_state}' state.",
                        }
                    )

                expected_ver = data.get("expected_version")
                if expected_ver is not None and int(expected_ver) != int(locked_sale.version):
                    raise _PricingRejectionAbort(
                        log_kwargs={
                            "sale": locked_sale,
                            "action": "PRICING_REJECTED",
                            "user": user,
                            "breakdown": cls.build_canonical_breakdown(locked_sale),
                            "was_rejected": True,
                            "rejection_code": "ERR_CART_VERSION_CONFLICT",
                            "rejection_message": f"Expected version {expected_ver}, got {locked_sale.version}.",
                        },
                        exc=ValidationError(
                            {
                                "error_code": "ERR_CART_VERSION_CONFLICT",
                                "expected_version": expected_ver,
                                "current_version": locked_sale.version,
                                "message_en": "Cart version changed before pricing confirmation.",
                                "message_bn": "মূল্য নিশ্চিত করার আগে কার্ট পরিবর্তিত হয়েছে; রিফ্রেশ করুন।",
                            }
                        ),
                    )

                locked_sale, breakdown = cls.recompute_sale_totals(locked_sale, user, allow_empty=False)
                server_total = q2(locked_sale.grand_total_bdt)
                server_hash = breakdown["calculated_pricing_hash"]

                raw_exp_total = data.get("expected_grand_total_bdt")
                exp_total: Optional[Decimal] = None
                if raw_exp_total is not None and str(raw_exp_total).strip() != "":
                    exp_total = parse_strict_decimal(
                        raw_exp_total,
                        "expected_grand_total_bdt",
                        min_val=Decimal("0.00"),
                    )
                    if exp_total != server_total:
                        raise _PricingRejectionAbort(
                            log_kwargs={
                                "sale": locked_sale,
                                "action": "PRICING_REJECTED",
                                "user": user,
                                "breakdown": breakdown,
                                "expected_grand_total_bdt": exp_total,
                                "was_rejected": True,
                                "rejection_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                                "rejection_message": f"Client expected total {exp_total}, server computed {server_total}.",
                            },
                            exc=ValidationError(
                                {
                                    "error_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                                    "expected_grand_total_bdt": f"{exp_total:.2f}",
                                    "server_grand_total_bdt": f"{server_total:.2f}",
                                    "server_pricing_hash": server_hash,
                                    "message_en": (
                                        f"Price confirmation mismatch: expected {exp_total:.2f} BDT, "
                                        f"authoritative server total is {server_total:.2f} BDT."
                                    ),
                                    "message_bn": (
                                        f"মূল্য গরমিল: প্রত্যাশিত {exp_total:.2f} টাকা, সার্ভারের সঠিক মোট {server_total:.2f} টাকা।"
                                    ),
                                }
                            ),
                        )

                raw_exp_hash = str(data.get("expected_pricing_hash") or "").strip()
                if raw_exp_hash and raw_exp_hash != server_hash:
                    raise _PricingRejectionAbort(
                        log_kwargs={
                            "sale": locked_sale,
                            "action": "PRICING_REJECTED",
                            "user": user,
                            "breakdown": breakdown,
                            "expected_grand_total_bdt": exp_total,
                            "was_rejected": True,
                            "rejection_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                            "rejection_message": f"Expected hash {raw_exp_hash[:16]} != server {server_hash[:16]}.",
                        },
                        exc=ValidationError(
                            {
                                "error_code": "ERR_PRICE_CONFIRMATION_MISMATCH",
                                "expected_pricing_hash": raw_exp_hash,
                                "server_pricing_hash": server_hash,
                                "server_grand_total_bdt": f"{server_total:.2f}",
                                "message_en": "Client pricing hash is stale or mismatched against server totals.",
                                "message_bn": "মূল্যের স্বাক্ষর (hash) পুরনো বা গরমিল; সর্বশেষ মূল্য লোড করুন।",
                            }
                        ),
                    )

                locked_sale.pricing_confirmed_at = timezone.now()
                locked_sale.pricing_confirmed_by = user
                locked_sale.pricing_confirmation_hash = server_hash
                locked_sale.version += 1
                locked_sale.updated_by = user
                locked_sale.save(
                    update_fields=[
                        "pricing_confirmed_at",
                        "pricing_confirmed_by",
                        "pricing_confirmation_hash",
                        "version",
                        "updated_by",
                        "updated_at",
                    ]
                )

                confirmed_breakdown = cls.build_canonical_breakdown(locked_sale)
                audit = cls._record_pricing_audit(
                    sale=locked_sale,
                    action="PRICING_CONFIRMED",
                    user=user,
                    breakdown=confirmed_breakdown,
                    expected_grand_total_bdt=exp_total if exp_total is not None else server_total,
                    was_rejected=False,
                )
                PosDomainService._record_lifecycle_audit(
                    sale=locked_sale,
                    from_state=locked_sale.lifecycle_state,
                    to_state=locked_sale.lifecycle_state,
                    action="PRICING_CONFIRMED",
                    user=user,
                    reason=f"Server-side pricing confirmed ({server_total:.2f} BDT)",
                    metadata={
                        "pricing_audit_id": audit.pricing_audit_id,
                        "pricing_confirmation_hash": server_hash,
                        "grand_total_bdt": f"{server_total:.2f}",
                    },
                )
                refreshed = (
                    PosSaleOrder.objects.filter(pk=locked_sale.pk)
                    .prefetch_related("lines__product", "lines__variant", "allocations", "payment_attempts", "lifecycle_audits")
                    .get()
                )
                return refreshed, confirmed_breakdown, audit
        except _PricingRejectionAbort as abort:
            cls._record_pricing_audit(**abort.log_kwargs)
            raise abort.exc
