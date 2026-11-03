"""
backend/apps/core/tablet_capsule_editor_service.py
Architecture Decision: ADR-0141 (P141).
Dosage-Form Specific Editor Service for Solid Oral Tablets and Capsules.
Governs dose strength, packaging hierarchy multipliers, loose-sale eligibility,
clinical route restrictions, and dimension consistency.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    MedicineProduct,
    PlanTier,
    SolidDosageFormType,
    SolidStoragePolicy,
    SolidStrengthUnit,
    TabletCapsuleFormConfig,
    TabletCoatingType,
    TabletRoute,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger("pharmacy.core.tablet_capsule_editor")


class TabletCapsuleEditorService:
    """
    Core domain service managing the Tablet & Capsule dosage form editor.
    Enforces packaging dimension consistency, loose-sale safety rules,
    clinical administration routes, and multi-tenant access boundaries.
    """

    HIDDEN_FIELDS = [
        "volume_ml",
        "concentration",
        "reconstitution_water_ml",
        "dropper_volume_ml",
        "needle_gauge",
        "is_reconstitution_required",
    ]

    ALLOWED_STRENGTH_UNITS = [SolidStrengthUnit.MG, SolidStrengthUnit.MCG, SolidStrengthUnit.G]
    ALLOWED_ROUTES = [
        TabletRoute.ORAL,
        TabletRoute.SUBLINGUAL,
        TabletRoute.BUCCAL,
        TabletRoute.CHEWABLE,
        TabletRoute.DISPERSIBLE,
        TabletRoute.EFFERVESCENT,
    ]

    @classmethod
    def get_editor_schema(cls) -> Dict[str, Any]:
        """
        Returns dynamic condition schema and field specifications for tablet and capsule editors.
        """
        return {
            "schema_version": "1.0",
            "dosage_form_family": "SOLID_ORAL",
            "supported_dosage_forms": [
                {"code": choice.value, "label_en": choice.label, "label_bn": cls._translate_form(choice.value)}
                for choice in SolidDosageFormType
            ],
            "sections": [
                {
                    "section_id": "clinical_spec",
                    "title_en": "Clinical Dosage Specification",
                    "title_bn": "ক্লিনিক্যাল ডোজ স্পেসিফিকেশন",
                    "fields": [
                        {
                            "field_key": "dose_strength",
                            "widget": "decimal_input",
                            "label_en": "Dose Strength",
                            "label_bn": "ডোজের মাত্রা",
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
                            "options": [{"value": u.value, "label": u.label} for u in SolidStrengthUnit],
                            "default": SolidStrengthUnit.MG.value,
                        },
                        {
                            "field_key": "route",
                            "widget": "dropdown_select",
                            "label_en": "Administration Route",
                            "label_bn": "সেবনের মাধ্যম",
                            "is_required": True,
                            "options": [{"value": r.value, "label": r.label} for r in TabletRoute],
                            "default": TabletRoute.ORAL.value,
                        },
                    ],
                },
                {
                    "section_id": "packaging_hierarchy",
                    "title_en": "Packaging Unit Conversion Hierarchy",
                    "title_bn": "প্যাকেজিং রূপান্তর স্তরবিন্যাস (Box -> Strip -> Unit)",
                    "fields": [
                        {
                            "field_key": "tablets_per_strip",
                            "widget": "number_stepper",
                            "label_en": "Tablets Per Strip",
                            "label_bn": "পাতা প্রতি ট্যাবলেট",
                            "is_required": True,
                            "min_value": 1,
                            "default": 10,
                        },
                        {
                            "field_key": "strips_per_box",
                            "widget": "number_stepper",
                            "label_en": "Strips Per Box",
                            "label_bn": "বক্স প্রতি পাতা",
                            "is_required": True,
                            "min_value": 1,
                            "default": 10,
                        },
                        {
                            "field_key": "box_size",
                            "widget": "computed_number",
                            "label_en": "Total Box Units",
                            "label_bn": "বক্সের মোট ট্যাবলেট সংখ্যা",
                            "is_required": True,
                            "computed_formula": "tablets_per_strip * strips_per_box",
                            "default": 100,
                        },
                        {
                            "field_key": "loose_sale_eligible",
                            "widget": "checkbox_toggle",
                            "label_en": "Loose Sale Eligible (POS)",
                            "label_bn": "খুচরা একক বিক্রয়যোগ্য (পিওএস)",
                            "is_required": False,
                            "default": True,
                            "help_en": "Allow dispensing individual unsealed tablets or partial strips at POS.",
                            "help_bn": "কাউন্টারে পাতা কেটে খুচরা ট্যাবলেট বিক্রির অনুমতি দিন।",
                        },
                    ],
                },
                {
                    "section_id": "storage_and_formulation",
                    "title_en": "Storage Policy & Coating",
                    "title_bn": "সংরক্ষণ নীতি ও কোটিং",
                    "fields": [
                        {
                            "field_key": "storage_policy",
                            "widget": "dropdown_select",
                            "label_en": "Storage Policy",
                            "label_bn": "সংরক্ষণ নীতি",
                            "is_required": True,
                            "options": [{"value": sp.value, "label": sp.label} for sp in SolidStoragePolicy],
                            "default": SolidStoragePolicy.ROOM_TEMPERATURE_DRY.value,
                        },
                        {
                            "field_key": "coating_type",
                            "widget": "dropdown_select",
                            "label_en": "Coating Type",
                            "label_bn": "কোটিং ধরণ",
                            "is_required": False,
                            "options": [{"value": ct.value, "label": ct.label} for ct in TabletCoatingType],
                            "default": TabletCoatingType.FILM_COATED.value,
                        },
                        {
                            "field_key": "is_scored",
                            "widget": "checkbox_toggle",
                            "label_en": "Scored Tablet (Splittable)",
                            "label_bn": "খাঁজকাটা ট্যাবলেট (বিভাজনযোগ্য)",
                            "is_required": False,
                            "default": False,
                        },
                    ],
                },
            ],
            "hidden_fields": cls.HIDDEN_FIELDS,
            "dimension_consistency_formula": "box_size == strips_per_box * tablets_per_strip",
            "business_rules": [
                {
                    "rule_id": "BR-TAB-01",
                    "name": "Dimension Consistency",
                    "description": "box_size must precisely equal strips_per_box multiplied by tablets_per_strip.",
                },
                {
                    "rule_id": "BR-TAB-02",
                    "name": "Loose Sale Safety Rule",
                    "description": "Effervescent tablets and moisture-sensitive solid formulations strictly prohibit loose sales.",
                },
                {
                    "rule_id": "BR-TAB-03",
                    "name": "Solid Administration Route",
                    "description": "Tablets and capsules require oral solid routes (Oral, Sublingual, Buccal, Chewable, Dispersible, Effervescent).",
                },
                {
                    "rule_id": "BR-TAB-04",
                    "name": "Solid Strength Units",
                    "description": "Strength measurement must use solid mass units (mg, mcg, g). Liquid volumetric units are rejected.",
                },
            ],
        }

    @classmethod
    def validate_payload(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates tablet/capsule input payload against domain rules and zero-trust hidden field guards.
        """
        if not isinstance(payload, dict):
            raise ValidationError({"error_code": "ERR_INVALID_PAYLOAD", "detail": "Payload must be a JSON object."})

        # 1. Zero-Trust Hidden Field Guard
        for field in cls.HIDDEN_FIELDS:
            val = payload.get(field)
            if val is not None and val != "":
                raise ValidationError({
                    "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                    "detail": f"Field '{field}' is hidden and prohibited for tablet and capsule dosage forms.",
                })

        # 2. Required Fields
        required_fields = [
            "dosage_form",
            "dose_strength",
            "strength_unit",
            "tablets_per_strip",
            "strips_per_box",
            "route",
            "storage_policy",
        ]
        for req in required_fields:
            if req not in payload or payload[req] is None or payload[req] == "":
                raise ValidationError({
                    "error_code": "ERR_REQUIRED_FIELD_MISSING",
                    "detail": f"Missing mandatory field '{req}' for tablet/capsule configuration.",
                })

        # 3. Dosage Form Validation
        dosage_form = str(payload.get("dosage_form")).strip().upper()
        valid_forms = [c.value for c in SolidDosageFormType]
        if dosage_form not in valid_forms:
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSAGE_FORM",
                "detail": f"Dosage form '{dosage_form}' is not a valid solid oral form. Valid choices: {valid_forms}.",
            })

        # 4. Strength Unit Validation
        strength_unit = str(payload.get("strength_unit")).strip().lower()
        if strength_unit not in [u.value for u in cls.ALLOWED_STRENGTH_UNITS]:
            raise ValidationError({
                "error_code": "ERR_INVALID_STRENGTH_UNIT_FOR_SOLID_FORM",
                "detail": f"Strength unit '{strength_unit}' is invalid for solid tablet/capsule. Allowed: mg, mcg, g.",
            })

        # 5. Dose Strength Numeric Validation
        try:
            dose_strength = Decimal(str(payload.get("dose_strength")))
            if dose_strength <= Decimal("0"):
                raise ValidationError({
                    "error_code": "ERR_INVALID_DOSE_STRENGTH",
                    "detail": "dose_strength must be a positive number greater than 0.",
                })
        except (InvalidOperation, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_DOSE_STRENGTH",
                "detail": "dose_strength must be a valid numeric decimal.",
            })

        # 6. Route Validation
        route = str(payload.get("route")).strip()
        valid_routes = [r.value for r in cls.ALLOWED_ROUTES]
        if route not in valid_routes:
            raise ValidationError({
                "error_code": "ERR_INVALID_ROUTE_FOR_DOSAGE_FORM",
                "detail": f"Route '{route}' is invalid for tablet/capsule forms. Valid routes: {valid_routes}.",
            })

        # 7. Packaging Multiplier Validation
        try:
            tablets_per_strip = int(payload.get("tablets_per_strip"))
            strips_per_box = int(payload.get("strips_per_box"))
            if tablets_per_strip < 1 or strips_per_box < 1:
                raise ValidationError({
                    "error_code": "ERR_INVALID_PACKAGING_MULTIPLIER",
                    "detail": "tablets_per_strip and strips_per_box must be positive integers >= 1.",
                })
        except (ValueError, TypeError):
            raise ValidationError({
                "error_code": "ERR_INVALID_PACKAGING_MULTIPLIER",
                "detail": "tablets_per_strip and strips_per_box must be valid integers.",
            })

        expected_box_size = tablets_per_strip * strips_per_box
        provided_box_size = payload.get("box_size")
        if provided_box_size is not None and provided_box_size != "":
            try:
                box_size = int(provided_box_size)
                if box_size != expected_box_size:
                    raise ValidationError({
                        "error_code": "ERR_DIMENSION_CONSISTENCY_MISMATCH",
                        "detail": (
                            f"Dimension consistency mismatch: box_size ({box_size}) does not equal "
                            f"strips_per_box ({strips_per_box}) * tablets_per_strip ({tablets_per_strip}) = {expected_box_size}."
                        ),
                    })
            except (ValueError, TypeError):
                raise ValidationError({
                    "error_code": "ERR_INVALID_BOX_SIZE",
                    "detail": "box_size must be a valid integer.",
                })
        else:
            box_size = expected_box_size

        # 8. Storage Policy
        storage_policy = str(payload.get("storage_policy", SolidStoragePolicy.ROOM_TEMPERATURE_DRY.value))
        valid_storage = [sp.value for sp in SolidStoragePolicy]
        if storage_policy not in valid_storage:
            raise ValidationError({
                "error_code": "ERR_INVALID_STORAGE_POLICY",
                "detail": f"storage_policy '{storage_policy}' is invalid. Allowed: {valid_storage}.",
            })

        # 9. Loose-Sale Eligibility Rules
        loose_sale_eligible = bool(payload.get("loose_sale_eligible", True))
        if dosage_form == SolidDosageFormType.EFFERVESCENT_TABLET.value and loose_sale_eligible:
            raise ValidationError({
                "error_code": "ERR_LOOSE_SALE_NOT_PERMITTED",
                "detail": "Loose sale is not permitted for effervescent tablets due to rapid atmospheric moisture degradation.",
            })
        if (
            storage_policy == SolidStoragePolicy.PROTECT_FROM_MOISTURE_LIGHT.value
            and dosage_form == SolidDosageFormType.EFFERVESCENT_TABLET
            and loose_sale_eligible
        ):
            raise ValidationError({
                "error_code": "ERR_LOOSE_SALE_NOT_PERMITTED",
                "detail": "Loose sale is strictly prohibited for moisture-sensitive formulations.",
            })

        coating_type = str(payload.get("coating_type", TabletCoatingType.FILM_COATED.value))
        is_scored = bool(payload.get("is_scored", False))

        return {
            "dosage_form": dosage_form,
            "dose_strength": dose_strength,
            "strength_unit": strength_unit,
            "tablets_per_strip": tablets_per_strip,
            "strips_per_box": strips_per_box,
            "box_size": box_size,
            "loose_sale_eligible": loose_sale_eligible,
            "route": route,
            "storage_policy": storage_policy,
            "is_scored": is_scored,
            "coating_type": coating_type,
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
        Lists accessible tablet/capsule configurations respecting tenant boundaries and global fallbacks.
        """
        cls._validate_user_permissions(user, tenant_id=tenant_id, action_type="read")

        qs = TabletCapsuleFormConfig.objects.select_related("product", "tenant").all()

        if cls._is_platform_operator(user):
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
        else:
            user_tenant = getattr(user, "tenant_id", None)
            # Show tenant configs or global configs (tenant=None)
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
        Retrieves a single tablet/capsule configuration.
        """
        cfg = TabletCapsuleFormConfig.objects.select_related("product", "tenant").filter(config_id=config_id).first()
        if not cfg:
            raise ValidationError({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Configuration '{config_id}' not found."})

        cls._validate_user_permissions(user, tenant_id=cfg.tenant_id, action_type="read")
        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def create_or_update_config(cls, payload: Dict[str, Any], user: PharmacyUser) -> Dict[str, Any]:
        """
        Creates or updates a tablet/capsule configuration.
        """
        # Determine target tenant
        payload_tenant = payload.get("tenant_id")
        is_operator = cls._is_platform_operator(user)

        if is_operator:
            target_tenant_id = payload_tenant  # Can be None for global catalog
        else:
            target_tenant_id = getattr(user, "tenant_id", None)
            if payload_tenant and payload_tenant != target_tenant_id:
                raise PermissionDenied({
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "detail": "Cannot configure tablet/capsule settings for another tenant.",
                })

        cls._validate_user_permissions(user, tenant_id=target_tenant_id, action_type="write")

        product_id = payload.get("product_id")
        if not product_id:
            raise ValidationError({"error_code": "ERR_PRODUCT_REQUIRED", "detail": "product_id is required."})

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise ValidationError({"error_code": "ERR_PRODUCT_NOT_FOUND", "detail": f"Product '{product_id}' not found."})

        # Validate domain rules
        validated_data = cls.validate_payload(payload)

        tenant_obj = Tenant.objects.filter(tenant_id=target_tenant_id).first() if target_tenant_id else None

        # Resolve existing config by config_id or (product, tenant)
        cfg = None
        given_config_id = payload.get("config_id")
        if given_config_id:
            cfg = TabletCapsuleFormConfig.objects.filter(config_id=given_config_id).first()
        if not cfg:
            cfg = TabletCapsuleFormConfig.objects.filter(product=product, tenant=tenant_obj).first()

        if cfg:
            cfg.dosage_form = validated_data["dosage_form"]
            cfg.dose_strength = validated_data["dose_strength"]
            cfg.strength_unit = validated_data["strength_unit"]
            cfg.tablets_per_strip = validated_data["tablets_per_strip"]
            cfg.strips_per_box = validated_data["strips_per_box"]
            cfg.box_size = validated_data["box_size"]
            cfg.loose_sale_eligible = validated_data["loose_sale_eligible"]
            cfg.route = validated_data["route"]
            cfg.storage_policy = validated_data["storage_policy"]
            cfg.is_scored = validated_data["is_scored"]
            cfg.coating_type = validated_data["coating_type"]
            cfg.is_active = validated_data["is_active"]
            cfg.save()
        else:
            config_id = given_config_id or f"tcc-{product.product_id}"
            if target_tenant_id and not config_id.endswith(target_tenant_id):
                config_id = f"{config_id}-{target_tenant_id}"
            cfg = TabletCapsuleFormConfig.objects.create(
                config_id=config_id,
                product=product,
                tenant=tenant_obj,
                dosage_form=validated_data["dosage_form"],
                dose_strength=validated_data["dose_strength"],
                strength_unit=validated_data["strength_unit"],
                tablets_per_strip=validated_data["tablets_per_strip"],
                strips_per_box=validated_data["strips_per_box"],
                box_size=validated_data["box_size"],
                loose_sale_eligible=validated_data["loose_sale_eligible"],
                route=validated_data["route"],
                storage_policy=validated_data["storage_policy"],
                is_scored=validated_data["is_scored"],
                coating_type=validated_data["coating_type"],
                is_active=validated_data["is_active"],
            )

        # Audit log
        user_id_val = str(getattr(user, "user_id", user.pk))
        AuditLogEntry.objects.create(
            tenant_id=target_tenant_id or "PLATFORM",
            user_id=user_id_val,
            actor_email=user.email or "system@pharmacy.local",
            actor_role=getattr(user, "role", "UNKNOWN"),
            action="TABLET_CAPSULE_CONFIG_UPSERTED",
            resource_type="TabletCapsuleFormConfig",
            resource_id=cfg.config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Upserted tablet/capsule configuration for product {product.product_id} (box_size={cfg.box_size}, loose_sale={cfg.loose_sale_eligible})."},
        )

        return cls._serialize_config(cfg)

    @classmethod
    @transaction.atomic
    def delete_config(cls, config_id: str, user: PharmacyUser) -> Dict[str, Any]:
        """
        Deletes a tenant-specific tablet/capsule configuration or marks global as inactive.
        """
        cfg = TabletCapsuleFormConfig.objects.filter(config_id=config_id).first()
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
            action="TABLET_CAPSULE_CONFIG_DELETED",
            resource_type="TabletCapsuleFormConfig",
            resource_id=config_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"details": f"Deleted tablet/capsule configuration for product {product_id}."},
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

        # Cashier role: read-only
        if role == UserRole.CASHIER and action_type == "write":
            raise PermissionDenied({
                "error_code": "ERR_PERMISSION_DENIED",
                "message_en": "Cashiers are not permitted to modify tablet/capsule form configurations.",
                "message_bn": "ক্যাশিয়ারদের ট্যাবলেট/ক্যাপসুল স্পেসিফিকেশন পরিবর্তন করার অনুমতি নেই।",
            })

        # Tenant cross-boundary check
        if tenant_id and tenant_id != user_tenant:
            raise PermissionDenied({
                "error_code": "ERR_CROSS_TENANT_VIOLATION",
                "message_en": "Access to another tenant's tablet/capsule configuration is prohibited.",
                "message_bn": "অন্য ফার্মেসির কনফিগারেশনে অ্যাক্সেস নিষিদ্ধ।",
            })

        # Check tenant lifecycle status
        tenant = Tenant.objects.filter(tenant_id=user_tenant).first()
        if tenant and tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied({
                "error_code": "ERR_TENANT_SUSPENDED",
                "message_en": "Tenant subscription account is suspended. Feature blocked.",
                "message_bn": "ফার্মেসির সাবস্ক্রিপশন অ্যাকাউন্ট স্থগিত থাকায় সেবা বন্ধ।",
            })

        # Subscription tier: BASIC tier cannot create customized packaging overrides
        if action_type == "write" and tenant and tenant.plan_tier == PlanTier.BASIC:
            raise PermissionDenied({
                "error_code": "ERR_SUBSCRIPTION_UNENTITLED",
                "message_en": "Multi-tier packaging editor requires a STANDARD or PRO subscription plan.",
                "message_bn": "মাল্টি-টিয়ার প্যাকেজিং এডিটর সুবিধার জন্য স্ট্যান্ডার্ড বা প্রো সাবস্ক্রিপশন আবশ্যক।",
            })

    @classmethod
    def _serialize_config(cls, cfg: TabletCapsuleFormConfig) -> Dict[str, Any]:
        return {
            "config_id": cfg.config_id,
            "product_id": cfg.product_id,
            "brand_name": getattr(cfg.product, "brand_name", ""),
            "generic_name": getattr(cfg.product, "generic_name", ""),
            "tenant_id": cfg.tenant_id,
            "dosage_form": cfg.dosage_form,
            "dose_strength": float(cfg.dose_strength),
            "strength_unit": cfg.strength_unit,
            "tablets_per_strip": cfg.tablets_per_strip,
            "strips_per_box": cfg.strips_per_box,
            "box_size": cfg.box_size,
            "loose_sale_eligible": cfg.loose_sale_eligible,
            "route": cfg.route,
            "storage_policy": cfg.storage_policy,
            "is_scored": cfg.is_scored,
            "coating_type": cfg.coating_type,
            "is_active": cfg.is_active,
            "created_at": cfg.created_at.isoformat() if cfg.created_at else None,
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @staticmethod
    def _translate_form(form_code: str) -> str:
        bn_map = {
            "TABLET": "স্ট্যান্ডার্ড ট্যাবলেট",
            "CAPSULE": "ক্যাপসুল",
            "CHEWABLE_TABLET": "চিউয়েবল ট্যাবলেট",
            "EFFERVESCENT_TABLET": "এফারভেসেন্ট ট্যাবলেট",
            "EXTENDED_RELEASE_TABLET": "এক্সটেন্ডেড রিলিজ ট্যাবলেট",
            "EXTENDED_RELEASE_CAPSULE": "এক্সটেন্ডেড রিলিজ ক্যাপসুল",
            "SUBLINGUAL_TABLET": "সাবলিঙ্গুয়াল ট্যাবলেট",
        }
        return bn_map.get(form_code, form_code)
