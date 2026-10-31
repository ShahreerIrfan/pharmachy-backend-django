"""
apps.identity.urls
Architecture Decision: ADR-0042
Routing for Custom PharmacyUser Identity and Authentication endpoints.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.identity.views import PharmacyUserViewSet

router = DefaultRouter()
router.register("users", PharmacyUserViewSet, basename="identity-user")

urlpatterns = router.urls
