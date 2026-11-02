"""
Strength Measurement Taxonomy & Incompatible Unit Conversion Guard Service.
Architecture Decision: ADR-0123 (P123).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Dict, Optional

from rest_framework.exceptions import ValidationError

logger = logging.getLogger("pharmacy.core.strength_taxonomy")


class StrengthTaxonomyService:
    """Provides canonical physical measurement taxonomy, parsing, and incompatible conversion prevention."""

    # Unit definitions with base unit conversion factor (within the same family)
    UNIT_TAXONOMY: Dict[str, Dict[str, Any]] = {
        # MASS FAMILY (Base: mg)
        "mg": {"family": "MASS", "ratio": Decimal("1.0"), "name": "Milligram"},
        "g": {"family": "MASS", "ratio": Decimal("1000.0"), "name": "Gram"},
        "mcg": {"family": "MASS", "ratio": Decimal("0.001"), "name": "Microgram"},
        "ug": {"family": "MASS", "ratio": Decimal("0.001"), "name": "Microgram (ug)"},

        # VOLUME FAMILY (Base: mL)
        "ml": {"family": "VOLUME", "ratio": Decimal("1.0"), "name": "Milliliter"},
        "l": {"family": "VOLUME", "ratio": Decimal("1000.0"), "name": "Liter"},

        # PERCENTAGE FAMILY (Base: %)
        "%": {"family": "PERCENTAGE", "ratio": Decimal("1.0"), "name": "Percentage w/w or w/v"},

        # CONCENTRATION FAMILY
        "mg/5ml": {"family": "CONCENTRATION", "ratio": Decimal("1.0"), "name": "Milligrams per 5 mL"},
        "mg/ml": {"family": "CONCENTRATION", "ratio": Decimal("5.0"), "name": "Milligrams per mL"},
        "mcg/ml": {"family": "CONCENTRATION", "ratio": Decimal("0.005"), "name": "Micrograms per mL"},

        # BIOLOGICAL UNITS
        "iu": {"family": "BIOLOGICAL", "ratio": Decimal("1.0"), "name": "International Unit"},
        "iu/ml": {"family": "BIOLOGICAL", "ratio": Decimal("1.0"), "name": "International Units per mL"},
    }

    @classmethod
    def normalize_unit_key(cls, unit_str: str) -> str:
        """Normalizes unit strings for canonical lookup (e.g. 'ML' -> 'ml', 'MG/5ML' -> 'mg/5ml')."""
        u = unit_str.strip().lower()
        if u in ["%", "percent", "percentage"]:
            return "%"
        return u

    @classmethod
    def get_unit_family(cls, unit: str) -> Optional[str]:
        key = cls.normalize_unit_key(unit)
        meta = cls.UNIT_TAXONOMY.get(key)
        return meta["family"] if meta else None

    @classmethod
    def get_taxonomy_catalog(cls) -> Dict[str, Any]:
        """Returns structured taxonomy catalog of supported physical measurement families."""
        families: Dict[str, list] = {}
        for k, v in cls.UNIT_TAXONOMY.items():
            fam = v["family"]
            if fam not in families:
                families[fam] = []
            families[fam].append({"unit": k, "name": v["name"]})
        return {
            "families_count": len(families),
            "families": families,
        }

    @classmethod
    def parse_strength(cls, raw_strength: str) -> Dict[str, Any]:
        """
        Parses human-readable composite strength into normalized numeric value, unit, and family.
        e.g. '500 mg' -> {numeric_value: 500, unit: 'mg', family: 'MASS'}
        """
        if not raw_strength or not raw_strength.strip():
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_STRENGTH_STRING",
                    "message_en": "Strength string cannot be empty.",
                    "message_bn": "স্ট্রেংথ তথ্য খালি রাখা যাবে না।",
                }
            )

        cleaned = raw_strength.strip()

        # Regex matching numeric (including decimal) followed by unit
        pattern = r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z%]+(?:/[0-9]*[a-zA-Z]+)?)\s*$"
        match = re.match(pattern, cleaned)
        if not match:
            # Fallback for composite (e.g. 500mg+65mg) - extracts leading component
            composite_match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z%]+)", cleaned)
            if composite_match:
                num_val = Decimal(composite_match.group(1))
                unit_key = cls.normalize_unit_key(composite_match.group(2))
                family = cls.get_unit_family(unit_key) or "MASS"
                return {
                    "raw_strength": cleaned,
                    "numeric_value": str(num_val),
                    "unit": unit_key,
                    "family": family,
                    "is_composite": True,
                }
            raise ValidationError(
                {
                    "error_code": "ERR_UNPARSEABLE_STRENGTH",
                    "message_en": f"Could not parse numeric strength and unit from '{cleaned}'.",
                    "message_bn": "স্ট্রেংথ ফরম্যাট সঠিকভাবে চিহ্নিত করা যায়নি।",
                }
            )

        num_val = Decimal(match.group(1))
        unit_raw = match.group(2)
        unit_key = cls.normalize_unit_key(unit_raw)
        family = cls.get_unit_family(unit_key)

        if not family:
            raise ValidationError(
                {
                    "error_code": "ERR_UNKNOWN_STRENGTH_UNIT",
                    "message_en": f"Unrecognized strength unit '{unit_raw}'.",
                    "message_bn": "অপরিচিত স্ট্রেংথ পরিমাপের ইউনিট।",
                }
            )

        return {
            "raw_strength": cleaned,
            "numeric_value": str(num_val),
            "unit": unit_key,
            "family": family,
            "is_composite": False,
        }

    @classmethod
    def convert_strength(
        cls,
        value: Decimal | float | str,
        from_unit: str,
        to_unit: str,
    ) -> Dict[str, Any]:
        """
        Converts a strength value from one unit to another, strictly rejecting cross-family conversions.
        """
        try:
            val_dec = Decimal(str(value))
        except Exception:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_NUMERIC_VALUE",
                    "message_en": f"Invalid numeric value '{value}'.",
                    "message_bn": "অবৈধ সংখ্যা মান।",
                }
            )

        from_key = cls.normalize_unit_key(from_unit)
        to_key = cls.normalize_unit_key(to_unit)

        from_meta = cls.UNIT_TAXONOMY.get(from_key)
        to_meta = cls.UNIT_TAXONOMY.get(to_key)

        if not from_meta:
            raise ValidationError(
                {
                    "error_code": "ERR_UNKNOWN_STRENGTH_UNIT",
                    "message_en": f"Source unit '{from_unit}' is not in the strength taxonomy.",
                    "message_bn": "উৎস ইউনিটটি ক্যাটালগে নেই।",
                }
            )

        if not to_meta:
            raise ValidationError(
                {
                    "error_code": "ERR_UNKNOWN_STRENGTH_UNIT",
                    "message_en": f"Target unit '{to_unit}' is not in the strength taxonomy.",
                    "message_bn": "লক্ষ্য ইউনিটটি ক্যাটালগে নেই।",
                }
            )

        # INCOMPATIBLE UNIT CONVERSION GUARD
        if from_meta["family"] != to_meta["family"]:
            raise ValidationError(
                {
                    "error_code": "ERR_INCOMPATIBLE_UNIT_CONVERSION",
                    "message_en": (
                        f"Incompatible unit conversion from '{from_unit}' ({from_meta['family']}) "
                        f"to '{to_unit}' ({to_meta['family']}). Cross-family conversion is strictly forbidden."
                    ),
                    "message_bn": "অসঙ্গতিপূর্ণ পরিমাপের ইউনিট রূপান্তর নিষিদ্ধ।",
                }
            )

        # Convert through base unit
        base_value = val_dec * from_meta["ratio"]
        target_value = base_value / to_meta["ratio"]
        normalized_str = f"{target_value.normalize():f}"

        return {
            "original_value": str(val_dec),
            "from_unit": from_key,
            "from_family": from_meta["family"],
            "converted_value": normalized_str,
            "to_unit": to_key,
            "to_family": to_meta["family"],
            "is_compatible": True,
            "formatted_result": f"{normalized_str} {to_key}",
        }
