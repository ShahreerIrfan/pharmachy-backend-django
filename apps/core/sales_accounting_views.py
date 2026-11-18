"""
backend/apps/core/sales_accounting_views.py
Architecture Decision: ADR-0283 (P283).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing General Ledger Sales Accounting Postings & Profitability:
- Post atomic revenue settlement, AR, VAT, discount, and lot COGS for sales.
- Retrieve postings, detail, and profitability summary.
- Audited reversal of sales accounting postings.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.sales_accounting_service import SalesAccountingService

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
    if hasattr(request, "data") and isinstance(request.data, dict) and request.data.get("tenant_id"):
        return str(request.data.get("tenant_id"))
    return ""


def _check_tenant_suspended(request, tenant_id: str):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Sales accounting operations blocked.",
                "message_en": "Tenant account subscription is suspended. Sales accounting operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। সেলস অ্যাকাউন্টিং অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class SalesAccountingViewSet(viewsets.ViewSet):
    """
    ViewSet governing General Ledger sales accounting postings and profitability.
    Endpoints:
    - GET  /api/v2/finance/sales-postings/
    - POST /api/v2/finance/sales-postings/
    - GET  /api/v2/finance/sales-postings/<posting_id>/
    - POST /api/v2/finance/sales-postings/<posting_id>/reverse/
    - GET  /api/v2/finance/sales-postings/summary/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        status_param = request.query_params.get("status")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        invoice_number = request.query_params.get("invoice_number")

        postings = SalesAccountingService.list_sales_postings(
            tenant_id=tenant_id,
            branch_id=branch_id,
            status_param=status_param,
            start_date=start_date,
            end_date=end_date,
            invoice_number=invoice_number,
            actor_user=request.user,
        )
        return Response({"count": len(postings), "postings": postings}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        posting = SalesAccountingService.get_sales_posting(
            tenant_id=tenant_id,
            posting_id=pk,
            actor_user=request.user,
        )
        return Response(posting, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        invoice_id = request.data.get("invoice_id")
        sale_id = request.data.get("sale_id")
        idempotency_key = request.data.get("idempotency_key", "")

        if invoice_id:
            result = SalesAccountingService.post_sale_invoice_accounting(
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                idempotency_key=idempotency_key,
                actor_user=request.user,
            )
        elif sale_id:
            result = SalesAccountingService.post_pos_sale_order_accounting(
                tenant_id=tenant_id,
                sale_id=sale_id,
                idempotency_key=idempotency_key,
                actor_user=request.user,
            )
        else:
            raise ValidationError(
                {"error_code": "ERR_MISSING_SALE_REFERENCE", "detail": "Either invoice_id or sale_id is required."}
            )

        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reason = request.data.get("reason", "")
        result = SalesAccountingService.reverse_sale_accounting(
            tenant_id=tenant_id,
            posting_id=pk,
            reason=reason,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        summary_data = SalesAccountingService.get_sales_profitability_summary(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )
        return Response(summary_data, status=status.HTTP_200_OK)
