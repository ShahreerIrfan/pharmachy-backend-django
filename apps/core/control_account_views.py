"""
backend/apps/core/control_account_views.py
Architecture Decision: ADR-0285 (P285).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing General Ledger Control Accounts:
- Reconciles Accounts Receivable (1030) against detailed Customer Subledgers.
- Reconciles Accounts Payable (2010) against detailed Supplier Subledgers.
- Provides real-time control accounts vs subledgers live overview.
- Applies audited double-entry balancing adjustment journals.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.control_account_service import ControlAccountService
from apps.core.models import TenantLifecycleStatus

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
                "detail": "Tenant account subscription is suspended. Control account operations blocked.",
                "message_en": "Tenant account subscription is suspended. Control account operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। কন্ট্রোল অ্যাকাউন্ট অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class ControlAccountViewSet(viewsets.ViewSet):
    """
    ViewSet governing General Ledger control accounts and on-demand subledger reconciliations.
    Endpoints:
    - GET  /api/v2/finance/control-accounts/
    - POST /api/v2/finance/control-accounts/reconcile/
    - GET  /api/v2/finance/control-accounts/overview/
    - GET  /api/v2/finance/control-accounts/<run_id>/
    - POST /api/v2/finance/control-accounts/<run_id>/adjust/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        control_account_type = request.query_params.get("control_account_type")
        status_param = request.query_params.get("status")
        branch_id = request.query_params.get("branch_id")

        runs = ControlAccountService.list_reconciliation_runs(
            tenant_id=tenant_id,
            control_account_type=control_account_type,
            status_param=status_param,
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response({"count": len(runs), "runs": runs}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        run = ControlAccountService.get_reconciliation_run(
            tenant_id=tenant_id,
            run_id=pk,
            actor_user=request.user,
        )
        return Response(run, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="reconcile")
    def reconcile(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        control_account_type = request.data.get("control_account_type")
        if not control_account_type:
            raise ValidationError(
                {"error_code": "ERR_MISSING_CONTROL_ACCOUNT_TYPE", "detail": "control_account_type is required."}
            )

        branch_id = request.data.get("branch_id")

        result = ControlAccountService.reconcile_control_account(
            tenant_id=tenant_id,
            control_account_type=control_account_type,
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="overview")
    def overview(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")

        result = ControlAccountService.get_control_accounts_overview(
            tenant_id=tenant_id,
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="adjust")
    def adjust(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reason = request.data.get("reason", "")
        contra_code = request.data.get("contra_account_code", "3020")

        result = ControlAccountService.apply_reconciliation_adjustment(
            tenant_id=tenant_id,
            run_id=pk,
            adjustment_reason=reason,
            contra_account_code=contra_code,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)
