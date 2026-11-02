"""
Dynamic Condition Evaluator & Zero-Trust Hidden Field Guard Engine.
Architecture Decision: ADR-0125 (P125).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set

from rest_framework.exceptions import ValidationError

from apps.core.medicine_editor_schema_service import MedicineEditorSchemaService

logger = logging.getLogger("pharmacy.core.dynamic_condition_evaluator")


class DynamicConditionEvaluator:
    """
    Evaluates dynamic field conditions, enforces type safety, and guards against
    hidden client field injection / data pollution.
    """

    ALLOWED_DOSAGE_FORMS = [
        "TABLET",
        "CAPSULE",
        "SYRUP",
        "SUSPENSION",
        "INJECTION",
        "EYE_DROPS",
        "OINTMENT",
        "INHALER",
        "SUPPOSITORY",
    ]

    ALLOWED_STRENGTH_UNITS = [
        "mg",
        "mcg",
        "g",
        "ml",
        "l",
        "%",
        "iu",
        "iu/ml",
        "mg/5ml",
        "mg/ml",
    ]

    @classmethod
    def evaluate_condition(cls, condition: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """Evaluates a single condition expression against current payload values."""
        field = condition.get("field")
        if not field:
            return False

        val = payload.get(field)

        if "equals" in condition:
            expected = condition["equals"]
            if isinstance(expected, bool):
                return bool(val) == expected
            return str(val).upper() == str(expected).upper()

        if "not_equals" in condition:
            expected = condition["not_equals"]
            if isinstance(expected, bool):
                return bool(val) != expected
            return str(val).upper() != str(expected).upper()

        if "in" in condition:
            allowed = [str(item).upper() for item in condition["in"]]
            return str(val).upper() in allowed

        if "not_in" in condition:
            disallowed = [str(item).upper() for item in condition["not_in"]]
            return str(val).upper() not in disallowed

        return False

    @classmethod
    def resolve_field_states(
        cls,
        payload: Dict[str, Any],
        conditional_rules: List[Dict[str, Any]],
        all_field_ids: Set[str],
    ) -> tuple[Set[str], Set[str], Set[str]]:
        """
        Determines active vs hidden fields and required fields based on conditional rules.
        Returns (active_fields, hidden_fields, required_fields).
        """
        dosage_form = str(payload.get("dosage_form", "")).upper()
        is_cold_chain = bool(payload.get("is_cold_chain", False))

        active_fields = {
            "brand_name",
            "generic_name",
            "dosage_form",
            "manufacturer",
            "strength",
            "strength_numeric",
            "strength_unit",
            "is_strip_applicable",
            "is_cold_chain",
            "is_prescription_required",
            "is_antibiotic",
            "is_narcotic",
        }
        hidden_fields = set()
        required_fields = {"brand_name", "generic_name", "dosage_form", "manufacturer", "strength"}

        # Packaging conditions
        if dosage_form in ["TABLET", "CAPSULE"]:
            active_fields.update(["strips_per_box", "units_per_strip"])
            required_fields.update(["strips_per_box", "units_per_strip"])
            hidden_fields.add("volume_ml")
        elif dosage_form in ["SYRUP", "SUSPENSION", "SOLUTION", "DROPS"]:
            active_fields.add("volume_ml")
            required_fields.add("volume_ml")
            hidden_fields.update(["strips_per_box", "units_per_strip"])
        else:
            hidden_fields.update(["strips_per_box", "units_per_strip", "volume_ml"])

        # Cold chain conditions
        if is_cold_chain:
            active_fields.update(["min_temperature_celsius", "max_temperature_celsius"])
            required_fields.update(["min_temperature_celsius", "max_temperature_celsius"])
        else:
            hidden_fields.update(["min_temperature_celsius", "max_temperature_celsius"])

        return active_fields, hidden_fields, required_fields

    @classmethod
    def validate_type_safety(cls, field_id: str, value: Any, field_meta: Dict[str, Any]) -> Any:
        """Enforces type safety according to schema definition."""
        field_type = field_meta.get("field_type", "text")

        if value is None or str(value).strip() == "":
            return None

        if field_type == "text":
            return str(value).strip()

        if field_type == "number":
            try:
                # Disallow floats (e.g. 10.5) for whole count quantities
                float_val = float(value)
                int_val = int(value)
                if float_val != int_val:
                    raise ValueError("Float not permitted for whole number count")
                if int_val <= 0:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_NUMBER_RANGE",
                            "field": field_id,
                            "message_en": f"Field '{field_id}' must be a positive integer (> 0).",
                            "message_bn": f"'{field_id}' ফিল্ডটি অবশ্যই ধনাত্মক পূর্ণসংখ্যা হতে হবে।",
                        }
                    )
                return int_val
            except (ValueError, TypeError):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_FIELD_TYPE",
                        "field": field_id,
                        "expected_type": "number",
                        "message_en": f"Field '{field_id}' expects a whole integer, got '{value}'.",
                        "message_bn": f"'{field_id}' ফিল্ডের মান একটি পূর্ণসংখ্যা হতে হবে।",
                    }
                )

        if field_type == "decimal":
            try:
                dec_val = Decimal(str(value))
                return dec_val
            except (ValueError, TypeError, InvalidOperation):
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_DECIMAL",
                        "field": field_id,
                        "expected_type": "decimal",
                        "message_en": f"Field '{field_id}' expects a valid decimal number, got '{value}'.",
                        "message_bn": f"'{field_id}' ফিল্ডের মান একটি সঠিক দশমিক সংখ্যা হতে হবে।",
                    }
                )

        if field_type == "boolean":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1"):
                return True
            if str(value).lower() in ("false", "0"):
                return False
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_BOOLEAN",
                    "field": field_id,
                    "message_en": f"Field '{field_id}' must be a boolean true or false.",
                    "message_bn": f"'{field_id}' ফিল্ডটি একটি বুলিয়ান মান (true/false) হতে হবে।",
                }
            )

        if field_type == "select":
            str_val = str(value).strip()
            if field_id == "dosage_form":
                if str_val.upper() not in [f.upper() for f in cls.ALLOWED_DOSAGE_FORMS]:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_SELECT_OPTION",
                            "field": field_id,
                            "allowed_options": cls.ALLOWED_DOSAGE_FORMS,
                            "message_en": f"Dosage form '{str_val}' is not in the allowed taxonomy list.",
                            "message_bn": f"ডোসেজ ফর্ম '{str_val}' অনুমোদিত তালিকায় নেই।",
                        }
                    )
                return str_val.upper()
            if field_id == "strength_unit":
                if str_val.lower() not in [u.lower() for u in cls.ALLOWED_STRENGTH_UNITS]:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_SELECT_OPTION",
                            "field": field_id,
                            "allowed_options": cls.ALLOWED_STRENGTH_UNITS,
                            "message_en": f"Strength unit '{str_val}' is not recognized.",
                            "message_bn": f"পরিমাপের একক '{str_val}' অনুমোদিত নয়।",
                        }
                    )
                return str_val.lower()
            return str_val

        return value

    @classmethod
    def evaluate_and_validate(
        cls,
        payload: Dict[str, Any],
        schema_version: Optional[str] = None,
        strict_hidden_check: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes dynamic condition evaluation and zero-trust hidden client field guard.
        """
        schema = MedicineEditorSchemaService.get_active_schema(version=schema_version)
        sections = schema.sections
        conditional_rules = schema.conditional_rules

        # Collect all schema fields metadata
        fields_meta: Dict[str, Dict[str, Any]] = {}
        for sec in sections:
            for f in sec.get("fields", []):
                fields_meta[f["field_id"]] = f

        all_field_ids = set(fields_meta.keys())

        # Resolve active vs hidden fields under current dynamic input
        active_fields, hidden_fields, required_fields = cls.resolve_field_states(
            payload=payload,
            conditional_rules=conditional_rules,
            all_field_ids=all_field_ids,
        )

        sanitized_payload: Dict[str, Any] = {}
        pruned_hidden_fields: List[str] = []

        # ZERO-TRUST HIDDEN FIELD GUARD
        for h_field in hidden_fields:
            if h_field in payload:
                val = payload[h_field]
                # If non-null, non-empty, non-default value submitted in a hidden field
                if val is not None and str(val).strip() != "" and val is not False:
                    if strict_hidden_check:
                        dosage_form = payload.get("dosage_form", "current conditions")
                        raise ValidationError(
                            {
                                "error_code": "ERR_HIDDEN_FIELD_VALUE_REJECTED",
                                "field": h_field,
                                "message_en": (
                                    f"Field '{h_field}' is inactive/hidden under {dosage_form} "
                                    "conditions and must not contain data."
                                ),
                                "message_bn": (
                                    f"বর্তমান শর্তানুসারে '{h_field}' ফিল্ডটি প্রযোজ্য নয় "
                                    "এবং এতে কোনো তথ্য রাখা নিষিদ্ধ।"
                                ),
                            }
                        )
                    else:
                        pruned_hidden_fields.append(h_field)
            # Ensure hidden fields are always explicit None in sanitized payload
            sanitized_payload[h_field] = None

        # Check required fields
        missing_req = []
        for req_field in required_fields:
            if req_field in active_fields:
                val = payload.get(req_field)
                if val is None or str(val).strip() == "":
                    missing_req.append(req_field)

        if missing_req:
            raise ValidationError(
                {
                    "error_code": "ERR_CONDITIONAL_FIELD_REQUIRED",
                    "missing_fields": missing_req,
                    "message_en": f"Required fields missing under active conditions: {', '.join(missing_req)}.",
                    "message_bn": f"বর্তমান শর্তানুসারে আবশ্যকীয় ফিল্ডসমূহ পূরণ করুন: {', '.join(missing_req)}।",
                }
            )

        # Validate type safety on all active fields
        for act_field in active_fields:
            val = payload.get(act_field)
            meta = fields_meta.get(act_field, {})
            cleaned_val = cls.validate_type_safety(act_field, val, meta)
            sanitized_payload[act_field] = cleaned_val

        # Specific cross-field condition check: Cold chain temperature sanity
        if sanitized_payload.get("is_cold_chain") is True:
            min_t = sanitized_payload.get("min_temperature_celsius")
            max_t = sanitized_payload.get("max_temperature_celsius")
            if min_t is not None and max_t is not None:
                if min_t >= max_t:
                    raise ValidationError(
                        {
                            "error_code": "ERR_INVALID_TEMPERATURE_RANGE",
                            "message_en": f"Min temperature ({min_t}°C) must be strictly less than max temperature ({max_t}°C).",
                            "message_bn": f"সর্বনিম্ন তাপমাত্রা ({min_t}°সে) সর্বোচ্চ তাপমাত্রা ({max_t}°সে) অপেক্ষা কম হতে হবে।",
                        }
                    )

        # Antibiotic Rx enforcement
        if sanitized_payload.get("is_antibiotic") is True:
            sanitized_payload["is_prescription_required"] = True

        return {
            "is_valid": True,
            "schema_version": schema.schema_version,
            "active_fields": sorted(list(active_fields)),
            "hidden_fields": sorted(list(hidden_fields)),
            "pruned_hidden_fields": sorted(pruned_hidden_fields),
            "sanitized_payload": sanitized_payload,
            "message_en": "Payload passed dynamic condition evaluation and zero-trust hidden field guarding.",
            "message_bn": "পেলোডটি শর্তভিত্তিক মূল্যায়ন এবং হিডেন ফিল্ড সুরক্ষা সফলভাবে উত্তীর্ণ হয়েছে।",
        }
