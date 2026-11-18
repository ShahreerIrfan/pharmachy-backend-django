"""
backend/apps/core/offline_stock_allocation_views.py
Architecture Decision: ADR-0310 (P310).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

REST API ViewSet governing Offline Stock Allocation Policies & Oversell Disputes:
- GET    /api/v2/offline-sync/stock-allocation-policies/                      : List branch allocation policies
- POST   /api/v2/offline-sync/stock-allocation-policies/                      : Create/update branch policy
- PATCH  /api/v2/offline-sync/stock-allocation-policies/<id>/                 : Update branch policy
- DELETE /api/v2/offline-sync/stock-allocation-policies/<id>/                 : Archive policy
- POST   /api/v2/offline-sync/stock-allocation-policies/reconcile-reservation/: Reconcile offline device reservation
- GET    /api/v2/offline-sync/stock-allocation-policies/disputes/             : List cross-device oversell disputes
- POST   /api/v2/offline-sync/stock-allocation-policies/resolve-dispute/      : Explicit audited dispute resolution
"""

from __future__ import annotations

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import OfflineStockAllocationPolicy, TenantLifecycleStatus
from apps.core.offline_stock_allocation_service import OfflineStockAllocationService


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


class OfflineStockAllocationPolicyViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflineStockAllocationService.list_policies(tenant_id=tenant_id, actor_user=request.user)
        return Response(res, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineStockAllocationService.upsert_policy(
            tenant_id=tenant_id,
            branch_id=payload.get("branch_id", ""),
            policy_mode=payload.get("policy_mode", "CONTROLLED_RECONCILIATION"),
            max_offline_reserve_pct=payload.get("max_offline_reserve_pct", 80),
            allow_negative_stock_posting=bool(payload.get("allow_negative_stock_posting", False)),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_201_CREATED if res["created"] else status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        existing = OfflineStockAllocationPolicy.objects.filter(tenant_id=tenant_id, policy_id=str(pk)).first()
        payload = request.data or {}
        branch_id = payload.get("branch_id") or (str(existing.branch_id) if existing else "")
        res = OfflineStockAllocationService.upsert_policy(
            tenant_id=tenant_id,
            branch_id=branch_id,
            policy_mode=payload.get("policy_mode", existing.policy_mode if existing else "CONTROLLED_RECONCILIATION"),
            max_offline_reserve_pct=payload.get(
                "max_offline_reserve_pct", existing.max_offline_reserve_pct if existing else 80
            ),
            allow_negative_stock_posting=bool(payload.get("allow_negative_stock_posting", False)),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflineStockAllocationService.archive_policy(
            tenant_id=tenant_id,
            policy_id=str(pk),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="reconcile-reservation")
    def reconcile_reservation(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineStockAllocationService.reconcile_offline_reservation(
            tenant_id=tenant_id,
            device_id=payload.get("device_id", ""),
            device_fingerprint_sha256=payload.get("device_fingerprint_sha256", ""),
            batch_id=payload.get("batch_id", ""),
            reserved_base_units=payload.get("reserved_base_units"),
            local_invoice_ref=payload.get("local_invoice_ref", ""),
            idempotency_key=payload.get("idempotency_key", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="disputes")
    def disputes(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = OfflineStockAllocationService.list_disputes(
            tenant_id=tenant_id,
            status_filter=request.query_params.get("status"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="resolve-dispute")
    def resolve_dispute(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = OfflineStockAllocationService.resolve_dispute(
            tenant_id=tenant_id,
            dispute_id=payload.get("dispute_id", ""),
            resolution_status=payload.get("resolution_status", ""),
            resolution_reference_id=payload.get("resolution_reference_id", ""),
            resolution_notes=payload.get("resolution_notes", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
