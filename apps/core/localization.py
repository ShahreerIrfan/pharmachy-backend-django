"""
apps/core/localization.py

Executable Localization Engine for Pharmacy ERP SaaS.
Handles English (en-US) & Bengali (bn-BD) formatting, South Asian currency
grouping (Lakhs & Crores), Asia/Dhaka time normalization, bidirectional
numeral translation, and Unicode-normalized bilingual drug search.
"""

import unicodedata
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

DHAKA_TZ = ZoneInfo("Asia/Dhaka")

BENGALI_DIGITS = ["০", "১", "২", "৩", "৪", "৫", "৬", "৭", "৮", "৯"]
ASCII_DIGITS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]

ASCII_TO_BN_TABLE = str.maketrans("".join(ASCII_DIGITS), "".join(BENGALI_DIGITS))
BN_TO_ASCII_TABLE = str.maketrans("".join(BENGALI_DIGITS), "".join(ASCII_DIGITS))

BENGALI_MONTHS = [
    "জানুয়ারি",
    "ফেব্রুয়ারি",
    "মার্চ",
    "এপ্রিল",
    "মে",
    "জুন",
    "জুলাই",
    "আগস্ট",
    "সেপ্টেম্বর",
    "অক্টোবর",
    "নভেম্বর",
    "ডিসেম্বর",
]


def to_bengali_numerals(value: int | str | Decimal) -> str:
    """Translates ASCII Arabic digits (0-9) to Bengali digits (০-৯)."""
    return str(value).translate(ASCII_TO_BN_TABLE)


def to_ascii_numerals(value: str) -> str:
    """Translates Bengali digits (০-৯) to ASCII Arabic digits (0-9)."""
    return str(value).translate(BN_TO_ASCII_TABLE)


def format_south_asian_number(amount: Decimal) -> str:
    """
    Formats a Decimal using South Asian numbering (Lakhs and Crores).
    Group pattern: Last 3 digits grouped, preceding digits in pairs of 2.
    Example: 152345.50 -> 1,52,345.50
    """
    if not isinstance(amount, Decimal):
        raise TypeError(f"Monetary amounts must be Decimal, not {type(amount).__name__}.")

    # Quantize to 2 decimal places with deterministic rounding
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    abs_quantized = abs(quantized)

    str_val = f"{abs_quantized:.2f}"
    parts = str_val.split(".")
    integer_part = parts[0]
    fraction_part = parts[1]

    if len(integer_part) <= 3:
        grouped_integer = integer_part
    else:
        last_three = integer_part[-3:]
        remaining = integer_part[:-3]
        groups = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        groups.append(last_three)
        grouped_integer = ",".join(groups)

    return f"{sign}{grouped_integer}.{fraction_part}"


def format_bdt_currency(amount: Decimal, locale: str = "bn-BD", include_symbol: bool = True) -> str:
    """
    Formats a Decimal into Bangladeshi Taka (BDT) per locale standards.
    Supports 'bn-BD' (৳১,৫২,৩৪৫.৫০) and 'en-US' (৳ 1,52,345.50).
    """
    formatted_ascii = format_south_asian_number(amount)

    if locale == "bn-BD":
        bn_number = to_bengali_numerals(formatted_ascii)
        return f"৳{bn_number}" if include_symbol else bn_number
    else:
        return f"৳ {formatted_ascii}" if include_symbol else formatted_ascii


def parse_numeric_input(raw_input: str) -> Decimal:
    """
    Parses user numeric input typed in either English or Bengali into a Decimal.
    Handles comma separators and both digit scripts.
    Example: '১,৫২,৩৪৫.৫০' -> Decimal('152345.50')
    """
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise ValueError("Input string cannot be empty.")

    cleaned = raw_input.strip().replace("৳", "").replace(" ", "").replace(",", "")
    ascii_cleaned = to_ascii_numerals(cleaned)

    try:
        parsed = Decimal(ascii_cleaned)
        return parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as e:
        raise ValueError(f"Could not parse numeric input '{raw_input}': {e}")


def format_dhaka_datetime(dt: datetime, locale: str = "bn-BD", include_time: bool = True) -> str:
    """
    Normalizes a datetime to Asia/Dhaka (UTC+6) and formats per locale.
    """
    if dt.tzinfo is None:
        # Assume UTC if naive
        dt_dhaka = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(DHAKA_TZ)
    else:
        dt_dhaka = dt.astimezone(DHAKA_TZ)

    day = dt_dhaka.day
    month = dt_dhaka.month
    year = dt_dhaka.year
    hour = dt_dhaka.hour
    minute = dt_dhaka.minute

    if locale == "bn-BD":
        bn_day = to_bengali_numerals(f"{day:02d}")
        bn_month = to_bengali_numerals(f"{month:02d}")
        bn_year = to_bengali_numerals(str(year))

        if not include_time:
            return f"{bn_day}/{bn_month}/{bn_year}"

        period = "পূর্বাহ্ণ" if hour < 12 else "অপরাহ্ণ"
        hour_12 = hour % 12 or 12
        bn_hour = to_bengali_numerals(f"{hour_12:02d}")
        bn_minute = to_bengali_numerals(f"{minute:02d}")
        return f"{bn_day}/{bn_month}/{bn_year} {bn_hour}:{bn_minute} {period}"
    else:
        period = "AM" if hour < 12 else "PM"
        hour_12 = hour % 12 or 12
        time_str = f" {hour_12:02d}:{minute:02d} {period}" if include_time else ""
        return f"{day:02d}/{month:02d}/{year}{time_str}"


def normalize_search_query(text: str) -> str:
    """
    Normalizes a search query using Unicode NFKC normalization,
    stripping zero-width joiners/non-joiners, trimming whitespace, and lowercasing.
    """
    if not text:
        return ""
    # NFKC normalizes composite characters and font variations
    normalized = unicodedata.normalize("NFKC", text)
    # Remove zero-width non-joiner (\u200C) and zero-width joiner (\u200D)
    cleaned = normalized.replace("\u200c", "").replace("\u200d", "")
    return cleaned.strip().lower()


class BilingualDrugCatalogSearch:
    """In-memory search engine index proving bilingual matching for medicines."""

    def __init__(self):
        self._entries: list[dict[str, Any]] = []

    def add_drug(self, sku_id: str, brand_name_en: str, generic_name_en: str, bangla_aliases: list[str]):
        self._entries.append(
            {
                "sku_id": sku_id,
                "brand_en": brand_name_en,
                "generic_en": generic_name_en,
                "normalized_brand": normalize_search_query(brand_name_en),
                "normalized_generic": normalize_search_query(generic_name_en),
                "normalized_aliases": [normalize_search_query(a) for a in bangla_aliases],
            }
        )

    def search(self, query: str) -> list[dict[str, Any]]:
        norm_q = normalize_search_query(query)
        if not norm_q:
            return []

        results = []
        for entry in self._entries:
            # 1. Match English Brand or Generic
            if norm_q in entry["normalized_brand"] or norm_q in entry["normalized_generic"]:
                results.append(entry)
                continue

            # 2. Match Bengali Phonetic Aliases
            for alias in entry["normalized_aliases"]:
                if norm_q in alias or alias in norm_q:
                    results.append(entry)
                    break

        return results
