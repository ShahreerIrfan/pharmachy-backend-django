"""
backend/apps/core/connectivity_detection_views.py
Architecture Decision: ADR-0309 (P309).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Non-Blocking Connectivity Detection & Last-Sync Status:
- GET    /api/v2/offline-sync/connectivity/                  : List tenant/branch connectivity & last-sync statuses
- POST   /api/v2/offline-sync/connectivity/heartbeat/        : Record device probe & classify state
- POST   /api/v2/offline-sync/connectivity/report-transition/: Record queued offline->reconnected transition
- GET    /api/v2/offline-sync/connectivity/history/          : Append-only connectivity transition history
- PATCH  /api/v2/offline-sync/connectivity/<id>/             : Owner/Manager threshold configuration
- DELETE /api/v2/offline-sync/connectivity/<id>/             : Owner/Manager soft-archive snapshot
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.connectivity_detection_service import ConnectivityDetectionService
from apps.core.models import TenantLifecycleStatus


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
        raise PermissionDenied(
            {"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."}
        )


class ConnectivityDetectionViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        include_archived = str(request.query_params.get("include_archived", "")).lower() == "true"
        res = ConnectivityDetectionService.list_snapshots(
            tenant_id=tenant_id,
            include_archived=include_archived,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = ConnectivityDetectionService.update_or_archive_snapshot(
            tenant_id=tenant_id,
            snapshot_id=str(pk),
            degraded_latency_threshold_ms=payload.get("degraded_latency_threshold_ms"),
            archive=payload.get("is_archived"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = ConnectivityDetectionService.update_or_archive_snapshot(
            tenant_id=tenant_id,
            snapshot_id=str(pk),
            archive=True,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="heartbeat")
    def heartbeat(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = ConnectivityDetectionService.record_heartbeat(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            probe_succeeded=bool(payload.get("probe_succeeded", True)),
            latency_ms=payload.get("latency_ms", 0),
            packet_loss_pct=payload.get("packet_loss_pct", "0.00"),
            consecutive_failures=payload.get("consecutive_failures", 0),
            pending_outbox_count=payload.get("pending_outbox_count", 0),
            pending_outbox_bdt=payload.get("pending_outbox_bdt", "0.00"),
            reason=payload.get("reason", ""),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="report-transition")
    def report_transition(self, request) -> Response:
        return self.heartbeat(request)

    @action(detail=False, methods=["get"], url_path="history")
    def history(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = ConnectivityDetectionService.list_transition_history(
            tenant_id=tenant_id,
            device_id=request.query_params.get("device_id"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
