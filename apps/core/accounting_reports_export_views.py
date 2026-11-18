"""
backend/apps/core/accounting_reports_export_views.py
Architecture Decision: ADR-0298 (P298).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing Accounting Reports & Permission-Scoped Historical Snapshots:
- GET    /api/v2/finance/accounting-exports/                       : List historical snapshots
- POST   /api/v2/finance/accounting-exports/                       : Create & seal immutable historical snapshot
- GET    /api/v2/finance/accounting-exports/{id}/                  : Retrieve snapshot + SHA-256 integrity check
- DELETE /api/v2/finance/accounting-exports/{id}/                  : Soft-archive snapshot
- GET    /api/v2/finance/accounting-exports/{id}/download/?format=csv : Download exact historical snapshot CSV
- GET    /api/v2/finance/accounting-exports/live/?report_type=...  : Live JSON or CSV export
"""

from __future__ import annotations

from django.http import HttpResponse
from rest_framework import permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.accounting_reports_export_service import AccountingReportsExportService
from apps.core.models import TenantLifecycleStatus


class AccountingExportCsvRenderer(renderers.BaseRenderer):
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
                "detail": "Tenant subscription is suspended. Accounting exports blocked.",
            }
        )


class AccountingReportsExportViewSet(viewsets.ViewSet):
    """
    REST ViewSet for Accounting Reports, Historical Snapshots & CSV Exports (P298).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = AccountingReportsExportService.list_snapshots(
            tenant_id=tenant_id,
            report_type=request.query_params.get("report_type"),
            branch_id=request.query_params.get("branch_id"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = AccountingReportsExportService.get_snapshot(
            tenant_id=tenant_id, snapshot_id=pk, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = AccountingReportsExportService.create_snapshot(
            tenant_id=tenant_id,
            report_type=payload.get("report_type", ""),
            branch_id=payload.get("branch_id"),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
            title=payload.get("title", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = AccountingReportsExportService.archive_snapshot(
            tenant_id=tenant_id, snapshot_id=pk, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=["get"],
        url_path="download",
        renderer_classes=[renderers.JSONRenderer, AccountingExportCsvRenderer],
    )
    def download(self, request, pk=None) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        csv_body = AccountingReportsExportService.export_snapshot_csv(
            tenant_id=tenant_id, snapshot_id=pk, actor_user=request.user
        )
        response = HttpResponse(csv_body, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="snapshot_{pk}.csv"'
        return response

    @action(
        detail=False,
        methods=["get"],
        url_path="live",
        renderer_classes=[renderers.JSONRenderer, AccountingExportCsvRenderer],
    )
    def live(self, request):
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        report_type = request.query_params.get("report_type", "TRIAL_BALANCE")
        payload, dr, cr, bal, cnt, _, _, _ = AccountingReportsExportService.compile_live_report(
            tenant_id=tenant_id,
            report_type=report_type,
            branch_id=request.query_params.get("branch_id"),
            start_date=request.query_params.get("start_date"),
            end_date=request.query_params.get("end_date"),
            actor_user=request.user,
        )
        if str(request.query_params.get("format", "")).lower() == "csv":
            csv_body = AccountingReportsExportService.render_payload_csv(report_type.upper(), payload)
            response = HttpResponse(csv_body, content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{report_type.lower()}_live.csv"'
            return response
        return Response(
            {
                "report_type": report_type.upper(),
                "is_book_balanced": bal,
                "total_debits_bdt": str(dr),
                "total_credits_bdt": str(cr),
                "row_count": cnt,
                "payload": payload,
            },
            status=status.HTTP_200_OK,
        )
