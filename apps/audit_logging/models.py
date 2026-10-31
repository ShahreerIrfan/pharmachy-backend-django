"""
apps.audit_logging.models
Architecture Decision: ADR-0035 & ADR-0050
Immutable Audit Trail Entity for High-Value Regulatory Actions in Bangladesh Pharmacy SaaS.
Enforces append-only semantics, actor/role attribution, before/after snapshots, and protected retention.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from django.db import models
from django.utils import timezone

from apps.audit_logging.redaction import SensitiveDataRedactor
from apps.core.errors import AuditImmutabilityError


class AuditActionStatus(models.TextChoices):
    SUCCESS = "SUCCESS", "Success"
    DENIED = "DENIED", "Denied"
    ERROR = "ERROR", "Error"


class AuditLogEntry(models.Model):
    """
    Immutable, append-only audit trail for dispensing, inventory adjustments,
    financial mutations, and security events conforming to ADR-0050.
    """

    entry_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    tenant_id = models.CharField(max_length=64, db_index=True)
    branch_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    actor_email = models.CharField(max_length=255, blank=True, default="")
    actor_role = models.CharField(max_length=64, blank=True, default="", db_index=True)
    device_id = models.CharField(max_length=64, null=True, blank=True)
    request_id = models.CharField(max_length=64, db_index=True)
    operation_id = models.CharField(max_length=64, null=True, blank=True)
    action = models.CharField(max_length=64, db_index=True)
    resource_type = models.CharField(max_length=64, db_index=True)
    resource_id = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=AuditActionStatus.choices,
        default=AuditActionStatus.SUCCESS,
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata_json = models.JSONField(default=dict, blank=True)

    # ADR-0050: Before / After Identifiers & State Snapshots
    before_state = models.JSONField(default=dict, blank=True, help_text="Redacted snapshot before state transition")
    after_state = models.JSONField(default=dict, blank=True, help_text="Redacted snapshot after state transition")

    # ADR-0050: Protected Retention Horizon (Minimum 730 days / 2 years per DGDA)
    retention_days = models.PositiveIntegerField(default=730, help_text="Mandatory retention duration in days")
    retention_until = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Protected date before which record cannot be purged",
    )
    is_immutable = models.BooleanField(default=True, editable=False)

    class Meta:
        verbose_name = "Audit Log Entry"
        verbose_name_plural = "Audit Log Entries"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["tenant_id", "-timestamp"]),
            models.Index(fields=["tenant_id", "action"]),
            models.Index(fields=["request_id"]),
            models.Index(fields=["tenant_id", "retention_until"]),
        ]

    def __str__(self) -> str:
        return f"[{self.timestamp}] {self.tenant_id} | {self.action} on {self.resource_type}:{self.resource_id} ({self.status})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Enforce append-only immutability: Updates to existing persisted records are strictly forbidden
        if not self._state.adding and AuditLogEntry.objects.filter(pk=self.pk).exists():
            raise AuditImmutabilityError(
                message_en="Audit log records are immutable and append-only. Updates are strictly forbidden.",
                message_bn="অডিট লগ রেকর্ড অপরিবর্তনযোগ্য। পরিবর্তন করা কঠোরভাবে নিষিদ্ধ।",
            )

        # Calculate retention horizon if not set
        if not self.retention_until:
            self.retention_until = timezone.now() + datetime.timedelta(days=self.retention_days)

        # Enforce zero-leakage redaction on all payloads before saving
        if self.before_state:
            self.before_state = SensitiveDataRedactor.redact(self.before_state)
        if self.after_state:
            self.after_state = SensitiveDataRedactor.redact(self.after_state)
        if self.metadata_json:
            self.metadata_json = SensitiveDataRedactor.redact(self.metadata_json)

        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """Enforces regulatory retention: prevents application-level deletion of audit entries."""
        raise AuditImmutabilityError(
            message_en="Audit log records are protected under regulatory retention policy. Deletion is strictly forbidden.",
            message_bn="নিয়ন্ত্রক রিটেনশন নীতির আওতায় অডিট লগ মুছে ফেলা কঠোরভাবে নিষিদ্ধ।",
        )
