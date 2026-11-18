"""
backend/apps/core/device_local_db_schema_views.py
Architecture Decision: ADR-0301 (P301).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Device Local SQLite Schema & Binding:
- GET    /api/v2/offline-sync/device-schemas/manifest/          : Fetch authoritative SQLite DDL + SHA-256
- GET    /api/v2/offline-sync/device-schemas/                   : List registered POS devices
- POST   /api/v2/offline-sync/device-schemas/                   : Register & bind device fingerprint
- PATCH  /api/v2/offline-sync/device-schemas/{id}/              : Update device offline policy / cursor
- DELETE /api/v2/offline-sync/device-schemas/{id}/              : Revoke device binding
- POST   /api/v2/offline-sync/device-schemas/validate-envelope/ : Validate pending offline command envelope
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.device_local_db_schema_service import DeviceLocalDbSchemaService
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


class DeviceLocalDbSchemaViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceLocalDbSchemaService.list_devices(
            tenant_id=tenant_id,
            branch_id=request.query_params.get("branch_id"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceLocalDbSchemaService.register_device(
            tenant_id=tenant_id, data=request.data or {}, actor_user=request.user
        )
        return Response(res, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceLocalDbSchemaService.update_device_policy(
            tenant_id=tenant_id, registration_id=pk, data=request.data or {}, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        reason = str((request.data or {}).get("reason", "")) if isinstance(request.data, dict) else ""
        res = DeviceLocalDbSchemaService.revoke_device(
            tenant_id=tenant_id, registration_id=pk, reason=reason, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="manifest")
    def manifest(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceLocalDbSchemaService.get_schema_manifest(tenant_id=tenant_id, actor_user=request.user)
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-envelope")
    def validate_envelope(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = DeviceLocalDbSchemaService.validate_command_envelope(
            tenant_id=tenant_id, envelope=request.data or {}, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)
