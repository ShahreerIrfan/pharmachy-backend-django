"""
backend/apps/core/recurring_expense_views.py
Architecture Decision: ADR-0288 (P288).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing Recurring Operating Expense Templates & Schedules:
- CRUD for recurring rent, utilities, internet broadband, maintenance, and administrative templates.
- Scheduled and on-demand expense generation with optional auto-approval into General Ledger.
- Multi-branch scoping and strict multi-tenant isolation.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.recurring_expense_service import RecurringExpenseService

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
                "detail": "Tenant account subscription is suspended. Recurring expense operations blocked.",
                "message_en": "Tenant account subscription is suspended. Recurring expense operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। রিকারিং খরচ অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class RecurringExpenseViewSet(viewsets.ViewSet):
    """
    ViewSet governing Recurring Expense Templates and generation engine.
    Endpoints:
    - GET    /api/v2/finance/recurring-expenses/
    - POST   /api/v2/finance/recurring-expenses/
    - GET    /api/v2/finance/recurring-expenses/<template_id>/
    - PATCH  /api/v2/finance/recurring-expenses/<template_id>/
    - DELETE /api/v2/finance/recurring-expenses/<template_id>/
    - POST   /api/v2/finance/recurring-expenses/generate/
    - POST   /api/v2/finance/recurring-expenses/<template_id>/run/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        is_active_raw = request.query_params.get("is_active")
        is_active = None
        if is_active_raw is not None:
            is_active = str(is_active_raw).lower() in ("true", "1", "yes")

        branch_id = request.query_params.get("branch_id")
        category_id = request.query_params.get("category_id")

        templates = RecurringExpenseService.list_recurring_templates(
            tenant_id=tenant_id,
            is_active=is_active,
            branch_id=branch_id,
            category_id=category_id,
            actor_user=request.user,
        )
        return Response({"count": len(templates), "templates": templates}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        template = RecurringExpenseService.get_recurring_template(
            tenant_id=tenant_id,
            template_id=pk,
            actor_user=request.user,
        )
        return Response(template, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        template = RecurringExpenseService.create_recurring_template(
            tenant_id=tenant_id,
            data=request.data,
            actor_user=request.user,
        )
        return Response(template, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        template = RecurringExpenseService.update_recurring_template(
            tenant_id=tenant_id,
            template_id=pk,
            data=request.data,
            actor_user=request.user,
        )
        return Response(template, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        result = RecurringExpenseService.archive_recurring_template(
            tenant_id=tenant_id,
            template_id=pk,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Recurring expense template archived successfully.",
                "message_bn": "রিকারিং খরচ টেমপ্লেট সফলভাবে আর্কাইভ করা হয়েছে।",
                "template": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="generate")
    def generate(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        as_of_date = None
        if request.data.get("as_of_date"):
            try:
                as_of_date = datetime.date.fromisoformat(str(request.data["as_of_date"]))
            except Exception:
                pass

        result = RecurringExpenseService.generate_due_expenses(
            tenant_id=tenant_id,
            as_of_date=as_of_date,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="run")
    def run_template(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        # Force run for single template
        template = RecurringExpenseService.get_recurring_template(
            tenant_id=tenant_id,
            template_id=pk,
            actor_user=request.user,
        )
        # Use template's next_run_date as cutoff to ensure it generates
        cutoff = datetime.date.fromisoformat(template["next_run_date"])
        result = RecurringExpenseService.generate_due_expenses(
            tenant_id=tenant_id,
            as_of_date=cutoff,
            template_id=pk,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)
