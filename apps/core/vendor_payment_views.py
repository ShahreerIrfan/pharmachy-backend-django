"""
backend/apps/core/vendor_payment_views.py
Architecture Decision: ADR-0273 (P273).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet governing supplier vendor payment disbursement and invoice allocation.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.vendor_payment_allocation_service import VendorPaymentAllocationService

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
                "detail": "Tenant account subscription is suspended. Vendor payment operations blocked.",
                "message_en": "Tenant account subscription is suspended. Vendor payment operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। সরবরাহকারী পেমেন্ট বরাদ্দ ব্লক করা হয়েছে।",
            }
        )


class VendorPaymentViewSet(viewsets.ViewSet):
    """
    ViewSet for managing vendor disbursements and invoice payment allocations.
    Endpoints:
    - GET  /api/v2/procurement/vendor-payments/
    - POST /api/v2/procurement/vendor-payments/
    - GET  /api/v2/procurement/vendor-payments/{id}/
    - POST /api/v2/procurement/vendor-payments/{id}/allocate-unapplied/
    - GET  /api/v2/procurement/vendor-payments/unpaid-invoices/?supplier_id=...
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.query_params.get("supplier_id")
        status_filter = request.query_params.get("status")

        try:
            payments = VendorPaymentAllocationService.list_vendor_payments(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=supplier_id,
                status=status_filter,
            )
            return Response({"payments": payments, "count": len(payments)}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.data.get("supplier_id")
        if not supplier_id:
            return Response(
                {
                    "error_code": "ERR_MISSING_SUPPLIER_ID",
                    "detail": "'supplier_id' is required to disburse and allocate a vendor payment.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            payment = VendorPaymentAllocationService.create_and_allocate_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=supplier_id,
                data=request.data,
            )
            return Response(payment, status=status.HTTP_201_CREATED)
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
            payment = VendorPaymentAllocationService.get_vendor_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                payment_id=pk,
            )
            return Response(payment, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="allocate-unapplied")
    def allocate_unapplied(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            updated = VendorPaymentAllocationService.allocate_unapplied_balance(
                tenant_id=tenant_id,
                actor_user=request.user,
                payment_id=pk,
                data=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["get"], url_path="unpaid-invoices")
    def unpaid_invoices(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.query_params.get("supplier_id")
        if not supplier_id:
            return Response(
                {
                    "error_code": "ERR_MISSING_SUPPLIER_ID",
                    "detail": "'supplier_id' query parameter is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        include_settled = str(request.query_params.get("include_settled", "false")).lower() in ("true", "1")

        try:
            invoices = VendorPaymentAllocationService.get_unpaid_invoices(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=supplier_id,
                include_settled=include_settled,
            )
            return Response({"supplier_id": supplier_id, "invoices": invoices, "count": len(invoices)}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
