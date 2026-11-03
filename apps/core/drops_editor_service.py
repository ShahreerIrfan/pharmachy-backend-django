"""
backend/apps/core/drops_editor_service.py
Architecture Decision: ADR-0147 (P147).
Dosage-Form Specific Editor Service for Ophthalmic, Otic, and Nasal Drops.
Governs route-safety, fill volume, concentration, opened-container shelf life (BUD),
and multi-tenant access control.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    DropsContainerType,
    DropsFormConfig,
    DropsRoute,
    DropsStorageTemp,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.drops_editor")


class DropsEditorService:
    """
    Core domain service managing the Drops Formulation & Opened-Container Editor.
    Enforces route-safety, fill volume, concentration, single-dose minim limits,
    and post-opening beyond-use dates (BUD).
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

    ALLOWED_ROUTES = [c.value for c in DropsRoute]
    ALLOWED_CONTAINER_TYPES = [c.value for c in DropsContainerType]
    ALLOWED_STORAGE_TEMPS = [c.value for c in DropsStorageTemp]
    ALLOWED_CONCENTRATION_UNITS = ["PERCENT_W_V", "MG_PER_ML", "IU_PER_ML", "MCG_PER_DOSE"]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and validation rules for drops formulations."""
        return {
            "schema_version": "1.0",
            "dosage_form_family": "DROPS_OPHTHALMIC_OTIC_NASAL",
            "supported_routes": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_route(c.value)}
                for c in DropsRoute
            ],
            "supported_container_types": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_container(c.value)}
                for c in DropsContainerType
            ],
            "supported_storage_temps": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_storage(c.value)}
                for c in DropsStorageTemp
            ],
            "supported_concentration_units": cls.ALLOWED_CONCENTRATION_UNITS,
            "default_opened_shelf_life_days": 28,
            "max_opened_shelf_life_days": 60,
            "min_opened_shelf_life_days": 1,
            "forbidden_fields": cls.HIDDEN_FIELDS,
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates drops configuration payload against clinical, sterility, and dimension rules.
        """
        # 1. Zero-trust isolation against solid oral fields
        for field in cls.HIDDEN_FIELDS:
            if field in payload and payload.get(field) is not None:
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is illegal for liquid drops dosage form.",
                    "field": field,
                })

        # 2. Product ID
        product_id = payload.get("product_id")
        if not product_id or not str(product_id).strip():
            raise ValidationError({
                "error_code": "ERR_MISSING_PRODUCT_ID",
                "detail": "product_id is mandatory.",
                "field": "product_id",
            })

        # 3. Route safety check
        route = str(payload.get("route", DropsRoute.OPHTHALMIC)).strip().upper()
        if route not in cls.ALLOWED_ROUTES:
            raise ValidationError({
                "error_code": "ERR_INVALID_DROPS_ROUTE",
                "detail": f"Invalid drops route '{route}'. Allowed: {cls.ALLOWED_ROUTES}.",
                "field": "route",
            })

        # 4. Container type check
        container_type = str(payload.get("container_type", DropsContainerType.DROPPER_BOTTLE)).strip().upper()
        if container_type not in cls.ALLOWED_CONTAINER_TYPES:
            raise ValidationError({
                "error_code": "ERR_INVALID_CONTAINER_TYPE",
                "detail": f"Invalid container_type '{container_type}'. Allowed: {cls.ALLOWED_CONTAINER_TYPES}.",
                "field": "container_type",
            })

        # 5. Volume mL validation
        try:
            volume_ml = Decimal(str(payload.get("volume_ml", "5.00")))
            if volume_ml <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_VOLUME",
                    "detail": "volume_ml must be greater than 0 mL.",
                    "field": "volume_ml",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_VOLUME",
                "detail": "volume_ml must be a valid positive numeric decimal.",
                "field": "volume_ml",
            })

        # 6. Concentration value validation
        try:
            concentration_value = Decimal(str(payload.get("concentration_value", "0.5000")))
            if concentration_value <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_CONCENTRATION",
                    "detail": "concentration_value must be greater than 0.",
                    "field": "concentration_value",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_CONCENTRATION",
                "detail": "concentration_value must be a valid numeric decimal.",
                "field": "concentration_value",
            })

        # 7. Concentration unit validation
        conc_unit = str(payload.get("concentration_unit", "PERCENT_W_V")).strip().upper()
        if conc_unit not in cls.ALLOWED_CONCENTRATION_UNITS:
            raise ValidationError({
                "error_code": "ERR_INVALID_CONCENTRATION_UNIT",
                "detail": f"Invalid concentration_unit '{conc_unit}'. Allowed: {cls.ALLOWED_CONCENTRATION_UNITS}.",
                "field": "concentration_unit",
            })

        # 8. Opened Shelf Life Days (BUD)
        try:
            opened_shelf_life_days = int(payload.get("opened_shelf_life_days", 28))
            if opened_shelf_life_days < 1 or opened_shelf_life_days > 60:
                raise ValidationError({
                    "error_code": "ERR_INVALID_OPENED_SHELF_LIFE",
                    "detail": f"opened_shelf_life_days ({opened_shelf_life_days}) must be between 1 and 60 days.",
                    "field": "opened_shelf_life_days",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_OPENED_SHELF_LIFE",
                "detail": "opened_shelf_life_days must be an integer between 1 and 60.",
                "field": "opened_shelf_life_days",
            })

        # 9. Single-dose minim safety check
        if container_type == DropsContainerType.SINGLE_DOSE_MINIM and opened_shelf_life_days != 1:
            raise ValidationError({
                "error_code": "ERR_SINGLE_DOSE_MINIM_EXPIRY",
                "detail": "Single-dose unit minims must have opened_shelf_life_days = 1 (discard within 24 hours).",
                "field": "opened_shelf_life_days",
            })

        # 10. Storage Temperature
        storage_temp = str(payload.get("storage_temp", DropsStorageTemp.ROOM_15_25)).strip().upper()
        if storage_temp not in cls.ALLOWED_STORAGE_TEMPS:
            raise ValidationError({
                "error_code": "ERR_INVALID_STORAGE_TEMP",
                "detail": f"Invalid storage_temp '{storage_temp}'. Allowed: {cls.ALLOWED_STORAGE_TEMPS}.",
                "field": "storage_temp",
            })

        # 11. Preservative details
        is_preservative_free = bool(payload.get("is_preservative_free", False))
        preservative_name = str(payload.get("preservative_name", "")).strip()

        # 12. Bilingual patient counseling
        counseling_en = payload.get("patient_instructions_en")
        counseling_bn = payload.get("patient_instructions_bn")
        if not counseling_en:
            counseling_en = cls._generate_instructions_en(route, opened_shelf_life_days, container_type)
        if not counseling_bn:
            counseling_bn = cls._generate_instructions_bn(route, opened_shelf_life_days, container_type)

        return {
            "product_id": str(product_id).strip(),
            "route": route,
            "container_type": container_type,
            "volume_ml": volume_ml,
            "concentration_value": concentration_value,
            "concentration_unit": conc_unit,
            "is_preservative_free": is_preservative_free,
            "preservative_name": preservative_name,
            "opened_shelf_life_days": opened_shelf_life_days,
            "storage_temp": storage_temp,
            "patient_instructions_en": counseling_en,
            "patient_instructions_bn": counseling_bn,
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    def list_configs(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists accessible drops configurations."""
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = DropsFormConfig.objects.select_related("product", "tenant").all()

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
        """Retrieves a single drops configuration."""
        cfg = DropsFormConfig.objects.select_related("product", "tenant").filter(config_id=config_id).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """Creates or updates a drops configuration."""
        payload_tenant = payload.get("tenant_id")
        is_operator = cls._is_platform_operator(user)

        if is_operator:
            target_tenant_id = payload_tenant
        else:
            target_tenant_id = getattr(user, "tenant_id", None)
            if payload_tenant and payload_tenant != target_tenant_id:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "detail": "Cannot configure drops settings for another tenant.",
                })

        cls._validate_user_permissions(user, tenant_id=target_tenant_id, action_type="write")

        validated = cls.validate_payload(payload)
        product_id = validated["product_id"]

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise ValidationError({
                "error_code": "ERR_PRODUCT_NOT_FOUND",
                "detail": f"Medicine product '{product_id}' was not found.",
            })

        target_tenant = None
        if target_tenant_id:
            target_tenant = Tenant.objects.filter(tenant_id=target_tenant_id).first()
            if not target_tenant:
                raise ValidationError({
                    "error_code": "ERR_TENANT_NOT_FOUND",
                    "detail": f"Tenant '{target_tenant_id}' was not found.",
                })

        config_id = payload.get("config_id")
        if config_id:
            cfg = DropsFormConfig.objects.filter(config_id=config_id).first()
            if not cfg:
                raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})
            if not is_operator and cfg.tenant_id != target_tenant_id:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "detail": "Cannot modify another tenant's config."})
        else:
            scope_prefix = target_tenant_id if target_tenant_id else "global"
            config_id = f"drops-{scope_prefix}-{product_id}-{uuid.uuid4().hex[:8]}"
            cfg = DropsFormConfig.objects.filter(product=product, tenant=target_tenant).first()
            if not cfg:
                cfg = DropsFormConfig(config_id=config_id, product=product, tenant=target_tenant)

        cfg.route = validated["route"]
        cfg.container_type = validated["container_type"]
        cfg.volume_ml = validated["volume_ml"]
        cfg.concentration_value = validated["concentration_value"]
        cfg.concentration_unit = validated["concentration_unit"]
        cfg.is_preservative_free = validated["is_preservative_free"]
        cfg.preservative_name = validated["preservative_name"]
        cfg.opened_shelf_life_days = validated["opened_shelf_life_days"]
        cfg.storage_temp = validated["storage_temp"]
        cfg.patient_instructions_en = validated["patient_instructions_en"]
        cfg.patient_instructions_bn = validated["patient_instructions_bn"]
        cfg.is_active = validated["is_active"]
        cfg.save()

        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="DROPS_CONFIG_UPSERTED",
            resource_type="DropsFormConfig",
            resource_id=cfg.config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "product_id": product_id,
                "route": cfg.route,
                "container_type": cfg.container_type,
                "volume_ml": str(cfg.volume_ml),
                "concentration_value": str(cfg.concentration_value),
                "opened_shelf_life_days": cfg.opened_shelf_life_days,
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> None:
        """Deletes a drops configuration."""
        cfg = DropsFormConfig.objects.filter(config_id=config_id).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="write")
        tenant_id_val = cfg.tenant_id
        cfg.delete()

        AuditLogEntry.objects.create(
            tenant_id=tenant_id_val or "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="DROPS_CONFIG_DELETED",
            resource_type="DropsFormConfig",
            resource_id=config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted drops config {config_id}."},
        )

    @classmethod
    def _validate_user_permissions(cls, user: PharmacyUser, tenant_id: Optional[str], action_type: str) -> None:
        """Enforces RBAC, tenant isolation, and subscription gating."""
        if cls._is_platform_operator(user):
            return

        user_role = getattr(user, "role", None)
        user_tenant = getattr(user, "tenant_id", None)

        if action_type == "write":
            allowed_roles = [UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.STAFF_PHARMACIST]
            if user_role not in allowed_roles:
                raise PermissionDenied({
                    "error_code": "ERR_PERMISSION_DENIED",
                    "detail": "Only Pharmacists and Store Owners are authorized to modify drops formulations.",
                })

        if tenant_id and user_tenant and tenant_id != user_tenant:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_VIOLATION",
                "detail": "Cross-tenant access violation.",
            })

        if user_tenant:
            tenant = Tenant.objects.filter(tenant_id=user_tenant).first()
            if tenant:
                if tenant.status == TenantLifecycleStatus.SUSPENDED:
                    raise PermissionDenied({
                        "error_code": "ERR_TENANT_SUSPENDED",
                        "detail": "Tenant account is suspended. Changes are locked.",
                    })
                if getattr(tenant, "plan_tier", None) == PlanTier.BASIC and action_type == "write":
                    raise PermissionDenied({
                        "error_code": "ERR_SUBSCRIPTION_UNENTITLED",
                        "detail": "Advanced dosage-form editor requires Professional or Enterprise tier.",
                    })

    @classmethod
    def _is_platform_operator(cls, user: PharmacyUser) -> bool:
        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return True
        return getattr(user, "role", None) == UserRole.SUPERADMIN

    @classmethod
    def _serialize_config(cls, cfg: DropsFormConfig) -> Dict[str, Any]:
        return {
            "config_id": cfg.config_id,
            "product_id": cfg.product_id,
            "product_name": getattr(cfg.product, "brand_name", cfg.product_id),
            "tenant_id": cfg.tenant_id,
            "route": cfg.route,
            "container_type": cfg.container_type,
            "volume_ml": str(cfg.volume_ml),
            "concentration_value": str(cfg.concentration_value),
            "concentration_unit": cfg.concentration_unit,
            "is_preservative_free": cfg.is_preservative_free,
            "preservative_name": cfg.preservative_name,
            "opened_shelf_life_days": cfg.opened_shelf_life_days,
            "storage_temp": cfg.storage_temp,
            "patient_instructions_en": cfg.patient_instructions_en,
            "patient_instructions_bn": cfg.patient_instructions_bn,
            "is_active": cfg.is_active,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @classmethod
    def _translate_route(cls, route: str) -> str:
        mapping = {
            "OPHTHALMIC": "চক্ষু প্রয়োগ (চোখের ড্রপ)",
            "OTIC": "কর্ণ প্রয়োগ (কানের ড্রপ)",
            "NASAL": "নাসিকা প্রয়োগ (নাকের ড্রপ)",
        }
        return mapping.get(route, route)

    @classmethod
    def _translate_container(cls, container: str) -> str:
        mapping = {
            "DROPPER_BOTTLE": "মাল্টি-ডোজ ড্রপার বোতল",
            "SINGLE_DOSE_MINIM": "একক ব্যবহার্য মিনিম",
            "SPRAY_PUMP": "নাসাল স্প্রে পাম্প",
        }
        return mapping.get(container, container)

    @classmethod
    def _translate_storage(cls, storage: str) -> str:
        mapping = {
            "ROOM_15_25": "সাধারণ রুম তাপমাত্রা (১৫°C - ২৫°C)",
            "REFRIGERATED_2_8": "রেফ্রিজারেটর কোল্ড-চেইন (২°C - ৮°C)",
            "COOL_BELOW_30": "৩০°C এর নিচে সংরক্ষণ করুন",
        }
        return mapping.get(storage, storage)

    @classmethod
    def _generate_instructions_en(cls, route: str, opened_days: int, container_type: str) -> str:
        if container_type == DropsContainerType.SINGLE_DOSE_MINIM:
            return "Sterile single-dose minim. Discard immediately after single application."
        if route == DropsRoute.OPHTHALMIC:
            return f"Instill into affected eye(s) as directed. Do not touch dropper tip to eye or skin. Discard {opened_days} days after opening."
        elif route == DropsRoute.OTIC:
            return f"Instill into affected ear canal(s). Do NOT instill in eyes. Discard {opened_days} days after opening."
        else:
            return f"Spray/instill into nostril(s). Discard {opened_days} days after opening."

    @classmethod
    def _generate_instructions_bn(cls, route: str, opened_days: int, container_type: str) -> str:
        if container_type == DropsContainerType.SINGLE_DOSE_MINIM:
            return "একক ব্যবহার্য মিনিম। একবার ব্যবহারের পর অবশিষ্ট অংশ তৎক্ষণাৎ ফেলে দিন।"
        if route == DropsRoute.OPHTHALMIC:
            return f"চোখে নির্দেশমতো ব্যবহার করুন। ড্রপারের মুখ চোখ বা ত্বকে স্পর্শ করবেন না। বোতল খোলার {opened_days} দিন পর ব্যবহার বন্ধ করুন।"
        elif route == DropsRoute.OTIC:
            return f"কানে নির্দেশমতো ড্রপ দিন। চোখে ব্যবহার সম্পূর্ণ নিষিদ্ধ। বোতল খোলার {opened_days} দিন পর ফেলে দিন।"
        else:
            return f"নাসারন্ধ্রে ব্যবহার করুন। বোতল খোলার {opened_days} দিন পর ব্যবহার বন্ধ করুন।"
