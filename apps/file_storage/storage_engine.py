"""
apps.file_storage.storage_engine
Architecture Decision: ADR-0051
Private Storage Engine and Cryptographic Signed Expiring URL Generation.
Enforces private-by-default isolation, tenant directory sharding, and HMAC-SHA256 expiring tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from apps.core.errors import PermissionDeniedError, StorageUrlExpiredError


class PrivateStorageEngine:
    """
    Manages private object storage filesystem/provider and generates
    tamper-proof, cryptographically signed expiring download URLs.
    """

    SALT = "private-storage-v2"
    DEFAULT_TTL_SECONDS = 900  # 15 minutes
    MIN_TTL_SECONDS = 60  # 1 minute
    MAX_TTL_SECONDS = 3600  # 1 hour

    @classmethod
    def get_storage_root(cls) -> Path:
        """Returns the isolated private root directory."""
        configured = getattr(settings, "SECURE_STORAGE_ROOT", None)
        if configured:
            root = Path(configured)
        else:
            root = Path(settings.BASE_DIR) / "var" / "secure_storage"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def store_file(
        cls,
        tenant_id: str,
        category: str,
        file_id: str,
        filename: str,
        content: bytes,
    ) -> str:
        """
        Persists binary content into private tenant-partitioned storage directory.
        Path pattern: {storage_root}/{tenant_id}/{category}/{year}/{month}/{file_id}{extension}
        """
        root = cls.get_storage_root()
        now = datetime.now(timezone.utc)
        year_str = now.strftime("%Y")
        month_str = now.strftime("%m")

        ext = Path(filename).suffix.lower()
        if not ext:
            ext = ".bin"

        dest_dir = root / tenant_id / category / year_str / month_str
        dest_dir.mkdir(parents=True, exist_ok=True)

        stored_filename = f"{file_id}{ext}"
        target_path = dest_dir / stored_filename

        with open(target_path, "wb") as f:
            f.write(content)

        # Store relative storage key
        rel_path = f"{tenant_id}/{category}/{year_str}/{month_str}/{stored_filename}"
        return rel_path

    @classmethod
    def read_file(cls, storage_path: str) -> bytes:
        """Reads content from private storage."""
        root = cls.get_storage_root()
        target = root / storage_path
        if not target.exists():
            raise FileNotFoundError(f"Stored file not found at {storage_path}")
        with open(target, "rb") as f:
            return f.read()

    @classmethod
    def get_absolute_path(cls, storage_path: str) -> Path:
        """Returns absolute path on disk for streaming responses."""
        return cls.get_storage_root() / storage_path

    @classmethod
    def delete_file(cls, storage_path: str) -> bool:
        """Safely removes physical file from storage."""
        root = cls.get_storage_root()
        target = root / storage_path
        if target.exists():
            target.unlink()
            return True
        return False

    @classmethod
    def _get_signing_key(cls) -> bytes:
        """Derives a dedicated signing key using SHA-256 HKDF-like construction."""
        secret = getattr(settings, "SECRET_KEY", "pharmacy-default-secret-key-storage-v2")
        return hashlib.sha256(f"{secret}:{cls.SALT}".encode("utf-8")).digest()

    @classmethod
    def generate_signed_token(
        cls,
        file_id: str,
        tenant_id: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> tuple[str, datetime]:
        """
        Generates a tamper-proof cryptographic token containing expiry timestamp.
        Token format: {expires_timestamp}:{hmac_signature}
        """
        ttl = max(cls.MIN_TTL_SECONDS, min(ttl_seconds, cls.MAX_TTL_SECONDS))
        expires_timestamp = int(time.time()) + ttl
        expires_at = datetime.fromtimestamp(expires_timestamp, tz=timezone.utc)

        key = cls._get_signing_key()
        payload = f"{file_id}:{tenant_id}:{expires_timestamp}".encode("utf-8")
        signature = hmac.new(key, payload, hashlib.sha256).hexdigest()

        token = f"{expires_timestamp}:{signature}"
        return token, expires_at

    @classmethod
    def verify_signed_token(
        cls,
        file_id: str,
        tenant_id: str,
        token: str,
    ) -> bool:
        """
        Verifies validity, authenticity, and freshness of signed expiring token.
        Raises StorageUrlExpiredError if timestamp has expired.
        Raises PermissionDeniedError if HMAC signature is tampered or invalid.
        """
        if not token or ":" not in token:
            raise PermissionDeniedError(
                message_en="Invalid signed storage token format.",
                message_bn="ফাইল ডাউনলোডের নিরাপত্তা টোকেনটি সঠিক নয়।",
            )

        parts = token.split(":", 1)
        try:
            expires_timestamp = int(parts[0])
            provided_signature = parts[1]
        except (ValueError, IndexError):
            raise PermissionDeniedError(
                message_en="Malformed signed storage token.",
                message_bn="স্বাক্ষরিত স্টোরেজ টোকেন বিকৃত হয়েছে।",
            )

        current_timestamp = int(time.time())
        if current_timestamp > expires_timestamp:
            raise StorageUrlExpiredError()

        key = cls._get_signing_key()
        payload = f"{file_id}:{tenant_id}:{expires_timestamp}".encode("utf-8")
        expected_signature = hmac.new(key, payload, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(provided_signature, expected_signature):
            raise PermissionDeniedError(
                message_en="Cryptographic signature verification failed for storage token.",
                message_bn="স্টোরেজ টোকেনের ক্রিপ্টোগ্রাফিক স্বাক্ষর যাচাইকরণ ব্যর্থ হয়েছে।",
            )

        return True
