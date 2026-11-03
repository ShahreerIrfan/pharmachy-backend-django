"""
backend/apps/core/conditional_pricing_service.py
Architecture Decision: ADR-0153 (P153).
Unit-Aware Multi-Tier Conditional Pricing Service.
Governs packs, strips, bottles, single pieces, and whole device price conversions with strict Decimal precision.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BaseDispensingUnit,
    ConditionalPricingConfig,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.conditional_pricing_service")


class ConditionalPricingService:
    """
    Core domain service managing the Conditional Pricing Form.
    Enforces unit-aware price conversions across packs, strips, and base pieces
    with zero-loss Decimal arithmetic and break-bulk safeguards.
    """

    ALLOWED_UNITS = [u.value for u in BaseDispensingUnit]

    @classmethod
    def _resolve_tenant(cls, user: PharmacyUser) -> Optional[Tenant]:
        tenant_id = getattr(user, "tenant_id", None)
        if tenant_id:
            return Tenant.objects.filter(tenant_id=tenant_id).first()
        return None

    @classmethod
    def check_permissions(cls, user: PharmacyUser, required_action: str = "read") -> None:
        """Enforces multi-tenant RBAC policies for pricing configurations."""
        if user.is_superuser:
            return

        if required_action in ["write", "delete"]:
            if getattr(user, "role", None) in [UserRole.CASHIER]:
                raise PermissionDenied({
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                    "detail": "Cashier role has read-only access to pricing catalog.",
                })

        tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if tenant:
            if not tenant.is_active or tenant.status in [
                TenantLifecycleStatus.SUSPENDED,
                TenantLifecycleStatus.ARCHIVED,
            ]:
                raise PermissionDenied({
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": f"Tenant '{tenant.name}' subscription is suspended or terminated.",
                })

    @classmethod
    def get_pricing_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and validation rules for unit-aware pricing."""
        return {
            "schema_version": "1.0",
            "editor_type": "CONDITIONAL_PRICING_FORM",
            "supported_base_units": [
                {"code": u.value, "label_en": u.label, "label_bn": cls._translate_unit(u.value)}
                for u in BaseDispensingUnit
            ],
            "max_multiplier": 500,
            "min_price_bdt": 0.0001,
            "max_price_bdt": 1000000.0,
            "default_vat_rate": 0.0,
        }

    @classmethod
    def calculate_conversions(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Calculates derived strip and pack prices from piece cost/MRP and packaging multipliers."""
        validated = cls.validate_pricing_payload(payload)
        return validated

    @classmethod
    def validate_pricing_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates unit pricing, checks dimensions and calculates mathematical parity.
        Strictly enforces positive Decimal arithmetic.
        """
        product_id = payload.get("product_id")
        if not product_id or not str(product_id).strip():
            raise ValidationError({
                "error_code": "ERR_MISSING_PRODUCT_ID",
                "detail": "product_id is mandatory.",
                "field": "product_id",
            })

        base_unit = payload.get("base_dispensing_unit", BaseDispensingUnit.PIECE)
        if base_unit not in cls.ALLOWED_UNITS:
            raise ValidationError({
                "error_code": "ERR_INVALID_DISPENSING_UNIT",
                "detail": f"Invalid base_dispensing_unit: '{base_unit}'. Allowed: {cls.ALLOWED_UNITS}",
                "field": "base_dispensing_unit",
            })

        # Packaging Multipliers
        try:
            pack_to_strip = int(payload.get("pack_to_strip_multiplier", 1))
            strip_to_piece = int(payload.get("strip_to_piece_multiplier", 1))
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_MULTIPLIER",
                "detail": "Packaging multipliers must be positive integers.",
                "field": "pack_to_strip_multiplier",
            })

        if pack_to_strip < 1 or strip_to_piece < 1:
            raise ValidationError({
                "error_code": "ERR_INVALID_MULTIPLIER",
                "detail": "Packaging multipliers must be at least 1.",
                "field": "pack_to_strip_multiplier",
            })

        expected_total = pack_to_strip * strip_to_piece
        total_units = payload.get("total_base_units_per_pack")
        if total_units is not None:
            try:
                total_int = int(total_units)
                if total_int != expected_total:
                    raise ValidationError({
                        "error_code": "ERR_DIMENSION_MISMATCH",
                        "detail": f"total_base_units_per_pack ({total_int}) must equal pack_to_strip ({pack_to_strip}) * strip_to_piece ({strip_to_piece}) = {expected_total}.",
                        "field": "total_base_units_per_pack",
                    })
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_DIMENSION_MISMATCH",
                    "detail": "total_base_units_per_pack must be an integer.",
                    "field": "total_base_units_per_pack",
                })
        else:
            total_int = expected_total

        # Prices (Piece)
        try:
            cost_piece = Decimal(str(payload.get("cost_per_piece_bdt", "0.00")))
            mrp_piece = Decimal(str(payload.get("mrp_per_piece_bdt", "0.00")))
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_PRICE",
                "detail": "cost_per_piece_bdt and mrp_per_piece_bdt must be valid numeric values.",
                "field": "mrp_per_piece_bdt",
            })

        if cost_piece <= Decimal("0"):
            raise ValidationError({
                "error_code": "ERR_INVALID_PRICE",
                "detail": "cost_per_piece_bdt must be greater than 0.",
                "field": "cost_per_piece_bdt",
            })

        if mrp_piece <= Decimal("0"):
            raise ValidationError({
                "error_code": "ERR_INVALID_PRICE",
                "detail": "mrp_per_piece_bdt must be greater than 0.",
                "field": "mrp_per_piece_bdt",
            })

        # Calculate or validate Strip prices
        strip_multiplier_dec = Decimal(str(strip_to_piece))
        calculated_strip_cost = (cost_piece * strip_multiplier_dec).quantize(Decimal("0.0001"))
        calculated_strip_mrp = (mrp_piece * strip_multiplier_dec).quantize(Decimal("0.0001"))

        strip_cost = Decimal(str(payload.get("strip_cost_bdt", calculated_strip_cost)))
        strip_mrp = Decimal(str(payload.get("strip_mrp_bdt", calculated_strip_mrp)))

        # Calculate or validate Pack prices
        total_multiplier_dec = Decimal(str(total_int))
        calculated_pack_cost = (cost_piece * total_multiplier_dec).quantize(Decimal("0.0001"))
        calculated_pack_mrp = (mrp_piece * total_multiplier_dec).quantize(Decimal("0.0001"))

        pack_cost = Decimal(str(payload.get("pack_cost_bdt", calculated_pack_cost)))
        pack_mrp = Decimal(str(payload.get("pack_mrp_bdt", calculated_pack_mrp)))

        # Break-bulk permissions
        allow_strip = bool(payload.get("allow_strip_sale", True))
        allow_piece = bool(payload.get("allow_piece_sale", True))

        if not allow_strip and not allow_piece and total_int > 1:
            raise ValidationError({
                "error_code": "ERR_INVALID_BREAK_BULK_POLICY",
                "detail": "Multi-unit pack must permit either strip sale or loose piece sale.",
                "field": "allow_piece_sale",
            })

        # VAT
        try:
            vat = Decimal(str(payload.get("vat_rate_percentage", "0.00")))
            if vat < Decimal("0") or vat > Decimal("100.00"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_VAT_RATE",
                    "detail": "vat_rate_percentage must be between 0.00 and 100.00.",
                    "field": "vat_rate_percentage",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_VAT_RATE",
                "detail": "vat_rate_percentage must be a valid decimal number.",
                "field": "vat_rate_percentage",
            })

        return {
            "product_id": str(product_id).strip(),
            "base_dispensing_unit": base_unit,
            "pack_to_strip_multiplier": pack_to_strip,
            "strip_to_piece_multiplier": strip_to_piece,
            "total_base_units_per_pack": total_int,
            "cost_per_piece_bdt": float(cost_piece),
            "mrp_per_piece_bdt": float(mrp_piece),
            "strip_cost_bdt": float(strip_cost),
            "strip_mrp_bdt": float(strip_mrp),
            "pack_cost_bdt": float(pack_cost),
            "pack_mrp_bdt": float(pack_mrp),
            "allow_strip_sale": allow_strip,
            "allow_piece_sale": allow_piece,
            "vat_rate_percentage": float(vat),
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    @transaction.atomic
    def create_or_update_pricing(
        cls,
        payload: Dict[str, Any],
        user: PharmacyUser,
        target_pricing_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates or updates a multi-tier conditional pricing configuration."""
        cls.check_permissions(user, required_action="write")
        tenant: Optional[Tenant] = cls._resolve_tenant(user)

        pricing: Optional[ConditionalPricingConfig] = None
        if target_pricing_id:
            try:
                pricing = ConditionalPricingConfig.objects.get(pk=target_pricing_id)
            except ConditionalPricingConfig.DoesNotExist:
                raise ValidationError({
                    "error_code": "ERR_PRICING_NOT_FOUND",
                    "detail": f"ConditionalPricingConfig '{target_pricing_id}' not found.",
                })
            user_tenant_id = getattr(user, "tenant_id", None)
            if pricing.tenant_id and user_tenant_id and str(pricing.tenant_id) != str(user_tenant_id) and not user.is_superuser:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                    "detail": "Cross-tenant modification prohibited.",
                })

        # Merge for partial update
        merged_payload = {}
        if pricing:
            merged_payload = {
                "product_id": str(pricing.product_id),
                "base_dispensing_unit": pricing.base_dispensing_unit,
                "pack_to_strip_multiplier": pricing.pack_to_strip_multiplier,
                "strip_to_piece_multiplier": pricing.strip_to_piece_multiplier,
                "total_base_units_per_pack": pricing.total_base_units_per_pack,
                "cost_per_piece_bdt": float(pricing.cost_per_piece_bdt),
                "mrp_per_piece_bdt": float(pricing.mrp_per_piece_bdt),
                "strip_cost_bdt": float(pricing.strip_cost_bdt),
                "strip_mrp_bdt": float(pricing.strip_mrp_bdt),
                "pack_cost_bdt": float(pricing.pack_cost_bdt),
                "pack_mrp_bdt": float(pricing.pack_mrp_bdt),
                "allow_strip_sale": pricing.allow_strip_sale,
                "allow_piece_sale": pricing.allow_piece_sale,
                "vat_rate_percentage": float(pricing.vat_rate_percentage),
                "is_active": pricing.is_active,
            }
        merged_payload.update(payload)
        validated = cls.validate_pricing_payload(merged_payload)

        product_id = validated["product_id"]
        try:
            product = MedicineProduct.objects.get(pk=product_id)
        except MedicineProduct.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Product '{product_id}' does not exist.",
            })

        # Check existing if not targeting specific ID
        created = False
        if not pricing:
            q = Q(product=product)
            if tenant:
                q &= Q(tenant=tenant)
            else:
                q &= Q(tenant__isnull=True)
            pricing = ConditionalPricingConfig.objects.filter(q).first()
            if not pricing:
                pricing = ConditionalPricingConfig(
                    product=product,
                    tenant=tenant,
                )
                created = True

        pricing.base_dispensing_unit = validated["base_dispensing_unit"]
        pricing.pack_to_strip_multiplier = validated["pack_to_strip_multiplier"]
        pricing.strip_to_piece_multiplier = validated["strip_to_piece_multiplier"]
        pricing.total_base_units_per_pack = validated["total_base_units_per_pack"]
        pricing.cost_per_piece_bdt = Decimal(str(validated["cost_per_piece_bdt"]))
        pricing.mrp_per_piece_bdt = Decimal(str(validated["mrp_per_piece_bdt"]))
        pricing.strip_cost_bdt = Decimal(str(validated["strip_cost_bdt"]))
        pricing.strip_mrp_bdt = Decimal(str(validated["strip_mrp_bdt"]))
        pricing.pack_cost_bdt = Decimal(str(validated["pack_cost_bdt"]))
        pricing.pack_mrp_bdt = Decimal(str(validated["pack_mrp_bdt"]))
        pricing.allow_strip_sale = validated["allow_strip_sale"]
        pricing.allow_piece_sale = validated["allow_piece_sale"]
        pricing.vat_rate_percentage = Decimal(str(validated["vat_rate_percentage"]))
        pricing.is_active = validated["is_active"]

        pricing.full_clean()
        pricing.save()

        action = "CONDITIONAL_PRICING_CREATED" if created else "CONDITIONAL_PRICING_UPDATED"
        AuditLogEntry.objects.create(
            tenant_id=getattr(tenant, "tenant_id", "PLATFORM") if tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action=action,
            resource_type="ConditionalPricingConfig",
            resource_id=str(pricing.pricing_id),
            metadata_json={
                "product_id": product_id,
                "pack_mrp": float(pricing.pack_mrp_bdt),
                "strip_mrp": float(pricing.strip_mrp_bdt),
                "piece_mrp": float(pricing.mrp_per_piece_bdt),
            },
            status=AuditActionStatus.SUCCESS,
        )

        return cls._serialize_pricing(pricing)

    @classmethod
    def get_pricing(cls, pricing_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """Retrieves details of a specific conditional pricing configuration."""
        cls.check_permissions(user, required_action="read")
        try:
            pricing = ConditionalPricingConfig.objects.select_related("product", "tenant").get(pk=pricing_id)
        except ConditionalPricingConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_PRICING_NOT_FOUND",
                "detail": f"ConditionalPricingConfig '{pricing_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if pricing.tenant_id and user_tenant_id and str(pricing.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant access prohibited.",
            })

        return cls._serialize_pricing(pricing)

    @classmethod
    def list_pricing(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists conditional pricing configurations scoped to tenant."""
        cls.check_permissions(user, required_action="read")
        qs = ConditionalPricingConfig.objects.select_related("product", "tenant").filter(is_active=True)

        user_tenant: Optional[Tenant] = cls._resolve_tenant(user)
        if not user.is_superuser:
            if user_tenant:
                qs = qs.filter(Q(tenant=user_tenant) | Q(tenant__isnull=True))
            else:
                qs = qs.filter(tenant__isnull=True)
        else:
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)

        if product_id:
            qs = qs.filter(product_id=product_id)

        return [cls._serialize_pricing(p) for p in qs]

    @classmethod
    @transaction.atomic
    def delete_or_archive_pricing(cls, pricing_id: str, user: PharmacyUser, hard_delete: bool = False) -> Dict[str, Any]:
        """Archives or deletes a pricing configuration."""
        cls.check_permissions(user, required_action="delete")
        try:
            pricing = ConditionalPricingConfig.objects.get(pk=pricing_id)
        except ConditionalPricingConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_PRICING_NOT_FOUND",
                "detail": f"ConditionalPricingConfig '{pricing_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        tenant = cls._resolve_tenant(user)
        if pricing.tenant_id and user_tenant_id and str(pricing.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant deletion prohibited.",
            })

        if hard_delete:
            pricing.delete()
            status_msg = "DELETED"
        else:
            pricing.is_active = False
            pricing.save(update_fields=["is_active", "updated_at"])
            status_msg = "ARCHIVED"

        AuditLogEntry.objects.create(
            tenant_id=getattr(tenant, "tenant_id", "PLATFORM") if tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="CONDITIONAL_PRICING_REMOVED",
            resource_type="ConditionalPricingConfig",
            resource_id=str(pricing_id),
            metadata_json={"status": status_msg},
            status=AuditActionStatus.SUCCESS,
        )

        return {"status": status_msg, "pricing_id": pricing_id}

    @classmethod
    def _serialize_pricing(cls, pricing: ConditionalPricingConfig) -> Dict[str, Any]:
        """Converts model instance to JSON dictionary."""
        return {
            "pricing_id": str(pricing.pricing_id),
            "product_id": str(pricing.product_id),
            "tenant_id": str(pricing.tenant_id) if pricing.tenant_id else None,
            "base_dispensing_unit": pricing.base_dispensing_unit,
            "base_dispensing_unit_display_bn": cls._translate_unit(pricing.base_dispensing_unit),
            "pack_to_strip_multiplier": pricing.pack_to_strip_multiplier,
            "strip_to_piece_multiplier": pricing.strip_to_piece_multiplier,
            "total_base_units_per_pack": pricing.total_base_units_per_pack,
            "cost_per_piece_bdt": float(pricing.cost_per_piece_bdt),
            "mrp_per_piece_bdt": float(pricing.mrp_per_piece_bdt),
            "strip_cost_bdt": float(pricing.strip_cost_bdt),
            "strip_mrp_bdt": float(pricing.strip_mrp_bdt),
            "pack_cost_bdt": float(pricing.pack_cost_bdt),
            "pack_mrp_bdt": float(pricing.pack_mrp_bdt),
            "allow_strip_sale": pricing.allow_strip_sale,
            "allow_piece_sale": pricing.allow_piece_sale,
            "vat_rate_percentage": float(pricing.vat_rate_percentage),
            "is_active": pricing.is_active,
            "created_at": pricing.created_at.isoformat(),
            "updated_at": pricing.updated_at.isoformat(),
        }

    @staticmethod
    def _translate_unit(code: str) -> str:
        translations = {
            "PIECE": "পিস / একক",
            "TABLET": "ট্যাবলেট",
            "CAPSULE": "ক্যাপসুল",
            "VIAL": "ভায়াল",
            "AMPOULE": "অ্যাম্পুল",
            "BOTTLE": "বোতল",
            "DEVICE": "ডিভাইস",
            "SACHET": "স্যাচেট",
            "SUPPOSITORY": "সাপোজিটরি",
        }
        return translations.get(code, code)
