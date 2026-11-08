"""
backend/apps/core/supplier_invoice_views.py
Architecture Decision: ADR-0190 (P190).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing 3-Way Supplier Invoice Matching:
- Reconciles Purchase Orders (PO) <-> Goods Receipt Notes (GRN) <-> Supplier Invoices.
- Runs automated variance matching against price and quantity tolerances.
- Surfaces the Discrepancy Exception Queue.
- Enforces managerial override for approved exceptions and formal rejection workflows.
- Posts matched/approved invoices to the immutable Accounts Payable SupplierLedgerEntry.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.supplier_invoice_service import SupplierInvoiceService

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
    """Blocks suspended tenants from mutating or accessing supplier invoice resources."""
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Supplier invoice processing locked.",
            }
        )


class SupplierInvoiceViewSet(viewsets.ViewSet):
    """
    REST API ViewSet governing Supplier Invoices and 3-Way Matching Engine.
    Routes:
      - GET    /api/v2/procurement/supplier-invoices/
      - POST   /api/v2/procurement/supplier-invoices/
      - GET    /api/v2/procurement/supplier-invoices/{pk}/
      - POST   /api/v2/procurement/supplier-invoices/{pk}/run-match/
      - POST   /api/v2/procurement/supplier-invoices/{pk}/approve-exception/
      - POST   /api/v2/procurement/supplier-invoices/{pk}/reject-invoice/
      - POST   /api/v2/procurement/supplier-invoices/{pk}/post-to-ap/
      - GET    /api/v2/procurement/supplier-invoices/exception-queue/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/supplier-invoices/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.query_params.get("supplier_id")
        matching_status = request.query_params.get("status")
        branch_id = request.query_params.get("branch_id") or _get_request_branch_id(request)

        try:
            invoices = SupplierInvoiceService.list_supplier_invoices(
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                matching_status=matching_status,
                branch_id=branch_id,
            )
            return Response({"count": len(invoices), "results": invoices}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/supplier-invoices/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            invoice = SupplierInvoiceService.get_supplier_invoice(
                tenant_id=tenant_id,
                invoice_id=pk,
            )
            return Response(invoice, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/supplier-invoices/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            invoice = SupplierInvoiceService.create_supplier_invoice(
                tenant_id=tenant_id,
                actor_user=request.user,
                data=request.data,
            )
            return Response(invoice, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="run-match")
    def run_match(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/supplier-invoices/{pk}/run-match/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            matched = SupplierInvoiceService.execute_three_way_match(
                tenant_id=tenant_id,
                actor_user=request.user,
                invoice_id=pk,
            )
            return Response(matched, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="approve-exception")
    def approve_exception(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/supplier-invoices/{pk}/approve-exception/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        justification = request.data.get("justification", "")
        notes = request.data.get("notes", "")
        try:
            approved = SupplierInvoiceService.approve_exception(
                tenant_id=tenant_id,
                actor_user=request.user,
                invoice_id=pk,
                justification=justification,
                notes=notes,
            )
            return Response(approved, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="reject-invoice")
    def reject_invoice(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/supplier-invoices/{pk}/reject-invoice/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        rejection_reason = request.data.get("rejection_reason", "")
        notes = request.data.get("notes", "")
        try:
            rejected = SupplierInvoiceService.reject_invoice(
                tenant_id=tenant_id,
                actor_user=request.user,
                invoice_id=pk,
                rejection_reason=rejection_reason,
                notes=notes,
            )
            return Response(rejected, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="post-to-ap")
    def post_to_ap(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/supplier-invoices/{pk}/post-to-ap/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            posted = SupplierInvoiceService.post_invoice_to_ap(
                tenant_id=tenant_id,
                actor_user=request.user,
                invoice_id=pk,
            )
            return Response(posted, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["get"], url_path="exception-queue")
    def exception_queue(self, request) -> Response:
        """GET /api/v2/procurement/supplier-invoices/exception-queue/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        try:
            queue = SupplierInvoiceService.get_exception_queue(
                tenant_id=tenant_id,
                branch_id=branch_id,
            )
            return Response({"count": len(queue), "results": queue}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

