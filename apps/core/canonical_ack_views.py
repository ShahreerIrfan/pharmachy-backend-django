"""
backend/apps/core/canonical_ack_views.py
Architecture Decision: ADR-0306 (P306).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Canonical Acknowledgments:
- GET  /api/v2/offline-sync/canonical-acks/                 : List canonical acknowledgements
- POST /api/v2/offline-sync/canonical-acks/commit/          : Transactionally commit & issue canonical acks
- POST /api/v2/offline-sync/canonical-acks/lookup/          : Crash-recovery lookup by idempotency_keys[]
- POST /api/v2/offline-sync/canonical-acks/confirm-applied/ : Confirm client persisted canonical IDs locally
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.canonical_ack_service import CanonicalAckService
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


class CanonicalAckViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = CanonicalAckService.list_acknowledgments(
            tenant_id=tenant_id,
            disposition=request.query_params.get("disposition"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="commit")
    def commit(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = CanonicalAckService.commit_and_acknowledge(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            commands=payload.get("commands", []),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="lookup")
    def lookup(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = CanonicalAckService.lookup_by_idempotency_keys(
            tenant_id=tenant_id,
            idempotency_keys=payload.get("idempotency_keys", []),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="confirm-applied")
    def confirm_applied(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = CanonicalAckService.confirm_client_applied(
            tenant_id=tenant_id,
            ack_ids=payload.get("ack_ids", []),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
