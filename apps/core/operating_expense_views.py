"""
backend/apps/core/operating_expense_views.py
Architecture Decision: ADR-0287 (P287) & ADR-0289 (P289).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing Operating Expense Entry Vouchers:
- CRUD for draft expenses, receipt URLs, amount, date, branch, category, payee, payment method.
- Owner/Finance Officer approval triggering balanced double-entry journal postings.
- Elevated sign-off required for high-value (>= 5,000 BDT) or reimbursable expense claims.
- Rejection workflow with mandatory audit reason.
- Reimbursement payout and settlement endpoint.
- Audited correction and reversal events preserving strict immutability of approved vouchers.
- Strict multi-tenant isolation and role enforcement.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.operating_expense_service import OperatingExpenseService

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
                "detail": "Tenant account subscription is suspended. Operating expense operations blocked.",
                "message_en": "Tenant account subscription is suspended. Operating expense operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। খরচ অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class OperatingExpenseViewSet(viewsets.ViewSet):
    """
    ViewSet governing Operating Expense Vouchers and Approval lifecycle.
    Endpoints:
    - GET    /api/v2/finance/expenses/
    - POST   /api/v2/finance/expenses/
    - GET    /api/v2/finance/expenses/<expense_id>/
    - PATCH  /api/v2/finance/expenses/<expense_id>/
    - DELETE /api/v2/finance/expenses/<expense_id>/
    - POST   /api/v2/finance/expenses/<expense_id>/approve/
    - POST   /api/v2/finance/expenses/<expense_id>/reject/
    - POST   /api/v2/finance/expenses/<expense_id>/reimburse/
    - POST   /api/v2/finance/expenses/<expense_id>/correct/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        status_param = request.query_params.get("status")
        category_id = request.query_params.get("category_id")
        branch_id = request.query_params.get("branch_id")
        is_reimbursable_param = request.query_params.get("is_reimbursable")
        is_high_value_param = request.query_params.get("is_high_value")

        is_reimbursable = None
        if is_reimbursable_param is not None:
            is_reimbursable = is_reimbursable_param.lower() in ("true", "1", "yes")

        is_high_value = None
        if is_high_value_param is not None:
            is_high_value = is_high_value_param.lower() in ("true", "1", "yes")

        expenses = OperatingExpenseService.list_expenses(
            tenant_id=tenant_id,
            status_param=status_param,
            category_id=category_id,
            branch_id=branch_id,
            is_reimbursable=is_reimbursable,
            is_high_value=is_high_value,
            actor_user=request.user,
        )
        return Response({"count": len(expenses), "expenses": expenses}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        expense = OperatingExpenseService.get_expense(
            tenant_id=tenant_id,
            expense_id=pk,
            actor_user=request.user,
        )
        return Response(expense, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        expense = OperatingExpenseService.create_expense_draft(
            tenant_id=tenant_id,
            data=request.data,
            actor_user=request.user,
        )
        return Response(expense, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        expense = OperatingExpenseService.update_expense_draft(
            tenant_id=tenant_id,
            expense_id=pk,
            data=request.data,
            actor_user=request.user,
        )
        return Response(expense, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        result = OperatingExpenseService.delete_expense_draft(
            tenant_id=tenant_id,
            expense_id=pk,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        notes = request.data.get("approval_notes", "")
        budget_override = bool(request.data.get("budget_override", False))
        budget_override_reason = str(request.data.get("budget_override_reason", ""))
        result = OperatingExpenseService.approve_and_post_expense(
            tenant_id=tenant_id,
            expense_id=pk,
            approval_notes=notes,
            budget_override=budget_override,
            budget_override_reason=budget_override_reason,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Expense voucher approved and posted to General Ledger.",
                "message_bn": "খরচ ভাউচার অনুমোদিত এবং জেনারেল লেজারে পোস্ট করা হয়েছে।",
                "budget_evaluation": result.get("budget_evaluation"),
                "expense": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reason = request.data.get("rejection_reason", "")
        result = OperatingExpenseService.reject_expense(
            tenant_id=tenant_id,
            expense_id=pk,
            rejection_reason=reason,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Expense voucher rejected.",
                "message_bn": "খরচ ভাউচার বাতিল করা হয়েছে।",
                "expense": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reimburse")
    def reimburse(self, request, pk=None) -> Response:
        """
        Disburses payout for an approved reimbursable expense voucher.
        """
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        payout_method = request.data.get("payout_method", "CASH")
        reference = request.data.get("reference", "")

        result = OperatingExpenseService.reimburse_expense(
            tenant_id=tenant_id,
            expense_id=pk,
            payout_method=payout_method,
            reference=reference,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Expense reimbursement disbursed successfully.",
                "message_bn": "খরচের রিইম্বার্সমেন্ট সফলভাবে পরিশোধ করা হয়েছে।",
                "expense": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="correct")
    def correct(self, request, pk=None) -> Response:
        """
        Executes an audited correction or reversal on an approved expense voucher.
        """
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        corr_type = request.data.get("correction_type", "")
        reason = request.data.get("reason", "")
        corrected_amount = request.data.get("corrected_amount_bdt")
        corrected_cat_id = request.data.get("corrected_category_id")

        result = OperatingExpenseService.correct_expense(
            tenant_id=tenant_id,
            expense_id=pk,
            correction_type=corr_type,
            reason=reason,
            corrected_amount_bdt=corrected_amount,
            corrected_category_id=corrected_cat_id,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Expense correction executed with balanced General Ledger adjustment.",
                "message_bn": "জেনারেল লেজার সমন্বয় সহ খরচ ভাউচার সংশোধন সম্পন্ন হয়েছে।",
                "correction": result,
            },
            status=status.HTTP_200_OK,
        )
