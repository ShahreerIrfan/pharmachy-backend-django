"""
backend/apps/core/sync_cursor_checkpoint_views.py
Architecture Decision: ADR-0308 (P308).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Crash-Safe Sync Cursor Checkpoints:
- GET  /api/v2/offline-sync/cursor-checkpoints/                : List device watermarks & checkpoint ledger
- POST /api/v2/offline-sync/cursor-checkpoints/commit-pull/    : Advance pull cursor ONLY after durable local apply
- POST /api/v2/offline-sync/cursor-checkpoints/commit-push-ack/: Independently confirm push ack watermark + BDT
- POST /api/v2/offline-sync/cursor-checkpoints/recover/        : Crash-recovery state + unconfirmed push acks
- POST /api/v2/offline-sync/cursor-checkpoints/audited-reset/  : Owner/Manager audited pull rewind (epoch+1)
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.sync_cursor_checkpoint_service import SyncCursorCheckpointService


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


class SyncCursorCheckpointViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = SyncCursorCheckpointService.list_watermarks_and_ledger(
            tenant_id=tenant_id,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="commit-pull")
    def commit_pull(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = SyncCursorCheckpointService.commit_pull_checkpoint(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            acknowledged_sequence_no=payload.get("acknowledged_sequence_no"),
            durable_local_apply_verified=bool(payload.get("durable_local_apply_verified", False)),
            local_apply_digest_sha256=payload.get("local_apply_digest_sha256", ""),
            cursor_token=payload.get("cursor_token", ""),
            applied_event_count=int(payload.get("applied_event_count", 0) or 0),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="commit-push-ack")
    def commit_push_ack(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = SyncCursorCheckpointService.commit_push_ack_checkpoint(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            push_acked_sequence_no=payload.get("push_acked_sequence_no"),
            ack_ids=payload.get("ack_ids", []),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="recover")
    def recover(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = SyncCursorCheckpointService.recover_sync_state(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="audited-reset")
    def audited_reset(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = SyncCursorCheckpointService.audited_reset_cursor(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            target_pull_sequence_no=payload.get("target_pull_sequence_no"),
            reason=payload.get("reason", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
