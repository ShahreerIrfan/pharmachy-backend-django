"""
apps.audit_logging.models
Architecture Decision: ADR-0035
Immutable Audit Trail Entity for High-Value Regulatory Actions in Bangladesh Pharmacy SaaS.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import models

from apps.audit_logging.redaction import SensitiveDataRedactor


class AuditActionStatus(models.TextChoices):
    SUCCESS = "SUCCESS", "Success"
    DENIED = "DENIED", "Denied"
    ERROR = "ERROR", "Error"


class AuditLogEntry(models.Model):
    """Immutable audit trail for dispensing, inventory adjustments, and regulatory actions."""

    entry_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    tenant_id = models.CharField(max_length=64, db_index=True)
    branch_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    user_id = models.CharField(max_length=64, null=True, blank=True)
    device_id = models.CharField(max_length=64, null=True, blank=True)
    request_id = models.CharField(max_length=64, db_index=True)
    operation_id = models.CharField(max_length=64, null=True, blank=True)
    action = models.CharField(max_length=64)
    resource_type = models.CharField(max_length=64)
    resource_id = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=AuditActionStatus.choices,
        default=AuditActionStatus.SUCCESS,
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata_json = models.JSONField(default=dict)

    class Meta:
        verbose_name = "Audit Log Entry"
        verbose_name_plural = "Audit Log Entries"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["tenant_id", "-timestamp"]),
            models.Index(fields=["tenant_id", "action"]),
            models.Index(fields=["request_id"]),
        ]

    def __str__(self) -> str:
        return f"[{self.timestamp}] {self.tenant_id} | {self.action} on {self.resource_type}:{self.resource_id} ({self.status})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Enforce zero-leakage redaction on all metadata before saving
        if self.metadata_json:
            self.metadata_json = SensitiveDataRedactor.redact(self.metadata_json)
        super().save(*args, **kwargs)
