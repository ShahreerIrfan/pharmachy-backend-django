"""
apps/core/time_and_money.py

Standardized Time and Monetary Calculation Engine for Bangladesh Pharmacy ERP & POS.
Architecture Decision: ADR-0055
Complies with:
  - Exact Decimal representation (no binary float corruption)
  - Decimal scale 2 quantization with ROUND_HALF_UP
  - South Asian Lakh/Crore grouping (1,52,345.50)
  - Aware UTC timestamps at rest (rejection of naive datetimes)
  - Display and reporting in Asia/Dhaka (UTC+6)
  - Unambiguous date parsing and FEFO expiry verification
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from apps.core.localization import (
    DHAKA_TZ,
    format_bdt_currency,
    format_dhaka_datetime,
)

UTC_TZ = ZoneInfo("UTC")
MONETARY_SCALE = 2
MONETARY_QUANTUM = Decimal("0.01")
PERCENT_QUANTUM = Decimal("0.01")


class MoneyStandard:
    """Enterprise Decimal money validator, arithmetic engine, and formatter."""

    CURRENCY_CODE = "BDT"
    CURRENCY_SYMBOL = "৳"
    ROUNDING_MODE = ROUND_HALF_UP

    @classmethod
    def to_decimal(cls, value: Any, field_name: str = "amount") -> Decimal:
        """
        Converts input to Decimal and quantizes to scale 2 with ROUND_HALF_UP.
        STRICT REJECTION: Passing raw float is strictly forbidden because
        binary float introduces subtle floating-point precision loss.
        """
        if isinstance(value, float):
            raise ValueError(
                f"Floating-point representation detected for monetary field '{field_name}'. "
                f"Raw floats like {value} are strictly prohibited. Pass value as Decimal, integer, or exact string."
            )

        if value is None:
            raise ValueError(f"Monetary field '{field_name}' cannot be None.")

        if isinstance(value, Decimal):
            dec_val = value
        elif isinstance(value, (int, str)):
            cleaned = str(value).strip().replace("৳", "").replace(",", "")
            if not cleaned:
                raise ValueError(f"Monetary field '{field_name}' cannot be empty string.")
            try:
                dec_val = Decimal(cleaned)
            except (InvalidOperation, ValueError) as err:
                raise ValueError(f"Invalid monetary format for '{field_name}': '{value}'.") from err
        else:
            raise TypeError(
                f"Unsupported type '{type(value).__name__}' for monetary field '{field_name}'. "
                "Must be Decimal, int, or string."
            )

        # Check for NaN or Infinity
        if not dec_val.is_finite():
            raise ValueError(f"Monetary field '{field_name}' must be finite (received {dec_val}).")

        return dec_val.quantize(MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE)

    @classmethod
    def validate_non_negative(cls, amount: Decimal, field_name: str = "amount") -> Decimal:
        """Ensures monetary amount is non-negative (>= 0.00 BDT)."""
        dec = cls.to_decimal(amount, field_name=field_name)
        if dec < Decimal("0.00"):
            raise ValueError(f"Monetary field '{field_name}' cannot be negative (received {dec} BDT).")
        return dec

    @classmethod
    def format_currency(cls, amount: Decimal, locale: str = "bn-BD", include_symbol: bool = True) -> str:
        """Formats Decimal amount into BDT currency with South Asian grouping."""
        dec = cls.to_decimal(amount, field_name="amount")
        return format_bdt_currency(dec, locale=locale, include_symbol=include_symbol)

    @classmethod
    def calculate_line_item(
        cls,
        unit_price: Any,
        quantity: int,
        discount_percent: Any = Decimal("0.00"),
        tax_percent: Any = Decimal("0.00"),
    ) -> dict[str, Any]:
        """
        Calculates a single line item with deterministic rounding:
          1. gross_subtotal = (unit_price * quantity)
          2. discount_amount = gross_subtotal * (discount_percent / 100) [quantized]
          3. taxable_amount = gross_subtotal - discount_amount
          4. tax_amount = taxable_amount * (tax_percent / 100) [quantized]
          5. line_total = taxable_amount + tax_amount
        """
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError(f"Quantity must be a positive integer > 0 (received {quantity}).")

        price_dec = cls.validate_non_negative(unit_price, field_name="unit_price")
        disc_pct = cls.to_decimal(discount_percent, field_name="discount_percent")
        tax_pct = cls.to_decimal(tax_percent, field_name="tax_percent")

        if disc_pct < Decimal("0.00") or disc_pct > Decimal("100.00"):
            raise ValueError(f"Discount percent must be between 0.00 and 100.00% (received {disc_pct}%).")
        if tax_pct < Decimal("0.00"):
            raise ValueError(f"Tax percent cannot be negative (received {tax_pct}%).")

        gross_subtotal = (price_dec * Decimal(quantity)).quantize(MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE)
        discount_amount = (gross_subtotal * (disc_pct / Decimal("100"))).quantize(
            MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE
        )
        taxable_amount = gross_subtotal - discount_amount
        tax_amount = (taxable_amount * (tax_pct / Decimal("100"))).quantize(
            MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE
        )
        line_total = (taxable_amount + tax_amount).quantize(MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE)

        return {
            "unit_price": str(price_dec),
            "quantity": quantity,
            "gross_subtotal": str(gross_subtotal),
            "discount_percent": str(disc_pct),
            "discount_amount": str(discount_amount),
            "taxable_amount": str(taxable_amount),
            "tax_percent": str(tax_pct),
            "tax_amount": str(tax_amount),
            "line_total": str(line_total),
            "formatted_bdt_en": format_bdt_currency(line_total, locale="en-US"),
            "formatted_bdt_bn": format_bdt_currency(line_total, locale="bn-BD"),
        }

    @classmethod
    def calculate_invoice(
        cls,
        items: list[dict[str, Any]],
        invoice_discount_amount: Any = Decimal("0.00"),
        paid_amount: Any = Decimal("0.00"),
    ) -> dict[str, Any]:
        """
        Calculates invoice totals from an item list with exact Decimal balance reconciliation.
        """
        if not items:
            raise ValueError("Invoice must contain at least one line item.")

        processed_items: list[dict[str, Any]] = []
        subtotal = Decimal("0.00")
        total_item_discounts = Decimal("0.00")
        total_tax = Decimal("0.00")

        for idx, item in enumerate(items):
            line_calc = cls.calculate_line_item(
                unit_price=item["unit_price"],
                quantity=int(item["quantity"]),
                discount_percent=item.get("discount_percent", Decimal("0.00")),
                tax_percent=item.get("tax_percent", Decimal("0.00")),
            )
            line_calc["item_name"] = item.get("item_name", f"Item #{idx + 1}")
            line_calc["sku_id"] = item.get("sku_id", f"SKU-{idx + 1}")
            processed_items.append(line_calc)

            subtotal += Decimal(line_calc["gross_subtotal"])
            total_item_discounts += Decimal(line_calc["discount_amount"])
            total_tax += Decimal(line_calc["tax_amount"])

        inv_discount = cls.validate_non_negative(invoice_discount_amount, field_name="invoice_discount_amount")
        paid = cls.validate_non_negative(paid_amount, field_name="paid_amount")

        total_discount = (total_item_discounts + inv_discount).quantize(MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE)
        net_taxable = max(Decimal("0.00"), subtotal - total_discount)
        grand_total = (net_taxable + total_tax).quantize(MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE)

        due_amount = max(Decimal("0.00"), grand_total - paid).quantize(MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE)
        change_amount = max(Decimal("0.00"), paid - grand_total).quantize(MONETARY_QUANTUM, rounding=cls.ROUNDING_MODE)

        return {
            "items": processed_items,
            "item_count": len(processed_items),
            "subtotal": str(subtotal),
            "total_item_discounts": str(total_item_discounts),
            "invoice_discount": str(inv_discount),
            "total_discount": str(total_discount),
            "net_taxable": str(net_taxable),
            "total_tax": str(total_tax),
            "grand_total": str(grand_total),
            "paid_amount": str(paid),
            "due_amount": str(due_amount),
            "change_amount": str(change_amount),
            "currency": cls.CURRENCY_CODE,
            "currency_symbol": cls.CURRENCY_SYMBOL,
            "display": {
                "subtotal_bdt_en": format_bdt_currency(subtotal, locale="en-US"),
                "subtotal_bdt_bn": format_bdt_currency(subtotal, locale="bn-BD"),
                "grand_total_bdt_en": format_bdt_currency(grand_total, locale="en-US"),
                "grand_total_bdt_bn": format_bdt_currency(grand_total, locale="bn-BD"),
                "due_amount_bdt_en": format_bdt_currency(due_amount, locale="en-US"),
                "due_amount_bdt_bn": format_bdt_currency(due_amount, locale="bn-BD"),
            },
        }


class TimeStandard:
    """Enterprise Time and Date normalization engine for Bangladesh Pharmacy SaaS."""

    STORAGE_TIMEZONE = "UTC"
    DISPLAY_TIMEZONE = "Asia/Dhaka"

    @classmethod
    def ensure_aware_utc(cls, value: Any, field_name: str = "timestamp") -> datetime:
        """
        Validates that a datetime or ISO-8601 string is timezone-aware and converts to UTC.
        STRICT REJECTION: Rejects naive datetimes to avoid ambiguous offsets across clients.
        """
        if value is None:
            raise ValueError(f"Datetime field '{field_name}' cannot be None.")

        if isinstance(value, str):
            clean_str = value.strip()
            if not clean_str:
                raise ValueError(f"Datetime string for '{field_name}' cannot be blank.")
            # Handle standard 'Z' ISO notation
            if clean_str.endswith("Z"):
                clean_str = clean_str[:-1] + "+00:00"
            try:
                parsed_dt = datetime.fromisoformat(clean_str)
            except Exception as err:
                raise ValueError(f"Invalid ISO-8601 datetime format for '{field_name}': '{value}'.") from err
        elif isinstance(value, datetime):
            parsed_dt = value
        else:
            raise TypeError(
                f"Unsupported type '{type(value).__name__}' for datetime field '{field_name}'. "
                "Must be aware datetime or ISO-8601 string."
            )

        if parsed_dt.tzinfo is None or parsed_dt.tzinfo.utcoffset(parsed_dt) is None:
            raise ValueError(
                f"Naive datetime detected for '{field_name}' ({parsed_dt}). "
                "All timestamps must be timezone-aware (ISO-8601 with offset or 'Z')."
            )

        return parsed_dt.astimezone(UTC_TZ)

    @classmethod
    def to_dhaka(cls, dt_val: datetime) -> datetime:
        """Converts an aware datetime to Asia/Dhaka (UTC+6)."""
        aware_utc = cls.ensure_aware_utc(dt_val)
        return aware_utc.astimezone(DHAKA_TZ)

    @classmethod
    def format_dhaka_display(
        cls,
        dt_val: datetime | str,
        locale: str = "bn-BD",
        include_time: bool = True,
    ) -> str:
        """Renders aware datetime into localized Asia/Dhaka presentation."""
        aware = cls.ensure_aware_utc(dt_val)
        return format_dhaka_datetime(aware, locale=locale, include_time=include_time)

    @classmethod
    def parse_date_only(cls, value: Any, field_name: str = "date") -> date:
        """
        Parses date string in unambiguous ISO (YYYY-MM-DD) or British/South Asian (DD/MM/YYYY) format.
        """
        if isinstance(value, date) and not isinstance(value, datetime):
            return value

        if isinstance(value, datetime):
            return value.date()

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Date string for '{field_name}' cannot be empty.")

        cleaned = value.strip()
        # Attempt YYYY-MM-DD
        if "-" in cleaned:
            parts = cleaned.split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
        # Attempt DD/MM/YYYY
        if "/" in cleaned:
            parts = cleaned.split("/")
            if len(parts) == 3:
                return date(int(parts[2]), int(parts[1]), int(parts[0]))

        raise ValueError(
            f"Unrecognized date format for '{field_name}': '{value}'. Supported formats: 'YYYY-MM-DD' or 'DD/MM/YYYY'."
        )

    @classmethod
    def is_batch_expired(
        cls,
        expiry_date: date | str,
        reference_time_utc: datetime | None = None,
    ) -> bool:
        """
        Checks whether a medicine batch has expired based on current date in Asia/Dhaka timezone.
        """
        exp = cls.parse_date_only(expiry_date, field_name="expiry_date")
        if reference_time_utc is None:
            dhaka_now = datetime.now(DHAKA_TZ)
        else:
            dhaka_now = cls.to_dhaka(reference_time_utc)
        current_dhaka_date = dhaka_now.date()
        return exp < current_dhaka_date
