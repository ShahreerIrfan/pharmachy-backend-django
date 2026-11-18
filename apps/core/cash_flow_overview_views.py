"""
backend/apps/core/cash_flow_overview_views.py
Architecture Decision: ADR-0294 (P294).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSets governing Financial Cash Flow Statements:
- GET /api/v2/finance/reports/cash-flow/ : Generate Cash Flow Statement & Accrual Reconciliation
- GET /api/v2/finance/reports/cash-flow/export/ : Export Cash Flow statement in CSV format
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from django.http import HttpResponse
from rest_framework import permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.cash_flow_overview_service import CashFlowOverviewService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger(__name__)


class CashFlowCsvExportRenderer(renderers.BaseRenderer):
    media_type = "text/csv"
    format = "csv"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, str):
            return data.encode("utf-8")
        return data


def _get_request_tenant_id(request) -> str:
    qp_tenant = getattr(request, "query_params", {}).get("tenant_id") if hasattr(request, "query_params") else request.GET.get("tenant_id")
    if qp_tenant:
        return str(qp_tenant)
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
                "detail": "Tenant account subscription is suspended. Cash flow reporting blocked.",
                "message_en": "Tenant account subscription is suspended. Cash flow reporting blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ক্যাশ ফ্লো রিপোর্ট দেখা সম্ভব নয়।",
            }
        )


class CashFlowOverviewViewSet(viewsets.ViewSet):
    """
    Dedicated ViewSet for Financial Cash Flow Statement reporting and accrual reconciliation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        report = CashFlowOverviewService.generate_cash_flow_overview(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )
        return Response(report, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="export", renderer_classes=[CashFlowCsvExportRenderer, renderers.JSONRenderer])
    def export(self, request) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        csv_content = CashFlowOverviewService.export_cash_flow_csv(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )

        response = HttpResponse(csv_content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="cash_flow_statement.csv"'
        return response
