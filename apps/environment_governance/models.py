"""
apps.environment_governance.models
Architecture Decision: ADR-0041
Data models for logging environment profile inspections, security posture, and database engine audits.
"""

from __future__ import annotations

import uuid

from django.db import models


class EnvironmentType(models.TextChoices):
    DEVELOPMENT = "development", "Development"
    TESTING = "testing", "Testing"
    PRODUCTION = "production", "Production"


class EnvironmentProfileAudit(models.Model):
    """
    Audited inspection record of active Django modular settings profile and transport hardening.
    """

    audit_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=64, db_index=True, default="tenant-default")
    environment = models.CharField(max_length=20, choices=EnvironmentType.choices, db_index=True)
    debug = models.BooleanField(default=False)
    database_engine = models.CharField(max_length=32, default="sqlite3")
    database_name = models.CharField(max_length=255, default="")
    is_hardened = models.BooleanField(default=False)
    ssl_redirect = models.BooleanField(default=False)
    session_cookie_secure = models.BooleanField(default=False)
    csrf_cookie_secure = models.BooleanField(default=False)
    secrets_validated = models.BooleanField(default=False)
    inspected_by = models.CharField(max_length=100, default="SYSTEM")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "environment_profile_audits"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant_id", "environment"]),
        ]

    def __str__(self) -> str:
        return f"EnvAudit-{self.audit_id} [{self.environment}] by {self.inspected_by}"
