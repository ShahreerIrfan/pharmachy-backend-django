"""
apps.data_privacy.apps
Architecture Decision: ADR-0052
"""

from __future__ import annotations

from django.apps import AppConfig


class DataPrivacyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.data_privacy"
    verbose_name = "Data Privacy Operations & Statutory Retention Subsystem"

