"""
apps.quality_governance.apps
Architecture Decision: ADR-0039
"""

from __future__ import annotations

from django.apps import AppConfig


class QualityGovernanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.quality_governance"
    verbose_name = "Quality Governance & Definition of Done"
