"""
Django AppConfig for apps.audit_logging.
Architecture Decision: ADR-0035
"""

from __future__ import annotations

from django.apps import AppConfig


class AuditLoggingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit_logging"
    label = "audit_logging"
    verbose_name = "Audit Logging & Observability"
