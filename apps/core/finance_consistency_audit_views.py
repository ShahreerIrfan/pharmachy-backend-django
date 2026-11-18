"""
backend/apps/core/finance_consistency_audit_views.py
Architecture Decision: ADR-0300 (P300).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing E2E Finance Consistency Audit:
- GET /api/v2/finance/consistency-audit/                 : Run live 6-point E2E consistency audit
- GET /api/v2/finance/consistency-audit/export/?format=csv : Export audit checklist CSV
"""

from __future__ import annotations

from django.http import HttpResponse
from rest_framework import permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.finance_consistency_audit_service import FinanceConsistencyAuditService
from apps.core.models import TenantLifecycleStatus


class ConsistencyAuditCsvRenderer(renderers.BaseRenderer):
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
    return ""


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied({"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."})


class FinanceConsistencyAuditViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        report = FinanceConsistencyAuditService.run_consistency_audit(
            tenant_id=tenant_id,
            branch_id=request.query_params.get("branch_id"),
            start_date=request.query_params.get("start_date"),
            end_date=request.query_params.get("end_date"),
            actor_user=request.user,
        )
        return Response(report, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["get"],
        url_path="export",
        renderer_classes=[renderers.JSONRenderer, ConsistencyAuditCsvRenderer],
    )
    def export_csv(self, request) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        csv_body = FinanceConsistencyAuditService.export_audit_csv(
            tenant_id=tenant_id,
            branch_id=request.query_params.get("branch_id"),
            start_date=request.query_params.get("start_date"),
            end_date=request.query_params.get("end_date"),
            actor_user=request.user,
        )
        response = HttpResponse(csv_body, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="finance_e2e_consistency_audit.csv"'
        return response
