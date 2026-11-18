"""
backend/apps/core/payroll_views.py
Architecture Decision: ADR-0291 (P291).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSets governing Payroll Runs and Employee Salary Privacy Boundaries:
- GET    /api/v2/finance/payroll/runs/ : List Payroll Runs
- POST   /api/v2/finance/payroll/runs/ : Create Draft Payroll Run with confidential staff slips
- GET    /api/v2/finance/payroll/runs/{id}/ : Retrieve Run Details and Slips
- POST   /api/v2/finance/payroll/runs/{id}/approve-and-post/ : Approve & post to GL
- POST   /api/v2/finance/payroll/runs/{id}/cancel/ : Cancel Run
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.payroll_service import PayrollExpenseService

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
                "detail": "Tenant account subscription is suspended. Payroll operations blocked.",
                "message_en": "Tenant account subscription is suspended. Payroll operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পেরোল কার্যক্রম বন্ধ রাখা হয়েছে।",
            }
        )


class PayrollRunViewSet(viewsets.ViewSet):
    """
    Dedicated ViewSet for Payroll Execution, Confidential Salary Slips,
    and Balanced General Ledger Postings.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        run_status = request.query_params.get("status")

        runs = PayrollExpenseService.list_payroll_runs(
            tenant_id=tenant_id,
            branch_id=branch_id,
            status=run_status,
            actor_user=request.user,
        )
        return Response({"count": len(runs), "runs": runs}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        run = PayrollExpenseService.create_payroll_run(
            tenant_id=tenant_id,
            data=request.data,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Draft payroll run created with confidential employee slips.",
                "message_bn": "কর্মচারীদের বেতনের ড্রাফট পেরোল রান সফলভাবে প্রস্তুত করা হয়েছে।",
                "payroll_run": run,
            },
            status=status.HTTP_201_CREATED,
        )

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        run = PayrollExpenseService.get_payroll_run(
            tenant_id=tenant_id,
            run_id=pk,
            actor_user=request.user,
        )
        return Response({"payroll_run": run}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="approve-and-post")
    def approve_and_post(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        notes = request.data.get("notes", "") if isinstance(request.data, dict) else ""
        run = PayrollExpenseService.approve_and_post_payroll(
            tenant_id=tenant_id,
            run_id=pk,
            notes=notes,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Payroll run approved and posted to General Ledger (Salaries Expense 6030).",
                "message_bn": "পেরোল রান অনুমোদিত এবং জেনারেল লেজারে (বেতন ব্যয় ৬০৩০) পোস্ট করা হয়েছে।",
                "payroll_run": run,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reason = request.data.get("reason", "") if isinstance(request.data, dict) else ""
        run = PayrollExpenseService.cancel_payroll_run(
            tenant_id=tenant_id,
            run_id=pk,
            reason=reason,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Payroll run cancelled successfully.",
                "message_bn": "পেরোল রান বাতিল করা হয়েছে।",
                "payroll_run": run,
            },
            status=status.HTTP_200_OK,
        )
