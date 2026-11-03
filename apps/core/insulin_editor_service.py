"""
backend/apps/core/insulin_editor_service.py
Architecture Decision: ADR-0145 (P145).
Dosage-Form Specific Editor Service for Insulin Preparations.
Governs standardized concentration (IU/mL), delivery device dimensions (total IU = concentration * volume),
unopened refrigeration (2°C - 8°C), in-use room-temperature stability (14 - 56 days),
and cold-chain temperature excursion protocols.
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
    InsulinActionCategory,
    InsulinDeliveryDevice,
    InsulinExcursionAction,
    InsulinFormConfig,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.insulin_editor")


class InsulinEditorService:
    """
    Core domain service managing the Insulin Formulation & Dimension Editor.
    Enforces standardized IU/mL concentrations, mathematical dimension consistency,
    in-use room-temperature shelf-life bounds, and cold-chain temperature excursion handling.
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

    ALLOWED_CONCENTRATIONS = [100, 200, 300, 500]
    ALLOWED_ACTION_CATEGORIES = [c.value for c in InsulinActionCategory]
    ALLOWED_DELIVERY_DEVICES = [c.value for c in InsulinDeliveryDevice]
    ALLOWED_EXCURSION_ACTIONS = [c.value for c in InsulinExcursionAction]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """
        Returns dynamic schema and validation rules for insulin formulations and delivery devices.
        """
        return {
            "schema_version": "1.0",
            "dosage_form_family": "INSULIN_BIOPHARMA",
            "supported_action_categories": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_action(c.value)}
                for c in InsulinActionCategory
            ],
            "supported_delivery_devices": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_device(c.value)}
                for c in InsulinDeliveryDevice
            ],
            "supported_concentrations": [
                {"value": 100, "label": "U-100 (100 IU/mL)"},
                {"value": 200, "label": "U-200 (200 IU/mL)"},
                {"value": 300, "label": "U-300 (300 IU/mL)"},
                {"value": 500, "label": "U-500 (500 IU/mL)"},
            ],
            "sections": [
                {
                    "section_id": "concentration_and_dimensions",
                    "title_en": "Concentration & Total Units Dimension",
                    "title_bn": "ঘনমাত্রা ও মোট ইনসুলিন ইউনিট",
                    "fields": [
                        {
                            "field_key": "action_category",
                            "widget": "dropdown_select",
                            "label_en": "Action Profile",
                            "label_bn": "কার্যকারিতার ধরন",
                            "is_required": True,
                            "options": [{"value": c.value, "label": c.label} for c in InsulinActionCategory],
                            "default": InsulinActionCategory.INTERMEDIATE_NPH.value,
                        },
                        {
                            "field_key": "delivery_device",
                            "widget": "dropdown_select",
                            "label_en": "Delivery Presentation",
                            "label_bn": "ডেলিভারি প্রেজেন্টেশন",
                            "is_required": True,
                            "options": [{"value": c.value, "label": c.label} for c in InsulinDeliveryDevice],
                            "default": InsulinDeliveryDevice.PEN_DISPOSABLE.value,
                        },
                        {
                            "field_key": "concentration_iu_per_ml",
                            "widget": "dropdown_select",
                            "label_en": "Strength Concentration (IU/mL)",
                            "label_bn": "ঘনমাত্রা (আইইউ/মি.লি.)",
                            "is_required": True,
                            "options": [{"value": u, "label": f"{u} IU/mL"} for u in cls.ALLOWED_CONCENTRATIONS],
                            "default": 100,
                        },
                        {
                            "field_key": "volume_ml",
                            "widget": "decimal_input",
                            "label_en": "Container Volume (mL)",
                            "label_bn": "কনটেইনারের আয়তন (মি.লি.)",
                            "is_required": True,
                            "min_value": 0.5,
                            "default": 3.0,
                        },
                        {
                            "field_key": "total_units",
                            "widget": "number_input",
                            "label_en": "Total Base Units (IU)",
                            "label_bn": "মোট সক্রিয় ইউনিট (আইইউ)",
                            "is_required": True,
                            "min_value": 50,
                            "default": 300,
                        },
                    ],
                },
                {
                    "section_id": "storage_and_in_use_stability",
                    "title_en": "Refrigeration & In-Use Stability",
                    "title_bn": "রেফ্রিজারেশন ও ইন-ইউজ স্থায়িত্বকাল",
                    "fields": [
                        {
                            "field_key": "unopened_min_temp_celsius",
                            "widget": "decimal_input",
                            "label_en": "Unopened Min Temp (°C)",
                            "label_bn": "সর্বনিম্ন তাপমাত্রা (২°সে)",
                            "is_required": False,
                            "default": 2.0,
                        },
                        {
                            "field_key": "unopened_max_temp_celsius",
                            "widget": "decimal_input",
                            "label_en": "Unopened Max Temp (°C)",
                            "label_bn": "সর্বোচ্চ তাপমাত্রা (৮°সে)",
                            "is_required": False,
                            "default": 8.0,
                        },
                        {
                            "field_key": "in_use_shelf_life_days",
                            "widget": "number_stepper",
                            "label_en": "In-Use Room Temp Stability (Days)",
                            "label_bn": "ব্যবহারকালীন রুম টেম্পারেচার মেয়াদ (দিন)",
                            "is_required": True,
                            "min_value": 14,
                            "max_value": 56,
                            "default": 28,
                        },
                        {
                            "field_key": "in_use_max_temp_celsius",
                            "widget": "decimal_input",
                            "label_en": "Max Room Temp (°C)",
                            "label_bn": "সর্বোচ্চ রুম টেম্পারেচার (৩০°সে)",
                            "is_required": False,
                            "default": 30.0,
                        },
                    ],
                },
                {
                    "section_id": "cold_chain_excursion_protocol",
                    "title_en": "Cold-Chain Excursion Actions",
                    "title_bn": "তাপমাত্রা বিচ্যুতির কর্মপরিকল্পনা",
                    "fields": [
                        {
                            "field_key": "excursion_action",
                            "widget": "dropdown_select",
                            "label_en": "Breach / Excursion Protocol",
                            "label_bn": "কোল্ড চেইন নষ্ট হলে করণীয়",
                            "is_required": True,
                            "options": [{"value": a.value, "label": a.label} for a in InsulinExcursionAction],
                            "default": InsulinExcursionAction.QUARANTINE_FOR_PHARMACIST_ASSESSMENT.value,
                        },
                        {
                            "field_key": "patient_counseling_en",
                            "widget": "textarea",
                            "label_en": "Patient Counseling Advice (EN)",
                            "label_bn": "রোগীর পরামর্শ (ইংরেজি)",
                            "is_required": False,
                            "default": "Store unopened pens in refrigerator (2°C - 8°C). Never freeze. In-use pen may be kept at room temperature below 30°C for up to 28 days.",
                        },
                        {
                            "field_key": "patient_counseling_bn",
                            "widget": "textarea",
                            "label_en": "Patient Counseling Advice (BN)",
                            "label_bn": "রোগীর পরামর্শ (বাংলা)",
                            "is_required": False,
                            "default": "অব্যবহৃত ইনসুলিন ফ্রিজের নরমালে (২-৮ ডিগ্রি) রাখুন, কখনোই ডিপ ফ্রিজে রাখবেন না। ব্যবহারের সময় রুমের তাপমাত্রায় (সর্বোচ্চ ৩০ ডিগ্রি) ২৮ দিন রাখা যাবে।",
                        },
                    ],
                },
            ],
            "hidden_fields": cls.HIDDEN_FIELDS,
            "business_rules": [
                {
                    "rule_id": "BR-INS-01",
                    "name": "Dimension Consistency",
                    "description": "Total units must equal concentration (IU/mL) multiplied by volume (mL). Example: 100 IU/mL * 3 mL = 300 IU.",
                },
                {
                    "rule_id": "BR-INS-02",
                    "name": "Standardized Concentration",
                    "description": "Concentration must be one of the standard biological strengths: U-100, U-200, U-300, or U-500 IU/mL.",
                },
                {
                    "rule_id": "BR-INS-03",
                    "name": "In-Use Stability Window",
                    "description": "Permitted in-use room-temperature stability must be between 14 and 56 days (default 28 days).",
                },
                {
                    "rule_id": "BR-INS-04",
                    "name": "Freezing Disposal Mandatory",
                    "description": "Frozen insulin is irreversibly denatured and cannot be dispensed. Excursion policy must mandate quarantine or disposal.",
                },
                {
                    "rule_id": "BR-INS-05",
                    "name": "Solid Field Isolation",
                    "description": "Solid oral tablet/strip attributes are forbidden for insulin editors.",
                },
            ],
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates insulin formulation payload against biopharmaceutical and dimension consistency rules.
        """
        if not isinstance(payload, dict):
            raise ValidationError({"error_code": "ERR_INVALID_PAYLOAD", "detail": "Payload must be a JSON object."})

        # 1. Zero-Trust Hidden Field Guard (Reject solid oral tablet/strip fields)
        for field in cls.HIDDEN_FIELDS:
            val = payload.get(field)
            if val is not None and val != "":
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is a solid oral packaging attribute and prohibited for insulin forms.",
                })

        # 2. Required Fields
        required_fields = [
            "action_category",
            "delivery_device",
            "concentration_iu_per_ml",
            "volume_ml",
            "total_units",
            "in_use_shelf_life_days",
            "excursion_action",
        ]
        for req in required_fields:
            if req not in payload or payload[req] is None or payload[req] == "":
                raise ValidationError({
                    "error_code": "ERR_REQUIRED_FIELD_MISSING",
                    "detail": f"Missing mandatory field '{req}' for insulin configuration.",
                })

        # 3. Action Category Validation
        action_category = str(payload.get("action_category")).strip().upper()
        if action_category not in cls.ALLOWED_ACTION_CATEGORIES:
            raise ValidationError({
                "error_code": "ERR_INVALID_ACTION_CATEGORY",
                "detail": f"Action category '{action_category}' is invalid. Allowed: {cls.ALLOWED_ACTION_CATEGORIES}.",
            })

        # 4. Delivery Device Validation
        delivery_device = str(payload.get("delivery_device")).strip().upper()
        if delivery_device not in cls.ALLOWED_DELIVERY_DEVICES:
            raise ValidationError({
                "error_code": "ERR_INVALID_DELIVERY_DEVICE",
                "detail": f"Delivery device '{delivery_device}' is invalid. Allowed: {cls.ALLOWED_DELIVERY_DEVICES}.",
            })

        # 5. Standardized Concentration Validation (100, 200, 300, 500)
        try:
            concentration = int(payload.get("concentration_iu_per_ml"))
            if concentration not in cls.ALLOWED_CONCENTRATIONS:
                raise ValidationError({
                    "error_code": "ERR_INVALID_INSULIN_CONCENTRATION",
                    "detail": f"Concentration '{concentration}' IU/mL is invalid. Standard strengths: {cls.ALLOWED_CONCENTRATIONS}.",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_INSULIN_CONCENTRATION",
                "detail": f"concentration_iu_per_ml must be one of: {cls.ALLOWED_CONCENTRATIONS}.",
            })

        # 6. Volume Numeric Validation
        try:
            volume_ml = Decimal(str(payload.get("volume_ml")))
            if volume_ml <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_VOLUME",
                    "detail": "volume_ml must be greater than 0 mL.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_VOLUME",
                "detail": "volume_ml must be a valid numeric decimal.",
            })

        # 7. Total Units & Mathematical Dimension Consistency
        try:
            total_units = int(payload.get("total_units"))
            if total_units <= 0:
                raise ValidationError({
                    "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                    "detail": "total_units must be a positive integer.",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                "detail": "total_units must be a valid positive integer.",
            })

        expected_units = int(round(Decimal(str(concentration)) * volume_ml))
        if total_units != expected_units:
            raise ValidationError({
                "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                "detail": (
                    f"Dimension consistency mismatch: total_units ({total_units} IU) does not equal "
                    f"concentration ({concentration} IU/mL) * volume ({volume_ml} mL) = {expected_units} IU."
                ),
            })

        # 8. In-Use Shelf Life Days (14 - 56 days)
        try:
            in_use_shelf_life_days = int(payload.get("in_use_shelf_life_days"))
            if in_use_shelf_life_days < 14 or in_use_shelf_life_days > 56:
                raise ValidationError({
                    "error_code": "ERR_INVALID_IN_USE_SHELF_LIFE",
                    "detail": f"in_use_shelf_life_days ({in_use_shelf_life_days}) must be between 14 and 56 days.",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_IN_USE_SHELF_LIFE",
                "detail": "in_use_shelf_life_days must be an integer between 14 and 56.",
            })

        # 9. Excursion Action Validation
        excursion_action = str(payload.get("excursion_action")).strip().upper()
        if excursion_action not in cls.ALLOWED_EXCURSION_ACTIONS:
            raise ValidationError({
                "error_code": "ERR_INVALID_EXCURSION_ACTION",
                "detail": f"excursion_action '{excursion_action}' is invalid. Allowed: {cls.ALLOWED_EXCURSION_ACTIONS}.",
            })

        # 10. Temperatures
        unopened_min = Decimal("2.00")
        unopened_max = Decimal("8.00")
        in_use_max = Decimal("30.00")

        if payload.get("unopened_min_temp_celsius") is not None:
            try:
                unopened_min = Decimal(str(payload.get("unopened_min_temp_celsius")))
            except (InvalidOperation, TypeError):
                unopened_min = Decimal("2.00")

        if payload.get("unopened_max_temp_celsius") is not None:
            try:
                unopened_max = Decimal(str(payload.get("unopened_max_temp_celsius")))
            except (InvalidOperation, TypeError):
                unopened_max = Decimal("8.00")

        if payload.get("in_use_max_temp_celsius") is not None:
            try:
                in_use_max = Decimal(str(payload.get("in_use_max_temp_celsius")))
            except (InvalidOperation, TypeError):
                in_use_max = Decimal("30.00")

        if unopened_min >= unopened_max:
            raise ValidationError({
                "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                "detail": f"unopened_min_temp_celsius ({unopened_min}°C) must be strictly less than unopened_max_temp_celsius ({unopened_max}°C).",
            })

        patient_counseling_en = str(payload.get("patient_counseling_en", "")).strip()
        patient_counseling_bn = str(payload.get("patient_counseling_bn", "")).strip()

        return {
            "action_category": action_category,
            "delivery_device": delivery_device,
            "concentration_iu_per_ml": concentration,
            "volume_ml": volume_ml,
            "total_units": total_units,
            "in_use_shelf_life_days": in_use_shelf_life_days,
            "unopened_min_temp_celsius": unopened_min,
            "unopened_max_temp_celsius": unopened_max,
            "in_use_max_temp_celsius": in_use_max,
            "excursion_action": excursion_action,
            "patient_counseling_en": patient_counseling_en,
            "patient_counseling_bn": patient_counseling_bn,
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
        Lists accessible insulin configurations with multi-tenant and global fallback filtering.
        """
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = InsulinFormConfig.objects.select_related("product", "tenant").all()

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
        Retrieves a single insulin configuration.
        """
        cfg = InsulinFormConfig.objects.select_related("product", "tenant").filter(config_id=config_id).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """
        Creates or updates an insulin formulation configuration.
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
                    "detail": "Cannot configure insulin settings for another tenant.",
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
            cfg = InsulinFormConfig.objects.filter(config_id=given_config_id).first()
        if not cfg:
            cfg = InsulinFormConfig.objects.filter(product=product, tenant=tenant_obj).first()

        if cfg:
            cfg.action_category = validated_data["action_category"]
            cfg.delivery_device = validated_data["delivery_device"]
            cfg.concentration_iu_per_ml = validated_data["concentration_iu_per_ml"]
            cfg.volume_ml = validated_data["volume_ml"]
            cfg.total_units = validated_data["total_units"]
            cfg.in_use_shelf_life_days = validated_data["in_use_shelf_life_days"]
            cfg.unopened_min_temp_celsius = validated_data["unopened_min_temp_celsius"]
            cfg.unopened_max_temp_celsius = validated_data["unopened_max_temp_celsius"]
            cfg.in_use_max_temp_celsius = validated_data["in_use_max_temp_celsius"]
            cfg.excursion_action = validated_data["excursion_action"]
            cfg.patient_counseling_en = validated_data["patient_counseling_en"]
            cfg.patient_counseling_bn = validated_data["patient_counseling_bn"]
            cfg.is_active = validated_data["is_active"]
            cfg.save()
        else:
            config_id = given_config_id or f"ins-{product.product_id}"
            if target_tenant_id and not config_id.endswith(target_tenant_id):
                config_id = f"{config_id}-{target_tenant_id}"
            cfg = InsulinFormConfig.objects.create(
                config_id=config_id,
                product=product,
                tenant=tenant_obj,
                action_category=validated_data["action_category"],
                delivery_device=validated_data["delivery_device"],
                concentration_iu_per_ml=validated_data["concentration_iu_per_ml"],
                volume_ml=validated_data["volume_ml"],
                total_units=validated_data["total_units"],
                in_use_shelf_life_days=validated_data["in_use_shelf_life_days"],
                unopened_min_temp_celsius=validated_data["unopened_min_temp_celsius"],
                unopened_max_temp_celsius=validated_data["unopened_max_temp_celsius"],
                in_use_max_temp_celsius=validated_data["in_use_max_temp_celsius"],
                excursion_action=validated_data["excursion_action"],
                patient_counseling_en=validated_data["patient_counseling_en"],
                patient_counseling_bn=validated_data["patient_counseling_bn"],
                is_active=validated_data["is_active"],
            )

        # Audit logging
        user_id_val = str(getattr(user, "user_id", user.pk))
        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=user_id_val,
            actor_email=user.email or "system@pharmacy.local",
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="INSULIN_CONFIG_UPSERTED",
            resource_type="InsulinFormConfig",
            resource_id=cfg.config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "details": (
                    f"Upserted insulin config for product {product.product_id} "
                    f"({cfg.delivery_device} {cfg.concentration_iu_per_ml}IU/mL [{cfg.total_units}IU in {cfg.volume_ml}mL])."
                )
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """
        Deletes an insulin configuration.
        """
        cfg = InsulinFormConfig.objects.filter(config_id=config_id).first()
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
            action="INSULIN_CONFIG_DELETED",
            resource_type="InsulinFormConfig",
            resource_id=config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted insulin config for product {product_id}."},
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
                "message_en": "Cashiers are not permitted to modify insulin configurations.",
                "message_bn": "ক্যাশিয়ারদের ইনসুলিন কনফিগারেশন পরিবর্তন করার অনুমতি নেই।",
            })

        if tenant_id and tenant_id != user_tenant:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_VIOLATION",
                "message_en": "Access to another tenant's insulin configuration is prohibited.",
                "message_bn": "অন্য ফার্মেসির ইনসুলিন কনফিগারেশনে অ্যাক্সেস নিষিদ্ধ।",
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
                "message_en": "Custom insulin configuration requires a STANDARD or PRO subscription plan.",
                "message_bn": "কাস্টম ইনসুলিন কনফিগারেশনের জন্য স্ট্যান্ডার্ড বা প্রো সাবস্ক্রিপশন আবশ্যক।",
            })

    @classmethod
    def _serialize_config(cls, cfg: InsulinFormConfig) -> Dict[str, Any]:
        return {
            "config_id": cfg.config_id,
            "product_id": cfg.product_id,
            "brand_name": getattr(cfg.product, "brand_name", ""),
            "generic_name": getattr(cfg.product, "generic_name", ""),
            "tenant_id": cfg.tenant_id,
            "action_category": cfg.action_category,
            "delivery_device": cfg.delivery_device,
            "concentration_iu_per_ml": cfg.concentration_iu_per_ml,
            "volume_ml": float(cfg.volume_ml),
            "total_units": cfg.total_units,
            "in_use_shelf_life_days": cfg.in_use_shelf_life_days,
            "unopened_min_temp_celsius": float(cfg.unopened_min_temp_celsius),
            "unopened_max_temp_celsius": float(cfg.unopened_max_temp_celsius),
            "in_use_max_temp_celsius": float(cfg.in_use_max_temp_celsius),
            "excursion_action": cfg.excursion_action,
            "patient_counseling_en": cfg.patient_counseling_en,
            "patient_counseling_bn": cfg.patient_counseling_bn,
            "is_active": cfg.is_active,
            "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @staticmethod
    def _translate_action(action_code: str) -> str:
        bn_map = {
            "RAPID_ACTING": "র‌্যাপিড-অ্যাক্টিং (অ্যাসপার্ট / লিসপ্রো / গুলিসিন)",
            "SHORT_ACTING_REGULAR": "শর্ট-অ্যাক্টিং রেগুলার (ক্রিস্টালাইন হিউম্যান)",
            "INTERMEDIATE_NPH": "ইন্টারমিডিয়েট এনপিএইচ (আইসোফেন)",
            "LONG_ACTING_ANALOG": "লং-অ্যাক্টিং অ্যানালগ (গ্লার্জিন / ডিটেমির / ডেগ্লুডেক)",
            "PREMIXED_BIPHASIC": "প্রিমিক্সড বাইফ্যাসিক (৭০/৩০, ৫০/৫০)",
        }
        return bn_map.get(action_code, action_code)

    @staticmethod
    def _translate_device(device_code: str) -> str:
        bn_map = {
            "PEN_DISPOSABLE": "ডিসপোজেবল পেন (৩ মি.লি.)",
            "CARTRIDGE_REFILL": "পেন কার্টিজ রিফিল (৩ মি.লি.)",
            "VIAL_MULTI_DOSE": "মাল্টি-ডোজ ভায়াল (১০ মি.লি.)",
            "PUMP_RESERVOIR": "ইনসুলিন পাম্প রিজার্ভার",
        }
        return bn_map.get(device_code, device_code)

