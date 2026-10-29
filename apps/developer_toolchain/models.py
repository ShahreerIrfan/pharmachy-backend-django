"""
apps.developer_toolchain.models
Architecture Decision: ADR-0036
Audit Log Model for Developer Quickstart and Environment Bootstrap Invocations.
"""

from __future__ import annotations

from django.db import models


class DeveloperBootstrapAudit(models.Model):
    """Tracks developer quickstart and environment bootstrap runs for governance and auditability."""

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    operator_role = models.CharField(max_length=64, default="ADMIN")
    tenant_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    action = models.CharField(max_length=64, default="VERIFY_ALL")
    duration_ms = models.FloatField(default=0.0)
    status = models.CharField(max_length=20, default="SUCCESS")
    report_json = models.JSONField(default=dict)

    class Meta:
        verbose_name = "Developer Bootstrap Audit"
        verbose_name_plural = "Developer Bootstrap Audits"
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return f"[{self.timestamp}] Quickstart {self.action} by {self.operator_role} ({self.status})"
