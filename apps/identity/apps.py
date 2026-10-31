"""
apps.identity.apps
Architecture Decision: ADR-0042
"""

from __future__ import annotations

from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.identity"
    verbose_name = "Identity & Access Management (IAM)"
