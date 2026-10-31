"""
apps.file_storage.apps
Architecture Decision: ADR-0051
"""

from __future__ import annotations

from django.apps import AppConfig


class FileStorageConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.file_storage"
    verbose_name = "Private File Storage & Antivirus Policy Subsystem"
