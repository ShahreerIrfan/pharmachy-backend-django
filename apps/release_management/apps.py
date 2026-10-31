"""
apps.release_management.apps
Architecture Decision: ADR-0038
"""

from __future__ import annotations

from django.apps import AppConfig


class ReleaseManagementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.release_management"
    verbose_name = "Release Management & Version Compatibility"
