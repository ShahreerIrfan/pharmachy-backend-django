"""
Medicine Editor Schema & Conditional Validation Engine Service.
Architecture Decision: ADR-0124 (P124).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import MedicineEditorSchema
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.medicine_editor_schema")


class MedicineEditorSchemaService:
    """
    Provides server-driven UI schema management, conditional form section rendering,
    and server-side payload validation for the medicine master editor.
    """

    CANONICAL_SECTIONS: List[Dict[str, Any]] = [
        {
            "section_id": "basic_info",
            "title_en": "Basic Information",
            "title_bn": "প্রাথমিক তথ্য",
            "display_order": 1,
            "fields": [
                {
                    "field_id": "brand_name",
                    "label_en": "Brand Name",
                    "label_bn": "ব্র্যান্ড নাম",
                    "field_type": "text",
                    "is_required": True,
                    "placeholder_en": "e.g. Napa Extra",
                    "placeholder_bn": "যেমন নাপা এক্সট্রা",
                },
                {
                    "field_id": "generic_name",
                    "label_en": "Generic Name",
                    "label_bn": "জেনেরিক নাম",
                    "field_type": "text",
                    "is_required": True,
                    "placeholder_en": "e.g. Paracetamol + Caffeine",
                    "placeholder_bn": "যেমন প্যারাসিটামল + ক্যাফেইন",
                },
                {
                    "field_id": "dosage_form",
                    "label_en": "Dosage Form",
                    "label_bn": "ডোসেজ ফরম",
                    "field_type": "select",
                    "is_required": True,
                    "options": [
                        "TABLET",
                        "CAPSULE",
                        "SYRUP",
                        "SUSPENSION",
                        "INJECTION",
                        "EYE_DROPS",
                        "OINTMENT",
                        "INHALER",
                        "SUPPOSITORY",
                    ],
                    "default_value": "TABLET",
                },
                {
                    "field_id": "manufacturer",
                    "label_en": "Manufacturer",
                    "label_bn": "উত্পাদক প্রতিষ্ঠান",
                    "field_type": "text",
                    "is_required": True,
                    "placeholder_en": "e.g. Beximco Pharmaceuticals Ltd.",
                    "placeholder_bn": "যেমন বেক্সিমকো ফার্মাসিউটিক্যালস লি.",
                },
            ],
        },
        {
            "section_id": "strength_spec",
            "title_en": "Dosage Strength",
            "title_bn": "মাত্রার বিবরণ",
            "display_order": 2,
            "fields": [
                {
                    "field_id": "strength",
                    "label_en": "Strength String",
                    "label_bn": "মাত্রার সংকেত",
                    "field_type": "text",
                    "is_required": True,
                    "placeholder_en": "e.g. 500 mg, 250mg/5mL",
                    "placeholder_bn": "যেমন ৫০০ মিলিগ্রাম, ২৫০মিগ্রা/৫মিলি",
                },
                {
                    "field_id": "strength_numeric",
                    "label_en": "Numeric Strength",
                    "label_bn": "সংখ্যার মান",
                    "field_type": "decimal",
                    "is_required": True,
                    "default_value": "500.0",
                },
                {
                    "field_id": "strength_unit",
                    "label_en": "Measurement Unit",
                    "label_bn": "পরিমাপের একক",
                    "field_type": "select",
                    "is_required": True,
                    "options": ["mg", "mcg", "g", "ml", "l", "%", "iu", "iu/ml", "mg/5ml", "mg/ml"],
                    "default_value": "mg",
                },
            ],
        },
        {
            "section_id": "packaging_config",
            "title_en": "Packaging & Conversion",
            "title_bn": "প্যাকেজিং ও রূপান্তর",
            "display_order": 3,
            "fields": [
                {
                    "field_id": "is_strip_applicable",
                    "label_en": "Strip Packaging Applicable",
                    "label_bn": "পাতা/স্ট্রিপ প্যাকেজিং প্রযোজ্য",
                    "field_type": "boolean",
                    "default_value": True,
                },
                {
                    "field_id": "strips_per_box",
                    "label_en": "Strips Per Box",
                    "label_bn": "প্রতি বাক্সে স্ট্রিপ সংখ্যা",
                    "field_type": "number",
                    "is_required": False,
                    "default_value": 10,
                },
                {
                    "field_id": "units_per_strip",
                    "label_en": "Units Per Strip",
                    "label_bn": "প্রতি স্ট্রিপে ট্যাবলেটের সংখ্যা",
                    "field_type": "number",
                    "is_required": False,
                    "default_value": 10,
                },
                {
                    "field_id": "volume_ml",
                    "label_en": "Volume (mL)",
                    "label_bn": "তরল আয়তন (মিলি)",
                    "field_type": "decimal",
                    "is_required": False,
                    "default_value": None,
                },
            ],
        },
        {
            "section_id": "cold_chain_spec",
            "title_en": "Cold Chain & Storage",
            "title_bn": "কোল্ড চেইন ও সংরক্ষণ",
            "display_order": 4,
            "fields": [
                {
                    "field_id": "is_cold_chain",
                    "label_en": "Requires Cold Chain Storage (2°C - 8°C)",
                    "label_bn": "কোল্ড চেইন স্টোরেজ আবশ্যক",
                    "field_type": "boolean",
                    "default_value": False,
                },
                {
                    "field_id": "min_temperature_celsius",
                    "label_en": "Minimum Storage Temperature (°C)",
                    "label_bn": "সর্বনিম্ন তাপমাত্রা (°সে)",
                    "field_type": "decimal",
                    "is_required": False,
                    "default_value": 2.0,
                },
                {
                    "field_id": "max_temperature_celsius",
                    "label_en": "Maximum Storage Temperature (°C)",
                    "label_bn": "সর্বোচ্চ তাপমাত্রা (°সে)",
                    "field_type": "decimal",
                    "is_required": False,
                    "default_value": 8.0,
                },
            ],
        },
        {
            "section_id": "clinical_governance",
            "title_en": "Clinical Governance & Restrictions",
            "title_bn": "ক্লিনিক্যাল বিধিবিধান ও সীমাবদ্ধতা",
            "display_order": 5,
            "fields": [
                {
                    "field_id": "is_prescription_required",
                    "label_en": "Prescription Only (Rx)",
                    "label_bn": "শুধুমাত্র প্রেসক্রিপশন দ্বারা বিক্রয়যোগ্য",
                    "field_type": "boolean",
                    "default_value": False,
                },
                {
                    "field_id": "is_antibiotic",
                    "label_en": "Antibiotic / Antimicrobial",
                    "label_bn": "অ্যান্টিবায়োটিক ঔষধ",
                    "field_type": "boolean",
                    "default_value": False,
                },
                {
                    "field_id": "is_narcotic",
                    "label_en": "Narcotic / Habit-Forming Schedule Drug",
                    "label_bn": "মাদকদ্রব্য বা নিয়ন্ত্রিত ঔষধ",
                    "field_type": "boolean",
                    "default_value": False,
                },
            ],
        },
    ]

    CANONICAL_CONDITIONAL_RULES: List[Dict[str, Any]] = [
        {
            "rule_id": "strip_packaging_condition",
            "condition": {"field": "dosage_form", "in": ["TABLET", "CAPSULE"]},
            "actions": [
                {"type": "set_field_visibility", "field": "strips_per_box", "visible": True, "required": True},
                {"type": "set_field_visibility", "field": "units_per_strip", "visible": True, "required": True},
                {"type": "set_field_visibility", "field": "volume_ml", "visible": False, "required": False},
            ],
        },
        {
            "rule_id": "liquid_volume_condition",
            "condition": {"field": "dosage_form", "in": ["SYRUP", "SUSPENSION", "SOLUTION", "DROPS"]},
            "actions": [
                {"type": "set_field_visibility", "field": "strips_per_box", "visible": False, "required": False},
                {"type": "set_field_visibility", "field": "units_per_strip", "visible": False, "required": False},
                {"type": "set_field_visibility", "field": "volume_ml", "visible": True, "required": True},
            ],
        },
        {
            "rule_id": "cold_chain_condition",
            "condition": {"field": "is_cold_chain", "equals": True},
            "actions": [
                {"type": "set_field_visibility", "field": "min_temperature_celsius", "visible": True, "required": True},
                {"type": "set_field_visibility", "field": "max_temperature_celsius", "visible": True, "required": True},
            ],
        },
        {
            "rule_id": "antibiotic_rx_enforcement",
            "condition": {"field": "is_antibiotic", "equals": True},
            "actions": [
                {"type": "enforce_value", "field": "is_prescription_required", "value": True},
            ],
        },
    ]

    @classmethod
    def ensure_canonical_schema_seeded(cls) -> MedicineEditorSchema:
        """Seeds canonical v1.0 medicine editor schema if none exists."""
        schema = MedicineEditorSchema.objects.filter(schema_version="v1.0").first()
        if not schema:
            schema = MedicineEditorSchema.objects.create(
                schema_id="med-schema-v1.0",
                schema_version="v1.0",
                title_en="Standard Medicine Master Editor",
                title_bn="স্ট্যান্ডার্ড ঔষধ বিবরণী ও মাস্টার ফর্ম",
                description="Canonical server-driven editor schema with conditional sections and validation.",
                is_active=True,
                sections=cls.CANONICAL_SECTIONS,
                conditional_rules=cls.CANONICAL_CONDITIONAL_RULES,
            )
        return schema

    @classmethod
    def get_active_schema(cls, version: Optional[str] = None) -> MedicineEditorSchema:
        """Retrieves active schema or explicit version, seeding canonical v1.0 if empty."""
        cls.ensure_canonical_schema_seeded()
        if version:
            schema = MedicineEditorSchema.objects.filter(schema_version=version).first()
            if not schema:
                raise ValidationError(
                    {
                        "error_code": "ERR_SCHEMA_VERSION_NOT_FOUND",
                        "message_en": f"Medicine editor schema version '{version}' not found.",
                        "message_bn": f"অনুরোধকৃত স্কিমা সংস্করণ '{version}' পাওয়া যায়নি।",
                    }
                )
            return schema

        schema = MedicineEditorSchema.objects.filter(is_active=True).order_by("-schema_version").first()
        if not schema:
            schema = cls.ensure_canonical_schema_seeded()
        return schema

    @classmethod
    def list_schemas(cls) -> List[Dict[str, Any]]:
        """Lists all registered schema versions."""
        cls.ensure_canonical_schema_seeded()
        schemas = MedicineEditorSchema.objects.all().order_by("-schema_version")
        return [
            {
                "schema_id": s.schema_id,
                "schema_version": s.schema_version,
                "title_en": s.title_en,
                "title_bn": s.title_bn,
                "description": s.description,
                "is_active": s.is_active,
                "sections_count": len(s.sections),
                "rules_count": len(s.conditional_rules),
                "created_at": s.created_at.isoformat(),
            }
            for s in schemas
        ]

    @classmethod
    def create_schema(cls, actor_user, data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new schema version. Platform superadmin only."""
        if not getattr(actor_user, "is_superuser", False) and getattr(actor_user, "role", "") != UserRole.SUPERADMIN:
            raise PermissionDenied(
                {
                    "error_code": "PLATFORM_CATALOG_OWNERSHIP_PROTECTED",
                    "message_en": "Medicine editor schemas are centrally curated by platform administrators.",
                    "message_bn": "ঔষধ বিবরণী স্কিমা কেন্দ্রীয়ভাবে প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটর দ্বারা পরিচালিত।",
                }
            )

        version = data.get("schema_version")
        if not version:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_SCHEMA_VERSION",
                    "message_en": "schema_version is required (e.g. 'v2.0').",
                    "message_bn": "স্কিমা সংস্করণ কোড আবশ্যক।",
                }
            )

        if MedicineEditorSchema.objects.filter(schema_version=version).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_SCHEMA_VERSION",
                    "message_en": f"Schema version '{version}' already exists.",
                    "message_bn": f"স্কিমা সংস্করণ '{version}' ইতিমধ্যেই বিদ্যমান।",
                }
            )

        schema = MedicineEditorSchema.objects.create(
            schema_id=f"med-schema-{version}",
            schema_version=version,
            title_en=data.get("title_en", f"Medicine Editor {version}"),
            title_bn=data.get("title_bn", f"ঔষধ বিবরণী ফর্ম {version}"),
            description=data.get("description", ""),
            is_active=data.get("is_active", True),
            sections=data.get("sections", cls.CANONICAL_SECTIONS),
            conditional_rules=data.get("conditional_rules", cls.CANONICAL_CONDITIONAL_RULES),
        )

        return {
            "schema_id": schema.schema_id,
            "schema_version": schema.schema_version,
            "title_en": schema.title_en,
            "title_bn": schema.title_bn,
            "is_active": schema.is_active,
            "sections": schema.sections,
            "conditional_rules": schema.conditional_rules,
        }

    @classmethod
    def validate_medicine_form(
        cls,
        payload: Dict[str, Any],
        schema_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validates form submission against active schema sections, required fields,
        and dynamic conditional business rules.
        """
        schema = cls.get_active_schema(version=schema_version)

        # 1. Base required fields check
        required_fields = ["brand_name", "generic_name", "dosage_form", "manufacturer", "strength"]
        missing = [f for f in required_fields if not str(payload.get(f, "")).strip()]
        if missing:
            raise ValidationError(
                {
                    "error_code": "ERR_SCHEMA_VALIDATION_FAILED",
                    "missing_fields": missing,
                    "message_en": f"Required fields missing: {', '.join(missing)}.",
                    "message_bn": f"আবশ্যকীয় ফিল্ডসমূহ পূরণ করুন: {', '.join(missing)}।",
                }
            )

        dosage_form = str(payload.get("dosage_form", "")).upper()

        # 2. Conditional Rule: Strip packaging for TABLET / CAPSULE
        if dosage_form in ["TABLET", "CAPSULE"]:
            strips = payload.get("strips_per_box")
            units = payload.get("units_per_strip")
            if strips is None or int(strips) <= 0:
                raise ValidationError(
                    {
                        "error_code": "ERR_CONDITIONAL_FIELD_REQUIRED",
                        "field": "strips_per_box",
                        "condition": f"Required for solid dosage form '{dosage_form}'",
                        "message_en": f"Strips per box is required for {dosage_form} and must be > 0.",
                        "message_bn": f"{dosage_form} ডোসেজ ফর্মের জন্য প্রতি বাক্সে পাতা/স্ট্রিপ সংখ্যা আবশ্যক।",
                    }
                )
            if units is None or int(units) <= 0:
                raise ValidationError(
                    {
                        "error_code": "ERR_CONDITIONAL_FIELD_REQUIRED",
                        "field": "units_per_strip",
                        "condition": f"Required for solid dosage form '{dosage_form}'",
                        "message_en": f"Units per strip is required for {dosage_form} and must be > 0.",
                        "message_bn": f"{dosage_form} ডোসেজ ফর্মের জন্য প্রতি পাতায় ট্যাবলেটের সংখ্যা আবশ্যক।",
                    }
                )

        # 3. Conditional Rule: Liquid Volume for SYRUP / SUSPENSION
        elif dosage_form in ["SYRUP", "SUSPENSION", "SOLUTION", "DROPS"]:
            vol = payload.get("volume_ml")
            if vol is None or Decimal(str(vol)) <= Decimal("0"):
                raise ValidationError(
                    {
                        "error_code": "ERR_CONDITIONAL_FIELD_REQUIRED",
                        "field": "volume_ml",
                        "condition": f"Required for liquid dosage form '{dosage_form}'",
                        "message_en": f"Volume in mL is required for {dosage_form} and must be > 0.",
                        "message_bn": f"{dosage_form} ডোসেজ ফর্মের জন্য মোট আয়তন (মিলি) প্রদান আবশ্যক।",
                    }
                )

        # 4. Conditional Rule: Cold chain storage parameters
        is_cold_chain = bool(payload.get("is_cold_chain", False))
        if is_cold_chain:
            min_temp = payload.get("min_temperature_celsius")
            max_temp = payload.get("max_temperature_celsius")
            if min_temp is None or max_temp is None:
                raise ValidationError(
                    {
                        "error_code": "ERR_CONDITIONAL_FIELD_REQUIRED",
                        "field": "cold_chain_temperatures",
                        "message_en": "Cold chain products must specify min and max storage temperatures.",
                        "message_bn": "কোল্ড চেইন ঔষধের জন্য সর্বনিম্ন এবং সর্বোচ্চ তাপমাত্রা নির্ধারণ আবশ্যক।",
                    }
                )
            try:
                min_dec = Decimal(str(min_temp))
                max_dec = Decimal(str(max_temp))
            except (ValueError, TypeError, InvalidOperation):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_TEMPERATURE_FORMAT",
                        "message_en": "Temperature values must be numeric decimal degrees Celsius.",
                        "message_bn": "তাপমাত্রার মান সঠিক দশমিক সংখ্যা হতে হবে।",
                    }
                )

            if min_dec >= max_dec:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                        "min_temperature": str(min_dec),
                        "max_temperature": str(max_dec),
                        "message_en": f"Min temperature ({min_dec}°C) must be strictly less than max temperature ({max_dec}°C).",
                        "message_bn": f"সর্বনিম্ন তাপমাত্রা ({min_dec}°সে) সর্বোচ্চ তাপমাত্রা ({max_dec}°সে) অপেক্ষা কম হতে হবে।",
                    }
                )

        # 5. Conditional Rule: Antibiotic stewardship prescription enforcement
        is_antibiotic = bool(payload.get("is_antibiotic", False))
        if is_antibiotic:
            payload["is_prescription_required"] = True

        return {
            "is_valid": True,
            "schema_version": schema.schema_version,
            "validated_payload": payload,
            "message_en": "Medicine editor payload passed all schema and conditional validation rules.",
            "message_bn": "ঔষধ ফর্মের সকল শর্ত ও ভ্যালিডেশন সফলভাবে উত্তীর্ণ হয়েছে।",
        }
