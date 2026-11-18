"""
backend/apps/core/chart_of_accounts_views.py
Architecture Decision: ADR-0281 (P281).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing the General Ledger (GL) Chart of Accounts (COA):
- List, create, update, and archive GL accounts.
- Idempotent provisioning of standard 21 pharmacy GL accounts.
- Trial balance generation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.chart_of_accounts_service import ChartOfAccountsService
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
                "detail": "Tenant account subscription is suspended. Chart of Accounts operations blocked.",
                "message_en": "Tenant account subscription is suspended. Chart of Accounts operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। চার্ট অফ অ্যাকাউন্টস অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class ChartOfAccountsViewSet(viewsets.ViewSet):
    """
    ViewSet governing Chart of Accounts.
    Endpoints:
    - GET    /api/v2/finance/chart-of-accounts/
    - POST   /api/v2/finance/chart-of-accounts/
    - POST   /api/v2/finance/chart-of-accounts/initialize/
    - PATCH  /api/v2/finance/chart-of-accounts/<account_id>/
    - DELETE /api/v2/finance/chart-of-accounts/<account_id>/
    - GET    /api/v2/finance/chart-of-accounts/trial-balance/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        category = request.query_params.get("category")
        status_param = request.query_params.get("status")
        branch_id = request.query_params.get("branch_id")
        search = request.query_params.get("search")

        accounts = ChartOfAccountsService.list_chart_of_accounts(
            tenant_id=tenant_id,
            category=category,
            status=status_param,
            branch_id=branch_id,
            search=search,
            actor_user=request.user,
        )
        return Response({"count": len(accounts), "accounts": accounts}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        account_code = request.data.get("account_code")
        account_name = request.data.get("account_name")
        category = request.data.get("category")
        account_name_bn = request.data.get("account_name_bn", "")
        normal_balance = request.data.get("normal_balance")
        parent_account_id = request.data.get("parent_account_id")
        branch_id = request.data.get("branch_id")
        description = request.data.get("description", "")
        is_system_account = bool(request.data.get("is_system_account", False))

        result = ChartOfAccountsService.create_account(
            tenant_id=tenant_id,
            account_code=account_code,
            account_name=account_name,
            category=category,
            account_name_bn=account_name_bn,
            normal_balance=normal_balance,
            parent_account_id=parent_account_id,
            branch_id=branch_id,
            description=description,
            is_system_account=is_system_account,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="initialize")
    def initialize(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.data.get("branch_id")
        result = ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=tenant_id,
            actor_user=request.user,
            branch_id=branch_id,
        )
        return Response(result, status=status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        account_id = pk
        account_name = request.data.get("account_name")
        account_name_bn = request.data.get("account_name_bn")
        description = request.data.get("description")
        status_param = request.data.get("status")

        result = ChartOfAccountsService.update_account(
            tenant_id=tenant_id,
            account_id=account_id,
            account_name=account_name,
            account_name_bn=account_name_bn,
            description=description,
            status=status_param,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        account_id = pk
        result = ChartOfAccountsService.archive_or_delete_account(
            tenant_id=tenant_id,
            account_id=account_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="trial-balance")
    def trial_balance(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        result = ChartOfAccountsService.get_trial_balance_summary(
            tenant_id=tenant_id,
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)
