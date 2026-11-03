"""
backend/apps/core/syrup_liquid_editor_service.py
Architecture Decision: ADR-0142 (P142).
Dosage-Form Specific Editor Service for Oral Liquids, Syrups, and Suspensions.
Governs concentration dimensions, nominal bottle volume, oral route restrictions,
sale units (Bottle/Pack), reconstitution water volume, and multi-tenant access boundaries.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    LiquidConcentrationUnit,
    LiquidDosageFormType,
    LiquidMeasuringDevice,
    LiquidRoute,
    LiquidSaleUnit,
    LiquidStoragePolicy,
    MedicineProduct,
    PlanTier,
    SyrupLiquidFormConfig,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.syrup_liquid_editor")


class SyrupLiquidEditorService:
    """
    Core domain service managing the Syrup & Oral Liquid dosage form editor.
    Enforces concentration-based strength dimensions, bottle volume checks,
    reconstitution water requirements, and zero-trust solid field isolation.
    """

    HIDDEN_FIELDS = [
        "strips_per_box",
        "tablets_per_strip",
        "units_per_strip",
        "box_size",
        "loose_sale_eligible",
        "is_scored",
        "coating_type",
        "needle_gauge",
    ]

    ALLOWED_CONCENTRATION_UNITS = [u.value for u in LiquidConcentrationUnit]
    ALLOWED_ROUTES = [r.value for r in LiquidRoute]
    ALLOWED_SALE_UNITS = [su.value for su in LiquidSaleUnit]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """
        Returns dynamic schema and field specifications for oral liquid and syrup editors.
        """
        return {
            "schema_version": "1.0",
            "dosage_form_family": "ORAL_LIQUID",
            "supported_dosage_forms": [
                {"code": choice.value, "label_en": choice.label, "label_bn": cls._translate_form(choice.value)}
                for choice in LiquidDosageFormType
            ],
            "sections": [
                {
                    "section_id": "concentration_spec",
                    "title_en": "Concentration & Active Strength",
                    "title_bn": "ঘনমাত্রা ও সক্রিয় উপাদানের পরিমাণ",
                    "fields": [
                        {
                            "field_key": "concentration_value",
                            "widget": "decimal_input",
                            "label_en": "Concentration Value",
                            "label_bn": "ঘনমাত্রার মান",
                            "is_required": True,
                            "min_value": 0.0001,
                            "default": 120.0,
                        },
                        {
                            "field_key": "concentration_unit",
                            "widget": "dropdown_select",
                            "label_en": "Concentration Unit",
                            "label_bn": "ঘনমাত্রার একক",
                            "is_required": True,
                            "options": [{"value": u.value, "label": u.label} for u in LiquidConcentrationUnit],
                            "default": LiquidConcentrationUnit.MG_PER_5ML.value,
                        },
                        {
                            "field_key": "bottle_volume_ml",
                            "widget": "decimal_input",
                            "label_en": "Bottle Volume (mL)",
                            "label_bn": "বোতলের মোট আয়তন (মি.লি.)",
                            "is_required": True,
                            "min_value": 1.0,
                            "default": 100.0,
                        },
                    ],
                },
                {
                    "section_id": "packaging_and_dispensing",
                    "title_en": "Packaging & Dispensing Units",
                    "title_bn": "প্যাকেজিং ও ডিসপেনসিং একক",
                    "fields": [
                        {
                            "field_key": "sale_unit",
                            "widget": "dropdown_select",
                            "label_en": "Sale / Dispensing Unit",
                            "label_bn": "বিক্রয় একক",
                            "is_required": True,
                            "options": [{"value": su.value, "label": su.label} for su in LiquidSaleUnit],
                            "default": LiquidSaleUnit.BOTTLE.value,
                        },
                        {
                            "field_key": "measuring_device",
                            "widget": "dropdown_select",
                            "label_en": "Measuring Device Included",
                            "label_bn": "পরিমাপক যন্ত্র সংযুক্ত",
                            "is_required": False,
                            "options": [{"value": md.value, "label": md.label} for md in LiquidMeasuringDevice],
                            "default": LiquidMeasuringDevice.MEASURING_CUP.value,
                        },
                        {
                            "field_key": "route",
                            "widget": "dropdown_select",
                            "label_en": "Administration Route",
                            "label_bn": "সেবনের মাধ্যম",
                            "is_required": True,
                            "options": [{"value": r.value, "label": r.label} for r in LiquidRoute],
                            "default": LiquidRoute.ORAL.value,
                        },
                    ],
                },
                {
                    "section_id": "reconstitution_spec",
                    "title_en": "Dry Syrup Reconstitution & Preservation",
                    "title_bn": "ড্রাই সিরাপ প্রস্তুতকরণ ও সংরক্ষণ",
                    "fields": [
                        {
                            "field_key": "is_reconstitution_required",
                            "widget": "checkbox_toggle",
                            "label_en": "Reconstitution Required (Dry Powder)",
                            "label_bn": "ব্যবহারের পূর্বে পানি মিশ্রণ আবশ্যক",
                            "is_required": False,
                            "default": False,
                        },
                        {
                            "field_key": "reconstitution_water_ml",
                            "widget": "decimal_input",
                            "label_en": "Reconstitution Water Volume (mL)",
                            "label_bn": "মিশ্রণযোগ্য পানির পরিমাণ (মি.লি.)",
                            "is_required": False,
                            "min_value": 1.0,
                        },
                        {
                            "field_key": "shelf_life_days_after_reconstitution",
                            "widget": "number_stepper",
                            "label_en": "Shelf Life After Reconstitution (Days)",
                            "label_bn": "পানি মিশ্রণের পর কার্যকাল (দিন)",
                            "is_required": False,
                            "default": 7,
                        },
                    ],
                },
                {
                    "section_id": "storage_and_flavor",
                    "title_en": "Storage Policy & Flavor",
                    "title_bn": "সংরক্ষণ নীতি ও ফ্লেভার",
                    "fields": [
                        {
                            "field_key": "storage_policy",
                            "widget": "dropdown_select",
                            "label_en": "Storage Policy",
                            "label_bn": "সংরক্ষণ নীতি",
                            "is_required": True,
                            "options": [{"value": sp.value, "label": sp.label} for sp in LiquidStoragePolicy],
                            "default": LiquidStoragePolicy.ROOM_TEMPERATURE_BELOW_30C.value,
                        },
                        {
                            "field_key": "flavor",
                            "widget": "text_input",
                            "label_en": "Flavor (e.g. Orange, Mango)",
                            "label_bn": "ফ্লেভার (যেমন কমলা, আম)",
                            "is_required": False,
                            "default": "",
                        },
                    ],
                },
            ],
            "hidden_fields": cls.HIDDEN_FIELDS,
            "business_rules": [
                {
                    "rule_id": "BR-LIQ-01",
                    "name": "Concentration Dimension Mandatory",
                    "description": "Strength must be expressed with volumetric concentration denominator (e.g. mg/5ml, mg/ml, %). Standalone mass units are prohibited.",
                },
                {
                    "rule_id": "BR-LIQ-02",
                    "name": "Solid Packaging Multipliers Prohibited",
                    "description": "Solid packaging fields (strips_per_box, tablets_per_strip, loose_sale_eligible) are hidden and strictly rejected for liquids.",
                },
                {
                    "rule_id": "BR-LIQ-03",
                    "name": "Liquid Dispensing Units",
                    "description": "Sale unit must be a bottle or sealed package (Bottle, Pack, Phial). Splitting bottles at POS is prohibited.",
                },
                {
                    "rule_id": "BR-LIQ-04",
                    "name": "Reconstitution Water Consistency",
                    "description": "Dry syrups and powders for oral suspension require positive reconstitution water volume in mL.",
                },
            ],
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates oral liquid/syrup payload against domain rules and zero-trust hidden field guards.
        """
        if not isinstance(payload, dict):
            raise ValidationError({"error_code": "ERR_INVALID_PAYLOAD", "detail": "Payload must be a JSON object."})

        # 1. Zero-Trust Hidden Field Guard (Reject solid oral tablet/strip fields)
        for field in cls.HIDDEN_FIELDS:
            val = payload.get(field)
            if val is not None and val != "":
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is a solid oral packaging attribute and prohibited for syrup and liquid forms.",
                })

        # 2. Required Fields
        required_fields = [
            "dosage_form",
            "concentration_value",
            "concentration_unit",
            "bottle_volume_ml",
            "sale_unit",
            "route",
            "storage_policy",
        ]
        for req in required_fields:
            if req not in payload or payload[req] is None or payload[req] == "":
                raise ValidationError({
                    "error_code": "ERR_REQUIRED_FIELD_MISSING",
                    "detail": f"Missing mandatory field '{req}' for syrup/liquid configuration.",
                })

        # 3. Dosage Form Validation
        dosage_form = str(payload.get("dosage_form")).strip().upper()
        valid_forms = [c.value for c in LiquidDosageFormType]
        if dosage_form not in valid_forms:
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSAGE_FORM",
                "detail": f"Dosage form '{dosage_form}' is not a valid oral liquid form. Valid choices: {valid_forms}.",
            })

        # 4. Concentration Unit & Dimension Validation
        concentration_unit = str(payload.get("concentration_unit")).strip().lower()
        if concentration_unit not in [u.lower() for u in cls.ALLOWED_CONCENTRATION_UNITS]:
            raise ValidationError({
                "error_code": "ERR_INVALID_STRENGTH_DIMENSION_FOR_LIQUID",
                "detail": (
                    f"Concentration unit '{concentration_unit}' lacks valid liquid volumetric denominator. "
                    f"Allowed: {cls.ALLOWED_CONCENTRATION_UNITS}. Standalone solid mass units (mg, mcg, g) are prohibited."
                ),
            })

        # 5. Concentration Value Numeric Validation
        try:
            concentration_value = Decimal(str(payload.get("concentration_value")))
            if concentration_value <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_CONCENTRATION_VALUE",
                    "detail": "concentration_value must be a positive number greater than 0.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_CONCENTRATION_VALUE",
                "detail": "concentration_value must be a valid numeric decimal.",
            })

        # 6. Bottle Volume Validation
        try:
            bottle_volume_ml = Decimal(str(payload.get("bottle_volume_ml")))
            if bottle_volume_ml <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_BOTTLE_VOLUME",
                    "detail": "bottle_volume_ml must be greater than 0.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_BOTTLE_VOLUME",
                "detail": "bottle_volume_ml must be a valid numeric decimal.",
            })

        # 7. Sale Unit Validation
        sale_unit = str(payload.get("sale_unit")).strip()
        if sale_unit not in cls.ALLOWED_SALE_UNITS:
            raise ValidationError({
                "error_code": "ERR_INVALID_SALE_UNIT_FOR_LIQUID",
                "detail": f"sale_unit '{sale_unit}' is invalid for oral liquids. Allowed units: {cls.ALLOWED_SALE_UNITS}.",
            })

        # 8. Route Validation
        route = str(payload.get("route")).strip()
        if route not in cls.ALLOWED_ROUTES:
            raise ValidationError({
                "error_code": "ERR_INVALID_ROUTE_FOR_DOSAGE_FORM",
                "detail": f"Route '{route}' is invalid for oral syrup/liquid forms. Valid routes: {cls.ALLOWED_ROUTES}.",
            })

        # 9. Storage Policy Validation
        storage_policy = str(payload.get("storage_policy")).strip()
        valid_storage = [sp.value for sp in LiquidStoragePolicy]
        if storage_policy not in valid_storage:
            raise ValidationError({
                "error_code": "ERR_INVALID_STORAGE_POLICY",
                "detail": f"storage_policy '{storage_policy}' is invalid. Allowed: {valid_storage}.",
            })

        # 10. Reconstitution Validation
        is_reconstitution_required = bool(payload.get("is_reconstitution_required", False))
        if dosage_form == LiquidDosageFormType.DRY_SYRUP_RECONSTITUTED.value:
            is_reconstitution_required = True

        reconstitution_water_ml = None
        raw_reconstitution_water = payload.get("reconstitution_water_ml")
        if is_reconstitution_required:
            if raw_reconstitution_water is None or raw_reconstitution_water == "":
                raise ValidationError({
                    "error_code": "ERR_RECONSTITUTION_WATER_REQUIRED",
                    "detail": "reconstitution_water_ml is mandatory when reconstitution is required.",
                })
            try:
                reconstitution_water_ml = Decimal(str(raw_reconstitution_water))
                if reconstitution_water_ml <= Decimal("0"):
                    raise ValidationError({
                        "error_code": "ERR_RECONSTITUTION_WATER_REQUIRED",
                        "detail": "reconstitution_water_ml must be greater than 0 mL.",
                    })
            except (InvalidOperation, TypeError):
                raise ValidationError({
                    "error_code": "ERR_RECONSTITUTION_WATER_REQUIRED",
                    "detail": "reconstitution_water_ml must be a valid numeric decimal.",
                })
        elif raw_reconstitution_water is not None and raw_reconstitution_water != "":
            try:
                reconstitution_water_ml = Decimal(str(raw_reconstitution_water))
            except (InvalidOperation, TypeError):
                reconstitution_water_ml = None

        shelf_life_days = None
        if is_reconstitution_required and payload.get("shelf_life_days_after_reconstitution") is not None:
            try:
                shelf_life_days = int(payload.get("shelf_life_days_after_reconstitution"))
                if shelf_life_days < 1:
                    shelf_life_days = 7
            except (ValueError, TypeError):
                shelf_life_days = 7

        measuring_device = str(payload.get("measuring_device", LiquidMeasuringDevice.MEASURING_CUP.value))
        flavor = str(payload.get("flavor", "")).strip()

        return {
            "dosage_form": dosage_form,
            "concentration_value": concentration_value,
            "concentration_unit": concentration_unit,
            "bottle_volume_ml": bottle_volume_ml,
            "sale_unit": sale_unit,
            "route": route,
            "storage_policy": storage_policy,
            "measuring_device": measuring_device,
            "is_reconstitution_required": is_reconstitution_required,
            "reconstitution_water_ml": reconstitution_water_ml,
            "shelf_life_days_after_reconstitution": shelf_life_days,
            "flavor": flavor,
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    def list_configs(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists accessible syrup/liquid configurations with multi-tenant and global fallback filtering.
        """
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = SyrupLiquidFormConfig.objects.select_related("product", "tenant").all()

        if cls._is_platform_operator(user):
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
        else:
            user_tenant = getattr(user, "tenant_id", None)
            qs = qs.filter(tenant_id__in=[user_tenant, None])

        if product_id:
            qs = qs.filter(product_id=product_id)

        results = []
        for cfg in qs.order_by("-updated_at"):
            results.append(cls._serialize_config(cfg))
        return results

    @classmethod
    def get_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """
        Retrieves a single syrup/liquid configuration.
        """
        cfg = SyrupLiquidFormConfig.objects.select_related("product", "tenant").filter(config_id=config_id).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """
        Creates or updates an oral liquid / syrup configuration.
        """
        payload_tenant = payload.get("tenant_id")
        is_operator = cls._is_platform_operator(user)

        if is_operator:
            target_tenant_id = payload_tenant
        else:
            target_tenant_id = getattr(user, "tenant_id", None)
            if payload_tenant and payload_tenant != target_tenant_id:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "detail": "Cannot configure syrup/liquid settings for another tenant.",
                })

        cls._validate_user_permissions(user, tenant_id=target_tenant_id, action_type="write")

        product_id = payload.get("product_id")
        if not product_id:
            raise ValidationError({"error_code": "ERR_PRODUCT_REQUIRED", "detail": "product_id is required."})

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise ValidationError({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{product_id}' not found."})

        validated_data = cls.validate_payload(payload)

        tenant_obj = Tenant.objects.filter(tenant_id=target_tenant_id).first() if target_tenant_id else None

        # Resolve existing config by config_id or (product, tenant)
        cfg = None
        given_config_id = payload.get("config_id")
        if given_config_id:
            cfg = SyrupLiquidFormConfig.objects.filter(config_id=given_config_id).first()
        if not cfg:
            cfg = SyrupLiquidFormConfig.objects.filter(product=product, tenant=tenant_obj).first()

        if cfg:
            cfg.dosage_form = validated_data["dosage_form"]
            cfg.concentration_value = validated_data["concentration_value"]
            cfg.concentration_unit = validated_data["concentration_unit"]
            cfg.bottle_volume_ml = validated_data["bottle_volume_ml"]
            cfg.sale_unit = validated_data["sale_unit"]
            cfg.route = validated_data["route"]
            cfg.storage_policy = validated_data["storage_policy"]
            cfg.measuring_device = validated_data["measuring_device"]
            cfg.is_reconstitution_required = validated_data["is_reconstitution_required"]
            cfg.reconstitution_water_ml = validated_data["reconstitution_water_ml"]
            cfg.shelf_life_days_after_reconstitution = validated_data["shelf_life_days_after_reconstitution"]
            cfg.flavor = validated_data["flavor"]
            cfg.is_active = validated_data["is_active"]
            cfg.save()
        else:
            config_id = given_config_id or f"slc-{product.product_id}"
            if target_tenant_id and not config_id.endswith(target_tenant_id):
                config_id = f"{config_id}-{target_tenant_id}"
            cfg = SyrupLiquidFormConfig.objects.create(
                config_id=config_id,
                product=product,
                tenant=tenant_obj,
                dosage_form=validated_data["dosage_form"],
                concentration_value=validated_data["concentration_value"],
                concentration_unit=validated_data["concentration_unit"],
                bottle_volume_ml=validated_data["bottle_volume_ml"],
                sale_unit=validated_data["sale_unit"],
                route=validated_data["route"],
                storage_policy=validated_data["storage_policy"],
                measuring_device=validated_data["measuring_device"],
                is_reconstitution_required=validated_data["is_reconstitution_required"],
                reconstitution_water_ml=validated_data["reconstitution_water_ml"],
                shelf_life_days_after_reconstitution=validated_data["shelf_life_days_after_reconstitution"],
                flavor=validated_data["flavor"],
                is_active=validated_data["is_active"],
            )

        # Audit log
        user_id_val = str(getattr(user, "user_id", user.pk))
        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=user_id_val,
            actor_email=user.email or "system@pharmacy.local",
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="SYRUP_LIQUID_CONFIG_UPSERTED",
            resource_type="SyrupLiquidFormConfig",
            resource_id=cfg.config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Upserted syrup/liquid config for product {product.product_id} ({cfg.bottle_volume_ml}mL {cfg.concentration_value}{cfg.concentration_unit})."},
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """
        Deletes a syrup/liquid configuration.
        """
        cfg = SyrupLiquidFormConfig.objects.filter(config_id=config_id).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="write")

        product_id = cfg.product_id
        cfg.delete()

        user_id_val = str(getattr(user, "user_id", user.pk))
        AuditLogEntry.objects.create(
            tenant_id=cfg.tenant_id or "PLATFORM",
            user_id=user_id_val,
            actor_email=user.email or "system@pharmacy.local",
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="SYRUP_LIQUID_CONFIG_DELETED",
            resource_type="SyrupLiquidFormConfig",
            resource_id=config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted syrup/liquid config for product {product_id}."},
        )

        return {"deleted": True, "config_id": config_id, "product_id": product_id}

    # =========================================================================
    # Internal Permissions & Serialization Helpers
    # =========================================================================

    @classmethod
    def _is_platform_operator(cls, user: PharmacyUser) -> bool:
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return True
        role = getattr(user, "role", "")
        return role in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR, "PLATFORM_SUPER_ADMIN"]

    @classmethod
    def _validate_user_permissions(
        cls,
        user: PharmacyUser,
        tenant_id: Optional[str] = None,
        action_type: str = "read",
    ) -> None:
        if not user or not user.is_authenticated:
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})

        if cls._is_platform_operator(user):
            return

        role = getattr(user, "role", "")
        user_tenant = getattr(user, "tenant_id", None)

        if role == UserRole.CASHIER and action_type == "write":
            raise PermissionDenied({
                "error_code": "ERR_PERMISSION_DENIED",
                "message_en": "Cashiers are not permitted to modify syrup/liquid configurations.",
                "message_bn": "ক্যাশিয়ারদের সিরাপ/লিকুইড কনফিগারেশন পরিবর্তন করার অনুমতি নেই।",
            })

        if tenant_id and tenant_id != user_tenant:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_VIOLATION",
                "message_en": "Access to another tenant's syrup/liquid configuration is prohibited.",
                "message_bn": "অন্য ফার্মেসির লিকুইড কনফিগারেশনে অ্যাক্সেস নিষিদ্ধ।",
            })

        tenant = Tenant.objects.filter(tenant_id=user_tenant).first()
        if tenant and tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied({
                "error_code": "ERR_TENANT_SUSPENDED",
                "message_en": "Tenant subscription account is suspended. Feature blocked.",
                "message_bn": "ফার্মেসির সাবস্ক্রিপশন অ্যাকাউন্ট স্থগিত থাকায় সেবা বন্ধ।",
            })

        if action_type == "write" and tenant and tenant.plan_tier == PlanTier.BASIC:
            raise PermissionDenied({
                "error_code": "ERR_SUBSCRIPTION_UNENTITLED",
                "message_en": "Custom liquid configuration requires a STANDARD or PRO subscription plan.",
                "message_bn": "কাস্টম লিকুইড কনফিগারেশনের জন্য স্ট্যান্ডার্ড বা প্রো সাবস্ক্রিপশন আবশ্যক।",
            })

    @classmethod
    def _serialize_config(cls, cfg: SyrupLiquidFormConfig) -> Dict[str, Any]:
        return {
            "config_id": cfg.config_id,
            "product_id": cfg.product_id,
            "brand_name": getattr(cfg.product, "brand_name", ""),
            "generic_name": getattr(cfg.product, "generic_name", ""),
            "tenant_id": cfg.tenant_id,
            "dosage_form": cfg.dosage_form,
            "concentration_value": float(cfg.concentration_value),
            "concentration_unit": cfg.concentration_unit,
            "bottle_volume_ml": float(cfg.bottle_volume_ml),
            "sale_unit": cfg.sale_unit,
            "route": cfg.route,
            "storage_policy": cfg.storage_policy,
            "measuring_device": cfg.measuring_device,
            "is_reconstitution_required": cfg.is_reconstitution_required,
            "reconstitution_water_ml": float(cfg.reconstitution_water_ml) if cfg.reconstitution_water_ml else None,
            "shelf_life_days_after_reconstitution": cfg.shelf_life_days_after_reconstitution,
            "flavor": cfg.flavor,
            "is_active": cfg.is_active,
            "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @staticmethod
    def _translate_form(form_code: str) -> str:
        bn_map = {
            "SYRUP": "সিরাপ",
            "SUSPENSION": "সাসপেনশন",
            "ORAL_SOLUTION": "ওরাল সল্যুশন",
            "ELIXIR": "এলিক্সির",
            "ORAL_EMULSION": "ওরাল ইমালশন",
            "ORAL_DROPS": "ওরাল ড্রপস",
            "DRY_SYRUP_RECONSTITUTED": "ড্রাই সিরাপ / পাউডার সাসপেনশন",
        }
        return bn_map.get(form_code, form_code)
