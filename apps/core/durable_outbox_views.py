"""
backend/apps/core/durable_outbox_views.py
Architecture Decision: ADR-0304 (P304).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Durable Outbox Ingestion & Conflict Queue:
- GET  /api/v2/offline-sync/outbox/                       : List outbox receipts / conflicts
- POST /api/v2/offline-sync/outbox/ingest/                : Ordered idempotent batch ingestion
- POST /api/v2/offline-sync/outbox/{id}/resolve-conflict/ : Manager audited conflict resolution
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.durable_outbox_service import DurableOutboxService
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
        raise PermissionDenied({"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."})


class DurableOutboxViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DurableOutboxService.list_receipts(
            tenant_id=tenant_id,
            sync_status=request.query_params.get("sync_status"),
            branch_id=request.query_params.get("branch_id"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="ingest")
    def ingest(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = DurableOutboxService.ingest_outbox_batch(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            commands=payload.get("commands", []),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="resolve-conflict")
    def resolve_conflict(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = DurableOutboxService.resolve_conflict(
            tenant_id=tenant_id,
            receipt_id=pk,
            resolution_action=payload.get("resolution_action", ""),
            notes=payload.get("notes", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
