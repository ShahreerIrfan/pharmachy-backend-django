"""
apps.secrets_vault.vault
Architecture Decision: ADR-0037
SecretStore Abstraction and Authenticated Symmetric Cryptography Engine.
"""

from __future__ import annotations

import base64
import hashlib
import os
from enum import Enum
from typing import Optional

from cryptography.fernet import Fernet


class SecretTier(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class SecretStore:
    """Provides authenticated symmetric encryption (Fernet / AES-128-CBC + HMAC-SHA256) for tenant secrets."""

    DEFAULT_MASTER_KEY_SALT = b"pharmacy-saas-secrets-vault-salt-2026"

    @classmethod
    def _derive_fernet_key(cls, master_key: Optional[str] = None) -> bytes:
        raw_key = master_key or os.environ.get("DJANGO_SECRET_KEY", "fallback-dev-vault-secret-key-32chars!!")
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            raw_key.encode("utf-8"),
            cls.DEFAULT_MASTER_KEY_SALT,
            iterations=100_000,
            dklen=32,
        )
        return base64.urlsafe_b64encode(derived)

    @classmethod
    def encrypt(cls, plaintext: str, master_key: Optional[str] = None) -> str:
        """Encrypts plaintext string using authenticated Fernet encryption."""
        if not plaintext:
            return ""
        fernet = Fernet(cls._derive_fernet_key(master_key))
        encrypted_bytes = fernet.encrypt(plaintext.encode("utf-8"))
        return encrypted_bytes.decode("utf-8")

    @classmethod
    def decrypt(cls, ciphertext: str, master_key: Optional[str] = None) -> str:
        """Decrypts ciphertext string back to plaintext."""
        if not ciphertext:
            return ""
        fernet = Fernet(cls._derive_fernet_key(master_key))
        decrypted_bytes = fernet.decrypt(ciphertext.encode("utf-8"))
        return decrypted_bytes.decode("utf-8")

    @classmethod
    def make_preview(cls, secret_value: str, prefix_len: int = 4, suffix_len: int = 4) -> str:
        """Generates a safe masked preview of a secret (e.g. 'sec-****-99a')."""
        if not secret_value:
            return ""
        if len(secret_value) <= prefix_len + suffix_len:
            return "*" * len(secret_value)
        return f"{secret_value[:prefix_len]}-****-{secret_value[-suffix_len:]}"
