"""
backend/apps/core/multi_level_unit_conversion_service.py
Architecture Decision: ADR-0163 (P163).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service layer for Multi-Level Packaging Unit Conversions, Fractional Tablet
Prevention, and Inappropriate Dosing-Unit Guardrails.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    InventoryBaseUnitStrategy,
    MedicineProduct,
    PackagingHierarchyNode,
    Tenant,
    TenantLifecycleStatus,
    UnitConversionPolicy,
)
from apps.core.packaging_hierarchy_service import PackagingHierarchyService
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.multi_level_unit_conversion_service")


class MultiLevelUnitConversionService:
    """Coordinates multi-level packaging conversions and enforces pharmaceutical unit guardrails."""

    DEFAULT_PROHIBITED_BY_FORM: Dict[str, List[str]] = {
        "TABLET": ["ML", "L", "PUFF", "ACTUATION", "DROP", "VIAL", "AMPOULE"],
        "CAPSULE": ["ML", "L", "PUFF", "ACTUATION", "DROP", "VIAL", "AMPOULE"],
        "SYRUP": ["TABLET", "CAPSULE", "STRIP", "BLISTER", "PUFF", "ACTUATION"],
        "SUSPENSION": ["TABLET", "CAPSULE", "STRIP", "BLISTER", "PUFF", "ACTUATION"],
        "INHALER": ["TABLET", "CAPSULE", "STRIP", "BLISTER", "DROP", "VIAL", "AMPOULE"],
        "INJECTION": ["TABLET", "CAPSULE", "STRIP", "BLISTER", "PUFF", "ACTUATION"],
        "DROPS": ["TABLET", "CAPSULE", "STRIP", "BLISTER", "PUFF", "ACTUATION"],
        "TOPICAL": ["TABLET", "CAPSULE", "STRIP", "PUFF", "ACTUATION", "VIAL"],
        "SACHET": ["TABLET", "PUFF", "ACTUATION", "VIAL", "AMPOULE"],
        "DEVICE": ["TABLET", "CAPSULE", "STRIP", "ML", "PUFF", "DROP"],
    }

    @classmethod
    def create_or_update_policy(
        cls,
        user: PharmacyUser,
        data: Dict[str, Any],
        policy_id: Optional[str] = None,
    ) -> UnitConversionPolicy:
        """Creates or updates a product's unit conversion and tablet-splitting policy."""
        # 1. RBAC check
        if getattr(user, "role", None) in [UserRole.CASHIER]:
            raise PermissionDenied({
                "error_code": "ERR_ROLE_UNAUTHORIZED",
                "detail": "Cashier role is not authorized to configure unit conversion policies.",
            })

        # 2. Tenant check
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
                "detail": "Suspended or inactive tenant cannot configure unit conversion policies.",
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

        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()

        is_splittable = bool(data.get("is_splittable", False))
        raw_fractions = data.get("allowed_split_fractions")
        if raw_fractions is None:
            allowed_fractions = [0.5, 1.0] if is_splittable else [1.0]
        else:
            allowed_fractions = [float(f) for f in raw_fractions]

        if not is_splittable and allowed_fractions != [1.0]:
            raise ValidationError({
                "error_code": "ERR_FRACTIONAL_TABLET_NOT_ALLOWED",
                "detail": "Non-splittable tablets or unsplit products cannot permit fractional splits.",
            })

        prohibited = data.get("prohibited_dosing_units")
        if prohibited is None:
            form_upper = product.dosage_form.strip().upper() if product.dosage_form else "TABLET"
            prohibited = cls.DEFAULT_PROHIBITED_BY_FORM.get(form_upper, ["PUFF", "ACTUATION"])

        ratio = Decimal(str(data.get("dosing_conversion_ratio", "1.0000")))
        if ratio <= Decimal("0.0000"):
            raise ValidationError({
                "error_code": "ERR_INVALID_RATIO",
                "detail": "dosing_conversion_ratio must be strictly positive.",
            })

        with transaction.atomic():
            if policy_id:
                policy = UnitConversionPolicy.objects.filter(policy_id=policy_id, tenant=tenant).first()
                if not policy:
                    raise NotFound({
                        "error_code": "ERR_POLICY_NOT_FOUND",
                        "detail": f"Unit conversion policy '{policy_id}' was not found.",
                    })
            else:
                policy = UnitConversionPolicy.objects.filter(
                    tenant=tenant, branch=branch, product=product
                ).first()
                if not policy:
                    policy = UnitConversionPolicy(
                        policy_id=f"ucp-{uuid.uuid4().hex[:12]}",
                        tenant=tenant,
                        branch=branch,
                        product=product,
                    )

            policy.is_splittable = is_splittable
            policy.allowed_split_fractions = allowed_fractions
            policy.clinical_dosing_unit = str(data.get("clinical_dosing_unit", policy.clinical_dosing_unit or "MG")).strip().upper()
            policy.dosing_conversion_ratio = ratio
            policy.prohibited_dosing_units = [str(u).strip().upper() for u in prohibited]
            policy.notes = data.get("notes", policy.notes)
            policy.is_active = bool(data.get("is_active", policy.is_active))
            policy.save()

            AuditLogEntry.objects.create(
                entry_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                user_id=str(user.pk),
                actor_role=getattr(user, "role", "UNKNOWN"),
                action="CONFIGURE_UNIT_CONVERSION_POLICY",
                resource_type="UnitConversionPolicy",
                resource_id=policy.policy_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "product_id": product.product_id,
                    "is_splittable": policy.is_splittable,
                    "allowed_split_fractions": policy.allowed_split_fractions,
                    "prohibited_units": policy.prohibited_dosing_units,
                },
            )

        return policy

    @classmethod
    def get_allowed_units(cls, product_id: str, tenant_id: str) -> Dict[str, Any]:
        """Provides the comprehensive list of permitted packaging, base, and prohibited units for a SKU."""
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Medicine product '{product_id}' was not found.",
            })

        policy = UnitConversionPolicy.objects.filter(tenant_id=tenant_id, product=product, is_active=True).first()
        is_splittable = policy.is_splittable if policy else False
        allowed_fractions = policy.allowed_split_fractions if policy else [1.0]
        clinical_unit = policy.clinical_dosing_unit if policy else "MG"

        form_upper = product.dosage_form.strip().upper() if product.dosage_form else "TABLET"
        prohibited = policy.prohibited_dosing_units if policy else cls.DEFAULT_PROHIBITED_BY_FORM.get(form_upper, ["PUFF"])

        # Fetch packaging units from PackagingHierarchyNode
        nodes = PackagingHierarchyNode.objects.filter(tenant_id=tenant_id, product=product, is_active=True)
        packaging_units = set()
        base_unit = None

        for n in nodes:
            packaging_units.add(n.parent_unit.upper())
            packaging_units.add(n.child_unit.upper())
            if n.is_base_level:
                base_unit = n.child_unit.upper()

        if not base_unit:
            strat = InventoryBaseUnitStrategy.objects.filter(tenant_id=tenant_id, product=product).first()
            if strat:
                base_unit = strat.base_unit
                packaging_units.add(strat.pack_unit_label.upper())
                packaging_units.add(strat.intermediate_unit_label.upper())
                packaging_units.add(strat.base_unit.upper())
            else:
                base_unit = "TABLET" if form_upper in ["TABLET", "CAPSULE"] else "BOTTLE"
                packaging_units.add(base_unit)

        return {
            "product_id": product.product_id,
            "brand_name": product.brand_name,
            "dosage_form": product.dosage_form,
            "base_unit": base_unit,
            "is_splittable": is_splittable,
            "allowed_split_fractions": allowed_fractions,
            "packaging_units": sorted(list(packaging_units)),
            "clinical_dosing_unit": clinical_unit,
            "prohibited_units": sorted(list(set(prohibited))),
        }

    @classmethod
    def validate_dispense_unit_and_quantity(
        cls,
        product_id: str,
        tenant_id: str,
        unit: str,
        quantity: Decimal,
    ) -> Dict[str, Any]:
        """Pre-flight check: prevents fractional tablets and inappropriate dosing units."""
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{product_id}' not found."})

        if quantity <= Decimal("0"):
            raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Quantity must be strictly positive."})

        u_upper = unit.strip().upper()
        policy = UnitConversionPolicy.objects.filter(tenant_id=tenant_id, product=product, is_active=True).first()
        form_upper = product.dosage_form.strip().upper() if product.dosage_form else "TABLET"

        prohibited = policy.prohibited_dosing_units if policy else cls.DEFAULT_PROHIBITED_BY_FORM.get(form_upper, ["PUFF"])
        if u_upper in prohibited:
            raise ValidationError({
                "error_code": "ERR_INCOMPATIBLE_DOSING_UNIT",
                "detail": f"Unit '{u_upper}' is incompatible with dosage form '{product.dosage_form}'.",
            })

        is_splittable = policy.is_splittable if policy else False
        allowed_fractions = policy.allowed_split_fractions if policy else [1.0]

        # Fractional check: does quantity have fractional component?
        has_fraction = (quantity % Decimal("1")) != Decimal("0")
        if has_fraction:
            # If item is solid tablet or capsule
            if u_upper in ["TABLET", "CAPSULE", "PIECE"] or form_upper in ["TABLET", "CAPSULE"]:
                if not is_splittable:
                    raise ValidationError({
                        "error_code": "ERR_FRACTIONAL_TABLET_NOT_ALLOWED",
                        "detail": f"Product '{product.brand_name}' is not scored/splittable. Fractional dispensing is prohibited.",
                    })
                # Check if fractional portion matches allowed fractions
                frac_part = float(quantity % Decimal("1"))
                if not any(abs(frac_part - f) < 0.001 for f in allowed_fractions if f < 1.0):
                    raise ValidationError({
                        "error_code": "ERR_INVALID_TABLET_SPLIT",
                        "detail": (
                            f"Fractional step ({frac_part}) is not permitted. "
                            f"Allowed split fractions: {allowed_fractions}."
                        ),
                    })
            else:
                # Other non-divisible items (Ampoules, Vials, Inhalers, Bottles)
                if u_upper in ["VIAL", "AMPOULE", "BOTTLE", "CANISTER", "TUBE"]:
                    raise ValidationError({
                        "error_code": "ERR_INDIVISIBLE_UNIT_FRACTION",
                        "detail": f"Unit '{u_upper}' is an indivisible pack item and cannot have fractional amounts.",
                    })

        return {
            "product_id": product.product_id,
            "unit": u_upper,
            "quantity": str(quantity),
            "is_valid": True,
            "is_splittable": is_splittable,
        }

    @classmethod
    def convert_multi_level(
        cls,
        product_id: str,
        tenant_id: str,
        from_unit: str,
        to_unit: str,
        quantity: Decimal,
        as_of_date: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Converts quantity between packaging tiers while enforcing dosing-unit guardrails."""
        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{product_id}' not found."})

        from_u = from_unit.strip().upper()
        to_u = to_unit.strip().upper()

        if quantity <= Decimal("0"):
            raise ValidationError({"error_code": "ERR_INVALID_QUANTITY", "detail": "Quantity must be strictly positive."})

        # Pre-validate from_unit and quantity
        cls.validate_dispense_unit_and_quantity(product_id, tenant_id, from_u, quantity)

        # Check prohibited for to_unit
        policy = UnitConversionPolicy.objects.filter(tenant_id=tenant_id, product=product, is_active=True).first()
        form_upper = product.dosage_form.strip().upper() if product.dosage_form else "TABLET"
        prohibited = policy.prohibited_dosing_units if policy else cls.DEFAULT_PROHIBITED_BY_FORM.get(form_upper, ["PUFF"])

        if to_u in prohibited:
            raise ValidationError({
                "error_code": "ERR_INCOMPATIBLE_DOSING_UNIT",
                "detail": f"Target unit '{to_u}' is incompatible with dosage form '{product.dosage_form}'.",
            })

        # Case A: Clinical dosing unit calculation (e.g. Tablets -> MG active substance)
        clinical_unit = policy.clinical_dosing_unit if policy else "MG"
        ratio = policy.dosing_conversion_ratio if policy else Decimal("500.0000")

        if to_u == clinical_unit:
            # First resolve base units
            tree = PackagingHierarchyService.resolve_packaging_tree(product_id, tenant_id, as_of_date)
            base_mult = tree.get("cumulative_factors", {}).get(from_u, 1)
            base_units = quantity * Decimal(str(base_mult))
            total_mg = base_units * ratio

            return {
                "product_id": product_id,
                "from_unit": from_u,
                "to_unit": to_u,
                "input_quantity": str(quantity),
                "converted_quantity": str(total_mg),
                "is_clinical_dosing_calculation": True,
                "base_units_consumed": str(base_units),
                "active_substance_potency_per_unit": f"{ratio} {clinical_unit}",
            }

        # Case B: Standard packaging tree conversion (e.g. Carton -> Box -> Strip -> Tablet)
        try:
            conv_res = PackagingHierarchyService.convert_packaging_tier(
                product_id=product_id,
                tenant_id=tenant_id,
                from_unit=from_u,
                to_unit=to_u,
                quantity=quantity,
                as_of_date=as_of_date,
            )
        except ValidationError as exc:
            # Fallback to InventoryBaseUnitStrategy if PackagingHierarchyNode wasn't configured
            strat = InventoryBaseUnitStrategy.objects.filter(tenant_id=tenant_id, product=product).first()
            if not strat:
                raise exc

            # Calculate via strategy
            if from_u == strat.pack_unit_label.upper() and to_u == strat.base_unit.upper():
                conv_res = {
                    "product_id": product_id,
                    "as_of_date": timezone.now().date().isoformat(),
                    "from_unit": from_u,
                    "to_unit": to_u,
                    "input_quantity": str(quantity),
                    "converted_quantity": str(quantity * Decimal(str(strat.total_base_units_per_pack))),
                    "conversion_ratio": str(strat.total_base_units_per_pack),
                    "base_unit": strat.base_unit,
                }
            elif from_u == strat.intermediate_unit_label.upper() and to_u == strat.base_unit.upper():
                conv_res = {
                    "product_id": product_id,
                    "as_of_date": timezone.now().date().isoformat(),
                    "from_unit": from_u,
                    "to_unit": to_u,
                    "input_quantity": str(quantity),
                    "converted_quantity": str(quantity * Decimal(str(strat.intermediate_to_base_multiplier))),
                    "conversion_ratio": str(strat.intermediate_to_base_multiplier),
                    "base_unit": strat.base_unit,
                }
            else:
                raise exc

        converted_qty = Decimal(str(conv_res["converted_quantity"]))

        # Validate resulting target quantity for fractional tablets
        cls.validate_dispense_unit_and_quantity(product_id, tenant_id, to_u, converted_qty)

        # Financial valuation
        cost_unit = product.cost_unit or Decimal("0.0000")
        mrp_unit = product.mrp_unit or Decimal("0.0000")
        cost_val = converted_qty * cost_unit
        mrp_val = converted_qty * mrp_unit

        conv_res["cost_valuation_bdt"] = f"{cost_val:.4f}"
        conv_res["mrp_valuation_bdt"] = f"{mrp_val:.4f}"
        return conv_res

