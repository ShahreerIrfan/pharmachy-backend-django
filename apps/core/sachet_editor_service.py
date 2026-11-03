"""
backend/apps/core/sachet_editor_service.py
Architecture Decision: ADR-0149 (P149).
Dosage-Form Specific Editor Service for Sachets, Powders, and Reconstitution Granules.
Governs grams per sachet, sachets per pack, dimension consistency, dissolution guidance,
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
    PowderCategory,
    PowderDiluentType,
    PowderStorageCondition,
    SachetFormConfig,
    SachetRoute,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.sachet_editor")


class SachetEditorService:
    """
    Core domain service managing the Sachet & Powder Dosage-Form Editor.
    Enforces grams per sachet, multi-tier pack counts, break-bulk loose sachet eligibility,
    and fluid mixing/dissolution instructions.
    """

    HIDDEN_FIELDS = [
        "strips_per_box",
        "tablets_per_strip",
        "units_per_strip",
        "is_scored",
        "coating_type",
        "needle_gauge",
    ]

    ALLOWED_CATEGORIES = [c.value for c in PowderCategory]
    ALLOWED_ROUTES = [c.value for c in SachetRoute]
    ALLOWED_DILUENTS = [c.value for c in PowderDiluentType]
    ALLOWED_STORAGE_CONDITIONS = [c.value for c in PowderStorageCondition]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """Returns dynamic schema and validation rules for sachet formulations."""
        return {
            "schema_version": "1.0",
            "dosage_form_family": "SACHET_POWDER_GRANULES",
            "supported_categories": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_category(c.value)}
                for c in PowderCategory
            ],
            "supported_routes": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_route(c.value)}
                for c in SachetRoute
            ],
            "supported_diluents": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_diluent(c.value)}
                for c in PowderDiluentType
            ],
            "supported_storage_conditions": [
                {"code": c.value, "label_en": c.label, "label_bn": cls._translate_storage(c.value)}
                for c in PowderStorageCondition
            ],
            "min_grams_per_sachet": 0.5,
            "max_grams_per_sachet": 500.0,
            "min_sachets_per_pack": 1,
            "max_sachets_per_pack": 500,
            "forbidden_fields": cls.HIDDEN_FIELDS,
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates sachet configuration payload against clinical, reconstitution, and dimension rules.
        """
        # 1. Zero-trust isolation against solid oral tablet attributes
        for field in cls.HIDDEN_FIELDS:
            if field in payload and payload.get(field) is not None:
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is illegal for sachet/powder dosage form.",
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

        # 3. Powder category check
        powder_category = str(payload.get("powder_category", PowderCategory.ORAL_REHYDRATION_SALTS)).strip().upper()
        if powder_category not in cls.ALLOWED_CATEGORIES:
            raise ValidationError({
                "error_code": "ERR_INVALID_POWDER_CATEGORY",
                "detail": f"Invalid powder_category '{powder_category}'. Allowed: {cls.ALLOWED_CATEGORIES}.",
                "field": "powder_category",
            })

        # 4. Route check
        administration_route = str(payload.get("administration_route", SachetRoute.ORAL)).strip().upper()
        if administration_route not in cls.ALLOWED_ROUTES:
            raise ValidationError({
                "error_code": "ERR_INVALID_ADMINISTRATION_ROUTE",
                "detail": f"Invalid administration_route '{administration_route}'. Allowed: {cls.ALLOWED_ROUTES}.",
                "field": "administration_route",
            })

        # 5. Grams per sachet
        try:
            grams_per_sachet = Decimal(str(payload.get("grams_per_sachet", "10.25")))
            if grams_per_sachet <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_GRAMS_PER_SACHET",
                    "detail": "grams_per_sachet must be greater than 0.",
                    "field": "grams_per_sachet",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_GRAMS_PER_SACHET",
                "detail": "grams_per_sachet must be a valid positive numeric decimal.",
                "field": "grams_per_sachet",
            })

        # 6. Sachets per pack
        try:
            sachets_per_pack = int(payload.get("sachets_per_pack", 25))
            if sachets_per_pack < 1 or sachets_per_pack > 500:
                raise ValidationError({
                    "error_code": "ERR_INVALID_SACHETS_PER_PACK",
                    "detail": f"sachets_per_pack ({sachets_per_pack}) must be between 1 and 500.",
                    "field": "sachets_per_pack",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_SACHETS_PER_PACK",
                "detail": "sachets_per_pack must be an integer between 1 and 500.",
                "field": "sachets_per_pack",
            })

        # 7. Dimension consistency check (total_pack_weight_grams = grams_per_sachet * sachets_per_pack)
        total_pack_weight_grams = None
        raw_pack_weight = payload.get("total_pack_weight_grams")
        if raw_pack_weight is not None and str(raw_pack_weight).strip() != "":
            try:
                total_pack_weight_grams = Decimal(str(raw_pack_weight))
                expected_total = (grams_per_sachet * Decimal(str(sachets_per_pack))).quantize(Decimal("0.01"))
                actual_total = total_pack_weight_grams.quantize(Decimal("0.01"))
                if expected_total != actual_total:
                    raise ValidationError({
                        "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                        "detail": (
                            f"Dimension mismatch: total_pack_weight_grams ({actual_total}) must match "
                            f"grams_per_sachet ({grams_per_sachet}) x sachets_per_pack ({sachets_per_pack}) = {expected_total}."
                        ),
                        "field": "total_pack_weight_grams",
                    })
            except (InvalidOperation, TypeError):
                raise ValidationError({
                    "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                    "detail": "total_pack_weight_grams must be a valid numeric decimal.",
                    "field": "total_pack_weight_grams",
                })

        # 8. Dissolution & Reconstitution requirements
        requires_dissolution = bool(payload.get("requires_dissolution", True))
        diluent_type = str(payload.get("diluent_type", PowderDiluentType.POTABLE_WATER)).strip().upper()
        if diluent_type not in cls.ALLOWED_DILUENTS:
            diluent_type = PowderDiluentType.POTABLE_WATER

        diluent_volume_ml = None
        raw_vol = payload.get("diluent_volume_ml")
        if requires_dissolution:
            if raw_vol is None or str(raw_vol).strip() == "":
                raise ValidationError({
                    "error_code": "ERR_MISSING_DILUENT_VOLUME",
                    "detail": "diluent_volume_ml is required when requires_dissolution is True.",
                    "field": "diluent_volume_ml",
                })
            try:
                diluent_volume_ml = Decimal(str(raw_vol))
                if diluent_volume_ml <= Decimal("0"):
                    raise ValidationError({
                        "error_code": "ERR_MISSING_DILUENT_VOLUME",
                        "detail": "diluent_volume_ml must be greater than 0.",
                        "field": "diluent_volume_ml",
                    })
            except (InvalidOperation, TypeError):
                raise ValidationError({
                    "error_code": "ERR_MISSING_DILUENT_VOLUME",
                    "detail": "diluent_volume_ml must be a valid numeric decimal.",
                    "field": "diluent_volume_ml",
                })
        else:
            if raw_vol is not None and str(raw_vol).strip() != "":
                try:
                    diluent_volume_ml = Decimal(str(raw_vol))
                except (InvalidOperation, TypeError):
                    diluent_volume_ml = None

        # 9. Loose sales eligibility
        loose_sachet_sale_eligible = bool(payload.get("loose_sachet_sale_eligible", True))

        # 10. Reconstituted stability hours
        try:
            reconstituted_stability_hours = int(payload.get("reconstituted_stability_hours", 24))
            if reconstituted_stability_hours < 1 or reconstituted_stability_hours > 72:
                reconstituted_stability_hours = 24
        except (ValueError, TypeError):
            reconstituted_stability_hours = 24

        # 11. Storage condition
        storage_condition = str(payload.get("storage_condition", PowderStorageCondition.COOL_DRY_BELOW_30)).strip().upper()
        if storage_condition not in cls.ALLOWED_STORAGE_CONDITIONS:
            storage_condition = PowderStorageCondition.COOL_DRY_BELOW_30

        # 12. Bilingual patient counseling
        instructions_en = payload.get("patient_instructions_en")
        instructions_bn = payload.get("patient_instructions_bn")
        if not instructions_en:
            instructions_en = cls._generate_instructions_en(powder_category, requires_dissolution, diluent_volume_ml, reconstituted_stability_hours)
        if not instructions_bn:
            instructions_bn = cls._generate_instructions_bn(powder_category, requires_dissolution, diluent_volume_ml, reconstituted_stability_hours)

        return {
            "product_id": str(product_id).strip(),
            "powder_category": powder_category,
            "administration_route": administration_route,
            "grams_per_sachet": grams_per_sachet,
            "sachets_per_pack": sachets_per_pack,
            "total_pack_weight_grams": total_pack_weight_grams,
            "loose_sachet_sale_eligible": loose_sachet_sale_eligible,
            "requires_dissolution": requires_dissolution,
            "diluent_type": diluent_type,
            "diluent_volume_ml": diluent_volume_ml,
            "reconstituted_stability_hours": reconstituted_stability_hours,
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
        """Lists accessible sachet configurations."""
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = SachetFormConfig.objects.select_related("product", "tenant").all()

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
            cfg = SachetFormConfig.objects.select_related("product", "tenant").get(config_id=config_id)
        except (SachetFormConfig.DoesNotExist, ValueError):
            raise ValidationError({
                "error_code": "ERR_CONFIG_NOT_FOUND",
                "detail": f"Sachet form configuration '{config_id}' was not found.",
            })

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """Creates or updates a sachet configuration."""
        payload_tenant = payload.get("tenant_id")
        is_operator = cls._is_platform_operator(user)

        if is_operator:
            target_tenant_id = payload_tenant
        else:
            target_tenant_id = getattr(user, "tenant_id", None)
            if payload_tenant and payload_tenant != target_tenant_id:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "detail": "Cannot configure sachet settings for another tenant.",
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
                cfg = SachetFormConfig.objects.filter(config_id=config_id).first()
            except ValueError:
                cfg = None
            if not cfg:
                raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})
            if not is_operator and cfg.tenant_id != target_tenant_id:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "detail": "Cannot modify another tenant's config."})
        else:
            cfg = SachetFormConfig.objects.filter(product=product, tenant=target_tenant).first()
            if not cfg:
                cfg = SachetFormConfig(product=product, tenant=target_tenant)

        cfg.powder_category = validated["powder_category"]
        cfg.administration_route = validated["administration_route"]
        cfg.grams_per_sachet = validated["grams_per_sachet"]
        cfg.sachets_per_pack = validated["sachets_per_pack"]
        cfg.total_pack_weight_grams = validated["total_pack_weight_grams"]
        cfg.loose_sachet_sale_eligible = validated["loose_sachet_sale_eligible"]
        cfg.requires_dissolution = validated["requires_dissolution"]
        cfg.diluent_type = validated["diluent_type"]
        cfg.diluent_volume_ml = validated["diluent_volume_ml"]
        cfg.reconstituted_stability_hours = validated["reconstituted_stability_hours"]
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
            action="SACHET_CONFIG_UPSERTED",
            resource_type="SachetFormConfig",
            resource_id=str(cfg.config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "product_id": product_id,
                "powder_category": cfg.powder_category,
                "grams_per_sachet": str(cfg.grams_per_sachet),
                "sachets_per_pack": cfg.sachets_per_pack,
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> None:
        """Deletes a sachet configuration."""
        try:
            cfg = SachetFormConfig.objects.filter(config_id=config_id).first()
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
            action="SACHET_CONFIG_DELETED",
            resource_type="SachetFormConfig",
            resource_id=str(config_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted sachet config {config_id}."},
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
                    "detail": "Only Pharmacists and Store Owners are authorized to modify sachet formulations.",
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
    def _serialize_config(cls, cfg: SachetFormConfig) -> Dict[str, Any]:
        return {
            "config_id": str(cfg.config_id),
            "product_id": cfg.product_id,
            "product_name": getattr(cfg.product, "brand_name", cfg.product_id),
            "tenant_id": cfg.tenant_id,
            "powder_category": cfg.powder_category,
            "administration_route": cfg.administration_route,
            "grams_per_sachet": str(cfg.grams_per_sachet),
            "sachets_per_pack": cfg.sachets_per_pack,
            "total_pack_weight_grams": str(cfg.total_pack_weight_grams) if cfg.total_pack_weight_grams is not None else None,
            "loose_sachet_sale_eligible": cfg.loose_sachet_sale_eligible,
            "requires_dissolution": cfg.requires_dissolution,
            "diluent_type": cfg.diluent_type,
            "diluent_volume_ml": str(cfg.diluent_volume_ml) if cfg.diluent_volume_ml is not None else None,
            "reconstituted_stability_hours": cfg.reconstituted_stability_hours,
            "storage_condition": cfg.storage_condition,
            "patient_instructions_en": cfg.patient_instructions_en,
            "patient_instructions_bn": cfg.patient_instructions_bn,
            "is_active": cfg.is_active,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @classmethod
    def _generate_instructions_en(
        cls,
        category: str,
        dissolution: bool,
        volume: Optional[Decimal],
        stability: int,
    ) -> str:
        if category == PowderCategory.ORAL_REHYDRATION_SALTS and dissolution and volume:
            return (
                f"Dissolve entire packet in exactly {volume} mL of clean drinking water. "
                f"Stir until fully dissolved. Discard unused solution after {stability} hours."
            )
        elif category == PowderCategory.BULK_LAXATIVE_HUSK:
            return "Mix 1 sachet in a full glass of water or juice. Drink immediately followed by another glass of water."
        elif category == PowderCategory.TOPICAL_DUSTING_POWDER:
            return "Sprinkle thinly onto affected clean and dry skin folds. For external use only. Do not ingest."
        return "Consume as directed by physician or pharmacist."

    @classmethod
    def _generate_instructions_bn(
        cls,
        category: str,
        dissolution: bool,
        volume: Optional[Decimal],
        stability: int,
    ) -> str:
        if category == PowderCategory.ORAL_REHYDRATION_SALTS and dissolution and volume:
            return (
                f"সম্পূর্ণ স্যাশে ঠিক {volume} মিলি বিশুদ্ধ খাবার পানিতে সম্পূর্ণ গুলিয়ে নিন। "
                f"তৈরির {stability} ঘণ্টা পর অবশিষ্ট স্যালাইনের পানি ফেলে দিন।"
            )
        elif category == PowderCategory.BULK_LAXATIVE_HUSK:
            return "এক গ্লাস পানিতে স্যাশের গুঁড়া মিশিয়ে সঙ্গে সঙ্গে পান করুন এবং এরপর আরও এক গ্লাস পানি খান।"
        elif category == PowderCategory.TOPICAL_DUSTING_POWDER:
            return "আক্রান্ত পরিষ্কার ও শুকনো ত্বকে হালকাভাবে ছিটিয়ে দিন। শুধুমাত্র বাহ্যিক ব্যবহারের জন্য।"
        return "চিকিৎসক বা ফার্মাসিস্টের পরামর্শ অনুযায়ী সেবন করুন।"

    @classmethod
    def _translate_category(cls, code: str) -> str:
        mapping = {
            "ORAL_REHYDRATION_SALTS": "খাবার স্যালাইন (ORS)",
            "EFFERVESCENT_POWDER": "ইফারভেসেন্ট পাউডার / এন্টাসিড",
            "BULK_LAXATIVE_HUSK": "ইসবগুলের ভুষি / বাল্ক ল্যাক্সেটিভ",
            "SUSPENSION_GRANULES": "সাসপেনশন গ্র্যানিউলস / ড্রাই সিরাপ",
            "TOPICAL_DUSTING_POWDER": "টপিকাল ডাস্টিং পাউডার (বাহ্যিক মলম গুঁড়া)",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_route(cls, code: str) -> str:
        mapping = {
            "ORAL": "মুখের মাধ্যমে সেব্য (Oral Ingestion)",
            "TOPICAL": "ত্বকে বাহ্যিক প্রয়োগ (Cutaneous Topical)",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_diluent(cls, code: str) -> str:
        mapping = {
            "POTABLE_WATER": "বিশুদ্ধ খাবার পানি",
            "BOILED_COOLED_WATER": "ফুটানো ঠান্ডা পানি",
            "JUICE_OR_WATER": "পানি, ফলের রস বা দুধ",
            "NOT_APPLICABLE": "প্রযোজ্য নয়",
        }
        return mapping.get(code, code)

    @classmethod
    def _translate_storage(cls, code: str) -> str:
        mapping = {
            "ROOM_15_25": "১৫° থেকে ২৫° সেলসিয়াসে শুষ্ক স্থানে সংরক্ষণ করুন",
            "COOL_DRY_BELOW_30": "৩০° সেলসিয়াসের নিচে আর্দ্রতামুক্ত স্থানে রাখুন",
            "PROTECT_FROM_MOISTURE": "বায়ুরোধী প্যাকেটে আর্দ্রতা থেকে সুরক্ষিত রাখুন",
        }
        return mapping.get(code, code)
