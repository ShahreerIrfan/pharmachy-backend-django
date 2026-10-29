"""
apps.secrets_vault.models
Architecture Decision: ADR-0037
Tenant Secret Record Entity and Rotation Audit Log.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from apps.secrets_vault.vault import SecretStore


class SecretType(models.TextChoices):
    DGDA_PORTAL_TOKEN = "DGDA_PORTAL_TOKEN", "DGDA Compliance Portal Token"
    BKASH_MERCHANT_SECRET = "BKASH_MERCHANT_SECRET", "bKash Merchant Secret"
    NAGAD_API_KEY = "NAGAD_API_KEY", "Nagad Payment Gateway API Key"
    SMS_GATEWAY_KEY = "SMS_GATEWAY_KEY", "SMS Notification Gateway Key"
    CUSTOM = "CUSTOM", "Custom Integration Credential"


class TenantSecretRecord(models.Model):
    """Encrypted store for tenant-specific third-party API credentials."""

    secret_id = models.CharField(max_length=64, primary_key=True, default=uuid.uuid4)
    tenant_id = models.CharField(max_length=64, db_index=True)
    secret_type = models.CharField(max_length=64, choices=SecretType.choices, default=SecretType.CUSTOM)
    encrypted_value = models.TextField()
    key_preview = models.CharField(max_length=32)
    version = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_rotated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Tenant Secret Record"
        verbose_name_plural = "Tenant Secret Records"
        unique_together = ("tenant_id", "secret_type")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.tenant_id}] {self.secret_type} (v{self.version}, active={self.is_active})"

    def set_secret(self, plaintext: str) -> None:
        """Encrypts plaintext and generates masked preview."""
        self.encrypted_value = SecretStore.encrypt(plaintext)
        self.key_preview = SecretStore.make_preview(plaintext)

    def get_secret(self) -> str:
        """Decrypts and returns the raw plaintext secret."""
        return SecretStore.decrypt(self.encrypted_value)

    def rotate_secret(self, new_plaintext: str) -> int:
        """Rotates the secret to a new value, updates timestamp, and increments version."""
        self.set_secret(new_plaintext)
        self.version += 1
        self.last_rotated_at = timezone.now()
        self.save()
        return self.version


class SecretRotationAudit(models.Model):
    """Immutable audit trail for secret rotation events."""

    audit_id = models.BigAutoField(primary_key=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    tenant_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    secret_id = models.CharField(max_length=64)
    operator_role = models.CharField(max_length=64)
    old_version = models.IntegerField()
    new_version = models.IntegerField()
    status = models.CharField(max_length=20, default="SUCCESS")

    class Meta:
        verbose_name = "Secret Rotation Audit"
        verbose_name_plural = "Secret Rotation Audits"
        ordering = ["-timestamp"]
