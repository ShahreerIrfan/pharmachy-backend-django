"""
backend/apps/core/topical_editor_service.py
Architecture Decision: ADR-0148 (P148).
Dosage-Form Specific Editor Service for Creams, Ointments, Gels, and Lotions.
Governs net fill weights, topical routes, concentration units, ophthalmic sterility,
and multi-tenant access control.
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
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
    TopicalContainerType,
    TopicalFormConfig,
    TopicalFormulationType,
    TopicalRoute,
    TopicalStorageCondition,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.topical_editor")


class TopicalEditorService:
    """
    Core domain service managing the Topical Cream & Ointment Editor.
    Enforces net weight in grams, topical routes, concentration, ophthalmic sterility,
    and post-opening shelf life (BUD).
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

    ALLOWED_FORMULATIONS = [c.value for c in TopicalFormulationType]
    ALLOWED_ROUTES = [c.value for c in TopicalRoute]
    ALLOWED_CONTAINERS = [c.value for c in TopicalContainerType]
    ALLOWED_STORAGE_CONDITIONS = [c.value for c in TopicalStorageCondition]
    ALLOWED_CONCENTRATION_UNITS = ["PERCENT_W_W", "MG_PER_G", "PERCENT_W_V", "IU_PER_G"]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and validation rules for topical formulations."""
        return {
            "schema_version": "1.0",
            "dosage_form_family": "TOPICAL_SEMISOLID_FLUID",
            "supported_formulations": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_formulation(c.value)}
                for c in TopicalFormulationType
            ],
            "supported_routes": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_route(c.value)}
                for c in TopicalRoute
            ],
            "supported_containers": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_container(c.value)}
                for c in TopicalContainerType
            ],
            "supported_storage_conditions": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_storage(c.value)}
                for c in TopicalStorageCondition
            ],
            "supported_concentration_units": cls.ALLOWED_CONCENTRATION_UNITS,
            "min_net_weight_grams": 1.0,
            "max_net_weight_grams": 1000.0,
            "forbidden_fields": cls.HIDDEN_FIELDS,
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates topical configuration payload against clinical, sterility, and packaging rules.
        """
        # 1. Zero-trust isolation against solid oral fields
        for field in cls.HIDDEN_FIELDS:
            if field in payload and payload.get(field) is not None:
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is illegal for topical dosage form.",
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

        # 3. Formulation type check
        formulation_type = str(payload.get("formulation_type", TopicalFormulationType.CREAM)).strip().upper()
        if formulation_type not in cls.ALLOWED_FORMULATIONS:
            raise ValidationError({
                "error_code": "ERR_INVALID_FORMULATION_TYPE",
                "detail": f"Invalid formulation_type '{formulation_type}'. Allowed: {cls.ALLOWED_FORMULATIONS}.",
                "field": "formulation_type",
            })

        # 4. Route check
        administration_route = str(payload.get("administration_route", TopicalRoute.TOPICAL_CUTANEOUS)).strip().upper()
        if administration_route not in cls.ALLOWED_ROUTES:
            raise ValidationError({
                "error_code": "ERR_INVALID_TOPICAL_ROUTE",
                "detail": f"Invalid administration_route '{administration_route}'. Allowed: {cls.ALLOWED_ROUTES}.",
                "field": "administration_route",
            })

        # 5. Container type check
        container_type = str(payload.get("container_type", TopicalContainerType.TUBE_ALUMINUM)).strip().upper()
        if container_type not in cls.ALLOWED_CONTAINERS:
            raise ValidationError({
                "error_code": "ERR_INVALID_CONTAINER_TYPE",
                "detail": f"Invalid container_type '{container_type}'. Allowed: {cls.ALLOWED_CONTAINERS}.",
                "field": "container_type",
            })

        # 6. Net weight in grams
        try:
            net_weight_grams = Decimal(str(payload.get("net_weight_grams", "15.00")))
            if net_weight_grams <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_NET_WEIGHT",
                    "detail": "net_weight_grams must be greater than 0.",
                    "field": "net_weight_grams",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_NET_WEIGHT",
                "detail": "net_weight_grams must be a valid positive numeric decimal.",
                "field": "net_weight_grams",
            })

        # 7. Concentration value
        try:
            concentration_value = Decimal(str(payload.get("concentration_value", "1.0000")))
            if concentration_value <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_CONCENTRATION",
                    "detail": "concentration_value must be greater than 0.",
                    "field": "concentration_value",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_CONCENTRATION",
                "detail": "concentration_value must be a valid positive numeric decimal.",
                "field": "concentration_value",
            })

        # 8. Concentration unit
        concentration_unit = str(payload.get("concentration_unit", "PERCENT_W_W")).strip().upper()
        if concentration_unit not in cls.ALLOWED_CONCENTRATION_UNITS:
            raise ValidationError({
                "error_code": "ERR_INVALID_CONCENTRATION_UNIT",
                "detail": f"Invalid concentration_unit '{concentration_unit}'. Allowed: {cls.ALLOWED_CONCENTRATION_UNITS}.",
                "field": "concentration_unit",
            })

        # 9. Sterility check for ophthalmic eye ointments
        is_sterile = bool(payload.get("is_sterile", False))
        if administration_route == TopicalRoute.TOPICAL_OPHTHALMIC and not is_sterile:
            raise ValidationError({
                "error_code": "ERR_OPHTHALMIC_STERILITY_MANDATORY",
                "detail": "Ophthalmic eye ointments (TOPICAL_OPHTHALMIC) must be certified sterile (is_sterile = true).",
                "field": "is_sterile",
            })

        # 10. Opened shelf life days (BUD)
        try:
            opened_shelf_life_days = int(payload.get("opened_shelf_life_days", 28))
            if administration_route == TopicalRoute.TOPICAL_OPHTHALMIC and opened_shelf_life_days > 28:
                raise ValidationError({
                    "error_code": "ERR_INVALID_OPENED_SHELF_LIFE",
                    "detail": "Sterile ophthalmic eye ointments must not exceed 28 days opened shelf life.",
                    "field": "opened_shelf_life_days",
                })
            if opened_shelf_life_days < 7 or opened_shelf_life_days > 180:
                raise ValidationError({
                    "error_code": "ERR_INVALID_OPENED_SHELF_LIFE",
                    "detail": f"opened_shelf_life_days ({opened_shelf_life_days}) must be between 7 and 180 days.",
                    "field": "opened_shelf_life_days",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_OPENED_SHELF_LIFE",
                "detail": "opened_shelf_life_days must be an integer between 7 and 180.",
                "field": "opened_shelf_life_days",
            })

        # 11. Applicator and Storage
        applicator_included = bool(payload.get("applicator_included", False))
        storage_condition = str(payload.get("storage_condition", TopicalStorageCondition.ROOM_15_25)).strip().upper()
        if storage_condition not in cls.ALLOWED_STORAGE_CONDITIONS:
            raise ValidationError({
                "error_code": "ERR_INVALID_STORAGE_CONDITION",
                "detail": f"Invalid storage_condition '{storage_condition}'. Allowed: {cls.ALLOWED_STORAGE_CONDITIONS}.",
                "field": "storage_condition",
            })

        # 12. Bilingual counseling
        instructions_en = payload.get("patient_instructions_en")
        instructions_bn = payload.get("patient_instructions_bn")
        if not instructions_en:
            instructions_en = cls._generate_instructions_en(formulation_type, administration_route, opened_shelf_life_days)
        if not instructions_bn:
            instructions_bn = cls._generate_instructions_bn(formulation_type, administration_route, opened_shelf_life_days)

        return {
            "product_id": str(product_id).strip(),
            "formulation_type": formulation_type,
            "administration_route": administration_route,
            "container_type": container_type,
            "net_weight_grams": net_weight_grams,
            "concentration_value": concentration_value,
            "concentration_unit": concentration_unit,
            "is_sterile": is_sterile,
            "applicator_included": applicator_included,
            "opened_shelf_life_days": opened_shelf_life_days,
            "storage_condition": storage_condition,
            "patient_instructions_en": instructions_en,
            "patient_instructions_bn": instructions_bn,
            "is_active": bool(payload.get("is_active", True)),
        }

    @classmethod
    def list_configs(
        cls,
        user: PharmacyUser,
        product_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists accessible topical configurations."""
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = TopicalFormConfig.objects.select_related("product", "tenant").all()

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
        """Retrieves single configuration ensuring multi-tenant access."""
        try:
            cfg = TopicalFormConfig.objects.select_related("product", "tenant").get(config_id=config_id)
        except (TopicalFormConfig.DoesNotExist, ValueError):
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"Topical form configuration '{config_id}' was not found.",
            })

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """Creates or updates a topical configuration."""
        payload_tenant = payload.get("tenant_id")
        is_operator = cls._is_platform_operator(user)

        if is_operator:
            target_tenant_id = payload_tenant
        else:
            target_tenant_id = getattr(user, "tenant_id", None)
            if payload_tenant and payload_tenant != target_tenant_id:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "detail": "Cannot configure topical settings for another tenant.",
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
            try:
                cfg = TopicalFormConfig.objects.filter(config_id=config_id).first()
            except ValueError:
                cfg = None
            if not cfg:
                raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})
            if not is_operator and cfg.tenant_id != target_tenant_id:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "detail": "Cannot modify another tenant's config."})
        else:
            cfg = TopicalFormConfig.objects.filter(product=product, tenant=target_tenant).first()
            if not cfg:
                cfg = TopicalFormConfig(product=product, tenant=target_tenant)

        cfg.formulation_type = validated["formulation_type"]
        cfg.administration_route = validated["administration_route"]
        cfg.container_type = validated["container_type"]
        cfg.net_weight_grams = validated["net_weight_grams"]
        cfg.concentration_value = validated["concentration_value"]
        cfg.concentration_unit = validated["concentration_unit"]
        cfg.is_sterile = validated["is_sterile"]
        cfg.applicator_included = validated["applicator_included"]
        cfg.opened_shelf_life_days = validated["opened_shelf_life_days"]
        cfg.storage_condition = validated["storage_condition"]
        cfg.patient_instructions_en = validated["patient_instructions_en"]
        cfg.patient_instructions_bn = validated["patient_instructions_bn"]
        cfg.is_active = validated["is_active"]
        cfg.save()

        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="TOPICAL_CONFIG_UPSERTED",
            resource_type="TopicalFormConfig",
            resource_id=str(cfg.config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "product_id": product_id,
                "formulation_type": cfg.formulation_type,
                "net_weight_grams": str(cfg.net_weight_grams),
                "concentration_value": str(cfg.concentration_value),
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> None:
        """Deletes a topical configuration."""
        try:
            cfg = TopicalFormConfig.objects.filter(config_id=config_id).first()
        except ValueError:
            cfg = None
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
            action="TOPICAL_CONFIG_DELETED",
            resource_type="TopicalFormConfig",
            resource_id=str(config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted topical config {config_id}."},
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
                    "detail": "Only Pharmacists and Store Owners are authorized to modify topical formulations.",
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
    def _serialize_config(cls, cfg: TopicalFormConfig) -> Dict[str, Any]:
        return {
            "config_id": str(cfg.config_id),
            "product_id": cfg.product_id,
            "product_name": getattr(cfg.product, "brand_name", cfg.product_id),
            "tenant_id": cfg.tenant_id,
            "formulation_type": cfg.formulation_type,
            "administration_route": cfg.administration_route,
            "container_type": cfg.container_type,
            "net_weight_grams": str(cfg.net_weight_grams),
            "concentration_value": str(cfg.concentration_value),
            "concentration_unit": cfg.concentration_unit,
            "is_sterile": cfg.is_sterile,
            "applicator_included": cfg.applicator_included,
            "opened_shelf_life_days": cfg.opened_shelf_life_days,
            "storage_condition": cfg.storage_condition,
            "patient_instructions_en": cfg.patient_instructions_en,
            "patient_instructions_bn": cfg.patient_instructions_bn,
            "is_active": cfg.is_active,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @classmethod
    def _generate_instructions_en(cls, formulation: str, route: str, opened_days: int) -> str:
        if route == TopicalRoute.TOPICAL_OPHTHALMIC:
            return f"Apply thin ribbon into lower eyelid. Sterile ophthalmic formulation. Discard {opened_days} days after opening."
        return f"Apply thin layer to affected skin area. Wash hands before and after application. Discard {opened_days} days after opening."

    @classmethod
    def _generate_instructions_bn(cls, formulation: str, route: str, opened_days: int) -> str:
        if route == TopicalRoute.TOPICAL_OPHTHALMIC:
            return f"চোখের নিচের পাতায় পাতলা করে লাগান। জীবানুমুক্ত ওপথালমিক মলম। খোলার {opened_days} দিন পর ব্যবহার বন্ধ করুন।"
        return f"আক্রান্ত ত্বকে পাতলা প্রলেপ দিন। ব্যবহারের পূর্বে ও পরে হাত ধুয়ে নিন। খোলার {opened_days} দিন পর ব্যবহার বন্ধ করুন।"

    @classmethod
    def _translate_formulation(cls, code: str) -> str:
        mapping = {
            "CREAM": "টপিকাল ক্রিম (Emulsion)",
            "OINTMENT": "টপিকাল অয়েন্টমেন্ট / মলম (Petrolatum)",
            "GEL": "টপিকাল জেল (Hydrogel)",
            "LOTION": "টপিকাল লোশন (Fluid)",
            "PASTE": "টপিকাল পেস্ট (High Powder)",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_route(cls, code: str) -> str:
        mapping = {
            "TOPICAL_CUTANEOUS": "ত্বকে বাহ্যিক প্রয়োগ (Cutaneous Skin)",
            "TOPICAL_OPHTHALMIC": "চোখে প্রয়োগ (Sterile Eye Ointment)",
            "TOPICAL_RECTAL": "মলদ্বারে প্রয়োগ (Rectal)",
            "TOPICAL_VAGINAL": "যোনিপথে প্রয়োগ (Vaginal)",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_container(cls, code: str) -> str:
        mapping = {
            "TUBE_ALUMINUM": "অ্যালুমিনিয়াম টিউব",
            "TUBE_PLASTIC": "প্লাস্টিক ল্যামিনেটেড টিউব",
            "JAR_POT": "ডার্মাটোলজিক্যাল পট / জার",
            "BOTTLE_PUMP": "ডিসপেনসার পাম্প বোতল",
            "SACHET": "একক ব্যবহারের স্যাশে",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_storage(cls, code: str) -> str:
        mapping = {
            "ROOM_15_25": "১৫° থেকে ২৫° সেলসিয়াসে সংরক্ষণ করুন (ফ্রিজ করবেন না)",
            "COOL_BELOW_30": "৩০° সেলসিয়াসের নিচে আলো ও তাপ থেকে দূরে রাখুন",
            "COLD_2_8": "২° থেকে ৮° সেলসিয়াসে ফ্রিজে রাখুন",
        }
        return mapping.get(code, code)
