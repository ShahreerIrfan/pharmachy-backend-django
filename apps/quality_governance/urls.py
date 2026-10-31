"""
apps.quality_governance.urls
Architecture Decision: ADR-0039
URL Routing for Quality Governance and Definition of Done APIs.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.quality_governance.views import (
    DefinitionOfDoneAuditViewSet,
    QualityIssueTicketViewSet,
)

router = DefaultRouter()
router.register("issues", QualityIssueTicketViewSet, basename="quality-issue")
router.register("dod-audits", DefinitionOfDoneAuditViewSet, basename="dod-audit")

urlpatterns = router.urls
