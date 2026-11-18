"""
backend/apps/core/expense_category_views.py
Architecture Decision: ADR-0286 (P286).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing Owner-Managed Operating Expense Categories:
- Lists, retrieves, creates, updates and archives operating expense categories.
- Direct integration with General Ledger Chart of Accounts (6000-series).
- Action to seed standard default categories (Rent, Utilities, Salaries, Transport, Internet, Maintenance, Misc).
- Strict multi-tenant isolation and role enforcement.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.expense_category_service import ExpenseCategoryService
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
                "detail": "Tenant account subscription is suspended. Expense category operations blocked.",
                "message_en": "Tenant account subscription is suspended. Expense category operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। খরচ ক্যাটাগরি অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class ExpenseCategoryViewSet(viewsets.ViewSet):
    """
    ViewSet governing operating expense categories CRUD and seeding.
    Endpoints:
    - GET    /api/v2/finance/expense-categories/
    - POST   /api/v2/finance/expense-categories/
    - GET    /api/v2/finance/expense-categories/<category_id>/
    - PATCH  /api/v2/finance/expense-categories/<category_id>/
    - DELETE /api/v2/finance/expense-categories/<category_id>/
    - POST   /api/v2/finance/expense-categories/seed/
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

        categories = ExpenseCategoryService.list_expense_categories(
            tenant_id=tenant_id,
            is_active=is_active,
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response({"count": len(categories), "categories": categories}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        category = ExpenseCategoryService.get_expense_category(
            tenant_id=tenant_id,
            category_id=pk,
            actor_user=request.user,
        )
        return Response(category, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        category = ExpenseCategoryService.create_expense_category(
            tenant_id=tenant_id,
            data=request.data,
            actor_user=request.user,
        )
        return Response(category, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        category = ExpenseCategoryService.update_expense_category(
            tenant_id=tenant_id,
            category_id=pk,
            data=request.data,
            actor_user=request.user,
        )
        return Response(category, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        result = ExpenseCategoryService.archive_expense_category(
            tenant_id=tenant_id,
            category_id=pk,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Expense category archived successfully.",
                "message_bn": "খরচ ক্যাটাগরি সফলভাবে আর্কাইভ করা হয়েছে।",
                "category": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="seed")
    def seed(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        seeded = ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=tenant_id,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "count": len(seeded),
                "categories": seeded,
                "message_en": "Standard default operating expense categories initialized.",
                "message_bn": "স্ট্যান্ডার্ড অপারেটিং খরচ ক্যাটাগরি সফলভাবে প্রস্তুত করা হয়েছে।",
            },
            status=status.HTTP_201_CREATED,
        )
