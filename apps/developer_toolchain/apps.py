"""
Django AppConfig for apps.developer_toolchain.
Architecture Decision: ADR-0036
"""

from __future__ import annotations

from django.apps import AppConfig


class DeveloperToolchainConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.developer_toolchain"
    label = "developer_toolchain"
    verbose_name = "Developer Toolchain & Quickstart Engine"
