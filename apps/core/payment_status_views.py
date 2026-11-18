"""
backend/apps/core/payment_status_views.py
Architecture Decision: ADR-0276 (P276).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet governing payment status lifecycle transitions, provider evidence, and reconciliation.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.payment_status_handling_service import PaymentStatusHandlingService

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
                "detail": "Tenant account subscription is suspended. Payment status operations blocked.",
                "message_en": "Tenant account subscription is suspended. Payment status operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পেমেন্ট অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class PaymentStatusViewSet(viewsets.ViewSet):
    """
    ViewSet governing payment lifecycle, provider verification evidence, and financial reconciliation.
    Endpoints:
    - GET   /api/v2/finance/payment-status/
    - POST  /api/v2/finance/payment-status/ (or /initiate/)
    - GET   /api/v2/finance/payment-status/{id}/
    - POST  /api/v2/finance/payment-status/{id}/confirm/
    - POST  /api/v2/finance/payment-status/{id}/fail/
    - POST  /api/v2/finance/payment-status/{id}/refund/
    - POST  /api/v2/finance/payment-status/{id}/reverse/
    - POST  /api/v2/finance/payment-status/{id}/reconcile/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        status_filter = request.query_params.get("status")
        tender_type = request.query_params.get("tender_type")
        is_reconciled_raw = request.query_params.get("is_reconciled")
        is_reconciled = None
        if is_reconciled_raw is not None:
            is_reconciled = is_reconciled_raw.lower() in ("true", "1")

        try:
            records = PaymentStatusHandlingService.list_payments(
                tenant_id=tenant_id,
                actor_user=request.user,
                branch_id=branch_id,
                status=status_filter,
                tender_type=tender_type,
                is_reconciled=is_reconciled,
            )
            return Response({"payments": records, "count": len(records)}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = PaymentStatusHandlingService.initiate_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
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

    def retrieve(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = PaymentStatusHandlingService.get_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                record_id=pk,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="initiate")
    def initiate(self, request) -> Response:
        return self.create(request)

    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = PaymentStatusHandlingService.confirm_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                record_id=pk,
                data=request.data,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="fail")
    def fail(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = PaymentStatusHandlingService.fail_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                record_id=pk,
                data=request.data,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="refund")
    def refund(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = PaymentStatusHandlingService.refund_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                record_id=pk,
                data=request.data,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = PaymentStatusHandlingService.reverse_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                record_id=pk,
                data=request.data,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="reconcile")
    def reconcile(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            result = PaymentStatusHandlingService.reconcile_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                record_id=pk,
                data=request.data,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
