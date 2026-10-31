"""
apps.core.validators
Architecture Decision: ADR-0014 & ADR-0046
Reusable, server-side domain input validators for Bangladesh Pharmacy ERP & POS SaaS:
- Strict Decimal precision for money (zero float drift).
- Quantity positive integer bounds.
- Bangladesh E.164 phone normalization.
- Date ordering (expiry >= today, purchase <= today).
- Cold-chain temperature safety checks.
- Referential integrity and pricing invariants.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone


def validate_money_decimal(
    val: Any,
    field_name: str = "amount",
    min_value: Decimal = Decimal("0.00"),
    max_value: Decimal = Decimal("999999999.99"),
) -> Decimal:
    """
    Validates that a monetary amount is an exact Decimal with at most 2 decimal places.
    Floating-point numbers are rejected to prevent financial rounding drift (ADR-0046).
    """
    if val is None:
        raise ValidationError(f"{field_name} is required.")

    if isinstance(val, float):
        raise ValidationError(
            f"{field_name} must not be a floating-point number. Use string or Decimal representation."
        )

    try:
        dec = Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError) as err:
        raise ValidationError(f"Invalid monetary decimal format for {field_name}: '{val}'") from err

    # Check decimal places
    if dec.as_tuple().exponent < -2:
        raise ValidationError(f"{field_name} cannot have more than 2 decimal places: '{dec}'")

    if dec < min_value:
        raise ValidationError(f"{field_name} cannot be less than {min_value} BDT.")

    if dec > max_value:
        raise ValidationError(f"{field_name} exceeds maximum allowable ceiling of {max_value} BDT.")

    return dec


def validate_positive_quantity(val: Any, field_name: str = "quantity", max_val: int = 1000000) -> int:
    """
    Validates that a quantity is an integer strictly greater than zero.
    """
    if val is None:
        raise ValidationError(f"{field_name} is required.")

    try:
        qty = int(val)
    except (ValueError, TypeError) as err:
        raise ValidationError(f"{field_name} must be a valid integer: '{val}'") from err

    if qty <= 0:
        raise ValidationError(f"{field_name} must be strictly positive (greater than 0). Received: {qty}")

    if qty > max_val:
        raise ValidationError(f"{field_name} exceeds reasonable dispensing maximum of {max_val}.")

    return qty


def validate_date_not_past(
    d: datetime.date | str | None,
    field_name: str = "expiry_date",
    today: datetime.date | None = None,
) -> datetime.date:
    """
    Validates that a date (e.g. batch expiry) is strictly on or after the current date (DGDA rule).
    """
    if d is None:
        raise ValidationError(f"{field_name} is required.")

    if isinstance(d, str):
        try:
            d = datetime.date.fromisoformat(d)
        except ValueError as err:
            raise ValidationError(f"Invalid ISO date format for {field_name}: '{d}'. Expected YYYY-MM-DD.") from err

    curr_today = today or timezone.now().date()
    if d < curr_today:
        raise ValidationError(
            f"{field_name} '{d}' is in the past. Current date is {curr_today}. "
            f"Receiving or dispensing expired stock is strictly prohibited under DGDA regulations."
        )

    return d


def validate_date_not_future(
    d: datetime.date | str | None,
    field_name: str = "purchase_date",
    today: datetime.date | None = None,
) -> datetime.date:
    """
    Validates that a historical date (e.g. purchase or manufacture date) is not in the future.
    """
    if d is None:
        raise ValidationError(f"{field_name} is required.")

    if isinstance(d, str):
        try:
            d = datetime.date.fromisoformat(d)
        except ValueError as err:
            raise ValidationError(f"Invalid ISO date format for {field_name}: '{d}'. Expected YYYY-MM-DD.") from err

    curr_today = today or timezone.now().date()
    if d > curr_today:
        raise ValidationError(f"{field_name} '{d}' cannot be in the future. Current date is {curr_today}.")

    return d


def validate_cold_chain_temps(
    requires_cold_chain: bool,
    min_temp: Decimal | str | float | None,
    max_temp: Decimal | str | float | None,
) -> tuple[Decimal | None, Decimal | None]:
    """
    Enforces that cold-chain pharmaceuticals have valid, non-inverted temperature bounds.
    Standard Bangladesh refrigerator vaccines/insulin: 2.00°C to 8.00°C.
    """
    if not requires_cold_chain:
        min_d = Decimal(str(min_temp)) if min_temp is not None else None
        max_d = Decimal(str(max_temp)) if max_temp is not None else None
        return min_d, max_d

    if min_temp is None or max_temp is None:
        raise ValidationError("Both min_temp_celsius and max_temp_celsius are mandatory for cold-chain medicines.")

    try:
        min_d = Decimal(str(min_temp))
        max_d = Decimal(str(max_temp))
    except (InvalidOperation, TypeError, ValueError) as err:
        raise ValidationError("Cold chain temperatures must be valid numeric values.") from err

    if min_d > max_d:
        raise ValidationError(
            f"Inverted temperature range: min_temp_celsius ({min_d}°C) cannot exceed max_temp_celsius ({max_d}°C)."
        )

    return min_d, max_d


def validate_mrp_above_cost(mrp: Decimal, cost: Decimal) -> None:
    """
    Validates the DGDA retail pricing rule: Maximum Retail Price (MRP) cannot be less than wholesale cost.
    """
    if mrp < cost:
        raise ValidationError(f"Maximum Retail Price ({mrp} BDT) cannot be less than purchase cost ({cost} BDT).")


def validate_discount_bounds(total_amount: Decimal, discount_amount: Decimal) -> None:
    """
    Validates that discount is non-negative and does not exceed the gross total amount.
    """
    if discount_amount < Decimal("0.00"):
        raise ValidationError("Discount amount cannot be negative.")

    if discount_amount > total_amount:
        raise ValidationError(
            f"Discount amount ({discount_amount} BDT) cannot exceed gross total ({total_amount} BDT)."
        )


def validate_enum_choice(val: str, enum_cls: Any, field_name: str) -> str:
    """
    Validates that a string matches one of the canonical enum choice values.
    """
    if not val:
        raise ValidationError(f"{field_name} is required.")

    if hasattr(enum_cls, "values"):
        valid_keys = [str(v).upper() for v in enum_cls.values]
    elif hasattr(enum_cls, "choices"):
        valid_keys = [str(k).upper() for k, _ in enum_cls.choices]
    else:
        valid_keys = [str(getattr(c, "value", c)).upper() for c in enum_cls]

    cleaned = str(val).strip().upper()
    if cleaned not in valid_keys:
        raise ValidationError(f"Invalid {field_name}: '{val}'. Allowed choices are: {', '.join(valid_keys)}.")

    return cleaned


def validate_bangladesh_phone_number(raw_phone: str) -> str:
    """
    Validates Bangladesh mobile number format: +8801[3-9]\\d{8}.
    """
    if not raw_phone:
        raise ValidationError("Phone number is required.")

    cleaned = re.sub(r"[\s\-\(\)]", "", str(raw_phone).strip())
    if cleaned.startswith("+880"):
        pass
    elif cleaned.startswith("880"):
        cleaned = "+" + cleaned
    elif cleaned.startswith("01"):
        cleaned = "+88" + cleaned
    elif cleaned.startswith("1"):
        cleaned = "+880" + cleaned
    else:
        raise ValidationError(f"Invalid Bangladesh phone number format: '{raw_phone}'. Must match +8801[3-9]XXXXXXXX.")

    if not re.match(r"^\+8801[3-9]\d{8}$", cleaned):
        raise ValidationError(
            f"Invalid Bangladesh mobile operator prefix or length: '{cleaned}'. "
            f"Valid operators: 013, 014, 015, 016, 017, 018, 019 (11 digits)."
        )

    return cleaned
