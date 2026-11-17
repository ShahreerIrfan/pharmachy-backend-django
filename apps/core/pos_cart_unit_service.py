"""
backend/apps/core/pos_cart_unit_service.py
Architecture Decision: ADR-0223 (P223).
Phase 12: POS, Sales Orders, Checkout & Returns.

Domain Service for P223 Cart Item Unit Selection & Container Restrictions:
- Resolves authoritative integer conversion factors across branch/tenant PackagingHierarchyNode
  (effective-dated), InventoryBaseUnitStrategy, LooseTabletSalesConfig, and MedicineVariant.
- Enforces strict whole-integer quantity & conversion validation (blocks fractional inputs and
  client conversion_factor spoofing).
- Enforces NonDivisiblePackPolicy (sterile pens/inhalers/vials/sealed boxes), Pharmacist split
  approval gates (HTTP 403 for Cashier without Pharmacist/Manager override),
  UnitConversionPolicy / dosage-form compatibility guardrails, and LooseTabletSalesConfig limits.
- Computes exact Decimal (ROUND_HALF_UP) unit prices across Tablet/Strip/Box/Bottle/Vial/Tube/Sachet.
- Performs atomic cart line unit conversion (`KEEP_QUANTITY` or `CONVERT_PRESERVE_BASE`), releases
  stale FEFO reservations, re-validates sellable batch stock, increments optimistic version,
  and persists tamper-evident PosCartUnitSelectionAudit records (even on rolled-back rejections).
"""

from __future__ import annotations

import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InventoryBaseUnitStrategy,
    LooseTabletSalesConfig,
    MedicineProduct,
    MedicineVariant,
    NonDivisibleContainerType,
    NonDivisiblePackPolicy,
    PackagingHierarchyNode,
    PosCartScanEvent,
    PosCartUnitSelectionAudit,
    PosPackagingUnit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosScanResolutionSource,
    Tenant,
    TenantMedicineConfig,
    UnitConversionPolicy,
)
from apps.core.multi_level_unit_conversion_service import MultiLevelUnitConversionService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.pos_cart_unit")

DECIMAL_2DP = Decimal("0.01")
DECIMAL_4DP = Decimal("0.0001")


def _q2(val: Any) -> Decimal:
    return Decimal(str(val)).quantize(DECIMAL_2DP, rounding=ROUND_HALF_UP)


def _q4(val: Any) -> Decimal:
    return Decimal(str(val)).quantize(DECIMAL_4DP, rounding=ROUND_HALF_UP)


class _UnitSelectionRejectionAbort(Exception):
    """Rolls back atomic cart changes while persisting outside-transaction PosCartUnitSelectionAudit."""

    def __init__(self, audit_kwargs: Dict[str, Any], exc: APIException) -> None:
        super().__init__(str(exc))
        self.audit_kwargs = audit_kwargs
        self.exc = exc


class PosCartUnitSelectionService:
    """
    Authoritative unit conversion, packaging option availability, container policy validation,
    and atomic cart line unit-switching service for POS carts & sales orders.
    """

    ATOMIC_BASE_UNITS = {
        PosPackagingUnit.TABLET,
        PosPackagingUnit.UNIT,
        PosPackagingUnit.BOTTLE,
        PosPackagingUnit.VIAL,
        PosPackagingUnit.TUBE,
        PosPackagingUnit.SACHET,
    }

    SPLIT_APPROVER_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
    }

    PACKAGING_LABELS: Dict[str, Tuple[str, str]] = {
        PosPackagingUnit.TABLET: ("Tablet / Unit", "ট্যাবলেট / পিস"),
        PosPackagingUnit.STRIP: ("Strip", "স্ট্রিপ"),
        PosPackagingUnit.BOX: ("Box", "বক্স"),
        PosPackagingUnit.BOTTLE: ("Bottle", "বোতল"),
        PosPackagingUnit.VIAL: ("Vial", "ভায়াল"),
        PosPackagingUnit.TUBE: ("Tube", "টিউব"),
        PosPackagingUnit.SACHET: ("Sachet", "স্যাশে"),
        PosPackagingUnit.UNIT: ("Unit", "একক"),
    }

    FORM_ALLOWED_UNITS: Dict[str, List[str]] = {
        "TABLET": [PosPackagingUnit.TABLET, PosPackagingUnit.STRIP, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "CAPSULE": [PosPackagingUnit.TABLET, PosPackagingUnit.STRIP, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "SYRUP": [PosPackagingUnit.BOTTLE, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "SUSPENSION": [PosPackagingUnit.BOTTLE, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "DROPS": [PosPackagingUnit.BOTTLE, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "INJECTION": [PosPackagingUnit.VIAL, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "TOPICAL": [PosPackagingUnit.TUBE, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "SACHET": [PosPackagingUnit.SACHET, PosPackagingUnit.BOX, PosPackagingUnit.UNIT],
        "INSULIN": [PosPackagingUnit.UNIT, PosPackagingUnit.VIAL, PosPackagingUnit.BOX],
        "INHALER": [PosPackagingUnit.UNIT, PosPackagingUnit.BOX],
        "DEVICE": [PosPackagingUnit.UNIT, PosPackagingUnit.BOX],
    }

    FORM_BASE_UNIT: Dict[str, str] = {
        "TABLET": PosPackagingUnit.TABLET,
        "CAPSULE": PosPackagingUnit.TABLET,
        "SYRUP": PosPackagingUnit.BOTTLE,
        "SUSPENSION": PosPackagingUnit.BOTTLE,
        "DROPS": PosPackagingUnit.BOTTLE,
        "INJECTION": PosPackagingUnit.VIAL,
        "TOPICAL": PosPackagingUnit.TUBE,
        "SACHET": PosPackagingUnit.SACHET,
        "INSULIN": PosPackagingUnit.UNIT,
        "INHALER": PosPackagingUnit.UNIT,
        "DEVICE": PosPackagingUnit.UNIT,
    }

    @classmethod
    def parse_strict_positive_int(cls, raw_value: Any, field_name: str = "quantity_units") -> int:
        """
        Strictly parses positive whole integers.
        Rejects floats (1.5), fractional strings ('1.5', '1.0'), booleans (True),
        and zero/negative numbers without silent int() truncation.
        """
        if isinstance(raw_value, bool) or isinstance(raw_value, float) or raw_value is None:
            raise ValidationError(
                {
                    "error_code": "ERR_FRACTIONAL_UNIT_PROHIBITED",
                    field_name: f"'{field_name}' must be a positive whole integer; fractional or non-integer values are prohibited.",
                    "message_en": f"'{field_name}' must be a positive whole integer; fractional quantities are prohibited.",
                    "message_bn": "পিওএস কার্টে ভগ্নাংশ পরিমাণ অনুমোদিত নয়; পূর্ণসংখ্যা দিন।",
                }
            )

        if isinstance(raw_value, Decimal):
            if raw_value % Decimal("1") != Decimal("0"):
                raise ValidationError(
                    {
                        "error_code": "ERR_FRACTIONAL_UNIT_PROHIBITED",
                        field_name: f"Fractional value '{raw_value}' is not allowed for '{field_name}'.",
                        "message_en": f"Fractional value '{raw_value}' is prohibited in POS carts.",
                        "message_bn": "পিওএস কার্টে ভগ্নাংশ পরিমাণ অনুমোদিত নয়।",
                    }
                )
            val = int(raw_value)
        elif isinstance(raw_value, int):
            val = raw_value
        elif isinstance(raw_value, str):
            clean = raw_value.strip()
            if not re.fullmatch(r"[+-]?\d+", clean):
                raise ValidationError(
                    {
                        "error_code": "ERR_FRACTIONAL_UNIT_PROHIBITED",
                        field_name: f"'{raw_value}' is not a valid whole integer for '{field_name}'.",
                        "message_en": f"Fractional or non-integer value '{raw_value}' is prohibited.",
                        "message_bn": "পিওএস কার্টে ভগ্নাংশ বা অবৈধ সংখ্যা অনুমোদিত নয়।",
                    }
                )
            val = int(clean)
        else:
            raise ValidationError(
                {
                    "error_code": "ERR_FRACTIONAL_UNIT_PROHIBITED",
                    field_name: f"'{field_name}' must be a whole integer.",
                    "message_en": f"'{field_name}' must be a whole integer.",
                    "message_bn": "অবৈধ সংখ্যা; পূর্ণসংখ্যা দিন।",
                }
            )

        if val <= 0:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_UNIT_QUANTITY",
                    field_name: f"'{field_name}' must be an integer >= 1.",
                    "message_en": f"'{field_name}' must be at least 1.",
                    "message_bn": "পরিমাণ অবশ্যই ১ বা তার বেশি হতে হবে।",
                }
            )
        return val

    @classmethod
    def normalize_dosage_form(cls, product: MedicineProduct) -> str:
        raw = str(getattr(product, "dosage_form", "") or "").strip().upper()
        if not raw:
            return ""
        if "INSULIN" in raw or "PRE-FILLED" in raw or "PEN" in raw:
            return "INSULIN"
        if "INHALER" in raw or "CANISTER" in raw:
            return "INHALER"
        if "DROP" in raw:
            return "DROPS"
        if any(k in raw for k in ("CREAM", "OINTMENT", "GEL", "TOPICAL", "LOTION")):
            return "TOPICAL"
        if "SUSPENSION" in raw:
            return "SUSPENSION"
        if "SYRUP" in raw or "ELIXIR" in raw or "SOLUTION" in raw:
            return "SYRUP"
        if any(k in raw for k in ("INJECTION", "AMPOULE", "VIAL", "IV INFUSION")):
            return "INJECTION"
        if "SACHET" in raw or "POWDER" in raw:
            return "SACHET"
        if "CAPSULE" in raw:
            return "CAPSULE"
        if "TABLET" in raw or "CAPLET" in raw:
            return "TABLET"
        if "DEVICE" in raw or "KIT" in raw:
            return "DEVICE"
        return raw

    @classmethod
    def map_unit_label(cls, raw_unit: str) -> str:
        clean = str(raw_unit or "").strip().upper()
        if clean in ("BOX", "CARTON", "SHIPPER_CARTON", "MASTER_CARTON"):
            return PosPackagingUnit.BOX
        if clean in ("STRIP", "BLISTER", "CARD"):
            return PosPackagingUnit.STRIP
        if clean in ("BOTTLE", "DROPTAINER"):
            return PosPackagingUnit.BOTTLE
        if clean in ("VIAL", "AMPOULE"):
            return PosPackagingUnit.VIAL
        if clean in ("TUBE",):
            return PosPackagingUnit.TUBE
        if clean in ("SACHET", "POUCH"):
            return PosPackagingUnit.SACHET
        if clean in ("UNIT", "PIECE", "PEN", "INHALER", "CANISTER", "KIT", "DEVICE"):
            return PosPackagingUnit.UNIT
        return PosPackagingUnit.TABLET

    @classmethod
    def _pick_branch_then_tenant(cls, queryset: Any, branch: Optional[Branch]) -> Optional[Any]:
        rows = list(queryset)
        if not rows:
            return None
        if branch:
            branch_row = next((r for r in rows if getattr(r, "branch_id", None) == branch.branch_id), None)
            if branch_row is not None:
                return branch_row
        tenant_row = next((r for r in rows if getattr(r, "branch_id", None) is None), None)
        return tenant_row or rows[0]

    @classmethod
    def get_active_policies(
        cls,
        tenant: Tenant,
        branch: Optional[Branch],
        product: MedicineProduct,
    ) -> Dict[str, Any]:
        today = timezone.now().date()
        branch_filter = Q(branch=branch) | Q(branch__isnull=True) if branch else Q(branch__isnull=True)

        nodes_raw = list(
            PackagingHierarchyNode.objects.filter(
                tenant=tenant,
                product=product,
                is_active=True,
                effective_from__lte=today,
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
            .filter(branch_filter)
            .order_by("tier_level", "effective_from")
        )
        # Apply tenant nodes first, then branch overrides cleanly
        tenant_nodes = [n for n in nodes_raw if n.branch_id is None]
        branch_nodes = [n for n in nodes_raw if branch and n.branch_id == branch.branch_id]
        ordered_nodes = tenant_nodes + branch_nodes

        base_strategy = cls._pick_branch_then_tenant(
            InventoryBaseUnitStrategy.objects.filter(tenant=tenant, product=product, is_active=True).filter(branch_filter),
            branch,
        )
        ucp = cls._pick_branch_then_tenant(
            UnitConversionPolicy.objects.filter(tenant=tenant, product=product, is_active=True).filter(branch_filter),
            branch,
        )
        ltsc = cls._pick_branch_then_tenant(
            LooseTabletSalesConfig.objects.filter(tenant=tenant, product=product, is_active=True).filter(branch_filter),
            branch,
        )
        nd_policy = cls._pick_branch_then_tenant(
            NonDivisiblePackPolicy.objects.filter(tenant=tenant, product=product, is_active=True)
            .filter(branch_filter)
            .order_by("-updated_at"),
            branch,
        )

        return {
            "hierarchy_nodes": ordered_nodes,
            "base_strategy": base_strategy,
            "unit_conversion_policy": ucp,
            "loose_tablet_config": ltsc,
            "non_divisible_policy": nd_policy,
        }

    @classmethod
    def resolve_packaging_context(
        cls,
        tenant: Tenant,
        branch: Optional[Branch],
        product: MedicineProduct,
        variant: Optional[MedicineVariant] = None,
    ) -> Dict[str, Any]:
        """
        Resolves canonical packaging factors, base unit, pricing MRP, and authoritative pinning sources.
        """
        policies = cls.get_active_policies(tenant, branch, product)
        nodes: List[PackagingHierarchyNode] = policies["hierarchy_nodes"]
        base_strategy: Optional[InventoryBaseUnitStrategy] = policies["base_strategy"]
        ltsc: Optional[LooseTabletSalesConfig] = policies["loose_tablet_config"]
        nd_policy: Optional[NonDivisiblePackPolicy] = policies["non_divisible_policy"]
        ucp: Optional[UnitConversionPolicy] = policies["unit_conversion_policy"]

        norm_form = cls.normalize_dosage_form(product)
        primary_base_unit = (
            cls.map_unit_label(base_strategy.base_unit)
            if base_strategy and base_strategy.base_unit
            else cls.FORM_BASE_UNIT.get(norm_form, PosPackagingUnit.TABLET)
        )

        is_solid = norm_form in ("TABLET", "CAPSULE", "")
        strip_factor = 10 if is_solid else 1
        strips_per_box: Optional[int] = None
        explicit_box: Optional[int] = None
        pinned_factors: Dict[str, Tuple[int, str]] = {}
        default_source = "DEFAULT_FORM"

        if base_strategy:
            if base_strategy.intermediate_to_base_multiplier > 1:
                strip_factor = int(base_strategy.intermediate_to_base_multiplier)
                pinned_factors[PosPackagingUnit.STRIP] = (strip_factor, "BASE_UNIT_STRATEGY")
                default_source = "BASE_UNIT_STRATEGY"
            if base_strategy.total_base_units_per_pack > 1:
                explicit_box = int(base_strategy.total_base_units_per_pack)
                pinned_factors[PosPackagingUnit.BOX] = (explicit_box, "BASE_UNIT_STRATEGY")
                default_source = "BASE_UNIT_STRATEGY"

        if ltsc and ltsc.strip_size_pieces >= 1:
            if PosPackagingUnit.STRIP not in pinned_factors or ltsc.strip_size_pieces != 10:
                strip_factor = max(1, int(ltsc.strip_size_pieces))
            pinned_factors[PosPackagingUnit.STRIP] = (strip_factor, "LOOSE_TABLET_CONFIG")
            if default_source == "DEFAULT_FORM":
                default_source = "LOOSE_TABLET_CONFIG"

        for node in nodes:
            p_unit = str(node.parent_unit or "").strip().upper()
            c_unit = str(node.child_unit or "").strip().upper()
            factor_val = max(1, int(node.conversion_factor))
            if p_unit in ("STRIP", "BLISTER") and c_unit in ("TABLET", "CAPSULE", "UNIT", "PIECE"):
                strip_factor = factor_val
                pinned_factors[PosPackagingUnit.STRIP] = (strip_factor, "PACKAGING_HIERARCHY")
                default_source = "PACKAGING_HIERARCHY"
            elif p_unit in ("BOX", "CARTON") and c_unit in ("STRIP", "BLISTER"):
                strips_per_box = factor_val
                default_source = "PACKAGING_HIERARCHY"
            elif p_unit in ("BOX", "CARTON") and c_unit in (
                "TABLET",
                "CAPSULE",
                "UNIT",
                "PIECE",
                "BOTTLE",
                "VIAL",
                "TUBE",
                "SACHET",
                "PEN",
                "AMPOULE",
                "CANISTER",
            ):
                explicit_box = factor_val
                pinned_factors[PosPackagingUnit.BOX] = (explicit_box, "PACKAGING_HIERARCHY")
                default_source = "PACKAGING_HIERARCHY"

        if variant and int(getattr(variant, "pack_size_numeric", 1) or 1) > 1:
            v_factor = int(variant.pack_size_numeric)
            v_label = f"{getattr(variant, 'variant_name', '')} {getattr(variant, 'pack_size', '')}".upper()
            looks_like_box = any(tok in v_label for tok in ("BOX", "CARTON", "PACK OF", "10X10"))
            if is_solid:
                if (looks_like_box or (PosPackagingUnit.STRIP in pinned_factors and v_factor > strip_factor)) and PosPackagingUnit.BOX not in pinned_factors:
                    explicit_box = v_factor
                    pinned_factors[PosPackagingUnit.BOX] = (explicit_box, "VARIANT_PACK_SIZE")
                elif PosPackagingUnit.STRIP not in pinned_factors and PosPackagingUnit.BOX not in pinned_factors:
                    strip_factor = v_factor
                    pinned_factors[PosPackagingUnit.STRIP] = (strip_factor, "VARIANT_PACK_SIZE")
            elif PosPackagingUnit.BOX not in pinned_factors:
                explicit_box = v_factor
                pinned_factors[PosPackagingUnit.BOX] = (explicit_box, "VARIANT_PACK_SIZE")
            if default_source == "DEFAULT_FORM":
                default_source = "VARIANT_PACK_SIZE"

        if explicit_box is not None:
            box_factor = max(1, explicit_box)
        elif strips_per_box is not None:
            box_factor = max(strip_factor, strip_factor * strips_per_box)
            pinned_factors[PosPackagingUnit.BOX] = (box_factor, "PACKAGING_HIERARCHY")
        elif nd_policy and int(nd_policy.approved_break_factor or 1) > 1 and not is_solid:
            box_factor = int(nd_policy.approved_break_factor)
            pinned_factors[PosPackagingUnit.BOX] = (box_factor, "NON_DIVISIBLE_POLICY")
        elif is_solid:
            box_factor = max(strip_factor, strip_factor * 10)
        else:
            box_factor = 10

        return {
            "normalized_form": norm_form,
            "primary_base_unit": primary_base_unit,
            "strip_factor": max(1, strip_factor),
            "box_factor": max(1, box_factor),
            "pinned_factors": pinned_factors,
            "default_source": default_source,
            "policies": policies,
            "hierarchy_nodes": nodes,
            "base_strategy": base_strategy,
            "unit_conversion_policy": ucp,
            "loose_tablet_config": ltsc,
            "non_divisible_policy": nd_policy,
        }

    @classmethod
    def compute_unit_price_bdt(
        cls,
        *,
        tenant: Tenant,
        product: MedicineProduct,
        variant: Optional[MedicineVariant],
        packaging_unit: str,
        conversion_factor: int,
        pkg_ctx: Dict[str, Any],
        cfg: Optional[TenantMedicineConfig] = None,
    ) -> Decimal:
        """Computes exact Decimal unit price (ROUND_HALF_UP to 2dp) respecting strip/loose pricing configs."""
        if cfg is None:
            cfg = TenantMedicineConfig.objects.filter(tenant=tenant, product=product).first()

        ltsc: Optional[LooseTabletSalesConfig] = pkg_ctx.get("loose_tablet_config")
        base_strategy: Optional[InventoryBaseUnitStrategy] = pkg_ctx.get("base_strategy")
        norm_form = str(pkg_ctx.get("normalized_form") or "")

        resolved_var = variant
        if resolved_var is None and conversion_factor > 1:
            resolved_var = (
                MedicineVariant.objects.filter(
                    product=product,
                    is_active=True,
                    pack_size_numeric=conversion_factor,
                )
                .order_by("variant_id")
                .first()
            )

        if cfg and cfg.custom_mrp_unit is not None and cfg.moderation_status == "APPROVED":
            base_mrp = _q2(cfg.custom_mrp_unit)
        elif resolved_var and Decimal(str(resolved_var.mrp_unit or "0")) > Decimal("0.00"):
            base_mrp = _q2(resolved_var.mrp_unit)
        elif base_strategy and Decimal(str(base_strategy.mrp_per_base_unit or "0")) > Decimal("0.00"):
            base_mrp = _q2(base_strategy.mrp_per_base_unit)
        else:
            base_mrp = _q2(product.mrp_unit)

        if (
            packaging_unit in (PosPackagingUnit.TABLET, PosPackagingUnit.UNIT)
            and conversion_factor == 1
            and ltsc is not None
            and norm_form in ("TABLET", "CAPSULE", "")
        ):
            if Decimal(str(ltsc.loose_unit_mrp or "0")) > Decimal("0.00"):
                loose_price = Decimal(str(ltsc.loose_unit_mrp))
            elif Decimal(str(ltsc.strip_mrp or "0")) > Decimal("0.00"):
                loose_price = Decimal(str(ltsc.strip_mrp)) / Decimal(max(1, int(ltsc.strip_size_pieces)))
            else:
                loose_price = base_mrp

            if ltsc.apply_loose_unit_surcharge and Decimal(str(ltsc.loose_surcharge_percentage or "0")) > Decimal("0.00"):
                surcharge_mul = Decimal("1.00") + (Decimal(str(ltsc.loose_surcharge_percentage)) / Decimal("100.00"))
                loose_price = loose_price * surcharge_mul
            return _q2(loose_price)

        if packaging_unit == PosPackagingUnit.STRIP:
            if resolved_var and int(resolved_var.pack_size_numeric or 1) == conversion_factor and Decimal(str(resolved_var.mrp_pack or "0")) > Decimal("0.00"):
                return _q2(resolved_var.mrp_pack)
            if ltsc and int(ltsc.strip_size_pieces) == conversion_factor and Decimal(str(ltsc.strip_mrp or "0")) > Decimal("0.00"):
                return _q2(ltsc.strip_mrp)
            return _q2(base_mrp * Decimal(conversion_factor))

        if resolved_var and packaging_unit == PosPackagingUnit.BOX and int(resolved_var.pack_size_numeric or 1) == conversion_factor and Decimal(str(resolved_var.mrp_pack or "0")) > Decimal("0.00"):
            return _q2(resolved_var.mrp_pack)

        return _q2(base_mrp * Decimal(conversion_factor))

    @classmethod
    def evaluate_unit_selection(
        cls,
        *,
        tenant: Tenant,
        branch: Optional[Branch],
        product: MedicineProduct,
        variant: Optional[MedicineVariant],
        packaging_unit: str,
        requested_conversion_factor: Optional[Any] = None,
        quantity_units: int = 1,
        actor_user: Any = None,
        override_reason: str = "",
        pkg_ctx: Optional[Dict[str, Any]] = None,
        enforce_loose_quantity_bounds: bool = True,
    ) -> Dict[str, Any]:
        """
        Validates packaging unit, conversion factor, dosing-form rules, non-divisible pack policy,
        pharmacist split approval gate, and loose tablet dispensing bounds.
        """
        unit_upper = str(packaging_unit or "").strip().upper()
        if unit_upper not in PosPackagingUnit.values:
            raise ValidationError(
                {
                    "error_code": "ERR_UNSUPPORTED_PACKAGING_UNIT",
                    "packaging_unit": f"Unsupported packaging unit '{packaging_unit}'.",
                    "message_en": f"Packaging unit '{packaging_unit}' is not supported.",
                    "message_bn": f"প্যাকেজিং ইউনিট '{packaging_unit}' সমর্থিত নয়।",
                }
            )

        ctx = pkg_ctx or cls.resolve_packaging_context(tenant, branch, product, variant=variant)
        norm_form = str(ctx["normalized_form"] or "")
        strip_factor = int(ctx["strip_factor"])
        box_factor = int(ctx["box_factor"])
        pinned_factors: Dict[str, Tuple[int, str]] = ctx["pinned_factors"]
        nd_policy: Optional[NonDivisiblePackPolicy] = ctx["non_divisible_policy"]
        ltsc: Optional[LooseTabletSalesConfig] = ctx["loose_tablet_config"]
        ucp: Optional[UnitConversionPolicy] = ctx["unit_conversion_policy"]

        # 1. Resolve & anti-spoof conversion factor
        explicit_factor: Optional[int] = None
        if requested_conversion_factor is not None:
            explicit_factor = cls.parse_strict_positive_int(requested_conversion_factor, "conversion_factor")

        if unit_upper in cls.ATOMIC_BASE_UNITS:
            canonical_factor = 1
            factor_source = ctx["default_source"]
            if explicit_factor is not None and explicit_factor != 1:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_UNIT_CONVERSION",
                        "conversion_factor": (
                            f"Atomic unit '{unit_upper}' must have conversion_factor=1; got {explicit_factor}."
                        ),
                        "message_en": f"Atomic packaging unit '{unit_upper}' must convert 1:1 to base units.",
                        "message_bn": f"একক ইউনিট '{unit_upper}'-এর কনভার্সন ফ্যাক্টর অবশ্যই ১ হতে হবে।",
                    }
                )
            resolved_factor = 1
        elif unit_upper == PosPackagingUnit.STRIP:
            pinned = pinned_factors.get(PosPackagingUnit.STRIP)
            canonical_factor = pinned[0] if pinned else strip_factor
            factor_source = pinned[1] if pinned else ctx["default_source"]
            if explicit_factor is not None:
                if pinned is not None and explicit_factor != canonical_factor:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_UNIT_CONVERSION",
                            "conversion_factor": (
                                f"Conversion factor mismatch for {unit_upper}: expected {canonical_factor} "
                                f"from {factor_source}, got {explicit_factor}."
                            ),
                            "message_en": f"Invalid STRIP conversion factor: expected {canonical_factor}.",
                            "message_bn": f"স্ট্রিপ কনভার্সন ফ্যাক্টর ভুল: {canonical_factor} হওয়া উচিত।",
                        }
                    )
                resolved_factor = explicit_factor
                if pinned is None:
                    factor_source = "CALLER_SPECIFIED"
            else:
                resolved_factor = canonical_factor
        else:  # BOX
            pinned = pinned_factors.get(PosPackagingUnit.BOX)
            canonical_factor = pinned[0] if pinned else box_factor
            factor_source = pinned[1] if pinned else ctx["default_source"]
            if explicit_factor is not None:
                if pinned is not None and explicit_factor != canonical_factor:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_UNIT_CONVERSION",
                            "conversion_factor": (
                                f"Conversion factor mismatch for BOX: expected {canonical_factor} "
                                f"from {factor_source}, got {explicit_factor}."
                            ),
                            "message_en": f"Invalid BOX conversion factor: expected {canonical_factor}.",
                            "message_bn": f"বক্স কনভার্সন ফ্যাক্টর ভুল: {canonical_factor} হওয়া উচিত।",
                        }
                    )
                if explicit_factor == 1 and canonical_factor > 1:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_UNIT_CONVERSION",
                            "conversion_factor": "Multi-unit BOX packaging cannot declare conversion_factor=1.",
                            "message_en": "Multi-unit BOX packaging cannot declare conversion_factor=1.",
                            "message_bn": "বক্স প্যাকেজিংয়ের কনভার্সন ফ্যাক্টর ১ হতে পারে না।",
                        }
                    )
                resolved_factor = explicit_factor
                if pinned is None:
                    factor_source = "CALLER_SPECIFIED"
            else:
                resolved_factor = canonical_factor

        # 2. Check NonDivisiblePackPolicy FIRST so sealed container guardrails emit ERR_NON_DIVISIBLE_PACK
        container_policy_applied = "STANDARD"
        override_approved_by = None
        actor_role = str(getattr(actor_user, "role", "") or "").upper()

        if nd_policy is not None:
            ctype = str(nd_policy.container_type or "")
            break_floor = max(1, int(nd_policy.approved_break_factor or 1))
            sealed_container_whole_units = {PosPackagingUnit.BOX}
            if ctype in (NonDivisibleContainerType.EYE_DROPS_BOTTLE, NonDivisibleContainerType.TOPICAL_TUBE) and break_floor <= 1:
                sealed_container_whole_units.update({PosPackagingUnit.BOTTLE, PosPackagingUnit.TUBE})
            elif ctype == NonDivisibleContainerType.STERILE_VIAL_BOX and break_floor <= 1:
                sealed_container_whole_units.add(PosPackagingUnit.VIAL)

            is_splitting_attempt = False
            if ctype == NonDivisibleContainerType.SEALED_BOX:
                if unit_upper != PosPackagingUnit.BOX and (resolved_factor < box_factor or unit_upper in (PosPackagingUnit.TABLET, PosPackagingUnit.UNIT)):
                    is_splitting_attempt = True
            elif unit_upper in (PosPackagingUnit.TABLET, PosPackagingUnit.UNIT) and unit_upper not in sealed_container_whole_units:
                is_splitting_attempt = True
            elif break_floor > 1 and resolved_factor < break_floor:
                is_splitting_attempt = True

            if is_splitting_attempt:
                if not nd_policy.is_splitting_allowed:
                    raise ValidationError(
                        {
                            "error_code": "ERR_NON_DIVISIBLE_PACK",
                            "packaging_unit": (
                                f"'{product.brand_name}' cannot be split ({nd_policy.prohibition_reason}). "
                                f"Select intact sealed pack/box instead."
                            ),
                            "message_en": f"Non-divisible container restriction: {nd_policy.prohibition_reason}",
                            "message_bn": f"সিলড কনটেইনার নীতি অনুযায়ী '{product.brand_name}' ভেঙে বা খোলা বিক্রি করা নিষিদ্ধ।",
                        }
                    )
                # Splitting allowed conditionally: check Pharmacist/Manager sign-off
                if nd_policy.require_pharmacist_approval:
                    if actor_role not in cls.SPLIT_APPROVER_ROLES:
                        raise PermissionDenied(
                            {
                                "error_code": "ERR_PHARMACIST_SPLIT_APPROVAL_REQUIRED",
                                "message_en": (
                                    f"Splitting '{product.brand_name}' ({ctype}) requires Pharmacist or Manager approval."
                                ),
                                "message_bn": f"'{product.brand_name}' প্যাক ভাঙতে ফার্মাসিস্ট বা ম্যানেজারের অনুমোদন প্রয়োজন।",
                            }
                        )
                    container_policy_applied = "PHARMACIST_OVERRIDE_APPROVED"
                    override_approved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
                else:
                    container_policy_applied = "CONDITIONAL_SPLIT_ALLOWED"
            else:
                container_policy_applied = "SEALED_INTACT_ALLOWED"

        # 3. Dosage-form / Dosing-unit guardrail check
        if ucp is not None:
            prohibited_units = {str(u).strip().upper() for u in (ucp.prohibited_dosing_units or []) if u}
        else:
            prohibited_units = set(MultiLevelUnitConversionService.DEFAULT_PROHIBITED_BY_FORM.get(norm_form, []))

        allowed_by_form = cls.FORM_ALLOWED_UNITS.get(norm_form)
        is_dosing_incompatible = unit_upper in prohibited_units or (
            allowed_by_form is not None and unit_upper not in allowed_by_form
        )
        if is_dosing_incompatible:
            raise ValidationError(
                {
                    "error_code": "ERR_INCOMPATIBLE_DOSING_UNIT",
                    "packaging_unit": (
                        f"Packaging unit '{unit_upper}' is clinically incompatible with dosage form '{product.dosage_form}'."
                    ),
                    "message_en": f"Unit '{unit_upper}' is incompatible with '{product.dosage_form}'.",
                    "message_bn": f"'{product.dosage_form}' ফর্মের ওষুধের জন্য '{unit_upper}' ইউনিট প্রযোজ্য নয়।",
                }
            )

        # 4. Loose Tablet Sales Policy check (solid oral units at factor=1)
        if (
            ltsc is not None
            and unit_upper in (PosPackagingUnit.TABLET, PosPackagingUnit.UNIT)
            and resolved_factor == 1
            and norm_form in ("TABLET", "CAPSULE", "")
        ):
            if not ltsc.allow_loose_sales:
                raise ValidationError(
                    {
                        "error_code": "ERR_LOOSE_TABLET_SALES_PROHIBITED",
                        "packaging_unit": (
                            f"Loose tablet sales prohibited for '{product.brand_name}': {ltsc.prohibition_reason}"
                        ),
                        "message_en": f"Loose tablet sales prohibited: {ltsc.prohibition_reason}",
                        "message_bn": f"'{product.brand_name}' খোলা ট্যাবলেট হিসেবে বিক্রি নিষিদ্ধ (স্ট্রিপ বা বক্স নির্বাচন করুন)।",
                    }
                )
            if enforce_loose_quantity_bounds:
                base_total = int(quantity_units) * resolved_factor
                if base_total < int(ltsc.min_loose_dispense_units):
                    raise ValidationError(
                        {
                            "error_code": "ERR_BELOW_MIN_LOOSE_DISPENSE",
                            "quantity_units": (
                                f"Loose dispensing for '{product.brand_name}' requires at least "
                                f"{ltsc.min_loose_dispense_units} base unit(s); requested {base_total}."
                            ),
                            "message_en": f"Minimum loose dispense is {ltsc.min_loose_dispense_units} tablet(s).",
                            "message_bn": f"সর্বনিম্ন {ltsc.min_loose_dispense_units}টি খোলা ট্যাবলেট নিতে হবে।",
                        }
                    )
                if base_total > int(ltsc.max_loose_dispense_units):
                    raise ValidationError(
                        {
                            "error_code": "ERR_EXCEEDS_MAX_LOOSE_DISPENSE",
                            "quantity_units": (
                                f"Loose dispensing for '{product.brand_name}' is capped at "
                                f"{ltsc.max_loose_dispense_units} base unit(s); requested {base_total}."
                            ),
                            "message_en": (
                                f"Exceeds maximum loose dispense limit ({ltsc.max_loose_dispense_units}); select STRIP or BOX."
                            ),
                            "message_bn": (
                                f"সর্বোচ্চ খোলা সীমা ({ltsc.max_loose_dispense_units}টি) অতিক্রম করেছে; স্ট্রিপ বা বক্স নিন।"
                            ),
                        }
                    )
            container_policy_applied = "LOOSE_SURCHARGE_APPLIED" if ltsc.apply_loose_unit_surcharge else "LOOSE_ALLOWED"

        unit_price_bdt = cls.compute_unit_price_bdt(
            tenant=tenant,
            product=product,
            variant=variant,
            packaging_unit=unit_upper,
            conversion_factor=resolved_factor,
            pkg_ctx=ctx,
        )

        return {
            "packaging_unit": unit_upper,
            "conversion_factor": resolved_factor,
            "conversion_source": factor_source,
            "unit_price_bdt": unit_price_bdt,
            "dosage_form_snapshot": str(product.dosage_form or "")[:64],
            "container_policy_applied": container_policy_applied,
            "override_approved_by": override_approved_by,
            "override_reason": str(override_reason or "")[:255],
            "pkg_ctx": ctx,
        }

    @classmethod
    def build_packaging_options(
        cls,
        *,
        tenant: Tenant,
        branch: Optional[Branch],
        product: MedicineProduct,
        variant: Optional[MedicineVariant],
        actor_user: Any = None,
        cfg: Optional[TenantMedicineConfig] = None,
        pkg_ctx: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Builds ordered packaging options for UI selectors (with allowed vs blocked flags & EN/BN reasons).
        """
        ctx = pkg_ctx or cls.resolve_packaging_context(tenant, branch, product, variant=variant)
        norm_form = str(ctx["normalized_form"] or "")
        primary_base = str(ctx["primary_base_unit"])
        strip_factor = int(ctx["strip_factor"])
        box_factor = int(ctx["box_factor"])

        candidate_units: List[Tuple[str, int]] = []
        if norm_form in ("TABLET", "CAPSULE", ""):
            candidate_units = [
                (PosPackagingUnit.TABLET, 1),
                (PosPackagingUnit.STRIP, strip_factor),
                (PosPackagingUnit.BOX, box_factor),
            ]
        elif primary_base in (PosPackagingUnit.BOTTLE, PosPackagingUnit.VIAL, PosPackagingUnit.TUBE, PosPackagingUnit.SACHET):
            candidate_units = [
                (primary_base, 1),
                (PosPackagingUnit.BOX, box_factor),
            ]
        else:
            candidate_units = [
                (PosPackagingUnit.UNIT, 1),
                (PosPackagingUnit.BOX, box_factor),
            ]

        options: List[Dict[str, Any]] = []
        blocked_units: List[str] = []

        for unit_code, factor in candidate_units:
            lbl_en, lbl_bn = cls.PACKAGING_LABELS.get(unit_code, (unit_code.title(), unit_code))
            is_allowed = True
            req_pharm = False
            block_code = ""
            block_en = ""
            block_bn = ""

            try:
                evaluated = cls.evaluate_unit_selection(
                    tenant=tenant,
                    branch=branch,
                    product=product,
                    variant=variant,
                    packaging_unit=unit_code,
                    requested_conversion_factor=None,
                    quantity_units=1,
                    actor_user=actor_user,
                    pkg_ctx=ctx,
                    enforce_loose_quantity_bounds=False,
                )
                unit_price = evaluated["unit_price_bdt"]
                source = evaluated["conversion_source"]
                policy_status = evaluated["container_policy_applied"]
            except PermissionDenied as pd:
                is_allowed = False
                req_pharm = True
                det = pd.detail if isinstance(pd.detail, dict) else {"message_en": str(pd.detail)}
                block_code = str(det.get("error_code") or "ERR_PHARMACIST_SPLIT_APPROVAL_REQUIRED")
                block_en = str(det.get("message_en") or "Pharmacist approval required.")
                block_bn = str(det.get("message_bn") or "ফার্মাসিস্টের অনুমোদন প্রয়োজন।")
                unit_price = cls.compute_unit_price_bdt(
                    tenant=tenant,
                    product=product,
                    variant=variant,
                    packaging_unit=unit_code,
                    conversion_factor=factor,
                    pkg_ctx=ctx,
                    cfg=cfg,
                )
                source = ctx["default_source"]
                policy_status = "PHARMACIST_APPROVAL_REQUIRED"
                blocked_units.append(unit_code)
            except ValidationError as ve:
                is_allowed = False
                det = ve.detail if isinstance(ve.detail, dict) else {"message_en": str(ve.detail)}
                block_code = str(det.get("error_code") or "ERR_UNIT_RESTRICTED")
                block_en = str(det.get("message_en") or det.get("packaging_unit") or "Unit restricted.")
                block_bn = str(det.get("message_bn") or "এই ইউনিট নির্বাচন অনুমোদিত নয়।")
                unit_price = cls.compute_unit_price_bdt(
                    tenant=tenant,
                    product=product,
                    variant=variant,
                    packaging_unit=unit_code,
                    conversion_factor=factor,
                    pkg_ctx=ctx,
                    cfg=cfg,
                )
                source = ctx["default_source"]
                policy_status = "RESTRICTED"
                blocked_units.append(unit_code)

            disp_en = lbl_en if factor == 1 else f"{lbl_en} ({factor}u)"
            disp_bn = lbl_bn if factor == 1 else f"{lbl_bn} ({factor}টি)"
            options.append(
                {
                    "packaging_unit": unit_code,
                    "label_en": disp_en,
                    "label_bn": disp_bn,
                    "conversion_factor": factor,
                    "unit_price_bdt": f"{unit_price:.2f}",
                    "conversion_source": source,
                    "container_policy_status": policy_status,
                    "is_allowed": is_allowed,
                    "requires_pharmacist_approval": req_pharm,
                    "block_error_code": block_code,
                    "block_reason_en": block_en,
                    "block_reason_bn": block_bn,
                }
            )

        return options, blocked_units

    @classmethod
    def get_unit_options_response(
        cls,
        *,
        actor_user: Any,
        branch_id: Optional[str] = None,
        product_id: Optional[str] = None,
        variant_id: Optional[str] = None,
        sale: Optional[PosSaleOrder] = None,
        line_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from apps.core.pos_domain_service import PosDomainService

        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            actor_user,
            branch_id=sale.branch_id if sale else branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        if not branch:
            raise ValidationError({"branch": "Target branch is required to inspect POS packaging unit options."})

        target_line: Optional[PosSaleOrderLine] = None
        if sale and line_id:
            target_line = sale.lines.select_related("product", "variant").filter(line_id=line_id).first()
            if not target_line:
                raise ValidationError({"line_id": f"Line '{line_id}' not found in sale '{sale.sale_id}'."})
            product = target_line.product
            variant = target_line.variant
        elif sale and not product_id:
            target_line = sale.lines.select_related("product", "variant").order_by("line_number").first()
            if not target_line:
                raise ValidationError({"sale": "Sale has no cart lines and no product_id was specified."})
            product = target_line.product
            variant = target_line.variant
        else:
            if not product_id:
                raise ValidationError({"product_id": "product_id or line_id is required."})
            product = MedicineProduct.objects.filter(product_id=str(product_id).strip()).first()
            if not product:
                raise ValidationError({"product_id": f"Product '{product_id}' was not found."})
            variant = None
            if variant_id:
                variant = MedicineVariant.objects.filter(
                    variant_id=str(variant_id).strip(), product=product, is_active=True
                ).first()

        ctx = cls.resolve_packaging_context(tenant, branch, product, variant=variant)
        options, blocked_units = cls.build_packaging_options(
            tenant=tenant,
            branch=branch,
            product=product,
            variant=variant,
            actor_user=actor_user,
            pkg_ctx=ctx,
        )

        # Compute sellable FEFO base units in branch
        today = timezone.now().date()
        avail_units = sum(
            b.available_quantity_base_units
            for b in BatchStock.objects.filter(
                tenant=tenant,
                branch=branch,
                product=product,
                status=BatchStatus.ACTIVE,
                is_active=True,
                is_quarantined=False,
                recall_notice__isnull=True,
                expiry_date__gt=today,
            )
        )

        return {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "sale_id": sale.sale_id if sale else None,
            "line_id": target_line.line_id if target_line else None,
            "current_packaging_unit": target_line.packaging_unit if target_line else None,
            "current_conversion_factor": int(target_line.conversion_factor) if target_line else None,
            "current_quantity_units": int(target_line.quantity_units) if target_line else None,
            "current_base_units_total": int(target_line.base_units_total) if target_line else None,
            "product_id": product.product_id,
            "variant_id": variant.variant_id if variant else None,
            "brand_name": product.brand_name,
            "dosage_form": product.dosage_form or "",
            "normalized_dosage_form": ctx["normalized_form"],
            "primary_base_unit": ctx["primary_base_unit"],
            "available_base_units": avail_units,
            "conversion_source": ctx["default_source"],
            "non_divisible_sealed_only": bool(
                ctx["non_divisible_policy"] and not ctx["non_divisible_policy"].is_splitting_allowed
            ),
            "blocked_units": blocked_units,
            "packaging_options": options,
        }

    @classmethod
    def _record_unit_audit(cls, **kwargs: Any) -> PosCartUnitSelectionAudit:
        return PosCartUnitSelectionAudit.objects.create(**kwargs)

    @classmethod
    def select_cart_line_unit(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> Tuple[PosSaleOrder, PosCartUnitSelectionAudit]:
        """
        Atomically switches or updates a cart line's packaging unit, converts to exact integer base units,
        enforces container/dosing/loose/pharmacist-approval guardrails and branch FEFO sellable stock,
        releases stale allocations, recalculates Decimal totals, and logs PosCartUnitSelectionAudit.
        """
        from apps.core.pos_domain_service import PosDomainService
        from apps.core.pos_search_cart_service import PosSearchCartService

        started = time.perf_counter()
        raw_mode = str(data.get("conversion_mode") or "KEEP_QUANTITY").strip().upper()
        if raw_mode not in ("KEEP_QUANTITY", "CONVERT_PRESERVE_BASE"):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_CONVERSION_MODE",
                    "conversion_mode": "conversion_mode must be 'KEEP_QUANTITY' or 'CONVERT_PRESERVE_BASE'.",
                }
            )

        actor_role = str(getattr(user, "role", "") or "").upper()
        override_reason = str(data.get("override_reason") or "").strip()[:255]

        try:
            with transaction.atomic():
                locked_sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
                tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
                    user,
                    branch_id=locked_sale.branch_id,
                    require_write=True,
                    allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES,
                )
                if not branch:
                    raise ValidationError({"branch": "Sale order branch is invalid."})

                can_mutate_unpaid_confirmed = (
                    locked_sale.lifecycle_state == PosSaleLifecycleState.CONFIRMED
                    and _q2(locked_sale.paid_amount_bdt) <= Decimal("0.00")
                )
                if locked_sale.lifecycle_state not in PosDomainService.MUTABLE_SALE_STATES and not can_mutate_unpaid_confirmed:
                    raise ValidationError(
                        {
                            "error_code": "ERR_IMMUTABLE_SALE_STATE",
                            "detail": (
                                f"Cannot change cart units on sale '{locked_sale.order_number}' "
                                f"in state '{locked_sale.lifecycle_state}'."
                            ),
                        }
                    )

                expected_ver = data.get("expected_version")
                if expected_ver is not None and int(expected_ver) != int(locked_sale.version):
                    raise _UnitSelectionRejectionAbort(
                        audit_kwargs={
                            "tenant": tenant,
                            "branch": branch,
                            "sale": locked_sale,
                            "to_unit": str(data.get("packaging_unit") or PosPackagingUnit.TABLET).upper()[:24],
                            "quantity_units": 1,
                            "base_units_before": 0,
                            "base_units_after": 1,
                            "was_rejected": True,
                            "rejection_code": "ERR_CART_VERSION_CONFLICT",
                            "rejection_message": f"Expected cart version {expected_ver}, actual {locked_sale.version}.",
                            "actor_user": user,
                            "actor_role": actor_role,
                        },
                        exc=ValidationError(
                            {
                                "error_code": "ERR_CART_VERSION_CONFLICT",
                                "expected_version": expected_ver,
                                "current_version": locked_sale.version,
                                "detail": "Cart was updated concurrently; refresh cart before selecting unit.",
                            }
                        ),
                    )

                line_id = str(data.get("line_id") or "").strip()
                line_num = data.get("line_number")
                lines = list(locked_sale.lines.select_related("product", "variant").order_by("line_number"))
                target_line: Optional[PosSaleOrderLine] = None
                if line_id:
                    target_line = next((l for l in lines if l.line_id == line_id), None)
                elif line_num is not None:
                    target_line = next((l for l in lines if l.line_number == int(line_num)), None)
                if not target_line:
                    raise ValidationError({"line_id": "Specified cart line was not found in this sale order."})

                prod = target_line.product
                var = target_line.variant
                from_unit = target_line.packaging_unit
                from_factor = int(target_line.conversion_factor)
                base_before = int(target_line.base_units_total)
                price_before = _q2(target_line.unit_price_bdt)

                requested_unit = str(data.get("packaging_unit") or "").strip().upper()
                if not requested_unit:
                    raise ValidationError({"packaging_unit": "Target packaging_unit is required."})

                requested_factor_raw = data.get("conversion_factor")

                def _abort(exc: APIException, *, err_code: str, err_msg: str, to_f: int = 1, qty: int = 1, base_after: int = 1) -> None:
                    raise _UnitSelectionRejectionAbort(
                        audit_kwargs={
                            "tenant": tenant,
                            "branch": branch,
                            "sale": locked_sale,
                            "sale_line": target_line,
                            "product": prod,
                            "variant": var,
                            "from_unit": from_unit,
                            "to_unit": requested_unit if requested_unit in PosPackagingUnit.values else from_unit,
                            "from_conversion_factor": from_factor,
                            "to_conversion_factor": max(1, to_f),
                            "quantity_units": max(1, qty),
                            "base_units_before": base_before,
                            "base_units_after": max(1, base_after),
                            "unit_price_before_bdt": price_before,
                            "unit_price_after_bdt": price_before,
                            "conversion_source": "REJECTED",
                            "container_policy_status": "REJECTED",
                            "was_rejected": True,
                            "rejection_code": err_code[:64],
                            "rejection_message": err_msg[:255],
                            "actor_user": user,
                            "actor_role": actor_role,
                            "override_reason": override_reason,
                        },
                        exc=exc,
                    )

                # First resolve & check unit/factor without final quantity to learn to_factor cleanly
                try:
                    pre_eval = cls.evaluate_unit_selection(
                        tenant=tenant,
                        branch=branch,
                        product=prod,
                        variant=var,
                        packaging_unit=requested_unit,
                        requested_conversion_factor=requested_factor_raw,
                        quantity_units=1,
                        actor_user=user,
                        override_reason=override_reason,
                        enforce_loose_quantity_bounds=False,
                    )
                except (ValidationError, PermissionDenied) as policy_exc:
                    det = policy_exc.detail if isinstance(policy_exc.detail, dict) else {"detail": str(policy_exc.detail)}
                    code = str(det.get("error_code") or "ERR_UNIT_SELECTION_REJECTED")
                    _abort(policy_exc, err_code=code, err_msg=str(det))

                to_factor = int(pre_eval["conversion_factor"])

                # Compute new integer quantity_units strictly
                if "quantity_units" in data and data["quantity_units"] is not None:
                    try:
                        new_qty = cls.parse_strict_positive_int(data["quantity_units"], "quantity_units")
                    except ValidationError as q_exc:
                        det = q_exc.detail if isinstance(q_exc.detail, dict) else {"detail": str(q_exc.detail)}
                        _abort(
                            q_exc,
                            err_code=str(det.get("error_code") or "ERR_FRACTIONAL_UNIT_PROHIBITED"),
                            err_msg=str(det),
                            to_f=to_factor,
                        )
                elif raw_mode == "CONVERT_PRESERVE_BASE":
                    if base_before % to_factor != 0:
                        msg = (
                            f"Cannot convert {base_before} base units cleanly into whole integer '{requested_unit}' "
                            f"(factor {to_factor}); fractional packs are prohibited."
                        )
                        _abort(
                            ValidationError(
                                {
                                    "error_code": "ERR_INEXACT_BASE_UNIT_CONVERSION",
                                    "packaging_unit": msg,
                                    "message_en": msg,
                                    "message_bn": f"{base_before}টি বেস ইউনিটকে পূর্ণ {requested_unit} ({to_factor}টি)-এ রূপান্তর করা যাচ্ছে না।",
                                }
                            ),
                            err_code="ERR_INEXACT_BASE_UNIT_CONVERSION",
                            err_msg=msg,
                            to_f=to_factor,
                            qty=target_line.quantity_units,
                            base_after=base_before,
                        )
                    new_qty = base_before // to_factor
                else:
                    new_qty = int(target_line.quantity_units)

                new_base_total = new_qty * to_factor

                # Re-run full evaluation WITH actual quantity_units (checks loose tablet min/max bounds!)
                try:
                    evaluated = cls.evaluate_unit_selection(
                        tenant=tenant,
                        branch=branch,
                        product=prod,
                        variant=var,
                        packaging_unit=requested_unit,
                        requested_conversion_factor=requested_factor_raw,
                        quantity_units=new_qty,
                        actor_user=user,
                        override_reason=override_reason,
                        pkg_ctx=pre_eval["pkg_ctx"],
                    )
                except (ValidationError, PermissionDenied) as policy_exc:
                    det = policy_exc.detail if isinstance(policy_exc.detail, dict) else {"detail": str(policy_exc.detail)}
                    code = str(det.get("error_code") or "ERR_UNIT_SELECTION_REJECTED")
                    _abort(
                        policy_exc,
                        err_code=code,
                        err_msg=str(det),
                        to_f=to_factor,
                        qty=new_qty,
                        base_after=new_base_total,
                    )

                # Validate FEFO sellable stock in branch (excluding expired, quarantined, recalled)
                other_base = sum(
                    int(l.base_units_total)
                    for l in lines
                    if l.product_id == prod.product_id and l.line_id != target_line.line_id
                )
                total_prod_base_needed = other_base + new_base_total

                today = timezone.now().date()
                locked_batches = list(
                    BatchStock.objects.select_for_update()
                    .filter(tenant=tenant, branch=branch, product=prod)
                    .order_by("expiry_date", "batch_id")
                )
                avail_total = sum(
                    b.available_quantity_base_units
                    for b in locked_batches
                    if b.status == BatchStatus.ACTIVE
                    and b.is_active
                    and not b.is_quarantined
                    and b.recall_notice_id is None
                    and b.expiry_date > today
                    and b.available_quantity_base_units > 0
                )
                if avail_total <= 0:
                    msg = f"No sellable FEFO stock available for '{prod.brand_name}' in branch '{branch.name}'."
                    _abort(
                        ValidationError({"error_code": "ERR_EXPIRED_OR_UNSELLABLE_STOCK", "stock": msg}),
                        err_code="ERR_EXPIRED_OR_UNSELLABLE_STOCK",
                        err_msg=msg,
                        to_f=to_factor,
                        qty=new_qty,
                        base_after=new_base_total,
                    )
                if total_prod_base_needed > avail_total:
                    msg = (
                        f"Requested {total_prod_base_needed} base units ({new_qty} {requested_unit} x {to_factor}) "
                        f"of '{prod.brand_name}', but only {avail_total} eligible base units exist in branch '{branch.name}'."
                    )
                    _abort(
                        ValidationError(
                            {
                                "error_code": "ERR_INSUFFICIENT_SELLABLE_STOCK",
                                "quantity_units": msg,
                                "message_en": msg,
                                "message_bn": f"পর্যাপ্ত বিক্রয়যোগ্য স্টক নেই (চাহিদা: {total_prod_base_needed}, মজুদ: {avail_total})।",
                            }
                        ),
                        err_code="ERR_INSUFFICIENT_SELLABLE_STOCK",
                        err_msg=msg,
                        to_f=to_factor,
                        qty=new_qty,
                        base_after=new_base_total,
                    )

                # Release any prior FEFO reservations cleanly & revert unpaid CONFIRMED to DRAFT so allocation must be re-confirmed
                prior_state = locked_sale.lifecycle_state
                PosDomainService._release_reserved_allocations(locked_sale)

                new_unit_price = _q2(evaluated["unit_price_bdt"])
                target_line.packaging_unit = evaluated["packaging_unit"]
                target_line.conversion_factor = to_factor
                target_line.quantity_units = new_qty
                target_line.base_units_total = new_base_total
                target_line.unit_price_bdt = new_unit_price
                target_line.dosage_form_snapshot = evaluated["dosage_form_snapshot"]
                target_line.container_policy_applied = evaluated["container_policy_applied"]
                target_line.unit_conversion_source = evaluated["conversion_source"]
                if evaluated["override_approved_by"] is not None:
                    target_line.container_override_approved_by = evaluated["override_approved_by"]
                    target_line.container_override_reason = evaluated["override_reason"]
                target_line.save()

                PosDomainService.compute_line_and_header_totals(locked_sale, user, allow_empty=True)
                locked_sale.refresh_from_db()
                from apps.core.pos_rx_safety_service import PosRxSafetyService

                PosRxSafetyService.evaluate_and_sync_sale_rx_gate(
                    locked_sale, actor_user=user, mutate=True, reason="Cart packaging/base quantity updated"
                )
                locked_sale.refresh_from_db()
                if prior_state == PosSaleLifecycleState.CONFIRMED:
                    locked_sale.lifecycle_state = PosSaleLifecycleState.DRAFT
                locked_sale.version += 1
                locked_sale.updated_by = user
                locked_sale.save(update_fields=["lifecycle_state", "version", "updated_by", "updated_at"])

                audit_row = cls._record_unit_audit(
                    tenant=tenant,
                    branch=branch,
                    sale=locked_sale,
                    sale_line=target_line,
                    product=prod,
                    variant=var,
                    from_unit=from_unit,
                    to_unit=evaluated["packaging_unit"],
                    from_conversion_factor=from_factor,
                    to_conversion_factor=to_factor,
                    quantity_units=new_qty,
                    base_units_before=base_before,
                    base_units_after=new_base_total,
                    unit_price_before_bdt=price_before,
                    unit_price_after_bdt=new_unit_price,
                    conversion_source=evaluated["conversion_source"],
                    container_policy_status=evaluated["container_policy_applied"],
                    was_rejected=False,
                    actor_user=user,
                    actor_role=actor_role,
                    override_approved_by=evaluated["override_approved_by"],
                    override_reason=evaluated["override_reason"],
                )

                elapsed = round((time.perf_counter() - started) * 1000, 2)
                PosCartScanEvent.objects.create(
                    tenant=tenant,
                    branch=branch,
                    sale=locked_sale,
                    cashier_user=user,
                    raw_query=target_line.line_id,
                    resolution_source=PosScanResolutionSource.BRAND_GENERIC_SEARCH,
                    matched_product=prod,
                    matched_variant=var,
                    packaging_unit=evaluated["packaging_unit"],
                    conversion_factor=to_factor,
                    quantity_delta=new_qty,
                    latency_ms=Decimal(str(elapsed)),
                    used_snapshot_cache=True,
                    was_rejected=False,
                )

                PosDomainService._record_lifecycle_audit(
                    sale=locked_sale,
                    from_state=prior_state,
                    to_state=locked_sale.lifecycle_state,
                    action="CART_LINE_UNIT_SELECTED",
                    user=user,
                    reason=(
                        f"Selected {evaluated['packaging_unit']} (x{to_factor}) -> "
                        f"{new_qty}u = {new_base_total} base units for {prod.brand_name}"
                    ),
                    metadata={
                        "unit_audit_id": audit_row.unit_audit_id,
                        "line_id": target_line.line_id,
                        "from_unit": from_unit,
                        "to_unit": evaluated["packaging_unit"],
                        "base_units_before": base_before,
                        "base_units_after": new_base_total,
                        "container_policy": evaluated["container_policy_applied"],
                    },
                )

                PosSearchCartService.refresh_branch_sku_read_model(
                    user, branch_id=branch.branch_id, product_ids=[prod.product_id]
                )

                refreshed_sale = (
                    PosSaleOrder.objects.filter(pk=locked_sale.pk)
                    .prefetch_related("lines__product", "lines__variant", "allocations", "payment_attempts", "lifecycle_audits")
                    .get()
                )
                return refreshed_sale, audit_row

        except _UnitSelectionRejectionAbort as abort:
            cls._record_unit_audit(**abort.audit_kwargs)
            raise abort.exc
