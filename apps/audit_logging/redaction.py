"""
apps.audit_logging.redaction
Architecture Decision: ADR-0035
Zero-PHI, Credential, and Financial Secret Redaction Engine for Bangladesh Pharmacy SaaS.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Set


class SensitiveDataRedactor:
    """Enforces zero-leakage redaction of credentials, PHI, financial PINs, and citizen NIDs."""

    REDACTED_TEXT = "[REDACTED]"
    REDACTED_NID = "[REDACTED_NID]"
    REDACTED_PHONE = "[REDACTED_PHONE]"
    REDACTED_FINANCIAL = "[REDACTED_FINANCIAL]"
    REDACTED_PHI = "[REDACTED_PHI]"

    # Exact or substring match for sensitive field keys
    CREDENTIAL_KEYS: Set[str] = {
        "password",
        "secret",
        "token",
        "authorization",
        "cookie",
        "access_token",
        "refresh_token",
        "api_key",
        "otp",
        "secret_key",
        "auth",
        "jwt",
    }

    FINANCIAL_KEYS: Set[str] = {
        "pin",
        "bkash_pin",
        "nagad_pin",
        "rocket_pin",
        "cvv",
        "card_number",
        "bank_account",
        "credit_card",
    }

    PHI_KEYS: Set[str] = {
        "diagnosis",
        "clinical_notes",
        "prescription_image",
        "symptoms",
        "patient_name",
        "medical_history",
        "therapy",
    }

    # Bangladesh National ID patterns (10-digit smart card, 13-digit legacy, 17-digit birth-year prefixed)
    NID_PATTERN = re.compile(r"\b(\d{10}|\d{13}|\d{17})\b")

    # Real Bangladesh mobile number pattern: +8801[3-9]\d{8} or 01[3-9]\d{8}
    REAL_PHONE_PATTERN = re.compile(r"(?:\+?88)?(01[3-9]\d{8})\b")
    ALLOWED_TEST_PHONE_PREFIXES = ("01700000", "8801700000", "+8801700000")

    # Bearer token regex
    BEARER_PATTERN = re.compile(r"Bearer\s+([A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE)

    # Credit card / 16-digit card pattern
    CARD_PATTERN = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")

    @classmethod
    def redact_string(cls, text: str) -> str:
        """Redacts sensitive values inside a text string using regex patterns."""
        if not text:
            return text

        # 1. Redact Bearer tokens
        redacted = cls.BEARER_PATTERN.sub("Bearer [REDACTED_TOKEN]", text)

        # 2. Redact 16-digit credit cards
        redacted = cls.CARD_PATTERN.sub(cls.REDACTED_FINANCIAL, redacted)

        # 3. Redact real phone numbers (exempt synthetic test prefix)
        def _replace_phone(match: re.Match[str]) -> str:
            val = match.group(0)
            for prefix in cls.ALLOWED_TEST_PHONE_PREFIXES:
                if prefix in val:
                    return val
            return cls.REDACTED_PHONE

        redacted = cls.REAL_PHONE_PATTERN.sub(_replace_phone, redacted)

        # 4. Redact Bangladesh NID
        def _replace_nid(match: re.Match[str]) -> str:
            # Check length: 10, 13, or 17 digits
            digits = match.group(1)
            # If it looks like an allowed test phone, don't replace as NID
            for prefix in cls.ALLOWED_TEST_PHONE_PREFIXES:
                if prefix in digits:
                    return digits
            return cls.REDACTED_NID

        redacted = cls.NID_PATTERN.sub(_replace_nid, redacted)

        return redacted

    @classmethod
    def redact(cls, data: Any) -> Any:
        """Recursively traverses dictionaries, lists, and values to redact sensitive data."""
        if data is None:
            return None

        if isinstance(data, dict):
            redacted_dict: Dict[str, Any] = {}
            for k, v in data.items():
                k_lower = str(k).lower().strip()

                # Check key categories (exempt pseudonymized domain tokens)
                if k_lower in {"patient_token", "prescription_token", "product_token", "session_token"}:
                    redacted_dict[k] = cls.redact(v)
                elif any(cred in k_lower for cred in cls.CREDENTIAL_KEYS):
                    redacted_dict[k] = cls.REDACTED_TEXT
                elif any(fin in k_lower for fin in cls.FINANCIAL_KEYS):
                    redacted_dict[k] = cls.REDACTED_FINANCIAL
                elif any(phi in k_lower for phi in cls.PHI_KEYS):
                    redacted_dict[k] = cls.REDACTED_PHI
                elif "nid" in k_lower or "national_id" in k_lower:
                    redacted_dict[k] = cls.REDACTED_NID
                else:
                    redacted_dict[k] = cls.redact(v)
            return redacted_dict

        if isinstance(data, list):
            return [cls.redact(item) for item in data]

        if isinstance(data, tuple):
            return tuple(cls.redact(item) for item in data)

        if isinstance(data, str):
            # Check if string contains JSON
            stripped = data.strip()
            if (stripped.startswith("{") and stripped.endswith("}")) or (
                stripped.startswith("[") and stripped.endswith("]")
            ):
                try:
                    parsed = json.loads(data)
                    redacted_json = cls.redact(parsed)
                    return json.dumps(redacted_json)
                except Exception:
                    pass
            return cls.redact_string(data)

        return data
