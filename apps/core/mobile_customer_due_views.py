"""
backend/apps/core/mobile_customer_due_views.py
Architecture Decision: ADR-0277 (P277).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet for Mobile Owner Customer Due Review, Repayment Recording, and Digital Receipts.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.mobile_customer_due_collection_service import MobileCustomerDueCollectionService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
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


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Mobile due collection blocked.",
                "message_en": "Tenant account subscription is suspended. Mobile due collection blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। মোবাইল ডিউ কালেকশন বন্ধ করা হয়েছে।",
            }
        )


class MobileCustomerDueCollectionViewSet(viewsets.ViewSet):
    """
    Mobile ViewSet for authorized owners/managers to review customer dues and record collections.
    Endpoints:
    - GET  /api/v2/mobile/due-collections/
    - GET  /api/v2/mobile/due-collections/summary/
    - POST /api/v2/mobile/due-collections/collect/
    - GET  /api/v2/mobile/due-collections/{id}/receipt/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        search = request.query_params.get("search")
        is_overdue_raw = request.query_params.get("is_overdue")
        is_overdue = None
        if is_overdue_raw is not None:
            is_overdue = is_overdue_raw.lower() in ("true", "1")

        try:
            records = MobileCustomerDueCollectionService.list_outstanding_dues(
                actor_user=request.user,
                tenant_id=tenant_id,
                branch_id=branch_id,
                search=search,
                is_overdue=is_overdue,
            )
            return Response({"dues": records, "count": len(records)}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")

        try:
            summary_data = MobileCustomerDueCollectionService.get_due_summary(
                actor_user=request.user,
                tenant_id=tenant_id,
                branch_id=branch_id,
            )
            return Response(summary_data, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="collect")
    def collect(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = MobileCustomerDueCollectionService.record_mobile_due_collection(
                actor_user=request.user,
                tenant_id=tenant_id,
                data=request.data,
            )
            http_status = status.HTTP_200_OK if result.get("is_idempotent_replay") else status.HTTP_201_CREATED
            return Response(result, status=http_status)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["get"], url_path="receipt")
    def receipt(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            receipt_data = MobileCustomerDueCollectionService.get_digital_receipt(
                actor_user=request.user,
                tenant_id=tenant_id,
                collection_id=pk,
            )
            return Response(receipt_data, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
