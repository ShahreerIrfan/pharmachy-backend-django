"""
apps.build_pipeline.apps
Architecture Decision: ADR-0040
Config for Automated Build Smoke Pipeline modular bounded context.
"""

from __future__ import annotations

from django.apps import AppConfig


class BuildPipelineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.build_pipeline"
    verbose_name = "Automated Build Smoke Pipeline"
