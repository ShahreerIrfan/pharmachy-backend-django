"""
backend/apps/core/inventory_base_unit_service.py
Architecture Decision: ADR-0161 (P161).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service layer for Inventory Base-Unit Strategy, Multi-Tier Packaging
Conversions, Indivisible Unit Rule Enforcement, and Precise Valuation.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStock,
    Branch,
    InventoryBaseUnitStrategy,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.inventory_base_unit_service")


class InventoryBaseUnitService:
    """Coordinates inventory base unit configurations, conversions, and indivisibility rules."""

    @classmethod
    def create_or_update_strategy(
        cls,
        user: PharmacyUser,
        data: Dict[str, Any],
        strategy_id: Optional[str] = None,
    ) -> InventoryBaseUnitStrategy:
        """
        Creates or updates a base unit strategy for a product within tenant scope.
        Requires non-cashier authorized role.
        """
        # 1. RBAC Check
        if getattr(user, "role", None) in [UserRole.CASHIER]:
            raise PermissionDenied({
                "error_code": "ERR_ROLE_UNAUTHORIZED",
                "detail": "Cashier role is not authorized to create or modify base unit strategies.",
            })

        # 2. Tenant lifecycle check
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({
                "error_code": "ERR_TENANT_REQUIRED",
                "detail": "Active tenant context is required.",
            })

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant or not tenant.is_active or tenant.status in [
            TenantLifecycleStatus.SUSPENDED,
            TenantLifecycleStatus.ARCHIVED,
        ]:
            raise PermissionDenied({
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Suspended or inactive tenant cannot configure inventory strategies.",
            })

        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                raise NotFound({
                    "error_code": "ERR_BRANCH_NOT_FOUND",
                    "detail": f"Branch '{branch_id}' not found for tenant '{tenant_id}'.",
                })

        product_id = data.get("product_id")
        if not product_id:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_REQUIRED",
                "detail": "Field 'product_id' is required.",
            })

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Medicine product '{product_id}' was not found.",
            })

        # Multiplier Validation
        p_to_i = int(data.get("pack_to_intermediate_multiplier", 1))
        i_to_b = int(data.get("intermediate_to_base_multiplier", 1))
        if p_to_i < 1 or i_to_b < 1:
            raise ValidationError({
                "error_code": "ERR_INVALID_UNIT_MULTIPLIER",
                "detail": "Multipliers must be strictly positive integers >= 1.",
            })

        declared_total = data.get("total_base_units_per_pack")
        expected_total = p_to_i * i_to_b
        if declared_total is not None and int(declared_total) != expected_total:
            raise ValidationError({
                "error_code": "ERR_INVALID_UNIT_MULTIPLIER",
                "detail": f"total_base_units_per_pack ({declared_total}) does not match pack ({p_to_i}) * strip ({i_to_b}) = {expected_total}.",
            })

        # Financial validation
        try:
            cost = Decimal(str(data.get("cost_per_base_unit", "0.0000")))
            mrp = Decimal(str(data.get("mrp_per_base_unit", "0.0000")))
        except Exception:
            raise ValidationError({
                "error_code": "ERR_INVALID_DECIMAL",
                "detail": "cost_per_base_unit and mrp_per_base_unit must be valid decimal numbers.",
            })

        if cost < Decimal("0.0000"):
            raise ValidationError({
                "error_code": "ERR_NEGATIVE_COST",
                "detail": "Cost per base unit cannot be negative.",
            })

        if mrp < cost:
            raise ValidationError({
                "error_code": "ERR_MRP_BELOW_COST",
                "detail": f"MRP per base unit ({mrp}) cannot be lower than cost ({cost}).",
            })

        # Indivisibility check
        is_indivisible = bool(data.get("is_indivisible", True))
        allow_decimal = bool(data.get("allow_decimal_quantities", False))
        decimal_places = int(data.get("decimal_places_allowed", 0))
        min_dispense = Decimal(str(data.get("minimum_dispense_quantity", "1.0000")))

        if is_indivisible:
            if allow_decimal:
                raise ValidationError({
                    "error_code": "ERR_INDIVISIBLE_DECIMAL_CONFLICT",
                    "detail": "Indivisible items cannot allow decimal quantities.",
                })
            if decimal_places != 0:
                raise ValidationError({
                    "error_code": "ERR_INDIVISIBLE_DECIMAL_CONFLICT",
                    "detail": "Indivisible items must have decimal_places_allowed set to 0.",
                })
            if min_dispense % Decimal("1") != Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INDIVISIBLE_FRACTION",
                    "detail": "Minimum dispense quantity for indivisible items must be a whole integer.",
                })

        with transaction.atomic():
            if strategy_id:
                strategy = InventoryBaseUnitStrategy.objects.filter(
                    strategy_id=strategy_id, tenant=tenant
                ).first()
                if not strategy:
                    raise NotFound({
                        "error_code": "ERR_STRATEGY_NOT_FOUND",
                        "detail": f"Strategy '{strategy_id}' not found for tenant '{tenant_id}'.",
                    })
            else:
                strategy = InventoryBaseUnitStrategy.objects.filter(
                    tenant=tenant, branch=branch, product=product
                ).first()
                if not strategy:
                    strategy = InventoryBaseUnitStrategy(
                        strategy_id=f"ibus-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        branch=branch,
                        product=product,
                    )

            strategy.base_unit = data.get("base_unit", strategy.base_unit)
            strategy.is_indivisible = is_indivisible
            strategy.allow_decimal_quantities = allow_decimal
            strategy.decimal_places_allowed = decimal_places
            strategy.minimum_dispense_quantity = min_dispense
            strategy.pack_unit_label = data.get("pack_unit_label", strategy.pack_unit_label)
            strategy.intermediate_unit_label = data.get("intermediate_unit_label", strategy.intermediate_unit_label)
            strategy.pack_to_intermediate_multiplier = p_to_i
            strategy.intermediate_to_base_multiplier = i_to_b
            strategy.total_base_units_per_pack = expected_total
            strategy.cost_per_base_unit = cost
            strategy.mrp_per_base_unit = mrp
            strategy.notes = data.get("notes", strategy.notes)
            strategy.is_active = data.get("is_active", strategy.is_active)
            strategy.save()

            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                user_id=str(user.pk),
                actor_role=getattr(user, "role", "UNKNOWN"),
                action="CONFIGURE_INVENTORY_BASE_UNIT_STRATEGY",
                resource_type="InventoryBaseUnitStrategy",
                resource_id=strategy.strategy_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "product_id": product.product_id,
                    "base_unit": strategy.base_unit,
                    "is_indivisible": strategy.is_indivisible,
                    "total_base_units_per_pack": strategy.total_base_units_per_pack,
                },
            )

        return strategy

    @classmethod
    def convert_to_base_units(
        cls,
        strategy: InventoryBaseUnitStrategy,
        quantity: Decimal,
        unit_tier: str,
    ) -> Dict[str, Any]:
        """
        Converts quantity from specified tier ('PACK', 'INTERMEDIATE', 'BASE') into base units.
        Enforces indivisible integer checks and calculates exact Decimal valuation.
        """
        tier_upper = unit_tier.strip().upper()
        if tier_upper not in ["PACK", "INTERMEDIATE", "BASE"]:
            raise ValidationError({
                "error_code": "ERR_INVALID_UNIT_TIER",
                "detail": f"Unit tier '{unit_tier}' must be one of: PACK, INTERMEDIATE, BASE.",
            })

        if quantity <= Decimal("0"):
            raise ValidationError({
                "error_code": "ERR_INVALID_QUANTITY",
                "detail": "Quantity must be strictly positive.",
            })

        if tier_upper == "PACK":
            base_qty = quantity * Decimal(str(strategy.total_base_units_per_pack))
        elif tier_upper == "INTERMEDIATE":
            base_qty = quantity * Decimal(str(strategy.intermediate_to_base_multiplier))
        else:  # BASE
            base_qty = quantity

        if strategy.is_indivisible:
            if base_qty % Decimal("1") != Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INDIVISIBLE_UNIT_FRACTION",
                    "detail": (
                        f"Item '{strategy.product.brand_name}' has indivisible base unit '{strategy.base_unit}'. "
                        f"Converted quantity ({base_qty}) cannot have fractional pieces."
                    ),
                })
            final_base_qty = int(base_qty)
        else:
            final_base_qty = base_qty

        cost_val = Decimal(str(final_base_qty)) * strategy.cost_per_base_unit
        mrp_val = Decimal(str(final_base_qty)) * strategy.mrp_per_base_unit

        return {
            "strategy_id": strategy.strategy_id,
            "product_id": strategy.product.product_id,
            "brand_name": strategy.product.brand_name,
            "base_unit": strategy.base_unit,
            "is_indivisible": strategy.is_indivisible,
            "input_tier": tier_upper,
            "input_quantity": str(quantity),
            "base_unit_quantity": final_base_qty,
            "total_base_units_per_pack": strategy.total_base_units_per_pack,
            "intermediate_to_base_multiplier": strategy.intermediate_to_base_multiplier,
            "cost_valuation_bdt": f"{cost_val:.4f}",
            "mrp_valuation_bdt": f"{mrp_val:.4f}",
        }

    @classmethod
    def decompose_base_units(
        cls,
        strategy: InventoryBaseUnitStrategy,
        total_base_units: int,
    ) -> Dict[str, Any]:
        """
        Reverses / unpacks base integer units back into hierarchy: Packs, Strips, and Loose Pieces.
        """
        if total_base_units < 0:
            raise ValidationError({
                "error_code": "ERR_NEGATIVE_QUANTITY",
                "detail": "Base units count cannot be negative.",
            })

        pack_factor = strategy.total_base_units_per_pack
        strip_factor = strategy.intermediate_to_base_multiplier

        packs = total_base_units // pack_factor
        remainder_after_packs = total_base_units % pack_factor

        strips = remainder_after_packs // strip_factor
        loose_base = remainder_after_packs % strip_factor

        return {
            "total_base_units": total_base_units,
            "base_unit": strategy.base_unit,
            "packs": packs,
            "pack_label": strategy.pack_unit_label,
            "intermediate": strips,
            "intermediate_label": strategy.intermediate_unit_label,
            "loose_base_units": loose_base,
            "exact_recon_formula": f"{packs} * {pack_factor} + {strips} * {strip_factor} + {loose_base} = {total_base_units}",
        }

    @classmethod
    def validate_dispense_quantity(
        cls,
        strategy: InventoryBaseUnitStrategy,
        quantity: Decimal,
    ) -> Dict[str, Any]:
        """Validates if a requested dispensing or checkout quantity satisfies indivisibility rules."""
        if quantity < strategy.minimum_dispense_quantity:
            raise ValidationError({
                "error_code": "ERR_BELOW_MINIMUM_DISPENSE_QTY",
                "detail": f"Quantity ({quantity}) is below minimum allowed step ({strategy.minimum_dispense_quantity}).",
            })

        if strategy.is_indivisible:
            if quantity % Decimal("1") != Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INDIVISIBLE_UNIT_FRACTION",
                    "detail": f"Unit '{strategy.base_unit}' is indivisible and cannot be dispensed in fractions.",
                })
        else:
            # Check allowed decimal places
            places = strategy.decimal_places_allowed
            q_str = f"{quantity:f}"
            if "." in q_str:
                decimals = len(q_str.split(".")[1].rstrip("0"))
                if decimals > places:
                    raise ValidationError({
                        "error_code": "ERR_EXCEEDS_DECIMAL_PRECISION",
                        "detail": f"Quantity has {decimals} decimal places, exceeding maximum allowed {places}.",
                    })

        return {
            "strategy_id": strategy.strategy_id,
            "is_valid": True,
            "validated_quantity": str(quantity),
            "base_unit": strategy.base_unit,
            "is_indivisible": strategy.is_indivisible,
        }

    @classmethod
    def calculate_inventory_valuation(
        cls,
        tenant: Tenant,
        branch: Optional[Branch] = None,
        product_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Calculates total inventory valuation in BDT using exact Decimal base-unit precision."""
        batches = BatchStock.objects.filter(tenant=tenant)
        if branch:
            batches = batches.filter(branch=branch)
        if product_id:
            batches = batches.filter(product_id=product_id)

        total_base_units = 0
        total_cost_valuation = Decimal("0.0000")

        for b in batches:
            qty = b.quantity_base_units
            cost = b.cost_per_unit
            total_base_units += qty
            total_cost_valuation += Decimal(str(qty)) * cost

        return {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id if branch else "ALL",
            "batch_count": batches.count(),
            "total_base_units": total_base_units,
            "total_cost_valuation_bdt": f"{total_cost_valuation:.4f}",
        }

