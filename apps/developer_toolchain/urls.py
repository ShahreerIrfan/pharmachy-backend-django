"""
apps.developer_toolchain.urls
Architecture Decision: ADR-0036
URL Configuration for Developer Quickstart API.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.developer_toolchain.views import DeveloperQuickstartViewSet

router = DefaultRouter()
router.register("developer/quickstart", DeveloperQuickstartViewSet, basename="developer-quickstart")

urlpatterns = router.urls
