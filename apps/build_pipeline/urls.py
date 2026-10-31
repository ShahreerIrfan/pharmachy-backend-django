"""
apps.build_pipeline.urls
Architecture Decision: ADR-0040
Routing for Automated Build Smoke Pipeline endpoints.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.build_pipeline.views import BuildSmokeViewSet

router = DefaultRouter()
router.register("smoke", BuildSmokeViewSet, basename="build-smoke")

urlpatterns = router.urls
