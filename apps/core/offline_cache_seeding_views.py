"""
backend/apps/core/offline_cache_seeding_views.py
Architecture Decision: ADR-0303 (P303).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Versioned Offline Cache Seeding:
- GET  /api/v2/offline-sync/cache-seeds/                   : List issued/applied seed snapshots
- POST /api/v2/offline-sync/cache-seeds/bootstrap/         : Generate versioned snapshot + consistency token
- POST /api/v2/offline-sync/cache-seeds/{id}/acknowledge/  : Acknowledge atomic local application & update cursor
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.offline_cache_seeding_service import OfflineCacheSeedingService


def _get_request_tenant_id(request) -> str:
    qp_tenant = (
        getattr(request, "query_params", {}).get("tenant_id")
        if hasattr(request, "query_params")
        else request.GET.get("tenant_id")
    )
    if qp_tenant:
        return str(qp_tenant)
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    if hasattr(request, "data") and isinstance(request.data, dict) and request.data.get("tenant_id"):
        return str(request.data.get("tenant_id"))
    return ""


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied({"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."})


class OfflineCacheSeedingViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflineCacheSeedingService.list_seed_history(
            tenant_id=tenant_id,
            branch_id=request.query_params.get("branch_id"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="bootstrap")
    def bootstrap(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineCacheSeedingService.generate_bootstrap_seed(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            if_none_match_token=payload.get("if_none_match_token"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="acknowledge")
    def acknowledge(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineCacheSeedingService.acknowledge_seed_applied(
            tenant_id=tenant_id,
            seed_id=pk,
            consistency_token=payload.get("consistency_token", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
