"""
apps.release_management.urls
Architecture Decision: ADR-0038
URL Routing for Release Manifests and Client Version Compatibility APIs.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.release_management.views import ReleaseManifestViewSet

router = DefaultRouter()
router.register("releases", ReleaseManifestViewSet, basename="release")

urlpatterns = router.urls
