"""
backend/apps/core/medicine_form_template_service.py
Architecture Decision: ADR-0131 (P131).
Service for Publishing Dosage-Form Templates, Layout Rules,
Allowed Clinical & Packaging Values, and Regulatory Validation Contracts.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import models, transaction

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.dosage_form_service import DosageFormRegistryService
from apps.core.models import DosageFormDefinition, MedicineFormTemplate
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.core.medicine_form_template")


class MedicineFormTemplateService:
    """
    Central service for Dosage-Form Medicine Form Templates.
    Publishes layout configurations, widgets, allowed values, and validation contracts.
    """

    CANONICAL_TEMPLATES = [
        {
            "template_id": "tmpl-tablet-v1",
            "dosage_form_code": "TABLET",
            "template_code": "TABLET_STANDARD",
            "template_version": "v1.0",
            "title_en": "Standard Solid Oral Tablet Form Template",
            "title_bn": "স্ট্যান্ডার্ড সলিড ট্যাবলেট ফরম টেমপ্লেট",
            "description": "Comprehensive responsive layout for solid oral tablets with box/strip/unit packaging hierarchy.",
            "is_default": True,
            "layout_rules": {
                "grid_columns": 12,
                "sections": [
                    {
                        "section_id": "identification",
                        "title_en": "Product Identification",
                        "title_bn": "ওষুধের প্রাথমিক পরিচিতি",
                        "collapsible": False,
                        "default_expanded": True,
                        "display_order": 1,
                        "fields": [
                            {
                                "field_key": "brand_name",
                                "widget_type": "text_input",
                                "column_span": 6,
                                "label_en": "Brand Name",
                                "label_bn": "ব্র্যান্ডের নাম",
                                "placeholder_en": "e.g. Napa 500mg",
                                "placeholder_bn": "যেমন: নাপা ৫০০ মিগ্রা",
                                "help_text_en": "Registered trade brand name",
                                "help_text_bn": "নিবন্ধিত বাণিজ্যিক নাম",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "",
                            },
                            {
                                "field_key": "generic_name",
                                "widget_type": "text_input",
                                "column_span": 6,
                                "label_en": "Generic Name",
                                "label_bn": "জেনেরিক নাম",
                                "placeholder_en": "e.g. Paracetamol",
                                "placeholder_bn": "যেমন: প্যারাসিটামল",
                                "help_text_en": "International Nonproprietary Name (INN)",
                                "help_text_bn": "আন্তর্জাতিক স্বীকৃত জেনেরিক নাম",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "",
                            },
                        ],
                    },
                    {
                        "section_id": "strength_spec",
                        "title_en": "Dosage Strength & Measurement",
                        "title_bn": "ডোজের মাত্রা ও পরিমাপ",
                        "collapsible": False,
                        "default_expanded": True,
                        "display_order": 2,
                        "fields": [
                            {
                                "field_key": "strength_numeric",
                                "widget_type": "decimal_input",
                                "column_span": 6,
                                "label_en": "Strength Value",
                                "label_bn": "মাত্রার মান",
                                "placeholder_en": "e.g. 500",
                                "placeholder_bn": "যেমন: ৫০০",
                                "help_text_en": "Numeric strength per solid tablet",
                                "help_text_bn": "প্রতি ট্যাবলেটের নির্দিষ্ট সংখ্যাসূচক মাত্রা",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 500.0,
                            },
                            {
                                "field_key": "strength_unit",
                                "widget_type": "dropdown_select",
                                "column_span": 6,
                                "label_en": "Strength Unit",
                                "label_bn": "মাত্রার একক",
                                "placeholder_en": "Select unit",
                                "placeholder_bn": "একক নির্বাচন করুন",
                                "help_text_en": "Prescribed scientific measurement unit",
                                "help_text_bn": "বৈজ্ঞানিক পরিমাপের একক",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "mg",
                            },
                        ],
                    },
                    {
                        "section_id": "packaging_hierarchy",
                        "title_en": "Packaging Unit Conversion Hierarchy",
                        "title_bn": "প্যাকেজিং রূপান্তর স্তরবিন্যাস (Box -> Strip -> Tablet)",
                        "collapsible": True,
                        "default_expanded": True,
                        "display_order": 3,
                        "fields": [
                            {
                                "field_key": "box_size",
                                "widget_type": "number_stepper",
                                "column_span": 4,
                                "label_en": "Total Box Units",
                                "label_bn": "বক্স প্রতি মোট ট্যাবলেট",
                                "placeholder_en": "e.g. 100",
                                "placeholder_bn": "যেমন: ১০০",
                                "help_text_en": "Total number of tablets in a master container box",
                                "help_text_bn": "একটি পূর্ণ বাক্সে মোট ট্যাবলেটের সংখ্যা",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 100,
                            },
                            {
                                "field_key": "strips_per_box",
                                "widget_type": "number_stepper",
                                "column_span": 4,
                                "label_en": "Strips Per Box",
                                "label_bn": "বক্স প্রতি পাতা (Strip)",
                                "placeholder_en": "e.g. 10",
                                "placeholder_bn": "যেমন: ১০",
                                "help_text_en": "Number of secondary blister/foil strips",
                                "help_text_bn": "প্রতি বাক্সে পাতার সংখ্যা",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 10,
                            },
                            {
                                "field_key": "units_per_strip",
                                "widget_type": "number_stepper",
                                "column_span": 4,
                                "label_en": "Tablets Per Strip",
                                "label_bn": "পাতা প্রতি ট্যাবলেট",
                                "placeholder_en": "e.g. 10",
                                "placeholder_bn": "যেমন: ১০",
                                "help_text_en": "Individual tablets sealed in each strip",
                                "help_text_bn": "প্রতি পাতায় ট্যাবলেটের সংখ্যা",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 10,
                            },
                        ],
                    },
                    {
                        "section_id": "clinical_governance",
                        "title_en": "Clinical Administration & Governance",
                        "title_bn": "ক্লিনিক্যাল প্রশাসন ও নিয়ন্ত্রণ",
                        "collapsible": True,
                        "default_expanded": True,
                        "display_order": 4,
                        "fields": [
                            {
                                "field_key": "administration_route",
                                "widget_type": "dropdown_select",
                                "column_span": 6,
                                "label_en": "Administration Route",
                                "label_bn": "প্রয়োগের মাধ্যম",
                                "placeholder_en": "Select route",
                                "placeholder_bn": "মাধ্যম নির্বাচন করুন",
                                "help_text_en": "Anatomical administration route",
                                "help_text_bn": "ওষুধ সেবনের শারীরিক পথ",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "Oral",
                            },
                            {
                                "field_key": "is_prescription_only",
                                "widget_type": "checkbox_toggle",
                                "column_span": 6,
                                "label_en": "Prescription Only (POM)",
                                "label_bn": "শুধুমাত্র চিকিৎসকের প্রেসক্রিপশন আবশ্যক",
                                "placeholder_en": "",
                                "placeholder_bn": "",
                                "help_text_en": "Mandatory registered prescription required for dispensing",
                                "help_text_bn": "বিক্রয়ের পূর্বে প্রেসক্রিপশন যাচাই বাধ্যতামূলক",
                                "is_required": False,
                                "is_read_only": False,
                                "default_value": False,
                            },
                        ],
                    },
                ],
            },
            "allowed_values": {
                "strength_units": ["mg", "mcg", "g"],
                "administration_routes": ["Oral", "Sublingual", "Buccal", "Chewable", "Dispersible"],
                "container_types": ["STRIP", "BLISTER_PACK", "ALU_ALU", "BOTTLE"],
                "storage_conditions": ["ROOM_TEMPERATURE", "COOL_DRY_PLACE", "PROTECT_FROM_LIGHT"],
                "dispensing_units": ["Tablet", "Piece", "Pcs"],
            },
            "validation_rules": [
                {
                    "rule_id": "VR-TAB-01",
                    "description": "Packaging multiplier: strips_per_box multiplied by units_per_strip must equal box_size",
                    "expression": "strips_per_box * units_per_strip == box_size",
                }
            ],
        },
        {
            "template_id": "tmpl-syrup-v1",
            "dosage_form_code": "SYRUP",
            "template_code": "SYRUP_LIQUID",
            "template_version": "v1.0",
            "title_en": "Oral Liquid Syrup / Suspension Form Template",
            "title_bn": "ওরাল লিকুইড সিরাপ ও সাসপেনশন ফরম টেমপ্লেট",
            "description": "Liquid dosage layout emphasizing bottle volume in milliliters and concentration units.",
            "is_default": True,
            "layout_rules": {
                "grid_columns": 12,
                "sections": [
                    {
                        "section_id": "identification",
                        "title_en": "Product Identification",
                        "title_bn": "ওষুধের প্রাথমিক পরিচিতি",
                        "collapsible": False,
                        "default_expanded": True,
                        "display_order": 1,
                        "fields": [
                            {
                                "field_key": "brand_name",
                                "widget_type": "text_input",
                                "column_span": 6,
                                "label_en": "Brand Name",
                                "label_bn": "ব্র্যান্ডের নাম",
                                "placeholder_en": "e.g. Napa Syrup 60ml",
                                "placeholder_bn": "যেমন: নাপা সিরাপ ৬০ মি.লি.",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "",
                            },
                            {
                                "field_key": "generic_name",
                                "widget_type": "text_input",
                                "column_span": 6,
                                "label_en": "Generic Name",
                                "label_bn": "জেনেরিক নাম",
                                "placeholder_en": "e.g. Paracetamol",
                                "placeholder_bn": "যেমন: প্যারাসিটামল",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "",
                            },
                        ],
                    },
                    {
                        "section_id": "liquid_volume_spec",
                        "title_en": "Liquid Volume & Concentration",
                        "title_bn": "তরল আয়তন ও ঘনমাত্রা",
                        "collapsible": False,
                        "default_expanded": True,
                        "display_order": 2,
                        "fields": [
                            {
                                "field_key": "volume_ml",
                                "widget_type": "decimal_input",
                                "column_span": 4,
                                "label_en": "Bottle Volume (mL)",
                                "label_bn": "বোতলের আয়তন (মি.লি.)",
                                "placeholder_en": "e.g. 100",
                                "placeholder_bn": "যেমন: ১০০",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 100.0,
                            },
                            {
                                "field_key": "strength_numeric",
                                "widget_type": "decimal_input",
                                "column_span": 4,
                                "label_en": "Concentration Value",
                                "label_bn": "ঘনমাত্রার মান",
                                "placeholder_en": "e.g. 120",
                                "placeholder_bn": "যেমন: ১২০",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 120.0,
                            },
                            {
                                "field_key": "strength_unit",
                                "widget_type": "dropdown_select",
                                "column_span": 4,
                                "label_en": "Concentration Unit",
                                "label_bn": "ঘনমাত্রার একক",
                                "placeholder_en": "Select unit",
                                "placeholder_bn": "একক নির্বাচন করুন",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "mg/5mL",
                            },
                        ],
                    },
                    {
                        "section_id": "storage_preservation",
                        "title_en": "Storage & Container Preservation",
                        "title_bn": "সংরক্ষণ ও পাত্রের বিবরণ",
                        "collapsible": True,
                        "default_expanded": True,
                        "display_order": 3,
                        "fields": [
                            {
                                "field_key": "container_type",
                                "widget_type": "dropdown_select",
                                "column_span": 6,
                                "label_en": "Container Type",
                                "label_bn": "পাত্রের ধরণ",
                                "placeholder_en": "Select container",
                                "placeholder_bn": "পাত্র নির্বাচন করুন",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "PET_BOTTLE",
                            },
                            {
                                "field_key": "storage_condition",
                                "widget_type": "dropdown_select",
                                "column_span": 6,
                                "label_en": "Storage Condition",
                                "label_bn": "সংরক্ষণের শর্ত",
                                "placeholder_en": "Select storage",
                                "placeholder_bn": "শর্ত নির্বাচন করুন",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "ROOM_TEMPERATURE",
                            },
                        ],
                    },
                ],
            },
            "allowed_values": {
                "strength_units": ["mg/5mL", "mcg/5mL", "g/100mL", "%"],
                "administration_routes": ["Oral"],
                "container_types": ["GLASS_BOTTLE", "PET_BOTTLE", "DROPS_BOTTLE"],
                "storage_conditions": ["ROOM_TEMPERATURE", "SHAKE_WELL_BEFORE_USE", "PROTECT_FROM_DIRECT_SUNLIGHT"],
                "dispensing_units": ["Bottle", "Phial"],
            },
            "validation_rules": [
                {
                    "rule_id": "VR-SYR-01",
                    "description": "Bottle volume must be strictly positive",
                    "expression": "volume_ml > 0",
                }
            ],
        },
        {
            "template_id": "tmpl-injection-v1",
            "dosage_form_code": "INJECTION",
            "template_code": "INJECTION_STERILE",
            "template_version": "v1.0",
            "title_en": "Sterile Parenteral Injection Form Template",
            "title_bn": "স্টেরাইল প্যারেন্টেরাল ইনজেকশন ফরম টেমপ্লেট",
            "description": "Parenteral sterile injection layout enforcing cold-chain parameters and vial/ampoule containers.",
            "is_default": True,
            "layout_rules": {
                "grid_columns": 12,
                "sections": [
                    {
                        "section_id": "identification",
                        "title_en": "Product Identification",
                        "title_bn": "ওষুধের প্রাথমিক পরিচিতি",
                        "collapsible": False,
                        "default_expanded": True,
                        "display_order": 1,
                        "fields": [
                            {
                                "field_key": "brand_name",
                                "widget_type": "text_input",
                                "column_span": 6,
                                "label_en": "Brand Name",
                                "label_bn": "ব্র্যান্ডের নাম",
                                "placeholder_en": "e.g. Ceftriaxone 1g IV",
                                "placeholder_bn": "যেমন: সেফট্রিয়াক্সন ১ গ্রাম আইভি",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "",
                            },
                            {
                                "field_key": "generic_name",
                                "widget_type": "text_input",
                                "column_span": 6,
                                "label_en": "Generic Name",
                                "label_bn": "জেনেরিক নাম",
                                "placeholder_en": "e.g. Ceftriaxone",
                                "placeholder_bn": "যেমন: সেফট্রিয়াক্সন",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": "",
                            },
                        ],
                    },
                    {
                        "section_id": "cold_chain_spec",
                        "title_en": "Cold Chain & Storage Thresholds",
                        "title_bn": "কোল্ড চেইন ও তাপমাত্রা নিয়ন্ত্রণ",
                        "collapsible": False,
                        "default_expanded": True,
                        "display_order": 2,
                        "fields": [
                            {
                                "field_key": "is_cold_chain",
                                "widget_type": "checkbox_toggle",
                                "column_span": 4,
                                "label_en": "Cold Chain Required",
                                "label_bn": "কোল্ড চেইন আবশ্যক",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": True,
                            },
                            {
                                "field_key": "min_temperature_celsius",
                                "widget_type": "decimal_input",
                                "column_span": 4,
                                "label_en": "Min Temp (°C)",
                                "label_bn": "সর্বনিম্ন তাপমাত্রা (°সে.)",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 2.0,
                            },
                            {
                                "field_key": "max_temperature_celsius",
                                "widget_type": "decimal_input",
                                "column_span": 4,
                                "label_en": "Max Temp (°C)",
                                "label_bn": "সর্বোচ্চ তাপমাত্রা (°সে.)",
                                "is_required": True,
                                "is_read_only": False,
                                "default_value": 8.0,
                            },
                        ],
                    },
                ],
            },
            "allowed_values": {
                "strength_units": ["mg/mL", "IU/mL", "mcg/mL", "%", "g"],
                "administration_routes": [
                    "Intravenous (IV)",
                    "Intramuscular (IM)",
                    "Subcutaneous (SC)",
                    "Intradermal",
                ],
                "container_types": ["VIAL", "AMPOULE", "PRE_FILLED_SYRINGE"],
                "storage_conditions": ["COLD_CHAIN_2_8C", "DEEP_FREEZE", "ROOM_TEMPERATURE"],
                "dispensing_units": ["Vial", "Ampoule"],
            },
            "validation_rules": [
                {
                    "rule_id": "VR-INJ-01",
                    "description": "Cold-chain min temperature must be strictly less than max temperature",
                    "expression": "min_temperature_celsius < max_temperature_celsius",
                }
            ],
        },
    ]

    @classmethod
    def serialize_template(cls, template: MedicineFormTemplate) -> Dict[str, Any]:
        """Converts MedicineFormTemplate to JSON Schema compliant dictionary."""
        return {
            "template_id": template.template_id,
            "dosage_form_code": template.dosage_form.form_code,
            "dosage_form_name_en": template.dosage_form.name_en,
            "dosage_form_name_bn": template.dosage_form.name_bn,
            "template_code": template.template_code,
            "template_version": template.template_version,
            "title_en": template.title_en,
            "title_bn": template.title_bn,
            "description": template.description or "",
            "is_active": template.is_active,
            "is_default": template.is_default,
            "layout_rules": template.layout_rules,
            "allowed_values": template.allowed_values,
            "validation_rules": template.validation_rules,
            "immutable_version_hash": template.immutable_version_hash,
            "created_at": template.created_at.isoformat() if template.created_at else None,
            "updated_at": template.updated_at.isoformat() if template.updated_at else None,
        }

    @classmethod
    def ensure_canonical_templates_seeded(cls) -> None:
        """Seeds canonical dosage form templates if not present."""
        DosageFormRegistryService.ensure_canonical_forms_seeded()

        for item in cls.CANONICAL_TEMPLATES:
            code = item["dosage_form_code"]
            dosage_form = DosageFormDefinition.objects.filter(form_code__iexact=code).first()
            if not dosage_form:
                logger.warning("Dosage form %s not found for template seeding", code)
                continue

            tmpl = MedicineFormTemplate.objects.filter(template_id=item["template_id"]).first()
            if not tmpl:
                MedicineFormTemplate.objects.create(
                    template_id=item["template_id"],
                    dosage_form=dosage_form,
                    template_code=item["template_code"],
                    template_version=item["template_version"],
                    title_en=item["title_en"],
                    title_bn=item["title_bn"],
                    description=item["description"],
                    is_active=True,
                    is_default=item["is_default"],
                    layout_rules=item["layout_rules"],
                    allowed_values=item["allowed_values"],
                    validation_rules=item.get("validation_rules", []),
                )
                logger.info("Seeded canonical medicine form template: %s", item["template_id"])

    @classmethod
    def list_templates(
        cls,
        dosage_form: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Queries and lists published dosage-form templates."""
        cls.ensure_canonical_templates_seeded()

        qs = MedicineFormTemplate.objects.select_related("dosage_form").all()
        if dosage_form:
            clean = dosage_form.strip().upper()
            qs = qs.filter(dosage_form__form_code__iexact=clean)

        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        if search:
            clean_search = search.strip()
            qs = qs.filter(
                models.Q(title_en__icontains=clean_search)
                | models.Q(title_bn__icontains=clean_search)
                | models.Q(template_code__icontains=clean_search)
            )

        return [cls.serialize_template(t) for t in qs]

    @classmethod
    def get_template(cls, identifier: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a template by template_id, template_code, or default template for dosage form code.
        """
        cls.ensure_canonical_templates_seeded()
        clean = str(identifier or "").strip()

        # 1. Match by template_id
        tmpl = (
            MedicineFormTemplate.objects.filter(template_id=clean)
            .select_related("dosage_form")
            .first()
        )
        if tmpl:
            return cls.serialize_template(tmpl)

        # 2. Match by template_code
        tmpl = (
            MedicineFormTemplate.objects.filter(template_code__iexact=clean, is_active=True)
            .select_related("dosage_form")
            .first()
        )
        if tmpl:
            return cls.serialize_template(tmpl)

        # 3. Match by dosage_form_code default template
        tmpl = (
            MedicineFormTemplate.objects.filter(
                dosage_form__form_code__iexact=clean,
                is_active=True,
                is_default=True,
            )
            .select_related("dosage_form")
            .first()
        )
        if tmpl:
            return cls.serialize_template(tmpl)

        return None

    @classmethod
    @transaction.atomic
    def create_template(cls, data: Dict[str, Any], actor: PharmacyUser) -> Dict[str, Any]:
        """
        Creates a new dosage-form template.
        Restricted to Platform Superadmins / Operators.
        """
        cls.ensure_canonical_templates_seeded()

        # 1. Validate Dosage Form
        dosage_code = str(data.get("dosage_form_code", "")).strip().upper()
        dosage_form = DosageFormDefinition.objects.filter(form_code__iexact=dosage_code).first()
        if not dosage_form:
            raise ValidationError(
                {"dosage_form_code": f"Dosage form '{dosage_code}' does not exist in registry."}
            )

        # 2. Validate Template Code & Version
        template_code = str(data.get("template_code", "")).strip().upper()
        if not template_code or len(template_code) < 3:
            raise ValidationError({"template_code": "template_code must be at least 3 characters."})

        template_version = str(data.get("template_version", "v1.0")).strip()
        if not template_version:
            raise ValidationError({"template_version": "template_version cannot be empty."})

        # Check uniqueness
        if MedicineFormTemplate.objects.filter(
            dosage_form=dosage_form,
            template_code=template_code,
            template_version=template_version,
        ).exists():
            raise ValidationError(
                {
                    "template_code": f"Template '{template_code}' version '{template_version}' already exists for {dosage_code}."
                }
            )

        title_en = str(data.get("title_en", "")).strip()
        title_bn = str(data.get("title_bn", "")).strip()
        if len(title_en) < 3 or len(title_bn) < 3:
            raise ValidationError(
                {"title_en": "Bilingual titles (EN and BN) must be at least 3 characters."}
            )

        # 3. Validate Layout Rules & Allowed Values
        layout_rules = data.get("layout_rules")
        if not isinstance(layout_rules, dict) or "sections" not in layout_rules:
            raise ValidationError({"layout_rules": "layout_rules must be a dict containing 'sections'."})

        allowed_values = data.get("allowed_values")
        if not isinstance(allowed_values, dict) or "strength_units" not in allowed_values:
            raise ValidationError({"allowed_values": "allowed_values must contain 'strength_units'."})

        template_id = f"tmpl-{uuid.uuid4().hex[:12]}"
        is_default = bool(data.get("is_default", False))

        # If this is set as default, demote other defaults for this dosage form
        if is_default:
            MedicineFormTemplate.objects.filter(
                dosage_form=dosage_form,
                is_default=True,
            ).update(is_default=False)

        tmpl = MedicineFormTemplate.objects.create(
            template_id=template_id,
            dosage_form=dosage_form,
            template_code=template_code,
            template_version=template_version,
            title_en=title_en,
            title_bn=title_bn,
            description=data.get("description", ""),
            is_active=bool(data.get("is_active", True)),
            is_default=is_default,
            layout_rules=layout_rules,
            allowed_values=allowed_values,
            validation_rules=data.get("validation_rules", []),
        )

        AuditLogEntry.objects.create(
            action="MEDICINE_FORM_TEMPLATE_CREATED",
            resource_type="MEDICINE_FORM_TEMPLATE",
            resource_id=tmpl.template_id,
            tenant_id="platform",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "actor": actor.email if actor else "system",
                "template_id": tmpl.template_id,
                "dosage_form": dosage_code,
                "template_code": tmpl.template_code,
                "template_version": tmpl.template_version,
                "immutable_version_hash": tmpl.immutable_version_hash,
            },
        )

        return cls.serialize_template(tmpl)
