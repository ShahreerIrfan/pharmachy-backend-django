"""
Dosage Form Metadata Registry Service.
Architecture Decision: ADR-0122 (P122).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import DosageFormDefinition

logger = logging.getLogger("pharmacy.core.dosage_form")


class DosageFormRegistryService:
    """Core domain logic for managing dosage form metadata, field groups, and packaging rules."""

    CANONICAL_FORMS = [
        {
            "form_id": "df-tablet",
            "form_code": "TABLET",
            "name_en": "Tablet",
            "name_bn": "ট্যাবলেট",
            "category": "SOLID",
            "default_route": "Oral",
            "is_strip_applicable": True,
            "is_volume_applicable": False,
            "is_cold_chain_applicable": False,
            "display_order": 10,
            "field_groups": [
                {
                    "group_id": "packaging",
                    "name_en": "Packaging Unit Conversion",
                    "name_bn": "প্যাকেজিং রূপান্তর",
                    "fields": ["box_size", "strip_size", "tablet_count"],
                    "required": True,
                },
                {
                    "group_id": "clinical",
                    "name_en": "Clinical Parameters",
                    "name_bn": "ক্লিনিক্যাল পরিমিতি",
                    "fields": ["strength", "route"],
                    "required": True,
                },
            ],
        },
        {
            "form_id": "df-capsule",
            "form_code": "CAPSULE",
            "name_en": "Capsule",
            "name_bn": "ক্যাপসুল",
            "category": "SOLID",
            "default_route": "Oral",
            "is_strip_applicable": True,
            "is_volume_applicable": False,
            "is_cold_chain_applicable": False,
            "display_order": 20,
            "field_groups": [
                {
                    "group_id": "packaging",
                    "name_en": "Packaging Unit Conversion",
                    "name_bn": "প্যাকেজিং রূপান্তর",
                    "fields": ["box_size", "strip_size", "capsule_count"],
                    "required": True,
                },
            ],
        },
        {
            "form_id": "df-syrup",
            "form_code": "SYRUP",
            "name_en": "Syrup",
            "name_bn": "সিরাপ",
            "category": "LIQUID",
            "default_route": "Oral",
            "is_strip_applicable": False,
            "is_volume_applicable": True,
            "is_cold_chain_applicable": False,
            "display_order": 30,
            "field_groups": [
                {
                    "group_id": "volume",
                    "name_en": "Volume & Bottle Packaging",
                    "name_bn": "বোতল ও আয়তন",
                    "fields": ["bottle_volume_ml", "measuring_cup_included"],
                    "required": True,
                },
            ],
        },
        {
            "form_id": "df-suspension",
            "form_code": "SUSPENSION",
            "name_en": "Suspension",
            "name_bn": "সাসপেনশন",
            "category": "LIQUID",
            "default_route": "Oral",
            "is_strip_applicable": False,
            "is_volume_applicable": True,
            "is_cold_chain_applicable": False,
            "display_order": 40,
            "field_groups": [
                {
                    "group_id": "volume",
                    "name_en": "Reconstitution & Volume",
                    "name_bn": "তৈরিকরণ ও আয়তন",
                    "fields": ["reconstitution_water_ml", "bottle_volume_ml"],
                    "required": True,
                },
            ],
        },
        {
            "form_id": "df-injection",
            "form_code": "INJECTION",
            "name_en": "Injection",
            "name_bn": "ইনজেকশন",
            "category": "INJECTABLE",
            "default_route": "Intravenous",
            "is_strip_applicable": False,
            "is_volume_applicable": True,
            "is_cold_chain_applicable": True,
            "display_order": 50,
            "field_groups": [
                {
                    "group_id": "vial_ampoule",
                    "name_en": "Vial / Ampoule Specification",
                    "name_bn": "ভায়াল / অ্যাম্পুল স্পেসিফিকেশন",
                    "fields": ["vial_type", "pack_count", "requires_cold_chain"],
                    "required": True,
                },
            ],
        },
        {
            "form_id": "df-eye-drops",
            "form_code": "EYE_DROPS",
            "name_en": "Eye Drops",
            "name_bn": "আই ড্রপস",
            "category": "LIQUID",
            "default_route": "Ophthalmic",
            "is_strip_applicable": False,
            "is_volume_applicable": True,
            "is_cold_chain_applicable": False,
            "display_order": 60,
            "field_groups": [
                {
                    "group_id": "volume",
                    "name_en": "Dropper Volume",
                    "name_bn": "ড্রপার আয়তন",
                    "fields": ["bottle_volume_ml"],
                    "required": True,
                },
            ],
        },
        {
            "form_id": "df-ointment",
            "form_code": "OINTMENT",
            "name_en": "Ointment / Cream",
            "name_bn": "মলম / ক্রিম",
            "category": "SEMI_SOLID",
            "default_route": "Topical",
            "is_strip_applicable": False,
            "is_volume_applicable": False,
            "is_cold_chain_applicable": False,
            "display_order": 70,
            "field_groups": [
                {
                    "group_id": "tube",
                    "name_en": "Tube Net Weight",
                    "name_bn": "টিউব নিট ওজন",
                    "fields": ["weight_grams"],
                    "required": True,
                },
            ],
        },
        {
            "form_id": "df-inhaler",
            "form_code": "INHALER",
            "name_en": "Inhaler",
            "name_bn": "ইনহেলার",
            "category": "INHALATION",
            "default_route": "Inhalation",
            "is_strip_applicable": False,
            "is_volume_applicable": False,
            "is_cold_chain_applicable": False,
            "display_order": 80,
            "field_groups": [
                {
                    "group_id": "metered_dose",
                    "name_en": "Metered Puffs",
                    "name_bn": "মিটারড পাফস",
                    "fields": ["puff_count", "actuator_type"],
                    "required": True,
                },
            ],
        },
    ]

    @classmethod
    def ensure_canonical_forms_seeded(cls):
        """Seeds canonical DGDA dosage form definitions idempotently."""
        for item in cls.CANONICAL_FORMS:
            DosageFormDefinition.objects.get_or_create(
                form_code=item["form_code"],
                defaults={
                    "form_id": item["form_id"],
                    "name_en": item["name_en"],
                    "name_bn": item["name_bn"],
                    "category": item["category"],
                    "default_route": item["default_route"],
                    "is_strip_applicable": item["is_strip_applicable"],
                    "is_volume_applicable": item["is_volume_applicable"],
                    "is_cold_chain_applicable": item["is_cold_chain_applicable"],
                    "display_order": item["display_order"],
                    "is_active": True,
                    "field_groups": item["field_groups"],
                },
            )

    @classmethod
    def _require_platform_admin(cls, actor_user: Any):
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_AUTHENTICATION_REQUIRED", "message_en": "Authentication required."})

        is_super = getattr(actor_user, "is_superuser", False) or str(getattr(actor_user, "role", "")).upper() in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]
        if not is_super:
            raise PermissionDenied(
                {
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": "Dosage form definitions are centrally managed by platform administrators.",
                    "message_bn": "ডোসেজ ফরম সংজ্ঞা কেন্দ্রীয়ভাবে সংরক্ষিত। কোনো ফার্মেসি এটি পরিবর্তন করতে পারবে না।",
                }
            )

    @classmethod
    def list_forms(
        cls,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Lists registered dosage forms ordered by display_order."""
        cls.ensure_canonical_forms_seeded()
        qs = DosageFormDefinition.objects.all()
        if category:
            qs = qs.filter(category=category.upper())
        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        results = []
        for df in qs.order_by("display_order", "name_en"):
            results.append(cls.serialize_form(df))
        return results

    @classmethod
    def serialize_form(cls, df: DosageFormDefinition) -> Dict[str, Any]:
        return {
            "form_id": df.form_id,
            "form_code": df.form_code,
            "name_en": df.name_en,
            "name_bn": df.name_bn,
            "category": df.category,
            "default_route": df.default_route,
            "is_strip_applicable": df.is_strip_applicable,
            "is_volume_applicable": df.is_volume_applicable,
            "is_cold_chain_applicable": df.is_cold_chain_applicable,
            "display_order": df.display_order,
            "is_active": df.is_active,
            "field_groups": df.field_groups or [],
        }

    @classmethod
    def create_form(cls, actor_user: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new dosage form definition with validation."""
        cls._require_platform_admin(actor_user)
        form_code = str(data.get("form_code", "")).strip().upper()
        name_en = str(data.get("name_en", "")).strip()
        name_bn = str(data.get("name_bn", "")).strip()

        if not form_code or not name_en or not name_bn:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_DOSAGE_FORM_INPUT",
                    "message_en": "form_code, name_en, and name_bn are required.",
                    "message_bn": "ফর্ম কোড, ইংরেজি নাম এবং বাংলা নাম আবশ্যক।",
                }
            )

        if DosageFormDefinition.objects.filter(form_code=form_code).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_DOSAGE_FORM",
                    "message_en": f"Dosage form with code '{form_code}' already exists.",
                    "message_bn": "এই কোডের ডোসেজ ফরম ইতিমধ্যে বিদ্যমান।",
                }
            )

        form_id = f"df-{form_code.lower().replace('_', '-')}"
        df = DosageFormDefinition.objects.create(
            form_id=form_id,
            form_code=form_code,
            name_en=name_en,
            name_bn=name_bn,
            category=data.get("category", "SOLID").upper(),
            default_route=data.get("default_route", "Oral"),
            is_strip_applicable=bool(data.get("is_strip_applicable", False)),
            is_volume_applicable=bool(data.get("is_volume_applicable", False)),
            is_cold_chain_applicable=bool(data.get("is_cold_chain_applicable", False)),
            display_order=int(data.get("display_order", 99)),
            is_active=bool(data.get("is_active", True)),
            field_groups=data.get("field_groups", []),
        )
        return cls.serialize_form(df)
