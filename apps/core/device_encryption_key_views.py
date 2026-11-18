"""
backend/apps/core/device_encryption_key_views.py
Architecture Decision: ADR-0302 (P302).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing SQLite Encryption Key Lifecycle:
- GET    /api/v2/offline-sync/device-keys/              : List device encryption key metadata (never plaintext)
- POST   /api/v2/offline-sync/device-keys/              : Provision initial wrapped DEK + one-time recovery code
- POST   /api/v2/offline-sync/device-keys/{id}/rotate/  : Atomic key rotation (vN -> vN+1)
- POST   /api/v2/offline-sync/device-keys/{id}/recover/ : Audited recovery via one-time recovery code
- DELETE /api/v2/offline-sync/device-keys/{id}/         : Emergency compromise revocation (+ cascade device revoke)
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.device_encryption_key_service import DeviceEncryptionKeyService
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


class DeviceEncryptionKeyViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceEncryptionKeyService.list_keys(
            tenant_id=tenant_id,
            device_registration_id=request.query_params.get("device_registration_id"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceEncryptionKeyService.provision_initial_key(
            tenant_id=tenant_id, data=request.data or {}, actor_user=request.user
        )
        return Response(res, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        reason = str((request.data or {}).get("reason", "")) if isinstance(request.data, dict) else ""
        res = DeviceEncryptionKeyService.revoke_compromised_key(
            tenant_id=tenant_id, key_id=pk, reason=reason, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="rotate")
    def rotate(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceEncryptionKeyService.rotate_device_key(
            tenant_id=tenant_id, key_id=pk, data=request.data or {}, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="recover")
    def recover(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = DeviceEncryptionKeyService.recover_device_key(
            tenant_id=tenant_id,
            key_id=pk,
            recovery_code=payload.get("recovery_code", ""),
            reason=payload.get("reason", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
