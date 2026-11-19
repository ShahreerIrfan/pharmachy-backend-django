"""
backend/apps/core/sync_backpressure_views.py
Architecture Decision: ADR-0317 (P317).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

DRF ViewSet for Sync Backpressure, Adaptive Batching & Retry Backoff:
- Provides policy limits (payload caps, batch size limits).
- Checks backpressure and throttle status before batch execution.
- Records sync outcomes and computes exponential backoff.
- Evaluates outbox progress with POS non-starvation heuristics.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import SyncBackpressurePolicy, Tenant
from apps.core.sync_backpressure_service import SyncBackpressureService

logger = logging.getLogger(__name__)


class SyncBackpressureViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant(self, request, tenant_id_param: str = None) -> Tenant:
        user = request.user
        tenant_id = (
            tenant_id_param
            or request.data.get("tenant_id")
            or request.query_params.get("tenant_id")
            or getattr(user, "tenant_id", None)
            or getattr(getattr(user, "tenant", None), "tenant_id", None)
        )
        if not tenant_id:
            tenant_id = "ten-default"
        return SyncBackpressureService.assert_tenant_active(str(tenant_id))

    @action(detail=False, methods=["get"], url_path="policy")
    def get_policy(self, request):
        tenant = self._resolve_tenant(request)
        device_id = request.query_params.get("device_id")
        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id query param is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        policy = SyncBackpressureService.get_or_create_policy(tenant=tenant, device_id=device_id)
        return Response(
            {
                "policy_id": policy.policy_id,
                "device_id": device_id,
                "max_batch_size": policy.max_batch_size,
                "max_payload_bytes": policy.max_payload_bytes,
                "retry_base_delay_ms": policy.retry_base_delay_ms,
                "retry_max_delay_ms": policy.retry_max_delay_ms,
                "retry_max_attempts": policy.retry_max_attempts,
                "is_throttled": policy.is_throttled,
                "throttle_reason": policy.throttle_reason,
                "retry_after_seconds": policy.retry_after_seconds,
                "consecutive_failures": policy.consecutive_failures,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_batch(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        event_count = int(request.data.get("event_count") or 0)
        payload_bytes = int(request.data.get("payload_bytes") or 0)

        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = SyncBackpressureService.validate_batch_and_check_backpressure(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            event_count=event_count,
            payload_bytes=payload_bytes,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="outcome")
    def record_outcome(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        is_success = bool(request.data.get("is_success"))
        failure_reason = str(request.data.get("failure_reason") or "").strip()

        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = SyncBackpressureService.record_sync_outcome(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            is_success=is_success,
            failure_reason=failure_reason,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="progress")
    def evaluate_progress(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        total_enqueued = int(request.data.get("total_enqueued") or 0)
        processed_count = int(request.data.get("processed_count") or 0)
        failed_count = int(request.data.get("failed_count") or 0)
        is_pos_checkout_active = bool(request.data.get("is_pos_checkout_active"))

        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = SyncBackpressureService.evaluate_outbox_progress(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            total_enqueued=total_enqueued,
            processed_count=processed_count,
            failed_count=failed_count,
            is_pos_checkout_active=is_pos_checkout_active,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="configure")
    def configure_policy(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = SyncBackpressureService.configure_policy(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            max_batch_size=request.data.get("max_batch_size"),
            max_payload_bytes=request.data.get("max_payload_bytes"),
            retry_base_delay_ms=request.data.get("retry_base_delay_ms"),
            retry_max_delay_ms=request.data.get("retry_max_delay_ms"),
            retry_max_attempts=request.data.get("retry_max_attempts"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="reset")
    def reset_throttle(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = SyncBackpressureService.reset_throttle(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
