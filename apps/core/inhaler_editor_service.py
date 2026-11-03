"""
backend/apps/core/inhaler_editor_service.py
Architecture Decision: ADR-0146 (P146).
Dosage-Form Specific Editor Service for Inhalers (MDI, DPI, SMI).
Governs actuation dosing, total puffs, dimension consistency, device packaging,
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
    InhalerDeviceType,
    InhalerFormConfig,
    InhalerRoute,
    InhalerStorageCondition,
    MedicineProduct,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.inhaler_editor")


class InhalerEditorService:
    """
    Core domain service managing the Inhaler Dosage-Form Editor.
    Enforces dose per actuation, metered device actuations, dimension consistency,
    dose counter and spacer device attributes.
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

    ALLOWED_DEVICE_TYPES = [c.value for c in InhalerDeviceType]
    ALLOWED_ROUTES = [c.value for c in InhalerRoute]
    ALLOWED_STORAGE_CONDITIONS = [c.value for c in InhalerStorageCondition]
    ALLOWED_DOSE_UNITS = ["MCG", "MG"]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and validation rules for inhaler formulations."""
        return {
            "schema_version": "1.0",
            "dosage_form_family": "INHALER_RESPIRATORY",
            "supported_device_types": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_device(c.value)}
                for c in InhalerDeviceType
            ],
            "supported_routes": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_route(c.value)}
                for c in InhalerRoute
            ],
            "supported_storage_conditions": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_storage(c.value)}
                for c in InhalerStorageCondition
            ],
            "supported_dose_units": cls.ALLOWED_DOSE_UNITS,
            "min_actuations": 10,
            "max_actuations": 1000,
            "default_actuations": 200,
            "forbidden_fields": cls.HIDDEN_FIELDS,
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates inhaler configuration payload against clinical, device, and dimension rules.
        """
        # 1. Zero-trust isolation against solid oral fields
        for field in cls.HIDDEN_FIELDS:
            if field in payload and payload.get(field) is not None:
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is illegal for inhaler dosage form.",
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

        # 3. Device type validation
        device_type = str(payload.get("device_type", InhalerDeviceType.MDI_PRESSURIZED)).strip().upper()
        if device_type not in cls.ALLOWED_DEVICE_TYPES:
            raise ValidationError({
                "error_code": "ERR_INVALID_DEVICE_TYPE",
                "detail": f"Invalid device_type '{device_type}'. Allowed: {cls.ALLOWED_DEVICE_TYPES}.",
                "field": "device_type",
            })

        # 4. Route validation
        administration_route = str(payload.get("administration_route", InhalerRoute.ORAL_INHALATION)).strip().upper()
        if administration_route not in cls.ALLOWED_ROUTES:
            raise ValidationError({
                "error_code": "ERR_INVALID_ADMINISTRATION_ROUTE",
                "detail": f"Invalid administration_route '{administration_route}'. Allowed: {cls.ALLOWED_ROUTES}.",
                "field": "administration_route",
            })

        # 5. Dose per actuation
        try:
            dose_per_actuation = Decimal(str(payload.get("dose_per_actuation", "100.0000")))
            if dose_per_actuation <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_DOSE_PER_ACTUATION",
                    "detail": "dose_per_actuation must be greater than 0.",
                    "field": "dose_per_actuation",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSE_PER_ACTUATION",
                "detail": "dose_per_actuation must be a valid positive numeric decimal.",
                "field": "dose_per_actuation",
            })

        # 6. Dose unit
        dose_unit = str(payload.get("dose_unit", "MCG")).strip().upper()
        if dose_unit not in cls.ALLOWED_DOSE_UNITS:
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSE_UNIT",
                "detail": f"Invalid dose_unit '{dose_unit}'. Allowed: {cls.ALLOWED_DOSE_UNITS}.",
                "field": "dose_unit",
            })

        # 7. Total actuations per device
        try:
            total_actuations = int(payload.get("total_actuations", 200))
            if total_actuations < 10 or total_actuations > 1000:
                raise ValidationError({
                    "error_code": "ERR_INVALID_ACTUATIONS_COUNT",
                    "detail": f"total_actuations ({total_actuations}) must be between 10 and 1000 actuations.",
                    "field": "total_actuations",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_ACTUATIONS_COUNT",
                "detail": "total_actuations must be an integer between 10 and 1000.",
                "field": "total_actuations",
            })

        # 8. Dimension consistency (Total Active Content = dose_per_actuation * total_actuations)
        total_active_content = None
        raw_total_content = payload.get("total_active_content")
        if raw_total_content is not None and str(raw_total_content).strip() != "":
            try:
                total_active_content = Decimal(str(raw_total_content))
                expected_total = (dose_per_actuation * Decimal(str(total_actuations))).quantize(Decimal("0.0001"))
                actual_total = total_active_content.quantize(Decimal("0.0001"))
                if expected_total != actual_total:
                    raise ValidationError({
                        "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                        "detail": (
                            f"Dimension mismatch: total_active_content ({actual_total}) must match "
                            f"dose_per_actuation ({dose_per_actuation}) x total_actuations ({total_actuations}) = {expected_total}."
                        ),
                        "field": "total_active_content",
                    })
            except (InvalidOperation, TypeError):
                raise ValidationError({
                    "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                    "detail": "total_active_content must be a valid numeric decimal.",
                    "field": "total_active_content",
                })

        # 9. Storage condition
        storage_condition = str(payload.get("storage_condition", InhalerStorageCondition.ROOM_15_25)).strip().upper()
        if storage_condition not in cls.ALLOWED_STORAGE_CONDITIONS:
            raise ValidationError({
                "error_code": "ERR_INVALID_STORAGE_CONDITION",
                "detail": f"Invalid storage_condition '{storage_condition}'. Allowed: {cls.ALLOWED_STORAGE_CONDITIONS}.",
                "field": "storage_condition",
            })

        # 10. Packaging & Device Features
        has_dose_counter = bool(payload.get("has_dose_counter", True))
        has_spacer_included = bool(payload.get("has_spacer_included", False))
        canister_pressure_warning = bool(payload.get("canister_pressure_warning", True))
        priming_required = bool(payload.get("priming_required", True))
        try:
            priming_puffs = int(payload.get("priming_puffs", 2))
            if priming_puffs < 0 or priming_puffs > 10:
                raise ValidationError({
                    "error_code": "ERR_INVALID_PRIMING_PUFFS",
                    "detail": "priming_puffs must be between 0 and 10.",
                    "field": "priming_puffs",
                })
        except (ValueError, TypeError):
            priming_puffs = 2

        # 11. Bilingual counseling
        instructions_en = payload.get("patient_instructions_en")
        instructions_bn = payload.get("patient_instructions_bn")
        if not instructions_en:
            instructions_en = cls._generate_instructions_en(device_type, dose_per_actuation, dose_unit, total_actuations, has_dose_counter)
        if not instructions_bn:
            instructions_bn = cls._generate_instructions_bn(device_type, dose_per_actuation, dose_unit, total_actuations, has_dose_counter)

        return {
            "product_id": str(product_id).strip(),
            "device_type": device_type,
            "administration_route": administration_route,
            "dose_per_actuation": dose_per_actuation,
            "dose_unit": dose_unit,
            "total_actuations": total_actuations,
            "total_active_content": total_active_content,
            "has_dose_counter": has_dose_counter,
            "has_spacer_included": has_spacer_included,
            "storage_condition": storage_condition,
            "priming_required": priming_required,
            "priming_puffs": priming_puffs,
            "canister_pressure_warning": canister_pressure_warning,
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
        """Lists accessible inhaler configurations."""
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = InhalerFormConfig.objects.select_related("product", "tenant").all()

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
            cfg = InhalerFormConfig.objects.select_related("product", "tenant").get(config_id=config_id)
        except (InhalerFormConfig.DoesNotExist, ValueError):
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"Inhaler form configuration '{config_id}' was not found.",
            })

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """Creates or updates an inhaler configuration."""
        payload_tenant = payload.get("tenant_id")
        is_operator = cls._is_platform_operator(user)

        if is_operator:
            target_tenant_id = payload_tenant
        else:
            target_tenant_id = getattr(user, "tenant_id", None)
            if payload_tenant and payload_tenant != target_tenant_id:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "detail": "Cannot configure inhaler settings for another tenant.",
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
                cfg = InhalerFormConfig.objects.filter(config_id=config_id).first()
            except ValueError:
                cfg = None
            if not cfg:
                raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})
            if not is_operator and cfg.tenant_id != target_tenant_id:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "detail": "Cannot modify another tenant's config."})
        else:
            cfg = InhalerFormConfig.objects.filter(product=product, tenant=target_tenant).first()
            if not cfg:
                cfg = InhalerFormConfig(product=product, tenant=target_tenant)

        cfg.device_type = validated["device_type"]
        cfg.administration_route = validated["administration_route"]
        cfg.dose_per_actuation = validated["dose_per_actuation"]
        cfg.dose_unit = validated["dose_unit"]
        cfg.total_actuations = validated["total_actuations"]
        cfg.total_active_content = validated["total_active_content"]
        cfg.has_dose_counter = validated["has_dose_counter"]
        cfg.has_spacer_included = validated["has_spacer_included"]
        cfg.storage_condition = validated["storage_condition"]
        cfg.priming_required = validated["priming_required"]
        cfg.priming_puffs = validated["priming_puffs"]
        cfg.canister_pressure_warning = validated["canister_pressure_warning"]
        cfg.patient_instructions_en = validated["patient_instructions_en"]
        cfg.patient_instructions_bn = validated["patient_instructions_bn"]
        cfg.is_active = validated["is_active"]
        cfg.save()

        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=str(getattr(user, "id", "system")),
            actor_email=getattr(user, "email", "system@pharmacy.local"),
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="INHALER_CONFIG_UPSERTED",
            resource_type="InhalerFormConfig",
            resource_id=str(cfg.config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "product_id": product_id,
                "device_type": cfg.device_type,
                "dose_per_actuation": str(cfg.dose_per_actuation),
                "total_actuations": cfg.total_actuations,
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> None:
        """Deletes an inhaler configuration."""
        try:
            cfg = InhalerFormConfig.objects.filter(config_id=config_id).first()
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
            action="INHALER_CONFIG_DELETED",
            resource_type="InhalerFormConfig",
            resource_id=str(config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted inhaler config {config_id}."},
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
                    "detail": "Only Pharmacists and Store Owners are authorized to modify inhaler formulations.",
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
    def _serialize_config(cls, cfg: InhalerFormConfig) -> Dict[str, Any]:
        return {
            "config_id": str(cfg.config_id),
            "product_id": cfg.product_id,
            "product_name": getattr(cfg.product, "brand_name", cfg.product_id),
            "tenant_id": cfg.tenant_id,
            "device_type": cfg.device_type,
            "administration_route": cfg.administration_route,
            "dose_per_actuation": str(cfg.dose_per_actuation),
            "dose_unit": cfg.dose_unit,
            "total_actuations": cfg.total_actuations,
            "total_active_content": str(cfg.total_active_content) if cfg.total_active_content is not None else None,
            "has_dose_counter": cfg.has_dose_counter,
            "has_spacer_included": cfg.has_spacer_included,
            "storage_condition": cfg.storage_condition,
            "priming_required": cfg.priming_required,
            "priming_puffs": cfg.priming_puffs,
            "canister_pressure_warning": cfg.canister_pressure_warning,
            "patient_instructions_en": cfg.patient_instructions_en,
            "patient_instructions_bn": cfg.patient_instructions_bn,
            "is_active": cfg.is_active,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @classmethod
    def _generate_instructions_en(
        cls,
        device_type: str,
        dose: Decimal,
        unit: str,
        actuations: int,
        counter: bool,
    ) -> str:
        counter_msg = "Monitor dose counter countdown." if counter else "Keep manual track of puff count."
        if device_type == InhalerDeviceType.MDI_PRESSURIZED:
            return (
                f"Shake well for 5 seconds before each spray. Inhale deeply and slowly. "
                f"Delivers {dose} {unit} per puff ({actuations} total puffs). {counter_msg} Rinse mouth after use."
            )
        elif device_type == InhalerDeviceType.DPI_DRY_POWDER:
            return (
                f"Load dose as instructed. Inhale rapidly and deeply through mouthpiece. "
                f"Do not exhale into device. Delivers {dose} {unit} per actuation. {counter_msg}"
            )
        elif device_type == InhalerDeviceType.SMI_SOFT_MIST:
            return (
                f"Turn base until click, open cap, press release button and inhale slowly. "
                f"Delivers {dose} {unit} per actuation. {counter_msg}"
            )
        return f"Inhale {dose} {unit} as prescribed. Rinse mouth after inhalation."

    @classmethod
    def _generate_instructions_bn(
        cls,
        device_type: str,
        dose: Decimal,
        unit: str,
        actuations: int,
        counter: bool,
    ) -> str:
        counter_msg = "ডোয কাউন্টারের গণনা খেয়াল রাখুন।" if counter else "ব্যবহারের হিসাব রাখুন।"
        if device_type == InhalerDeviceType.MDI_PRESSURIZED:
            return (
                f"প্রতিটি ব্যবহারের পূর্বে ৫ সেকেন্ড ঝাঁকিয়ে নিন। ধীরে ধীরে গভীরভাবে শ্বাস টানুন। "
                f"প্রতি পাফে {dose} {unit} ওষুধ নির্গত হয় (মোট {actuations} পাফ)। {counter_msg} ব্যবহারের পর মুখ কুলকুচি করুন।"
            )
        elif device_type == InhalerDeviceType.DPI_DRY_POWDER:
            return (
                f"নির্দেশ অনুযায়ী ডোজ লোড করুন এবং দ্রুত ও গভীরভাবে শ্বাস টানুন। "
                f"ইনহেলারের ভেতরে শ্বাস ছাড়বেন না। {counter_msg} ব্যবহারের পর মুখ ধুয়ে ফেলুন।"
            )
        return "নির্দেশনা অনুযায়ী ইনহেল করুন। ব্যবহারের পর মুখ ভালো করে কুলকুচি করুন।"

    @classmethod
    def _translate_device(cls, code: str) -> str:
        mapping = {
            "MDI_PRESSURIZED": "প্রেসারাইজড মিটারড-ডোজ ইনহেলার (pMDI)",
            "DPI_DRY_POWDER": "ড্রাই পাউডার ইনহেলার (DPI)",
            "SMI_SOFT_MIST": "সফট মিস্ট ইনহেলার (SMI)",
            "BREATH_ACTUATED_MDI": "শ্বাস-চালিত ইনহেলার (BAI)",
            "NEBULIZER_AMPOULE": "নেবুলাইজার অ্যাম্পুল / রেসপ্যুল",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_route(cls, code: str) -> str:
        mapping = {
            "ORAL_INHALATION": "মুখের মাধ্যমে শ্বাসনালীতে (Oral Inhalation)",
            "NASAL_INHALATION": "নাকের মাধ্যমে (Nasal Inhalation)",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_storage(cls, code: str) -> str:
        mapping = {
            "ROOM_15_25": "১৫° থেকে ২৫° সেলসিয়াসে সংরক্ষণ করুন (ফ্রিজ করবেন না)",
            "COLD_2_8": "বিতরণের পূর্বে ২° থেকে ৮° সেলসিয়াসে ফ্রিজে রাখুন",
            "PROTECT_FROM_HEAT": "৩০° সেলসিয়াসের নিচে রাখুন, রোদ ও তাপ থেকে দূরে রাখুন",
        }
        return mapping.get(code, code)
