"""
apps.secrets_vault.urls
Architecture Decision: ADR-0037
URL Routing for Tenant Secret Vault API.
"""

from __future__ import annotations

from rest_framework.routers import DefaultRouter

from apps.secrets_vault.views import TenantSecretViewSet

router = DefaultRouter()
router.register("secrets", TenantSecretViewSet, basename="tenant-secret")

urlpatterns = router.urls

