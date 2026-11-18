"""
backend/apps/core/expense_budget_views.py
Architecture Decision: ADR-0296 (P296).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing Operating Expense Budget Controls:
- GET    /api/v2/finance/expense-budgets/            : List active budgets with live actuals & utilization
- POST   /api/v2/finance/expense-budgets/            : Create planned budget (Owner / Finance Officer)
- GET    /api/v2/finance/expense-budgets/{id}/       : Retrieve budget detail
- PATCH  /api/v2/finance/expense-budgets/{id}/       : Update planned budget amount/thresholds/mode
- DELETE /api/v2/finance/expense-budgets/{id}/       : Audited soft-archive of budget
- POST   /api/v2/finance/expense-budgets/evaluate/   : Pre-flight budget threshold & enforcement check
- GET    /api/v2/finance/expense-budgets/alerts/     : Immutable budget threshold & override alert log
- GET    /api/v2/finance/expense-budgets/export/     : CSV variance report export (?format=csv)
"""

from __future__ import annotations

import logging

from django.http import HttpResponse
from rest_framework import permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.expense_budget_service import ExpenseBudgetService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger(__name__)


class ExpenseBudgetCsvExportRenderer(renderers.BaseRenderer):
    media_type = "text/csv"
    format = "csv"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, str):
            return data.encode("utf-8")
        return data


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
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
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
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Expense budget controls blocked.",
                "message_en": "Tenant account subscription is suspended. Expense budget controls blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। বাজেট নিয়ন্ত্রণ বন্ধ করা হয়েছে।",
            }
        )


class ExpenseBudgetViewSet(viewsets.ViewSet):
    """
    REST ViewSet for Operating Expense Budget Controls (P296).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        fy = request.query_params.get("fiscal_year")
        fm = request.query_params.get("fiscal_month")
        branch_id = request.query_params.get("branch_id")
        category_id = request.query_params.get("category_id")
        include_archived = str(request.query_params.get("include_archived", "false")).lower() in ("true", "1", "yes")

        result = ExpenseBudgetService.list_budgets(
            tenant_id=tenant_id,
            fiscal_year=int(fy) if fy else None,
            fiscal_month=int(fm) if fm else None,
            branch_id=branch_id,
            category_id=category_id,
            include_archived=include_archived,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        budget = ExpenseBudgetService.get_budget(
            tenant_id=tenant_id,
            budget_id=pk,
            actor_user=request.user,
        )
        return Response(budget, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        budget = ExpenseBudgetService.create_budget(
            tenant_id=tenant_id,
            data=request.data or {},
            actor_user=request.user,
        )
        return Response(budget, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        budget = ExpenseBudgetService.update_budget(
            tenant_id=tenant_id,
            budget_id=pk,
            data=request.data or {},
            actor_user=request.user,
        )
        return Response(budget, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        reason = ""
        if hasattr(request, "data") and isinstance(request.data, dict):
            reason = str(request.data.get("reason", ""))
        result = ExpenseBudgetService.archive_budget(
            tenant_id=tenant_id,
            budget_id=pk,
            reason=reason,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="evaluate")
    def evaluate(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        payload = request.data or {}
        result = ExpenseBudgetService.evaluate_proposed_expense(
            tenant_id=tenant_id,
            branch_id=payload.get("branch_id"),
            category_id=payload.get("category_id"),
            amount_bdt=payload.get("amount_bdt"),
            expense_date=payload.get("expense_date"),
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="alerts")
    def alerts(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        budget_id = request.query_params.get("budget_id")
        branch_id = request.query_params.get("branch_id")
        result = ExpenseBudgetService.list_budget_alerts(
            tenant_id=tenant_id,
            budget_id=budget_id,
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["get"],
        url_path="export",
        renderer_classes=[renderers.JSONRenderer, ExpenseBudgetCsvExportRenderer],
    )
    def export_csv(self, request) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)

        fy = request.query_params.get("fiscal_year")
        fm = request.query_params.get("fiscal_month")
        branch_id = request.query_params.get("branch_id")

        csv_body = ExpenseBudgetService.export_budgets_csv(
            tenant_id=tenant_id,
            fiscal_year=int(fy) if fy else None,
            fiscal_month=int(fm) if fm else None,
            branch_id=branch_id,
            actor_user=request.user,
        )
        response = HttpResponse(csv_body, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="expense_budget_variance.csv"'
        return response
