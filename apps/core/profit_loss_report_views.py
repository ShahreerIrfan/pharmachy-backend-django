"""
backend/apps/core/profit_loss_report_views.py
Architecture Decision: ADR-0293 (P293).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSets governing Financial Profit & Loss Statements:
- GET /api/v2/finance/reports/profit-and-loss/ : Generate comprehensive P&L Statement
- GET /api/v2/finance/reports/profit-and-loss/export/ : Export P&L in CSV/text format
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from django.http import HttpResponse
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.profit_loss_report_service import ProfitLossReportService

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
                "detail": "Tenant account subscription is suspended. Financial reporting blocked.",
                "message_en": "Tenant account subscription is suspended. Financial reporting blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। লাভ-ক্ষতি রিপোর্ট দেখা সম্ভব নয়।",
            }
        )


class ProfitLossReportViewSet(viewsets.ViewSet):
    """
    Dedicated ViewSet for Financial Profit & Loss (Income Statement) reporting.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        report = ProfitLossReportService.generate_profit_and_loss_report(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )
        return Response(report, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        csv_content = ProfitLossReportService.export_profit_and_loss_csv(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )

        response = HttpResponse(csv_content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="profit_and_loss_report.csv"'
        return response

