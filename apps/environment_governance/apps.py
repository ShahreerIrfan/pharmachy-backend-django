"""
apps.environment_governance.apps
Architecture Decision: ADR-0041
"""

from __future__ import annotations

from django.apps import AppConfig


class EnvironmentGovernanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.environment_governance"
    verbose_name = "Environment Profile & Modular Settings Governance"
