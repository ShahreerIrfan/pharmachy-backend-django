"""
apps.secrets_vault.rotation
Architecture Decision: ADR-0037
Secret Rotation Engine and Dual-Key Grace Period Verification.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional


@dataclass
class DualKeyConfig:
    primary_key: str
    secondary_key: Optional[str] = None
    version: int = 1


class DualKeyRotationValidator:
    """Supports seamless secret rotation by validating tokens against primary and secondary keys."""

    def __init__(self, primary_key: str, secondary_key: Optional[str] = None, version: int = 1) -> None:
        self.primary_key = primary_key
        self.secondary_key = secondary_key
        self.version = version

    def sign(self, message: str) -> str:
        """Signs message using active primary key."""
        return hmac.new(self.primary_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, message: str, signature: str) -> bool:
        """Verifies message signature against primary key first; falls back to secondary key during grace period."""
        primary_sig = self.sign(message)
        if hmac.compare_digest(primary_sig, signature):
            return True

        if self.secondary_key:
            secondary_sig = hmac.new(
                self.secondary_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(secondary_sig, signature):
                return True

        return False

    def rotate(self, new_key: str) -> None:
        """Rotates key: previous primary becomes secondary, new key becomes active primary."""
        self.secondary_key = self.primary_key
        self.primary_key = new_key
        self.version += 1


class SecretRotationEngine:
    """Utility for generating cryptographically secure secrets and managing rotation lifecycles."""

    @classmethod
    def generate_secure_secret(cls, prefix: str = "sec", length: int = 40) -> str:
        """Generates a high-entropy URL-safe cryptographic secret string."""
        raw = secrets.token_urlsafe(length)
        return f"{prefix}-{raw}"

