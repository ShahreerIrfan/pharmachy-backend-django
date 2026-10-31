"""
apps.secrets_vault.validator
Architecture Decision: ADR-0037
Missing-Secret Fail-Fast Invariant and Entropy Validation Engine.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set

from apps.secrets_vault.vault import SecretTier


class MissingSecretError(Exception):
    """Raised when one or more mandatory environment secrets are missing on startup."""

    pass


class InsecureSecretError(Exception):
    """Raised when an environment secret uses an insecure known default or low entropy in production."""

    pass


class FailFastSecretValidator:
    """Enforces fail-fast startup validation for development, staging, and production tiers."""

    REQUIRED_PRODUCTION_SECRETS: List[str] = [
        "DJANGO_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "REDIS_URL",
        "JWT_SIGNING_KEY",
    ]

    REQUIRED_STAGING_SECRETS: List[str] = [
        "DJANGO_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "JWT_SIGNING_KEY",
    ]

    REQUIRED_DEVELOPMENT_SECRETS: List[str] = [
        "DJANGO_SECRET_KEY",
    ]

    KNOWN_INSECURE_KEYS: Set[str] = {
        "django-insecure-dev-key-pharmacy-saas-2026-bangladesh",
        "dev-insecure-key",
        "secret",
        "changeme",
        "password",
        "123456",
        "admin",
        "default",
    }

    @classmethod
    def get_required_secrets(cls, tier: SecretTier) -> List[str]:
        if tier == SecretTier.PRODUCTION:
            return cls.REQUIRED_PRODUCTION_SECRETS
        elif tier == SecretTier.STAGING:
            return cls.REQUIRED_STAGING_SECRETS
        return cls.REQUIRED_DEVELOPMENT_SECRETS

    @classmethod
    def validate_key_entropy(cls, key_name: str, key_val: str, tier: SecretTier) -> None:
        """Validates that keys in production have sufficient entropy (>= 32 chars, letters, digits)."""
        if tier != SecretTier.PRODUCTION:
            return

        if len(key_val) < 32:
            raise InsecureSecretError(
                f"Production secret '{key_name}' has insufficient length ({len(key_val)} < 32 characters)."
            )

        has_letter = bool(re.search(r"[a-zA-Z]", key_val))
        has_digit = bool(re.search(r"[0-9]", key_val))
        if not (has_letter and has_digit):
            raise InsecureSecretError(
                f"Production secret '{key_name}' must contain both alphabetic characters and digits for entropy."
            )

    @classmethod
    def assert_environment_secrets(cls, tier: SecretTier, env: Dict[str, Any]) -> None:
        """Evaluates environment configuration and fails fast if any secrets are missing or insecure."""
        required_keys = cls.get_required_secrets(tier)
        missing_keys: List[str] = []

        for k in required_keys:
            val = env.get(k)
            if not val or not str(val).strip():
                missing_keys.append(k)

        if missing_keys:
            raise MissingSecretError(
                f"Fail-Fast Violation: Missing required environment secret(s) for tier '{tier.value}': {missing_keys}"
            )

        # In Staging and Production: prohibit known insecure keys
        if tier in {SecretTier.STAGING, SecretTier.PRODUCTION}:
            for k in required_keys:
                val = str(env.get(k, "")).strip()
                if val in cls.KNOWN_INSECURE_KEYS or val.startswith("django-insecure-"):
                    raise InsecureSecretError(
                        f"Insecure secret rejected for tier '{tier.value}': '{k}' uses a known insecure default."
                    )
                cls.validate_key_entropy(k, val, tier)
