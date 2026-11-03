"""
backend/apps/core/suspension_reconstitution_service.py
Architecture Decision: ADR-0143 (P143).
Dosage-Form Specific Editor Service for Dry Powder Oral Suspensions and Reconstituted Syrups.
Governs diluent type, water volume, final reconstituted volume, multi-step instructions,
stability shelf life, beyond-use dates (BUD), and licensed pharmacist review verification.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    MedicineProduct,
    PlanTier,
    ReconstitutionDiluentType,
    ReconstitutionDosageFormType,
    ReconstitutionStoragePolicy,
    SuspensionReconstitutionConfig,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.suspension_reconstitution")


class SuspensionReconstitutionEditorService:
    """
    Core domain service managing the Suspension Reconstitution & Beyond-Use Date (BUD) editor.
    Enforces dimension consistency (final_volume >= water_volume), BUD stability limits,
    pharmacist-only review authorization, and zero-trust solid field isolation.
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

    ALLOWED_DOSAGE_FORMS = [c.value for c in ReconstitutionDosageFormType]
    ALLOWED_DILUENTS = [c.value for c in ReconstitutionDiluentType]
    ALLOWED_STORAGE_POLICIES = [c.value for c in ReconstitutionStoragePolicy]

    PHARMACIST_ROLES = [
        UserRole.STAFF_PHARMACIST,
        UserRole.PHARMACY_OWNER,
        UserRole.SUPERADMIN,
        UserRole.PLATFORM_OPERATOR,
        "PLATFORM_SUPER_ADMIN",
    ]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """
        Returns dynamic schema and validation rules for dry powder suspension reconstitution.
        """
        return {
            "schema_version": "1.0",
            "dosage_form_family": "RECONSTITUTED_SUSPENSION",
            "supported_dosage_forms": [
                {"code": choice.value, "label_en": choice.label, "label_bn": cls._translate_form(choice.value)}
                for choice in ReconstitutionDosageFormType
            ],
            "sections": [
                {
                    "section_id": "diluent_and_volumes",
                    "title_en": "Reconstitution Diluent & Volumes",
                    "title_bn": "মিশ্রণযোগ্য তরল ও আয়তন",
                    "fields": [
                        {
                            "field_key": "dosage_form",
                            "widget": "dropdown_select",
                            "label_en": "Dosage Form",
                            "label_bn": "ডোজের ধরন",
                            "is_required": True,
                            "options": [
                                {"value": c.value, "label": c.label} for c in ReconstitutionDosageFormType
                            ],
                            "default": ReconstitutionDosageFormType.DRY_SYRUP_RECONSTITUTED.value,
                        },
                        {
                            "field_key": "reconstitution_diluent",
                            "widget": "dropdown_select",
                            "label_en": "Reconstitution Diluent",
                            "label_bn": "প্রস্তুতকারক দ্রাবক / পানি",
                            "is_required": True,
                            "options": [
                                {"value": d.value, "label": d.label} for d in ReconstitutionDiluentType
                            ],
                            "default": ReconstitutionDiluentType.BOILED_COOLED_WATER.value,
                        },
                        {
                            "field_key": "water_volume_ml",
                            "widget": "decimal_input",
                            "label_en": "Diluent Water Volume (mL)",
                            "label_bn": "যোগ করার পানির পরিমাণ (মি.লি.)",
                            "is_required": True,
                            "min_value": 1.0,
                            "default": 35.0,
                        },
                        {
                            "field_key": "final_volume_ml",
                            "widget": "decimal_input",
                            "label_en": "Final Reconstituted Volume (mL)",
                            "label_bn": "প্রস্তুতকৃত মোট আয়তন (মি.লি.)",
                            "is_required": True,
                            "min_value": 1.0,
                            "default": 50.0,
                        },
                    ],
                },
                {
                    "section_id": "stability_and_bud",
                    "title_en": "Stability & Beyond-Use Date (BUD)",
                    "title_bn": "স্থায়িত্ব ও ব্যবহারের শেষ তারিখ",
                    "fields": [
                        {
                            "field_key": "shelf_life_days",
                            "widget": "number_stepper",
                            "label_en": "Reconstituted Shelf Life (Days)",
                            "label_bn": "মিশ্রণের পর স্থায়িত্বকাল (দিন)",
                            "is_required": True,
                            "min_value": 1,
                            "max_value": 60,
                            "default": 7,
                        },
                        {
                            "field_key": "opening_date",
                            "widget": "date_picker",
                            "label_en": "Preparation / Opening Date",
                            "label_bn": "প্রস্তুতকরণ বা বোতল খোলার তারিখ",
                            "is_required": False,
                        },
                        {
                            "field_key": "beyond_use_date",
                            "widget": "date_picker",
                            "label_en": "Beyond-Use Date (BUD)",
                            "label_bn": "ব্যবহারের শেষ তারিখ (বিইউডি)",
                            "is_required": False,
                        },
                        {
                            "field_key": "storage_policy",
                            "widget": "dropdown_select",
                            "label_en": "Post-Reconstitution Storage",
                            "label_bn": "মিশ্রণ-পরবর্তী সংরক্ষণ পদ্ধতি",
                            "is_required": True,
                            "options": [
                                {"value": sp.value, "label": sp.label} for sp in ReconstitutionStoragePolicy
                            ],
                            "default": ReconstitutionStoragePolicy.REFRIGERATE_2_8C_AFTER_RECONSTITUTION.value,
                        },
                    ],
                },
                {
                    "section_id": "instructions_and_counseling",
                    "title_en": "Preparation Steps & Patient Counseling",
                    "title_bn": "প্রস্তুত প্রণালী ও রোগীর পরামর্শ নির্দেশনা",
                    "fields": [
                        {
                            "field_key": "instructions_en",
                            "widget": "textarea",
                            "label_en": "Preparation Instructions (EN)",
                            "label_bn": "প্রস্তুত প্রণালী (ইংরেজি)",
                            "is_required": True,
                            "default": "Boil drinking water, let cool to room temperature. Add half the water, shake well to disperse powder, then add remaining water up to the mark.",
                        },
                        {
                            "field_key": "instructions_bn",
                            "widget": "textarea",
                            "label_en": "Preparation Instructions (BN)",
                            "label_bn": "প্রস্তুত প্রণালী (বাংলা)",
                            "is_required": False,
                            "default": "ফুটিয়ে ঠাণ্ডা করা পানি বোতলের নির্দিষ্ট দাগ পর্যন্ত দুই ধাপে মিশিয়ে ভালো করে ঝাঁকিয়ে নিন।",
                        },
                        {
                            "field_key": "handling_guidance_en",
                            "widget": "textarea",
                            "label_en": "Handling Guidance (EN)",
                            "label_bn": "ব্যবহার নির্দেশিকা (ইংরেজি)",
                            "is_required": False,
                            "default": "Shake well before each use. Store in refrigerator (2°C - 8°C). Discard unused portion after beyond-use date.",
                        },
                        {
                            "field_key": "handling_guidance_bn",
                            "widget": "textarea",
                            "label_en": "Handling Guidance (BN)",
                            "label_bn": "ব্যবহার নির্দেশিকা (বাংলা)",
                            "is_required": False,
                            "default": "প্রতিবার সেবনের পূর্বে বোতলটি ভালো করে ঝাঁকিয়ে নিন। নির্দেশিত মেয়াদের পর অবশিষ্ট ওষুধ সেবন করবেন না।",
                        },
                    ],
                },
                {
                    "section_id": "pharmacist_review",
                    "title_en": "Pharmacist Clinical Review",
                    "title_bn": "ফার্মাসিস্ট ক্লিনিক্যাল পর্যালোচনা",
                    "fields": [
                        {
                            "field_key": "is_pharmacist_reviewed",
                            "widget": "checkbox_toggle",
                            "label_en": "Pharmacist Reviewed & Approved",
                            "label_bn": "ফার্মাসিস্ট দ্বারা পর্যালোচিত ও অনুমোদিত",
                            "is_required": False,
                            "default": False,
                        },
                        {
                            "field_key": "reviewer_notes",
                            "widget": "textarea",
                            "label_en": "Reviewer Notes",
                            "label_bn": "পর্যালোচকের মন্তব্য",
                            "is_required": False,
                            "default": "",
                        },
                    ],
                },
            ],
            "hidden_fields": cls.HIDDEN_FIELDS,
            "business_rules": [
                {
                    "rule_id": "BR-REC-01",
                    "name": "Volume Consistency",
                    "description": "Reconstituted final volume must be greater than or equal to diluent water volume (final_volume_ml >= water_volume_ml).",
                },
                {
                    "rule_id": "BR-REC-02",
                    "name": "Beyond-Use Date Stability",
                    "description": "Beyond-use date is computed as opening_date + shelf_life_days. BUD cannot be prior to opening date or exceed maximum stability.",
                },
                {
                    "rule_id": "BR-REC-03",
                    "name": "Pharmacist Review Requirement",
                    "description": "Approval of clinical handling guidance requires verification by a registered pharmacist or owner.",
                },
                {
                    "rule_id": "BR-REC-04",
                    "name": "Solid Field Isolation",
                    "description": "Solid oral strip/box attributes are strictly forbidden for suspension reconstitution editors.",
                },
            ],
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates suspension reconstitution payload against clinical rules and zero-trust guards.
        """
        if not isinstance(payload, dict):
            raise ValidationError({"error_code": "ERR_INVALID_PAYLOAD", "detail": "Payload must be a JSON object."})

        # 1. Zero-Trust Hidden Field Guard (Reject solid oral tablet/strip fields)
        for field in cls.HIDDEN_FIELDS:
            val = payload.get(field)
            if val is not None and val != "":
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is a solid oral packaging attribute and prohibited for suspension reconstitution forms.",
                })

        # 2. Required Fields
        required_fields = [
            "dosage_form",
            "reconstitution_diluent",
            "water_volume_ml",
            "final_volume_ml",
            "shelf_life_days",
            "instructions_en",
            "storage_policy",
        ]
        for req in required_fields:
            if req not in payload or payload[req] is None or payload[req] == "":
                raise ValidationError({
                    "error_code": "ERR_REQUIRED_FIELD_MISSING",
                    "detail": f"Missing mandatory field '{req}' for suspension reconstitution configuration.",
                })

        # 3. Dosage Form Validation
        dosage_form = str(payload.get("dosage_form")).strip().upper()
        if dosage_form not in cls.ALLOWED_DOSAGE_FORMS:
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSAGE_FORM",
                "detail": f"Dosage form '{dosage_form}' is invalid. Allowed: {cls.ALLOWED_DOSAGE_FORMS}.",
            })

        # 4. Diluent Type Validation
        reconstitution_diluent = str(payload.get("reconstitution_diluent")).strip().upper()
        if reconstitution_diluent not in cls.ALLOWED_DILUENTS:
            raise ValidationError({
                "error_code": "ERR_INVALID_DILUENT_TYPE",
                "detail": f"Diluent '{reconstitution_diluent}' is invalid. Allowed: {cls.ALLOWED_DILUENTS}.",
            })

        # 5. Storage Policy Validation
        storage_policy = str(payload.get("storage_policy")).strip()
        if storage_policy not in cls.ALLOWED_STORAGE_POLICIES:
            raise ValidationError({
                "error_code": "ERR_INVALID_STORAGE_POLICY",
                "detail": f"storage_policy '{storage_policy}' is invalid. Allowed: {cls.ALLOWED_STORAGE_POLICIES}.",
            })

        # 6. Water Volume Validation
        try:
            water_volume_ml = Decimal(str(payload.get("water_volume_ml")))
            if water_volume_ml <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_WATER_VOLUME",
                    "detail": "water_volume_ml must be greater than 0 mL.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_WATER_VOLUME",
                "detail": "water_volume_ml must be a valid numeric decimal.",
            })

        # 7. Final Volume Validation
        try:
            final_volume_ml = Decimal(str(payload.get("final_volume_ml")))
            if final_volume_ml <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_FINAL_VOLUME",
                    "detail": "final_volume_ml must be greater than 0 mL.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_FINAL_VOLUME",
                "detail": "final_volume_ml must be a valid numeric decimal.",
            })

        # 8. Dimension Consistency: final_volume_ml >= water_volume_ml
        if final_volume_ml < water_volume_ml:
            raise ValidationError({
                "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                "detail": (
                    f"Reconstituted final volume ({final_volume_ml} mL) cannot be less than "
                    f"diluent water volume ({water_volume_ml} mL)."
                ),
            })

        # 9. Shelf Life Days Validation (1 - 60 days)
        try:
            shelf_life_days = int(payload.get("shelf_life_days"))
            if shelf_life_days < 1 or shelf_life_days > 60:
                raise ValidationError({
                    "error_code": "ERR_INVALID_SHELF_LIFE",
                    "detail": "shelf_life_days must be an integer between 1 and 60 days.",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_SHELF_LIFE",
                "detail": "shelf_life_days must be a valid integer.",
            })

        # 10. Opening Date & Beyond-Use Date (BUD) Validation
        raw_opening_date = payload.get("opening_date")
        raw_bud = payload.get("beyond_use_date")

        parsed_opening_date = None
        parsed_bud = None

        if raw_opening_date:
            try:
                if isinstance(raw_opening_date, date):
                    parsed_opening_date = raw_opening_date
                else:
                    parsed_opening_date = date.fromisoformat(str(raw_opening_date).strip())
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_OPENING_DATE",
                    "detail": f"opening_date '{raw_opening_date}' is not a valid ISO date (YYYY-MM-DD).",
                })

        if raw_bud:
            try:
                if isinstance(raw_bud, date):
                    parsed_bud = raw_bud
                else:
                    parsed_bud = date.fromisoformat(str(raw_bud).strip())
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_BEYOND_USE_DATE",
                    "detail": f"beyond_use_date '{raw_bud}' is not a valid ISO date (YYYY-MM-DD).",
                })

        if parsed_opening_date:
            max_bud = parsed_opening_date + timedelta(days=shelf_life_days)
            if parsed_bud is None:
                parsed_bud = max_bud
            else:
                if parsed_bud < parsed_opening_date:
                    raise ValidationError({
                        "error_code": "ERR_INVALID_BEYOND_USE_DATE",
                        "detail": (
                            f"Beyond-use date ({parsed_bud.isoformat()}) cannot be prior to "
                            f"opening date ({parsed_opening_date.isoformat()})."
                        ),
                    })
                if parsed_bud > max_bud:
                    raise ValidationError({
                        "error_code": "ERR_INVALID_BEYOND_USE_DATE",
                        "detail": (
                            f"Beyond-use date ({parsed_bud.isoformat()}) exceeds maximum shelf life "
                            f"({shelf_life_days} days from opening date: {max_bud.isoformat()})."
                        ),
                    })

        # 11. Instructions & Guidance Validation
        instructions_en = str(payload.get("instructions_en", "")).strip()
        if len(instructions_en) < 5:
            raise ValidationError({
                "error_code": "ERR_REQUIRED_FIELD_MISSING",
                "detail": "instructions_en must contain at least 5 characters describing preparation steps.",
            })
        instructions_bn = str(payload.get("instructions_bn", "")).strip()
        handling_guidance_en = str(payload.get("handling_guidance_en", "")).strip()
        handling_guidance_bn = str(payload.get("handling_guidance_bn", "")).strip()
        is_pharmacist_reviewed = bool(payload.get("is_pharmacist_reviewed", False))
        reviewer_notes = str(payload.get("reviewer_notes", "")).strip()

        return {
            "dosage_form": dosage_form,
            "reconstitution_diluent": reconstitution_diluent,
            "water_volume_ml": water_volume_ml,
            "final_volume_ml": final_volume_ml,
            "shelf_life_days": shelf_life_days,
            "opening_date": parsed_opening_date,
            "beyond_use_date": parsed_bud,
            "storage_policy": storage_policy,
            "instructions_en": instructions_en,
            "instructions_bn": instructions_bn,
            "handling_guidance_en": handling_guidance_en,
            "handling_guidance_bn": handling_guidance_bn,
            "is_pharmacist_reviewed": is_pharmacist_reviewed,
            "reviewer_notes": reviewer_notes,
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
        Lists accessible suspension reconstitution configurations with multi-tenant and global monograph filtering.
        """
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = SuspensionReconstitutionConfig.objects.select_related("product", "tenant", "reviewed_by").all()

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
        Retrieves a single suspension reconstitution configuration.
        """
        cfg = SuspensionReconstitutionConfig.objects.select_related("product", "tenant", "reviewed_by").filter(
            config_id=config_id
        ).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """
        Creates or updates a suspension reconstitution configuration with pharmacist review checks.
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
                    "detail": "Cannot configure suspension reconstitution settings for another tenant.",
                })

        cls._validate_user_permissions(user, tenant_id=target_tenant_id, action_type="write")

        validated_data = cls.validate_payload(payload)

        # Pharmacist review verification check
        if validated_data["is_pharmacist_reviewed"]:
            user_role = getattr(user, "role", "")
            if not is_operator and user_role not in cls.PHARMACIST_ROLES:
                raise PermissionDenied({
                    "error_code": "ERR_PHARMACIST_REVIEW_REQUIRED",
                    "message_en": "Reconstitution handling guidance approval requires licensed pharmacist or owner credentials.",
                    "message_bn": "সাসপেনশন প্রস্তুতকরণ নির্দেশনা অনুমোদনের জন্য রেজিস্টার্ড ফার্মাসিস্ট বা মালিকের অনুমতি আবশ্যক।",
                })

        product_id = payload.get("product_id")
        if not product_id:
            raise ValidationError({"error_code": "ERR_PRODUCT_REQUIRED", "detail": "product_id is required."})

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise ValidationError({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{product_id}' not found."})

        tenant_obj = Tenant.objects.filter(tenant_id=target_tenant_id).first() if target_tenant_id else None

        # Resolve existing config by config_id or (product, tenant)
        cfg = None
        given_config_id = payload.get("config_id")
        if given_config_id:
            cfg = SuspensionReconstitutionConfig.objects.filter(config_id=given_config_id).first()
        if not cfg:
            cfg = SuspensionReconstitutionConfig.objects.filter(product=product, tenant=tenant_obj).first()

        now = timezone.now()
        reviewed_by_user = user if validated_data["is_pharmacist_reviewed"] else None
        reviewed_at_val = now if validated_data["is_pharmacist_reviewed"] else None

        if cfg:
            cfg.dosage_form = validated_data["dosage_form"]
            cfg.reconstitution_diluent = validated_data["reconstitution_diluent"]
            cfg.water_volume_ml = validated_data["water_volume_ml"]
            cfg.final_volume_ml = validated_data["final_volume_ml"]
            cfg.shelf_life_days = validated_data["shelf_life_days"]
            cfg.opening_date = validated_data["opening_date"]
            cfg.beyond_use_date = validated_data["beyond_use_date"]
            cfg.storage_policy = validated_data["storage_policy"]
            cfg.instructions_en = validated_data["instructions_en"]
            cfg.instructions_bn = validated_data["instructions_bn"]
            cfg.handling_guidance_en = validated_data["handling_guidance_en"]
            cfg.handling_guidance_bn = validated_data["handling_guidance_bn"]
            cfg.is_pharmacist_reviewed = validated_data["is_pharmacist_reviewed"]
            if validated_data["is_pharmacist_reviewed"]:
                cfg.reviewed_by = reviewed_by_user
                cfg.reviewed_at = reviewed_at_val
            else:
                cfg.reviewed_by = None
                cfg.reviewed_at = None
            cfg.reviewer_notes = validated_data["reviewer_notes"]
            cfg.is_active = validated_data["is_active"]
            cfg.save()
        else:
            config_id = given_config_id or f"src-{product.product_id}"
            if target_tenant_id and not config_id.endswith(target_tenant_id):
                config_id = f"{config_id}-{target_tenant_id}"
            cfg = SuspensionReconstitutionConfig.objects.create(
                config_id=config_id,
                product=product,
                tenant=tenant_obj,
                dosage_form=validated_data["dosage_form"],
                reconstitution_diluent=validated_data["reconstitution_diluent"],
                water_volume_ml=validated_data["water_volume_ml"],
                final_volume_ml=validated_data["final_volume_ml"],
                shelf_life_days=validated_data["shelf_life_days"],
                opening_date=validated_data["opening_date"],
                beyond_use_date=validated_data["beyond_use_date"],
                storage_policy=validated_data["storage_policy"],
                instructions_en=validated_data["instructions_en"],
                instructions_bn=validated_data["instructions_bn"],
                handling_guidance_en=validated_data["handling_guidance_en"],
                handling_guidance_bn=validated_data["handling_guidance_bn"],
                is_pharmacist_reviewed=validated_data["is_pharmacist_reviewed"],
                reviewed_by=reviewed_by_user,
                reviewed_at=reviewed_at_val,
                reviewer_notes=validated_data["reviewer_notes"],
                is_active=validated_data["is_active"],
            )

        # Audit logging
        user_id_val = str(getattr(user, "user_id", user.pk))
        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=user_id_val,
            actor_email=user.email or "system@pharmacy.local",
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="SUSPENSION_RECONSTITUTION_CONFIG_UPSERTED",
            resource_type="SuspensionReconstitutionConfig",
            resource_id=cfg.config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "details": (
                    f"Upserted suspension reconstitution config for product {product.product_id} "
                    f"({cfg.water_volume_ml}mL -> {cfg.final_volume_ml}mL, BUD {cfg.beyond_use_date})."
                )
            },
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """
        Deletes a suspension reconstitution configuration.
        """
        cfg = SuspensionReconstitutionConfig.objects.filter(config_id=config_id).first()
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
            action="SUSPENSION_RECONSTITUTION_CONFIG_DELETED",
            resource_type="SuspensionReconstitutionConfig",
            resource_id=config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted suspension reconstitution config for product {product_id}."},
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
                "message_en": "Cashiers are not permitted to modify suspension reconstitution configurations.",
                "message_bn": "ক্যাশিয়ারদের সাসপেনশন কনফিগারেশন পরিবর্তন করার অনুমতি নেই।",
            })

        if tenant_id and tenant_id != user_tenant:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_VIOLATION",
                "message_en": "Access to another tenant's suspension configuration is prohibited.",
                "message_bn": "অন্য ফার্মেসির সাসপেনশন কনফিগারেশনে অ্যাক্সেস নিষিদ্ধ।",
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
                "message_en": "Custom suspension reconstitution configuration requires a STANDARD or PRO subscription plan.",
                "message_bn": "কাস্টম সাসপেনশন কনফিগারেশনের জন্য স্ট্যান্ডার্ড বা প্রো সাবস্ক্রিপশন আবশ্যক।",
            })

    @classmethod
    def _serialize_config(cls, cfg: SuspensionReconstitutionConfig) -> Dict[str, Any]:
        return {
            "config_id": cfg.config_id,
            "product_id": cfg.product_id,
            "brand_name": getattr(cfg.product, "brand_name", ""),
            "generic_name": getattr(cfg.product, "generic_name", ""),
            "tenant_id": cfg.tenant_id,
            "dosage_form": cfg.dosage_form,
            "reconstitution_diluent": cfg.reconstitution_diluent,
            "water_volume_ml": float(cfg.water_volume_ml),
            "final_volume_ml": float(cfg.final_volume_ml),
            "shelf_life_days": cfg.shelf_life_days,
            "opening_date": cfg.opening_date.isoformat() if cfg.opening_date else None,
            "beyond_use_date": cfg.beyond_use_date.isoformat() if cfg.beyond_use_date else None,
            "storage_policy": cfg.storage_policy,
            "instructions_en": cfg.instructions_en,
            "instructions_bn": cfg.instructions_bn,
            "handling_guidance_en": cfg.handling_guidance_en,
            "handling_guidance_bn": cfg.handling_guidance_bn,
            "is_pharmacist_reviewed": cfg.is_pharmacist_reviewed,
            "reviewed_by_id": str(cfg.reviewed_by.user_id) if cfg.reviewed_by else None,
            "reviewed_by_name": getattr(cfg.reviewed_by, "full_name", "") if cfg.reviewed_by else None,
            "reviewed_at": cfg.reviewed_at.isoformat() if cfg.reviewed_at else None,
            "reviewer_notes": cfg.reviewer_notes,
            "is_active": cfg.is_active,
            "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @staticmethod
    def _translate_form(form_code: str) -> str:
        bn_map = {
            "SUSPENSION": "সাসপেনশন",
            "DRY_SYRUP_RECONSTITUTED": "ড্রাই সিরাপ / পাউডার সাসপেনশন",
            "ORAL_POWDER_SUSPENSION": "ওরাল পাউডার ফর সাসপেনশন",
        }
        return bn_map.get(form_code, form_code)
