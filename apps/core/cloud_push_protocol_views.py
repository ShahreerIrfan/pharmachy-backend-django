"""
backend/apps/core/cloud_push_protocol_views.py
Architecture Decision: ADR-0305 (P305).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Signed HTTPS Cloud Sync Push Protocol:
- GET  /api/v2/offline-sync/push/               : List audited push batches
- POST /api/v2/offline-sync/push/execute/       : Execute signed, ordered, bounded push batch
- GET  /api/v2/offline-sync/push/protocol-spec/ : Authoritative push protocol limits & versions
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.cloud_push_protocol_service import CloudPushProtocolService
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


class CloudPushProtocolViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = CloudPushProtocolService.list_push_batches(
            tenant_id=tenant_id,
            branch_id=request.query_params.get("branch_id"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="execute")
    def execute(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = CloudPushProtocolService.execute_signed_push(
            tenant_id=tenant_id,
            data=request.data or {},
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="protocol-spec")
    def protocol_spec(self, request) -> Response:
        return Response(CloudPushProtocolService.get_protocol_spec(), status=status.HTTP_200_OK)
