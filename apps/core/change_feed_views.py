"""
backend/apps/core/change_feed_views.py
Architecture Decision: ADR-0307 (P307).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Incremental Change Feed Pull:
- GET  /api/v2/offline-sync/change-feed/            : List tenant/branch-scoped change feed events
- POST /api/v2/offline-sync/change-feed/pull/       : Device-bound incremental pull with cursor, limit, filters
- POST /api/v2/offline-sync/change-feed/publish/    : Authorized emit of UPSERT / TOMBSTONE / REVERSAL events
- POST /api/v2/offline-sync/change-feed/checkpoint/ : Monotonic durable device cursor checkpoint
- GET  /api/v2/offline-sync/change-feed/cursors/    : Device sync lag & cursor health monitoring
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.change_feed_service import ChangeFeedService
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


class ChangeFeedPullViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        since_raw = request.query_params.get("since_sequence", 0)
        res = ChangeFeedService.list_events(
            tenant_id=tenant_id,
            branch_id=request.query_params.get("branch_id"),
            entity_type=request.query_params.get("entity_type"),
            since_sequence=int(since_raw) if str(since_raw).isdigit() else 0,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="pull")
    def pull(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = ChangeFeedService.pull_change_feed(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            since_sequence=payload.get("since_sequence"),
            limit=payload.get("limit", 50),
            entity_types=payload.get("entity_types"),
            exclude_self_device=bool(payload.get("exclude_self_device", False)),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="publish")
    def publish(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = ChangeFeedService.publish_change_event(
            tenant_id=tenant_id,
            entity_type=payload.get("entity_type", ""),
            entity_id=payload.get("entity_id", ""),
            operation=payload.get("operation", "UPSERT"),
            payload_json=payload.get("payload_json", {}),
            branch_id=payload.get("branch_id"),
            entity_version=payload.get("entity_version", 1),
            monetary_impact_bdt=payload.get("monetary_impact_bdt", "0.00"),
            source_device_id=payload.get("source_device_id", ""),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        code = status.HTTP_200_OK if res.get("idempotent_replay") else status.HTTP_201_CREATED
        return Response(res, status=code)

    @action(detail=False, methods=["post"], url_path="checkpoint")
    def checkpoint(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = ChangeFeedService.checkpoint_cursor(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            acknowledged_sequence_no=payload.get("acknowledged_sequence_no"),
            cursor_token=payload.get("cursor_token", ""),
            applied_event_count=payload.get("applied_event_count", 0),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="cursors")
    def cursors(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = ChangeFeedService.list_device_cursors(
            tenant_id=tenant_id,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
