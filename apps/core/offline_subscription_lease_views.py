"""
backend/apps/core/offline_subscription_lease_views.py
Architecture Decision: ADR-0312 (P312).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Signed Offline Subscription/Role Leases & Protected Recovery:
- GET  /api/v2/offline-sync/subscription-leases/                        : List device offline leases
- POST /api/v2/offline-sync/subscription-leases/issue/                  : Issue HMAC-SHA256 signed lease
- POST /api/v2/offline-sync/subscription-leases/verify-lease/           : Anti-clock-rollback & role verification
- POST /api/v2/offline-sync/subscription-leases/protected-recovery-sync/: Protected recovery even when expired/suspended
- POST /api/v2/offline-sync/subscription-leases/revoke/                 : Audited revocation + P307 tombstone
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.offline_subscription_lease_service import OfflineSubscriptionLeaseService


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


class OfflineSubscriptionLeaseViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflineSubscriptionLeaseService.list_leases(tenant_id=tenant_id, actor_user=request.user)
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="issue")
    def issue(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineSubscriptionLeaseService.issue_signed_lease(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            plan_tier=payload.get("plan_tier", "STANDARD"),
            allowed_roles=payload.get("allowed_roles"),
            entitlements=payload.get("entitlements"),
            lease_duration_hours=payload.get("lease_duration_hours", 72),
            grace_recovery_window_hours=payload.get("grace_recovery_window_hours", 168),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        code = status.HTTP_200_OK if res.get("idempotent_replay") else status.HTTP_201_CREATED
        return Response(res, status=code)

    @action(detail=False, methods=["post"], url_path="verify-lease")
    def verify_lease(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineSubscriptionLeaseService.verify_lease(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            lease_id=payload.get("lease_id", ""),
            lease_signature_hmac_sha256=payload.get("lease_signature_hmac_sha256", ""),
            client_reported_epoch_ms=payload.get("client_reported_epoch_ms"),
            requested_role=payload.get("requested_role", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="protected-recovery-sync")
    def protected_recovery_sync(self, request) -> Response:
        """
        Intentionally does NOT call _check_tenant_suspended(request):
        Protected recovery sync MUST stay reachable so lapsed/suspended tenants can safely recover queued offline data!
        """
        tenant_id = _get_request_tenant_id(request)
        payload = request.data or {}
        res = OfflineSubscriptionLeaseService.protected_recovery_sync(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            lease_id=payload.get("lease_id", ""),
            pending_outbox_count=payload.get("pending_outbox_count", 0),
            pending_outbox_bdt=payload.get("pending_outbox_bdt", "0.00"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="revoke")
    def revoke(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineSubscriptionLeaseService.revoke_lease(
            tenant_id=tenant_id,
            lease_id=payload.get("lease_id", ""),
            reason=payload.get("reason", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
