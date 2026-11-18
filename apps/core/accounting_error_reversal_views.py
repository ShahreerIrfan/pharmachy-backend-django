"""
backend/apps/core/accounting_error_reversal_views.py
Architecture Decision: ADR-0299 (P299).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing Accounting Error Reversal Requests:
- GET    /api/v2/finance/journal-reversals/                 : List reversal requests
- POST   /api/v2/finance/journal-reversals/                 : Submit reversal / reverse-and-rebook request
- DELETE /api/v2/finance/journal-reversals/{id}/            : Cancel pending request
- POST   /api/v2/finance/journal-reversals/{id}/approve/    : Approve & execute compensating reversing journal
- POST   /api/v2/finance/journal-reversals/{id}/reject/     : Reject pending request
- GET    /api/v2/finance/journal-reversals/export/?format=csv : CSV audit trail export
"""

from __future__ import annotations

from django.http import HttpResponse
from rest_framework import permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.accounting_error_reversal_service import AccountingErrorReversalService
from apps.core.models import TenantLifecycleStatus


class ReversalCsvRenderer(renderers.BaseRenderer):
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
    if hasattr(request, "data") and isinstance(request.data, dict) and request.data.get("tenant_id"):
        return str(request.data.get("tenant_id"))
    return ""


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied({"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription is suspended."})


class AccountingErrorReversalViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = AccountingErrorReversalService.list_reversal_requests(
            tenant_id=tenant_id,
            status_filter=request.query_params.get("status"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = AccountingErrorReversalService.create_reversal_request(
            tenant_id=tenant_id,
            original_journal_id=str(payload.get("original_journal_id", "")),
            error_category=payload.get("error_category", "WRONG_AMOUNT"),
            reason=payload.get("reason", ""),
            replacement_lines=payload.get("replacement_lines"),
            replacement_posting_date=payload.get("replacement_posting_date"),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        res = AccountingErrorReversalService.cancel_reversal_request(
            tenant_id=tenant_id, request_id=pk, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = AccountingErrorReversalService.approve_and_execute_reversal(
            tenant_id=tenant_id,
            request_id=pk,
            approval_notes=payload.get("approval_notes", ""),
            owner_self_approval_ack=bool(payload.get("owner_self_approval_ack", False)),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = AccountingErrorReversalService.reject_reversal_request(
            tenant_id=tenant_id,
            request_id=pk,
            reason=payload.get("reason", ""),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["get"],
        url_path="export",
        renderer_classes=[renderers.JSONRenderer, ReversalCsvRenderer],
    )
    def export_csv(self, request) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        csv_body = AccountingErrorReversalService.export_reversals_csv(tenant_id=tenant_id, actor_user=request.user)
        response = HttpResponse(csv_body, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="accounting_reversals_audit.csv"'
        return response
