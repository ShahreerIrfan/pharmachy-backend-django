"""
backend/apps/core/suppository_editor_service.py
Architecture Decision: ADR-0150 (P150).
Dosage-Form Specific Editor Service for Suppositories, Pessaries, and Bougies.
Governs piece counts, active strength, anatomical routes, melt-protection storage,
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
    SuppositoryBaseType,
    SuppositoryFormConfig,
    SuppositoryRoute,
    SuppositoryStorageCondition,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.suppository_editor")


class SuppositoryEditorService:
    """
    Core domain service managing the Suppository & Pessary Dosage-Form Editor.
    Enforces pieces per strip, strips per box, dimension consistency,
    melt-protection temperature policy, and break-bulk loose piece sales.
    """

    HIDDEN_FIELDS = [
        "tablets_per_strip",
        "units_per_strip",
        "is_scored",
        "coating_type",
        "needle_gauge",
    ]

    ALLOWED_BASES = [c.value for c in SuppositoryBaseType]
    ALLOWED_ROUTES = [c.value for c in SuppositoryRoute]
    ALLOWED_STORAGE_CONDITIONS = [c.value for c in SuppositoryStorageCondition]
    ALLOWED_STRENGTH_UNITS = ["MG", "G", "IU", "MCG"]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and validation rules for suppository formulations."""
        return {
            "schema_version": "1.0",
            "dosage_form_family": "SUPPOSITORY_PESSARY",
            "supported_bases": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_base(c.value)}
                for c in SuppositoryBaseType
            ],
            "supported_routes": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_route(c.value)}
                for c in SuppositoryRoute
            ],
            "supported_storage_conditions": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_storage(c.value)}
                for c in SuppositoryStorageCondition
            ],
            "supported_strength_units": cls.ALLOWED_STRENGTH_UNITS,
            "min_pieces_per_strip": 1,
            "max_pieces_per_strip": 50,
            "min_strips_per_pack": 1,
            "max_strips_per_pack": 100,
            "forbidden_fields": cls.HIDDEN_FIELDS,
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates suppository configuration payload against clinical, thermal, and dimension rules.
        """
        # 1. Zero-trust isolation against solid oral tablet attributes
        for field in cls.HIDDEN_FIELDS:
            if field in payload and payload.get(field) is not None:
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is illegal for suppository dosage form.",
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

        # 3. Base type check
        base_type = str(payload.get("suppository_base_type", SuppositoryBaseType.FATTY_BASE_COCOA_BUTTER)).strip().upper()
        if base_type not in cls.ALLOWED_BASES:
            raise ValidationError({
                "error_code": "ERR_INVALID_SUPPOSITORY_BASE",
                "detail": f"Invalid suppository_base_type '{base_type}'. Allowed: {cls.ALLOWED_BASES}.",
                "field": "suppository_base_type",
            })

        # 4. Route check
        administration_route = str(payload.get("administration_route", SuppositoryRoute.RECTAL)).strip().upper()
        if administration_route not in cls.ALLOWED_ROUTES:
            raise ValidationError({
                "error_code": "ERR_INVALID_SUPPOSITORY_ROUTE",
                "detail": f"Invalid administration_route '{administration_route}'. Allowed: {cls.ALLOWED_ROUTES}.",
                "field": "administration_route",
            })

        # 5. Strength value
        try:
            strength_value = Decimal(str(payload.get("strength_value", "250.0000")))
            if strength_value <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_STRENGTH",
                    "detail": "strength_value must be greater than 0.",
                    "field": "strength_value",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_STRENGTH",
                "detail": "strength_value must be a valid positive numeric decimal.",
                "field": "strength_value",
            })

        # 6. Strength unit
        strength_unit = str(payload.get("strength_unit", "MG")).strip().upper()
        if strength_unit not in cls.ALLOWED_STRENGTH_UNITS:
            raise ValidationError({
                "error_code": "ERR_INVALID_STRENGTH_UNIT",
                "detail": f"Invalid strength_unit '{strength_unit}'. Allowed: {cls.ALLOWED_STRENGTH_UNITS}.",
                "field": "strength_unit",
            })

        # 7. Pieces per strip
        try:
            pieces_per_strip = int(payload.get("pieces_per_strip", 5))
            if pieces_per_strip < 1 or pieces_per_strip > 50:
                raise ValidationError({
                    "error_code": "ERR_INVALID_PIECES_PER_STRIP",
                    "detail": f"pieces_per_strip ({pieces_per_strip}) must be between 1 and 50.",
                    "field": "pieces_per_strip",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_PIECES_PER_STRIP",
                "detail": "pieces_per_strip must be an integer between 1 and 50.",
                "field": "pieces_per_strip",
            })

        # 8. Strips per pack
        try:
            strips_per_pack = int(payload.get("strips_per_pack", 2))
            if strips_per_pack < 1 or strips_per_pack > 100:
                raise ValidationError({
                    "error_code": "ERR_INVALID_STRIPS_PER_PACK",
                    "detail": f"strips_per_pack ({strips_per_pack}) must be between 1 and 100.",
                    "field": "strips_per_pack",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_STRIPS_PER_PACK",
                "detail": "strips_per_pack must be an integer between 1 and 100.",
                "field": "strips_per_pack",
            })

        # 9. Dimension consistency (total_pieces_per_pack = pieces_per_strip * strips_per_pack)
        total_pieces_per_pack = None
        raw_total_pieces = payload.get("total_pieces_per_pack")
        if raw_total_pieces is not None and str(raw_total_pieces).strip() != "":
            try:
                total_pieces_per_pack = int(raw_total_pieces)
                expected_total = pieces_per_strip * strips_per_pack
                if total_pieces_per_pack != expected_total:
                    raise ValidationError({
                        "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                        "detail": (
                            f"Dimension mismatch: total_pieces_per_pack ({total_pieces_per_pack}) must match "
                            f"pieces_per_strip ({pieces_per_strip}) x strips_per_pack ({strips_per_pack}) = {expected_total}."
                        ),
                        "field": "total_pieces_per_pack",
                    })
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                    "detail": "total_pieces_per_pack must be a valid integer.",
                    "field": "total_pieces_per_pack",
                })

        # 10. Thermal melt-protection storage checks
        requires_refrigeration = bool(payload.get("requires_refrigeration", False))
        storage_condition = str(payload.get("storage_condition", SuppositoryStorageCondition.COOL_BELOW_25)).strip().upper()
        if storage_condition not in cls.ALLOWED_STORAGE_CONDITIONS:
            storage_condition = SuppositoryStorageCondition.COOL_BELOW_25

        if requires_refrigeration and storage_condition not in [
            SuppositoryStorageCondition.REFRIGERATED_2_8,
            SuppositoryStorageCondition.COOL_BELOW_25,
        ]:
            raise ValidationError({
                "error_code": "ERR_MELT_PROTECTION_STORAGE_REQUIRED",
                "detail": "Suppositories requiring refrigeration must specify REFRIGERATED_2_8 or COOL_BELOW_25 storage condition.",
                "field": "storage_condition",
            })

        # 11. Loose sales eligibility
        loose_piece_sale_eligible = bool(payload.get("loose_piece_sale_eligible", True))

        # 12. Bilingual counseling
        instructions_en = payload.get("patient_instructions_en")
        instructions_bn = payload.get("patient_instructions_bn")
        if not instructions_en:
            instructions_en = cls._generate_instructions_en(administration_route, base_type, storage_condition)
        if not instructions_bn:
            instructions_bn = cls._generate_instructions_bn(administration_route, base_type, storage_condition)

        return {
            "product_id": str(product_id).strip(),
            "suppository_base_type": base_type,
            "administration_route": administration_route,
            "strength_value": strength_value,
            "strength_unit": strength_unit,
            "pieces_per_strip": pieces_per_strip,
            "strips_per_pack": strips_per_pack,
            "total_pieces_per_pack": total_pieces_per_pack,
            "loose_piece_sale_eligible": loose_piece_sale_eligible,
            "requires_refrigeration": requires_refrigeration,
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
        """Lists accessible suppository configurations."""
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = SuppositoryFormConfig.objects.select_related("product", "tenant").all()

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
            cfg = SuppositoryFormConfig.objects.select_related("product", "tenant").get(config_id=config_id)
        except (SuppositoryFormConfig.DoesNotExist, ValueError):
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"Suppository form configuration '{config_id}' was not found.",
            })

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """Creates or updates a suppository configuration."""
        payload_tenant = payload.get("tenant_id")
        is_operator = cls._is_platform_operator(user)

        if is_operator:
            target_tenant_id = payload_tenant
        else:
            target_tenant_id = getattr(user, "tenant_id", None)
            if payload_tenant and payload_tenant != target_tenant_id:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "detail": "Cannot configure suppository settings for another tenant.",
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
                cfg = SuppositoryFormConfig.objects.filter(config_id=config_id).first()
            except ValueError:
                cfg = None
            if not cfg:
                raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})
            if not is_operator and cfg.tenant_id != target_tenant_id:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "detail": "Cannot modify another tenant's config."})
        else:
            cfg = SuppositoryFormConfig.objects.filter(product=product, tenant=target_tenant).first()
            if not cfg:
                cfg = SuppositoryFormConfig(product=product, tenant=target_tenant)

        cfg.suppository_base_type = validated["suppository_base_type"]
        cfg.administration_route = validated["administration_route"]
        cfg.strength_value = validated["strength_value"]
        cfg.strength_unit = validated["strength_unit"]
        cfg.pieces_per_strip = validated["pieces_per_strip"]
        cfg.strips_per_pack = validated["strips_per_pack"]
        cfg.total_pieces_per_pack = validated["total_pieces_per_pack"]
        cfg.loose_piece_sale_eligible = validated["loose_piece_sale_eligible"]
        cfg.requires_refrigeration = validated["requires_refrigeration"]
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
            action="SUPPOSITORY_CONFIG_UPSERTED",
            resource_type="SuppositoryFormConfig",
            resource_id=str(cfg.config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "product_id": product_id,
                "base_type": cfg.suppository_base_type,
                "route": cfg.administration_route,
                "pieces_per_strip": cfg.pieces_per_strip,
                "strips_per_pack": cfg.strips_per_pack,
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> None:
        """Deletes a suppository configuration."""
        try:
            cfg = SuppositoryFormConfig.objects.filter(config_id=config_id).first()
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
            action="SUPPOSITORY_CONFIG_DELETED",
            resource_type="SuppositoryFormConfig",
            resource_id=str(config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted suppository config {config_id}."},
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
                    "detail": "Only Pharmacists and Store Owners are authorized to modify suppository formulations.",
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
    def _serialize_config(cls, cfg: SuppositoryFormConfig) -> Dict[str, Any]:
        return {
            "config_id": str(cfg.config_id),
            "product_id": cfg.product_id,
            "product_name": getattr(cfg.product, "brand_name", cfg.product_id),
            "tenant_id": cfg.tenant_id,
            "suppository_base_type": cfg.suppository_base_type,
            "administration_route": cfg.administration_route,
            "strength_value": str(cfg.strength_value),
            "strength_unit": cfg.strength_unit,
            "pieces_per_strip": cfg.pieces_per_strip,
            "strips_per_pack": cfg.strips_per_pack,
            "total_pieces_per_pack": cfg.total_pieces_per_pack,
            "loose_piece_sale_eligible": cfg.loose_piece_sale_eligible,
            "requires_refrigeration": cfg.requires_refrigeration,
            "storage_condition": cfg.storage_condition,
            "patient_instructions_en": cfg.patient_instructions_en,
            "patient_instructions_bn": cfg.patient_instructions_bn,
            "is_active": cfg.is_active,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @classmethod
    def _generate_instructions_en(cls, route: str, base: str, storage: str) -> str:
        chill_note = "If soft, chill in cold water before unwrapping. " if base == SuppositoryBaseType.FATTY_BASE_COCOA_BUTTER else ""
        if route == SuppositoryRoute.RECTAL:
            return f"{chill_note}Unwrap and insert pointed end first into rectum. Lie down for 15 minutes."
        elif route == SuppositoryRoute.VAGINAL:
            return "Unwrap and insert deeply into vagina, preferably at bedtime using applicator if provided."
        return "Unwrap and insert as directed by physician."

    @classmethod
    def _generate_instructions_bn(cls, route: str, base: str, storage: str) -> str:
        chill_note = "নরম হলে মোড়ক খোলার পূর্বে ঠান্ডা পানিতে কিছুক্ষণ শক্ত করে নিন। " if base == SuppositoryBaseType.FATTY_BASE_COCOA_BUTTER else ""
        if route == SuppositoryRoute.RECTAL:
            return f"{chill_note}মোড়ক খুলে সূচালো অংশটি মলদ্বারে প্রবেশ করান। ব্যবহারের পর ১৫ মিনিট শুয়ে থাকুন।"
        elif route == SuppositoryRoute.VAGINAL:
            return "মোড়ক খুলে রাতে ঘুমানোর পূর্বে যোনিপথে গভীরভাবে প্রবেশ করান।"
        return "চিকিৎসকের পরামর্শ অনুযায়ী সঠিক নিয়মে ব্যবহার করুন।"

    @classmethod
    def _translate_base(cls, code: str) -> str:
        mapping = {
            "FATTY_BASE_COCOA_BUTTER": "ফ্যাটি বেস / কোকো বাটার (শরীরের তাপে গলে)",
            "WATER_SOLUBLE_PEG": "পানিতে দ্রবণীয় পিইজি (ম্যাক্রোগল)",
            "GLYCERINATED_GELATIN": "গ্লিসারিনেটেড জেলাটিন বেস",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_route(cls, code: str) -> str:
        mapping = {
            "RECTAL": "মলদ্বারে প্রয়োগ (Rectal Suppository)",
            "VAGINAL": "যোনিপথে প্রয়োগ (Vaginal Pessary)",
            "URETHRAL": "মূত্রনালীতে প্রয়োগ (Urethral Bougie)",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_storage(cls, code: str) -> str:
        mapping = {
            "REFRIGERATED_2_8": "রেফ্রিজারেটর ২° থেকে ৮° সেলসিয়াসে সংরক্ষণ করুন",
            "COOL_BELOW_25": "২৫° সেলসিয়াসের নিচে ঠান্ডা স্থানে রাখুন",
            "COOL_BELOW_30": "৩০° সেলসিয়াসের নিচে শুষ্ক স্থানে রাখুন",
        }
        return mapping.get(code, code)
