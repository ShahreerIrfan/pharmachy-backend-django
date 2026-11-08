"""
backend/apps/core/purchase_order_views.py
Architecture Decision: ADR-0185 (P185).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing purchase orders, financial liability commitments,
guarded lifecycle state transitions, and delivery receipt tracking.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.purchase_order_service import PurchaseOrderService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    """Extracts tenant ID from authenticated user, request tenant, or header."""
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    return ""


def _get_request_branch_id(request) -> str:
    """Extracts branch ID from authenticated user or header."""
    user = getattr(request, "user", None)
    if user and getattr(user, "branch_id", None):
        return str(user.branch_id)
    header = request.META.get("HTTP_X_BRANCH_ID")
    if header:
        return str(header)
    return ""


def _check_tenant_suspended(request):
    """Blocks suspended tenants from mutating or accessing procurement resources."""
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Procurement access blocked.",
                "message_en": "Tenant account subscription is suspended. Procurement access blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ক্রয়াদেশ মডিউল বন্ধ।",
            }
        )


class PurchaseOrderViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for pharmaceutical purchase orders.
    Enforces role authorization, DGDA margin ceilings, guarded state machine,
    and goods delivery tracking.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/orders/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        supplier_id = request.query_params.get("supplier_id")
        status_filter = request.query_params.get("status")
        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        try:
            results = PurchaseOrderService.list_purchase_orders(
                tenant_id=tenant_id,
                branch_id=branch_id,
                supplier_id=supplier_id,
                status=status_filter,
                is_active=is_active,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/orders/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            po_data = PurchaseOrderService.get_purchase_order(tenant_id=tenant_id, po_id=pk)
            return Response(po_data, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/orders/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        target_tenant = request.data.get("tenant_id") or tenant_id
        if target_tenant != tenant_id and not getattr(request.user, "is_superuser", False):
            return Response(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant purchase order creation forbidden.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        branch_id = request.data.get("branch_id") or _get_request_branch_id(request)
        try:
            created = PurchaseOrderService.create_purchase_order(
                tenant_id=target_tenant,
                branch_id=branch_id,
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None) -> Response:
        """PATCH /api/v2/procurement/orders/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            updated = PurchaseOrderService.update_purchase_order(
                tenant_id=tenant_id,
                actor_user=request.user,
                po_id=pk,
                data=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/orders/{pk}/approve/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            approved = PurchaseOrderService.approve_purchase_order(
                tenant_id=tenant_id,
                actor_user=request.user,
                po_id=pk,
                data=request.data,
            )
            return Response(approved, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="order")
    def order(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/orders/{pk}/order/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            ordered = PurchaseOrderService.order_purchase_order(
                tenant_id=tenant_id,
                actor_user=request.user,
                po_id=pk,
                data=request.data,
            )
            return Response(ordered, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="receive")
    def receive(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/orders/{pk}/receive/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            received = PurchaseOrderService.receive_items(
                tenant_id=tenant_id,
                actor_user=request.user,
                po_id=pk,
                data=request.data,
            )
            return Response(received, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/orders/{pk}/close/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            closed = PurchaseOrderService.close_purchase_order(
                tenant_id=tenant_id,
                actor_user=request.user,
                po_id=pk,
                data=request.data,
            )
            return Response(closed, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/orders/{pk}/cancel/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            cancelled = PurchaseOrderService.cancel_purchase_order(
                tenant_id=tenant_id,
                actor_user=request.user,
                po_id=pk,
                data=request.data,
            )
            return Response(cancelled, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

