"""
apps.environment_governance.urls
Architecture Decision: ADR-0041
Routing for Environment Profile Governance and Inspection endpoints.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.environment_governance.views import EnvironmentProfileViewSet

router = DefaultRouter()
router.register("profile", EnvironmentProfileViewSet, basename="environment-profile")

urlpatterns = router.urls
