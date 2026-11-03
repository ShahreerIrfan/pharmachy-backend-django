"""
backend/apps/core/injection_vial_editor_service.py
Architecture Decision: ADR-0144 (P144).
Dosage-Form Specific Editor Service for Injectable Medications, Vials, Ampoules, and Infusions.
Governs ingredient strength dimensions, fill volume, sterile route restrictions,
single-dose vs. multi-dose presentation, diluent reconstitution, and cold-chain temperature thresholds.
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
    InjectionContainerType,
    InjectionDiluentType,
    InjectionDosageFormType,
    InjectionSterileRoute,
    InjectionStrengthUnit,
    InjectionVialFormConfig,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.injection_vial_editor")


class InjectionVialEditorService:
    """
    Core domain service managing the Injection Vial, Ampoule & Sterile Route Editor.
    Enforces parenteral route restrictions, cold-chain temperature bounds,
    single-dose preservative prohibition, diluent requirements, and zero-trust solid field isolation.
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

    ALLOWED_DOSAGE_FORMS = [c.value for c in InjectionDosageFormType]
    ALLOWED_CONTAINER_TYPES = [c.value for c in InjectionContainerType]
    ALLOWED_STERILE_ROUTES = [c.value for c in InjectionSterileRoute]
    ALLOWED_STRENGTH_UNITS = [c.value for c in InjectionStrengthUnit]
    ALLOWED_DILUENT_TYPES = [c.value for c in InjectionDiluentType]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """
        Returns dynamic schema and validation rules for injectable dosage forms.
        """
        return {
            "schema_version": "1.0",
            "dosage_form_family": "PARENTERAL_INJECTION",
            "supported_dosage_forms": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_form(c.value)}
                for c in InjectionDosageFormType
            ],
            "supported_containers": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_container(c.value)}
                for c in InjectionContainerType
            ],
            "sections": [
                {
                    "section_id": "parenteral_spec",
                    "title_en": "Formulation & Container Specification",
                    "title_bn": "ফর্মুলেশন ও কনটেইনার স্পেসিফিকেশন",
                    "fields": [
                        {
                            "field_key": "dosage_form",
                            "widget": "dropdown_select",
                            "label_en": "Dosage Form",
                            "label_bn": "ডোজের ধরন",
                            "is_required": True,
                            "options": [{"value": c.value, "label": c.label} for c in InjectionDosageFormType],
                            "default": InjectionDosageFormType.INJECTION_SOLUTION.value,
                        },
                        {
                            "field_key": "container_type",
                            "widget": "dropdown_select",
                            "label_en": "Container / Packaging Type",
                            "label_bn": "কনটেইনার / মোড়কের ধরন",
                            "is_required": True,
                            "options": [{"value": c.value, "label": c.label} for c in InjectionContainerType],
                            "default": InjectionContainerType.VIAL_SINGLE_DOSE.value,
                        },
                        {
                            "field_key": "strength_value",
                            "widget": "decimal_input",
                            "label_en": "Strength Value",
                            "label_bn": "সক্রিয় উপাদানের মাত্রা",
                            "is_required": True,
                            "min_value": 0.0001,
                            "default": 500.0,
                        },
                        {
                            "field_key": "strength_unit",
                            "widget": "dropdown_select",
                            "label_en": "Strength Unit",
                            "label_bn": "মাত্রার একক",
                            "is_required": True,
                            "options": [{"value": u.value, "label": u.label} for u in InjectionStrengthUnit],
                            "default": InjectionStrengthUnit.MG_PER_ML.value,
                        },
                        {
                            "field_key": "fill_volume_ml",
                            "widget": "decimal_input",
                            "label_en": "Fill Volume (mL)",
                            "label_bn": "ভলিউম / মোট পরিমাণ (মি.লি.)",
                            "is_required": True,
                            "min_value": 0.1,
                            "default": 10.0,
                        },
                        {
                            "field_key": "sterile_route",
                            "widget": "dropdown_select",
                            "label_en": "Sterile Route of Administration",
                            "label_bn": "প্রয়োগের স্টেরাইল রুট",
                            "is_required": True,
                            "options": [{"value": r.value, "label": r.label} for r in InjectionSterileRoute],
                            "default": InjectionSterileRoute.IV.value,
                        },
                    ],
                },
                {
                    "section_id": "cold_chain_and_preservative",
                    "title_en": "Cold Chain & Preservative Controls",
                    "title_bn": "কোল্ড চেইন ও প্রিজারভেটিভ নিয়ন্ত্রণ",
                    "fields": [
                        {
                            "field_key": "is_cold_chain_required",
                            "widget": "checkbox_toggle",
                            "label_en": "Cold-Chain Required (2°C - 8°C)",
                            "label_bn": "কোল্ড চেইন সংরক্ষণ আবশ্যক (২°সে - ৮°সে)",
                            "is_required": False,
                            "default": False,
                        },
                        {
                            "field_key": "min_storage_temp_celsius",
                            "widget": "decimal_input",
                            "label_en": "Min Storage Temp (°C)",
                            "label_bn": "সর্বনিম্ন তাপমাত্রা (°সে)",
                            "is_required": False,
                            "default": 2.0,
                        },
                        {
                            "field_key": "max_storage_temp_celsius",
                            "widget": "decimal_input",
                            "label_en": "Max Storage Temp (°C)",
                            "label_bn": "সর্বোচ্চ তাপমাত্রা (°সে)",
                            "is_required": False,
                            "default": 8.0,
                        },
                        {
                            "field_key": "has_preservative",
                            "widget": "checkbox_toggle",
                            "label_en": "Contains Preservative (Multi-dose only)",
                            "label_bn": "প্রিজারভেটিভ সংযুক্ত (কেবল মাল্টি-ডোজ)",
                            "is_required": False,
                            "default": False,
                        },
                    ],
                },
                {
                    "section_id": "reconstitution_diluent",
                    "title_en": "Lyophilized Powder Reconstitution",
                    "title_bn": "পাউডার ইনজেকশন দ্রবণকরণ",
                    "fields": [
                        {
                            "field_key": "requires_diluent",
                            "widget": "checkbox_toggle",
                            "label_en": "Diluent Reconstitution Required",
                            "label_bn": "প্রয়োগের পূর্বে ডিলুয়েন্ট আবশ্যক",
                            "is_required": False,
                            "default": False,
                        },
                        {
                            "field_key": "diluent_type",
                            "widget": "dropdown_select",
                            "label_en": "Diluent Type",
                            "label_bn": "ডিলুয়েন্টের ধরন",
                            "is_required": False,
                            "options": [{"value": d.value, "label": d.label} for d in InjectionDiluentType],
                            "default": InjectionDiluentType.NONE.value,
                        },
                        {
                            "field_key": "diluent_volume_ml",
                            "widget": "decimal_input",
                            "label_en": "Diluent Volume (mL)",
                            "label_bn": "ডিলুয়েন্টের পরিমাণ (মি.লি.)",
                            "is_required": False,
                            "min_value": 0.5,
                        },
                    ],
                },
            ],
            "hidden_fields": cls.HIDDEN_FIELDS,
            "business_rules": [
                {
                    "rule_id": "BR-INJ-01",
                    "name": "Sterile Parenteral Route Mandatory",
                    "description": "Route must be parenteral (IV, IM, SC, ID, Intravitreal, Epidural). Oral routes are strictly prohibited.",
                },
                {
                    "rule_id": "BR-INJ-02",
                    "name": "Cold-Chain Temperature Bounds",
                    "description": "When cold chain is required, min_storage_temp_celsius must be strictly less than max_storage_temp_celsius.",
                },
                {
                    "rule_id": "BR-INJ-03",
                    "name": "Single-Dose Ampoule Preservative Prohibition",
                    "description": "Single-dose glass ampoules cannot contain antimicrobial preservatives.",
                },
                {
                    "rule_id": "BR-INJ-04",
                    "name": "Lyophilized Reconstitution Diluent Mandatory",
                    "description": "Powder injections require positive diluent volume and valid diluent type.",
                },
                {
                    "rule_id": "BR-INJ-05",
                    "name": "Solid Oral Packaging Forbidden",
                    "description": "Solid oral strip/box packaging fields are forbidden for injectable dosage forms.",
                },
            ],
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates injection vial payload against parenteral pharmaceutical rules.
        """
        if not isinstance(payload, dict):
            raise ValidationError({"error_code": "ERR_INVALID_PAYLOAD", "detail": "Payload must be a JSON object."})

        # 1. Zero-Trust Hidden Field Guard (Reject solid oral tablet/strip fields)
        for field in cls.HIDDEN_FIELDS:
            val = payload.get(field)
            if val is not None and val != "":
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is a solid oral packaging attribute and prohibited for injection vial forms.",
                })

        # 2. Required Fields
        required_fields = [
            "dosage_form",
            "container_type",
            "strength_value",
            "strength_unit",
            "fill_volume_ml",
            "sterile_route",
        ]
        for req in required_fields:
            if req not in payload or payload[req] is None or payload[req] == "":
                raise ValidationError({
                    "error_code": "ERR_REQUIRED_FIELD_MISSING",
                    "detail": f"Missing mandatory field '{req}' for injection vial configuration.",
                })

        # 3. Dosage Form Validation
        dosage_form = str(payload.get("dosage_form")).strip().upper()
        if dosage_form not in cls.ALLOWED_DOSAGE_FORMS:
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSAGE_FORM",
                "detail": f"Dosage form '{dosage_form}' is invalid for injectables. Allowed: {cls.ALLOWED_DOSAGE_FORMS}.",
            })

        # 4. Container Type Validation
        container_type = str(payload.get("container_type")).strip().upper()
        if container_type not in cls.ALLOWED_CONTAINER_TYPES:
            raise ValidationError({
                "error_code": "ERR_INVALID_CONTAINER_TYPE",
                "detail": f"Container type '{container_type}' is invalid. Allowed: {cls.ALLOWED_CONTAINER_TYPES}.",
            })

        # 5. Sterile Route Validation
        sterile_route = str(payload.get("sterile_route")).strip().upper()
        if sterile_route not in cls.ALLOWED_STERILE_ROUTES:
            raise ValidationError({
                "error_code": "ERR_INVALID_STERILE_ROUTE",
                "detail": (
                    f"Route '{sterile_route}' is invalid for parenteral injectables. "
                    f"Allowed: {cls.ALLOWED_STERILE_ROUTES}. Oral administration routes are prohibited."
                ),
            })

        # 6. Strength Unit Validation
        strength_unit = str(payload.get("strength_unit")).strip()
        if strength_unit not in cls.ALLOWED_STRENGTH_UNITS:
            raise ValidationError({
                "error_code": "ERR_INVALID_STRENGTH_UNIT",
                "detail": f"Strength unit '{strength_unit}' is invalid for injectables. Allowed: {cls.ALLOWED_STRENGTH_UNITS}.",
            })

        # 7. Strength Value Numeric Validation
        try:
            strength_value = Decimal(str(payload.get("strength_value")))
            if strength_value <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_STRENGTH_VALUE",
                    "detail": "strength_value must be greater than 0.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_STRENGTH_VALUE",
                "detail": "strength_value must be a valid numeric decimal.",
            })

        # 8. Fill Volume Validation
        try:
            fill_volume_ml = Decimal(str(payload.get("fill_volume_ml")))
            if fill_volume_ml <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_FILL_VOLUME",
                    "detail": "fill_volume_ml must be greater than 0 mL.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_FILL_VOLUME",
                "detail": "fill_volume_ml must be a valid numeric decimal.",
            })

        # 9. Cold Chain Validation
        is_cold_chain_required = bool(payload.get("is_cold_chain_required", False))
        min_temp = None
        max_temp = None
        if is_cold_chain_required:
            raw_min = payload.get("min_storage_temp_celsius")
            raw_max = payload.get("max_storage_temp_celsius")
            if raw_min is None or raw_min == "" or raw_max is None or raw_max == "":
                raise ValidationError({
                    "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                    "detail": "Both min_storage_temp_celsius and max_storage_temp_celsius are required when cold chain is active.",
                })
            try:
                min_temp = Decimal(str(raw_min))
                max_temp = Decimal(str(raw_max))
            except (InvalidOperation, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                    "detail": "Storage temperatures must be valid numeric values.",
                })

            if min_temp >= max_temp:
                raise ValidationError({
                    "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                    "detail": f"min_storage_temp_celsius ({min_temp}°C) must be strictly less than max_storage_temp_celsius ({max_temp}°C).",
                })
        else:
            if payload.get("min_storage_temp_celsius") is not None:
                try:
                    min_temp = Decimal(str(payload.get("min_storage_temp_celsius")))
                except (InvalidOperation, TypeError):
                    min_temp = None
            if payload.get("max_storage_temp_celsius") is not None:
                try:
                    max_temp = Decimal(str(payload.get("max_storage_temp_celsius")))
                except (InvalidOperation, TypeError):
                    max_temp = None

        # 10. Single-Dose Ampoule Preservative Prohibition
        has_preservative = bool(payload.get("has_preservative", False))
        if container_type == InjectionContainerType.AMPOULE_GLASS.value and has_preservative:
            raise ValidationError({
                "error_code": "ERR_SINGLE_DOSE_PRESERVATIVE_PROHIBITED",
                "detail": "Single-dose glass ampoules cannot contain antimicrobial preservatives.",
            })

        # 11. Lyophilized Diluent Requirements
        requires_diluent = bool(payload.get("requires_diluent", False))
        if dosage_form == InjectionDosageFormType.INJECTION_POWDER_LYOPHILIZED.value:
            requires_diluent = True

        diluent_type = str(payload.get("diluent_type", InjectionDiluentType.NONE.value)).strip().upper()
        diluent_volume_ml = None

        if requires_diluent:
            raw_vol = payload.get("diluent_volume_ml")
            if raw_vol is None or raw_vol == "":
                raise ValidationError({
                    "error_code": "ERR_DILUENT_REQUIRED",
                    "detail": "diluent_volume_ml is required for lyophilized powder injections.",
                })
            try:
                diluent_volume_ml = Decimal(str(raw_vol))
                if diluent_volume_ml <= Decimal("0"):
                    raise ValidationError({
                        "error_code": "ERR_DILUENT_REQUIRED",
                        "detail": "diluent_volume_ml must be greater than 0 mL.",
                    })
            except (InvalidOperation, TypeError):
                raise ValidationError({
                    "error_code": "ERR_DILUENT_REQUIRED",
                    "detail": "diluent_volume_ml must be a valid numeric decimal.",
                })

            if diluent_type in [InjectionDiluentType.NONE.value, "NONE", "", "NULL"]:
                raise ValidationError({
                    "error_code": "ERR_DILUENT_REQUIRED",
                    "detail": "A valid diluent_type is required for lyophilized powder injections.",
                })
            if diluent_type not in cls.ALLOWED_DILUENT_TYPES:
                raise ValidationError({
                    "error_code": "ERR_DILUENT_REQUIRED",
                    "detail": f"diluent_type '{diluent_type}' is invalid. Allowed: {cls.ALLOWED_DILUENT_TYPES}.",
                })
        else:
            if payload.get("diluent_volume_ml") is not None:
                try:
                    diluent_volume_ml = Decimal(str(payload.get("diluent_volume_ml")))
                except (InvalidOperation, TypeError):
                    diluent_volume_ml = None

        return {
            "dosage_form": dosage_form,
            "container_type": container_type,
            "strength_value": strength_value,
            "strength_unit": strength_unit,
            "fill_volume_ml": fill_volume_ml,
            "sterile_route": sterile_route,
            "is_cold_chain_required": is_cold_chain_required,
            "min_storage_temp_celsius": min_temp,
            "max_storage_temp_celsius": max_temp,
            "has_preservative": has_preservative,
            "requires_diluent": requires_diluent,
            "diluent_type": diluent_type,
            "diluent_volume_ml": diluent_volume_ml,
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
        Lists accessible injection vial configurations with multi-tenant and global fallback filtering.
        """
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = InjectionVialFormConfig.objects.select_related("product", "tenant").all()

        if cls._is_platform_operator(user):
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
        else:
            user_tenant = getattr(user, "tenant_id", None)
            if user_tenant:
                qs = qs.filter(Q(tenant_id=user_tenant) | Q(tenant__isnull=True))
            else:
                qs = qs.filter(tenant__isnull=True)

        if product_id:
            qs = qs.filter(product_id=product_id)

        results = []
        for cfg in qs.order_by("-updated_at"):
            results.append(cls._serialize_config(cfg))
        return results

    @classmethod
    def get_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """
        Retrieves a single injection vial configuration.
        """
        cfg = InjectionVialFormConfig.objects.select_related("product", "tenant").filter(config_id=config_id).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """
        Creates or updates an injection vial configuration.
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
                    "detail": "Cannot configure injection vial settings for another tenant.",
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
            cfg = InjectionVialFormConfig.objects.filter(config_id=given_config_id).first()
        if not cfg:
            cfg = InjectionVialFormConfig.objects.filter(product=product, tenant=tenant_obj).first()

        if cfg:
            cfg.dosage_form = validated_data["dosage_form"]
            cfg.container_type = validated_data["container_type"]
            cfg.strength_value = validated_data["strength_value"]
            cfg.strength_unit = validated_data["strength_unit"]
            cfg.fill_volume_ml = validated_data["fill_volume_ml"]
            cfg.sterile_route = validated_data["sterile_route"]
            cfg.is_cold_chain_required = validated_data["is_cold_chain_required"]
            cfg.min_storage_temp_celsius = validated_data["min_storage_temp_celsius"]
            cfg.max_storage_temp_celsius = validated_data["max_storage_temp_celsius"]
            cfg.has_preservative = validated_data["has_preservative"]
            cfg.requires_diluent = validated_data["requires_diluent"]
            cfg.diluent_type = validated_data["diluent_type"]
            cfg.diluent_volume_ml = validated_data["diluent_volume_ml"]
            cfg.is_active = validated_data["is_active"]
            cfg.save()
        else:
            config_id = given_config_id or f"ivc-{product.product_id}"
            if target_tenant_id and not config_id.endswith(target_tenant_id):
                config_id = f"{config_id}-{target_tenant_id}"
            cfg = InjectionVialFormConfig.objects.create(
                config_id=config_id,
                product=product,
                tenant=tenant_obj,
                dosage_form=validated_data["dosage_form"],
                container_type=validated_data["container_type"],
                strength_value=validated_data["strength_value"],
                strength_unit=validated_data["strength_unit"],
                fill_volume_ml=validated_data["fill_volume_ml"],
                sterile_route=validated_data["sterile_route"],
                is_cold_chain_required=validated_data["is_cold_chain_required"],
                min_storage_temp_celsius=validated_data["min_storage_temp_celsius"],
                max_storage_temp_celsius=validated_data["max_storage_temp_celsius"],
                has_preservative=validated_data["has_preservative"],
                requires_diluent=validated_data["requires_diluent"],
                diluent_type=validated_data["diluent_type"],
                diluent_volume_ml=validated_data["diluent_volume_ml"],
                is_active=validated_data["is_active"],
            )

        # Audit logging
        user_id_val = str(getattr(user, "user_id", user.pk))
        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=user_id_val,
            actor_email=user.email or "system@pharmacy.local",
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="INJECTION_VIAL_CONFIG_UPSERTED",
            resource_type="InjectionVialFormConfig",
            resource_id=cfg.config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "details": (
                    f"Upserted injection vial config for product {product.product_id} "
                    f"({cfg.container_type} {cfg.strength_value}{cfg.strength_unit} / {cfg.fill_volume_ml}mL [{cfg.sterile_route}])."
                )
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """
        Deletes an injection vial configuration.
        """
        cfg = InjectionVialFormConfig.objects.filter(config_id=config_id).first()
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
            action="INJECTION_VIAL_CONFIG_DELETED",
            resource_type="InjectionVialFormConfig",
            resource_id=config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted injection vial config for product {product_id}."},
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
                "message_en": "Cashiers are not permitted to modify injection vial configurations.",
                "message_bn": "ক্যাশিয়ারদের ইনজেকশন কনফিগারেশন পরিবর্তন করার অনুমতি নেই।",
            })

        if tenant_id and tenant_id != user_tenant:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_VIOLATION",
                "message_en": "Access to another tenant's injection configuration is prohibited.",
                "message_bn": "অন্য ফার্মেসির ইনজেকশন কনফিগারেশনে অ্যাক্সেস নিষিদ্ধ।",
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
                "message_en": "Custom injection vial configuration requires a STANDARD or PRO subscription plan.",
                "message_bn": "কাস্টম ইনজেকশন কনফিগারেশনের জন্য স্ট্যান্ডার্ড বা প্রো সাবস্ক্রিপশন আবশ্যক।",
            })

    @classmethod
    def _serialize_config(cls, cfg: InjectionVialFormConfig) -> Dict[str, Any]:
        return {
            "config_id": cfg.config_id,
            "product_id": cfg.product_id,
            "brand_name": getattr(cfg.product, "brand_name", ""),
            "generic_name": getattr(cfg.product, "generic_name", ""),
            "tenant_id": cfg.tenant_id,
            "dosage_form": cfg.dosage_form,
            "container_type": cfg.container_type,
            "strength_value": float(cfg.strength_value),
            "strength_unit": cfg.strength_unit,
            "fill_volume_ml": float(cfg.fill_volume_ml),
            "sterile_route": cfg.sterile_route,
            "is_cold_chain_required": cfg.is_cold_chain_required,
            "min_storage_temp_celsius": float(cfg.min_storage_temp_celsius) if cfg.min_storage_temp_celsius is not None else None,
            "max_storage_temp_celsius": float(cfg.max_storage_temp_celsius) if cfg.max_storage_temp_celsius is not None else None,
            "has_preservative": cfg.has_preservative,
            "requires_diluent": cfg.requires_diluent,
            "diluent_type": cfg.diluent_type,
            "diluent_volume_ml": float(cfg.diluent_volume_ml) if cfg.diluent_volume_ml is not None else None,
            "is_active": cfg.is_active,
            "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @staticmethod
    def _translate_form(form_code: str) -> str:
        bn_map = {
            "INJECTION_SOLUTION": "ইনজেকশন দ্রবণ (সলিউশন)",
            "INJECTION_SUSPENSION": "ইনজেকশন সাসপেনশন",
            "INJECTION_POWDER_LYOPHILIZED": "লাইওফিলাইজড পাউডার ইনজেকশন",
            "INFUSION_IV": "ইন্ট্রাভেনাস ইনফিউশন",
        }
        return bn_map.get(form_code, form_code)

    @staticmethod
    def _translate_container(container_code: str) -> str:
        bn_map = {
            "VIAL_SINGLE_DOSE": "একক-ডোজ ভায়াল",
            "VIAL_MULTI_DOSE": "মাল্টি-ডোজ ভায়াল",
            "AMPOULE_GLASS": "কাঁচের অ্যাম্পুল",
            "PREFILLED_SYRINGE": "প্রি-ফিল্ড সিরিঞ্জ",
            "INFUSION_BOTTLE": "ইনফিউশন বোতল / ব্যাগ",
            "CARTRIDGE": "কার্টিজ (ইনসুলিন / ডেন্টাল)",
        }
        return bn_map.get(container_code, container_code)
