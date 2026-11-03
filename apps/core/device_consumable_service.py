"""
backend/apps/core/device_consumable_service.py
Architecture Decision: ADR-0151 (P151).
Medical Device, Needles, Diagnostic Test Strips, and Consumables Editor Service.
Provides physical and compatibility governance without medicine-only restrictions.
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
    DeviceCategory,
    DeviceConsumableConfig,
    MedicineProduct,
    NeedleGaugeChoice,
    RegulatoryDeviceClass,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.device_consumable_service")


class DeviceConsumableService:
    """
    Core domain service managing the Device and Consumable Editor.
    Governs needle gauge/length, diagnostic strip meter compatibility, sterility,
    single-use status, and rejects medicine-only constraints.
    """

    MEDICINE_ONLY_FORBIDDEN_FIELDS = [
        "dosage_form",
        "is_antibiotic",
        "pom_required",
        "prescription_required",
        "narcotic_schedule",
        "active_ingredient",
        "active_ingredient_concentration",
        "dgda_antibiotic_register",
        "coating_type",
        "scored_tablet",
    ]

    ALLOWED_CATEGORIES = [c.value for c in DeviceCategory]
    ALLOWED_GAUGES = [c.value for c in NeedleGaugeChoice]
    ALLOWED_REGULATORY_CLASSES = [c.value for c in RegulatoryDeviceClass]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and validation rules for device/consumable editor."""
        return {
            "schema_version": "1.0",
            "editor_type": "DEVICE_AND_CONSUMABLE_EDITOR",
            "supported_categories": [
                {
                    "code": c.value,
                    "label_en": c.label,
                    "label_bn": cls._translate_category(c.value),
                }
                for c in DeviceCategory
            ],
            "supported_needle_gauges": [
                {"code": c.value, "label": c.label} for c in NeedleGaugeChoice
            ],
            "supported_regulatory_classes": [
                {
                    "code": c.value,
                    "label_en": c.label,
                    "label_bn": cls._translate_regulatory_class(c.value),
                }
                for c in RegulatoryDeviceClass
            ],
            "forbidden_medicine_fields": cls.MEDICINE_ONLY_FORBIDDEN_FIELDS,
            "min_temperature_celsius": -20.0,
            "max_temperature_celsius": 60.0,
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates medical device or consumable payload.
        Strictly rejects pharmaceutical drug attributes and validates device dimensions.
        """
        # 1. Zero-tolerance rejection of medicine-only fields
        for field in cls.MEDICINE_ONLY_FORBIDDEN_FIELDS:
            if field in payload and payload.get(field) is not None:
                raise ValidationError({
                    "error_code": "ERR_MEDICINE_FIELD_REJECTED",
                    "detail": f"Medicine-only field '{field}' is illegal for medical device/consumable catalog.",
                    "field": field,
                })

        # 2. Product ID validation
        product_id = payload.get("product_id")
        if not product_id or not str(product_id).strip():
            raise ValidationError({
                "error_code": "ERR_MISSING_PRODUCT_ID",
                "detail": "product_id is mandatory.",
                "field": "product_id",
            })

        # 3. Category validation
        category = payload.get("device_category", DeviceCategory.GENERAL_CONSUMABLE)
        if category not in cls.ALLOWED_CATEGORIES:
            raise ValidationError({
                "error_code": "ERR_INVALID_DEVICE_CATEGORY",
                "detail": f"Invalid device_category: {category}. Allowed: {cls.ALLOWED_CATEGORIES}",
                "field": "device_category",
            })

        # 4. Brand Name
        brand_name = payload.get("brand_name", "")
        if not brand_name or len(str(brand_name).strip()) < 2:
            raise ValidationError({
                "error_code": "ERR_INVALID_BRAND_NAME",
                "detail": "brand_name is mandatory and must be at least 2 characters.",
                "field": "brand_name",
            })

        # 5. Needle-specific validation
        needle_gauge = payload.get("needle_gauge", NeedleGaugeChoice.NA)
        needle_length = payload.get("needle_length_mm", 0.0)

        try:
            needle_length_dec = Decimal(str(needle_length))
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_NEEDLE_LENGTH",
                "detail": "needle_length_mm must be a valid number.",
                "field": "needle_length_mm",
            })

        if category == DeviceCategory.NEEDLE_OR_SYRINGE:
            if needle_gauge not in cls.ALLOWED_GAUGES or needle_gauge == NeedleGaugeChoice.NA:
                raise ValidationError({
                    "error_code": "ERR_INVALID_NEEDLE_GAUGE",
                    "detail": "Needles/syringes must specify an exact gauge (e.g., 21G, 25G, 30G, 31G).",
                    "field": "needle_gauge",
                })
            if needle_length_dec <= Decimal("0.00"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_NEEDLE_LENGTH",
                    "detail": "Needle length in mm must be greater than 0.",
                    "field": "needle_length_mm",
                })

        # 6. Diagnostic Test Strip specific validation
        meter_compat = payload.get("meter_compatibility", "")
        strip_count = payload.get("strip_count_per_pack", 0)

        if category == DeviceCategory.DIAGNOSTIC_TEST_STRIP:
            if not meter_compat or not str(meter_compat).strip():
                raise ValidationError({
                    "error_code": "ERR_MISSING_METER_COMPATIBILITY",
                    "detail": "Diagnostic test strips must specify meter compatibility (e.g., Accu-Chek Instant).",
                    "field": "meter_compatibility",
                })
            try:
                strip_count_int = int(strip_count)
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_STRIP_COUNT",
                    "detail": "strip_count_per_pack must be an integer.",
                    "field": "strip_count_per_pack",
                })
            if strip_count_int < 1:
                raise ValidationError({
                    "error_code": "ERR_INVALID_STRIP_COUNT",
                    "detail": "Diagnostic test strips must have at least 1 strip per pack.",
                    "field": "strip_count_per_pack",
                })

        # 7. Temperature range validation
        temp_min = payload.get("storage_temperature_celsius_min", 2.0)
        temp_max = payload.get("storage_temperature_celsius_max", 30.0)
        try:
            temp_min_dec = Decimal(str(temp_min))
            temp_max_dec = Decimal(str(temp_max))
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_TEMPERATURE",
                "detail": "Storage temperatures must be decimal numbers.",
                "field": "storage_temperature_celsius",
            })

        if temp_min_dec > temp_max_dec:
            raise ValidationError({
                "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                "detail": f"storage_temperature_celsius_min ({temp_min_dec}) cannot exceed max ({temp_max_dec}).",
                "field": "storage_temperature_celsius_min",
            })

        return {
            "product_id": str(product_id).strip(),
            "device_category": category,
            "brand_name": str(brand_name).strip(),
            "model_number": str(payload.get("model_number", "")).strip(),
            "needle_gauge": needle_gauge,
            "needle_length_mm": float(needle_length_dec),
            "meter_compatibility": str(meter_compat).strip(),
            "strip_count_per_pack": int(payload.get("strip_count_per_pack", 0)),
            "sample_volume_microliter": float(payload.get("sample_volume_microliter", 0.0)),
            "test_time_seconds": int(payload.get("test_time_seconds", 0)),
            "is_sterile": bool(payload.get("is_sterile", False)),
            "is_single_use": bool(payload.get("is_single_use", True)),
            "regulatory_device_class": payload.get("regulatory_device_class", RegulatoryDeviceClass.CLASS_A_LOW_RISK),
            "storage_temperature_celsius_min": float(temp_min_dec),
            "storage_temperature_celsius_max": float(temp_max_dec),
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    def _resolve_tenant(cls, user: PharmacyUser, payload_tenant_id: Optional[str] = None) -> Optional[Tenant]:
        tenant_id = payload_tenant_id or getattr(user, "tenant_id", None)
        if tenant_id:
            return Tenant.objects.filter(tenant_id=tenant_id).first()
        return None

    @classmethod
    def check_permissions(cls, user: PharmacyUser, required_action: str = "read") -> None:
        """Enforces multi-tenant RBAC policies for device and consumable catalog operations."""
        if user.is_superuser:
            return

        if required_action in ["write", "delete"]:
            if getattr(user, "role", None) in [UserRole.CASHIER]:
                raise PermissionDenied({
                    "error_code": "ERR_ROLE_UNAUTHORIZED",
                    "detail": "Cashier role has read-only access to device & consumable catalog.",
                })

        # Tenant lifecycle & suspension validation
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
    @transaction.atomic
    def create_or_update_config(
        cls,
        payload: Dict[str, Any],
        user: PharmacyUser,
        target_config_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates or updates a medical device/consumable configuration record."""
        cls.check_permissions(user, required_action="write")
        validated = cls.validate_payload(payload)

        product_id = validated["product_id"]
        tenant: Optional[Tenant] = cls._resolve_tenant(user)

        try:
            product = MedicineProduct.objects.get(pk=product_id)
        except MedicineProduct.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Product with ID '{product_id}' does not exist.",
            })

        # Tenant isolation
        prod_tenant_id = getattr(product, "tenant_id", None)
        if prod_tenant_id and tenant and str(prod_tenant_id) != str(tenant.tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cannot configure device for a product belonging to another tenant.",
            })

        config: Optional[DeviceConsumableConfig] = None
        if target_config_id:
            try:
                config = DeviceConsumableConfig.objects.get(pk=target_config_id)
            except DeviceConsumableConfig.DoesNotExist:
                raise ValidationError({
                    "error_code": "ERR_CONFIG_NOT_FOUND",
                    "detail": f"DeviceConsumableConfig with ID '{target_config_id}' not found.",
                })
            if config.tenant_id and tenant and str(config.tenant_id) != str(tenant.tenant_id) and not user.is_superuser:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                    "detail": "Cross-tenant modification prohibited.",
                })
        else:
            # Check existing for product + tenant
            q = Q(product=product)
            if tenant:
                q &= Q(tenant=tenant)
            else:
                q &= Q(tenant__isnull=True)
            config = DeviceConsumableConfig.objects.filter(q).first()

        created = False
        if not config:
            config = DeviceConsumableConfig(
                product=product,
                tenant=tenant,
            )
            created = True

        config.device_category = validated["device_category"]
        config.brand_name = validated["brand_name"]
        config.model_number = validated["model_number"]
        config.needle_gauge = validated["needle_gauge"]
        config.needle_length_mm = Decimal(str(validated["needle_length_mm"]))
        config.meter_compatibility = validated["meter_compatibility"]
        config.strip_count_per_pack = validated["strip_count_per_pack"]
        config.sample_volume_microliter = Decimal(str(validated["sample_volume_microliter"]))
        config.test_time_seconds = validated["test_time_seconds"]
        config.is_sterile = validated["is_sterile"]
        config.is_single_use = validated["is_single_use"]
        config.regulatory_device_class = validated["regulatory_device_class"]
        config.storage_temperature_celsius_min = Decimal(str(validated["storage_temperature_celsius_min"]))
        config.storage_temperature_celsius_max = Decimal(str(validated["storage_temperature_celsius_max"]))
        config.is_active = validated["is_active"]

        config.full_clean()
        config.save()

        # Audit log entry
        action = "DEVICE_CONFIG_CREATED" if created else "DEVICE_CONFIG_UPDATED"
        AuditLogEntry.objects.create(
            tenant_id=getattr(tenant, "tenant_id", "PLATFORM") if tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action=action,
            resource_type="DeviceConsumableConfig",
            resource_id=str(config.id),
            metadata_json={
                "brand_name": config.brand_name,
                "device_category": config.device_category,
                "needle_gauge": config.needle_gauge,
                "is_sterile": config.is_sterile,
            },
            status=AuditActionStatus.SUCCESS,
        )

        return cls._serialize_config(config)

    @classmethod
    def get_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """Retrieves a single device/consumable configuration."""
        cls.check_permissions(user, required_action="read")
        try:
            config = DeviceConsumableConfig.objects.select_related("product", "tenant").get(pk=config_id)
        except DeviceConsumableConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"DeviceConsumableConfig '{config_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        if config.tenant_id and user_tenant_id and str(config.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant access prohibited.",
            })

        return cls._serialize_config(config)

    @classmethod
    def list_configs(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        device_category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists device/consumable configs scoped to tenant or global."""
        cls.check_permissions(user, required_action="read")
        qs = DeviceConsumableConfig.objects.select_related("product", "tenant").filter(is_active=True)

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
        if device_category:
            qs = qs.filter(device_category=device_category)

        return [cls._serialize_config(c) for c in qs]

    @classmethod
    @transaction.atomic
    def delete_or_archive_config(cls, config_id: str, user: PharmacyUser, hard_delete: bool = False) -> Dict[str, Any]:
        """Archives or deletes a device/consumable configuration."""
        cls.check_permissions(user, required_action="delete")
        try:
            config = DeviceConsumableConfig.objects.get(pk=config_id)
        except DeviceConsumableConfig.DoesNotExist:
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"DeviceConsumableConfig '{config_id}' not found.",
            })

        user_tenant_id = getattr(user, "tenant_id", None)
        tenant = cls._resolve_tenant(user)
        if config.tenant_id and user_tenant_id and str(config.tenant_id) != str(user_tenant_id) and not user.is_superuser:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_ACCESS_DENIED",
                "detail": "Cross-tenant deletion prohibited.",
            })

        if hard_delete:
            config.delete()
            status_msg = "DELETED"
        else:
            config.is_active = False
            config.save(update_fields=["is_active", "updated_at"])
            status_msg = "ARCHIVED"

        AuditLogEntry.objects.create(
            tenant_id=getattr(tenant, "tenant_id", "PLATFORM") if tenant else "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="DEVICE_CONFIG_REMOVED",
            resource_type="DeviceConsumableConfig",
            resource_id=str(config_id),
            metadata_json={"status": status_msg},
            status=AuditActionStatus.SUCCESS,
        )

        return {"status": status_msg, "config_id": config_id}

    @classmethod
    def _serialize_config(cls, config: DeviceConsumableConfig) -> Dict[str, Any]:
        """Converts model instance to JSON dictionary."""
        return {
            "id": str(config.id),
            "product_id": str(config.product_id),
            "tenant_id": str(config.tenant_id) if config.tenant_id else None,
            "device_category": config.device_category,
            "device_category_display_en": config.get_device_category_display(),
            "device_category_display_bn": cls._translate_category(config.device_category),
            "brand_name": config.brand_name,
            "model_number": config.model_number,
            "needle_gauge": config.needle_gauge,
            "needle_length_mm": float(config.needle_length_mm),
            "meter_compatibility": config.meter_compatibility,
            "strip_count_per_pack": config.strip_count_per_pack,
            "sample_volume_microliter": float(config.sample_volume_microliter),
            "test_time_seconds": config.test_time_seconds,
            "is_sterile": config.is_sterile,
            "is_single_use": config.is_single_use,
            "regulatory_device_class": config.regulatory_device_class,
            "regulatory_device_class_display_bn": cls._translate_regulatory_class(config.regulatory_device_class),
            "storage_temperature_celsius_min": float(config.storage_temperature_celsius_min),
            "storage_temperature_celsius_max": float(config.storage_temperature_celsius_max),
            "is_active": config.is_active,
            "created_at": config.created_at.isoformat(),
            "updated_at": config.updated_at.isoformat(),
        }

    @staticmethod
    def _translate_category(code: str) -> str:
        translations = {
            "NEEDLE_OR_SYRINGE": "সূঁচ বা সিরিঞ্জ",
            "DIAGNOSTIC_TEST_STRIP": "ডায়াগনস্টিক টেস্ট স্ট্রিপ",
            "MONITORING_DEVICE": "পর্যবেক্ষণ বা মনিটরিং ডিভাইস",
            "SURGICAL_CONSUMABLE": "সার্জিক্যাল কনজিউমেবল",
            "RESPIRATORY_DEVICE": "শ্বাসযন্ত্রের ডিভাইস",
            "GENERAL_CONSUMABLE": "সাধারণ মেডিকেল কনজিউমেবল",
        }
        return translations.get(code, code)

    @staticmethod
    def _translate_regulatory_class(code: str) -> str:
        translations = {
            "CLASS_A_LOW_RISK": "ক্লাস এ (কম ঝুঁকি)",
            "CLASS_B_LOW_MODERATE": "ক্লাস বি (কম-মাঝারি ঝুঁকি)",
            "CLASS_C_MODERATE_HIGH": "ক্লাস সি (মাঝারি-উচ্চ ঝুঁকি)",
            "CLASS_D_HIGH_RISK": "ক্লাস ডি (উচ্চ ঝুঁকি)",
            "NON_REGULATED": "অ-নিয়ন্ত্রিত খুচরা সামগ্রী",
        }
        return translations.get(code, code)
