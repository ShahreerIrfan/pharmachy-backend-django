"""
backend/apps/core/purchase_requisition_views.py
Architecture Decision: ADR-0184 (P184).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing purchase requisitions, department budgets,
budget ceiling enforcement, and multi-tier approval state transitions.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.purchase_requisition_service import PurchaseRequisitionService

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
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। রিকুইজিশন মডিউল বন্ধ।",
            }
        )


class PurchaseRequisitionViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for department and branch purchase requisitions.
    Enforces role authorization, department budget caps, and approval workflows.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/requisitions/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        department = request.query_params.get("department")
        priority = request.query_params.get("priority")
        status_filter = request.query_params.get("status")
        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        try:
            results = PurchaseRequisitionService.list_requisitions(
                tenant_id=tenant_id,
                branch_id=branch_id,
                department=department,
                priority=priority,
                status=status_filter,
                is_active=is_active,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/requisitions/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            req_data = PurchaseRequisitionService.get_requisition(tenant_id=tenant_id, requisition_id=pk)
            return Response(req_data, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/requisitions/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        target_tenant = request.data.get("tenant_id") or tenant_id
        if target_tenant != tenant_id and not getattr(request.user, "is_superuser", False):
            return Response(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant purchase requisition creation forbidden.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        branch_id = request.data.get("branch_id") or _get_request_branch_id(request)
        try:
            created = PurchaseRequisitionService.create_requisition(
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
        """PATCH /api/v2/procurement/requisitions/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            updated = PurchaseRequisitionService.update_requisition(
                tenant_id=tenant_id,
                actor_user=request.user,
                requisition_id=pk,
                data=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/requisitions/{pk}/submit/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            submitted = PurchaseRequisitionService.submit_requisition(
                tenant_id=tenant_id,
                actor_user=request.user,
                requisition_id=pk,
                data=request.data,
            )
            return Response(submitted, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/requisitions/{pk}/approve/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            approved = PurchaseRequisitionService.approve_requisition(
                tenant_id=tenant_id,
                actor_user=request.user,
                requisition_id=pk,
                data=request.data,
            )
            return Response(approved, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/requisitions/{pk}/reject/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            rejected = PurchaseRequisitionService.reject_requisition(
                tenant_id=tenant_id,
                actor_user=request.user,
                requisition_id=pk,
                data=request.data,
            )
            return Response(rejected, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/requisitions/{pk}/cancel/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            cancelled = PurchaseRequisitionService.cancel_requisition(
                tenant_id=tenant_id,
                actor_user=request.user,
                requisition_id=pk,
                data=request.data,
            )
            return Response(cancelled, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

