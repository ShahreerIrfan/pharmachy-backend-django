"""
backend/apps/core/financial_period_views.py
Architecture Decision: ADR-0292 (P292).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSets governing Financial Accounting Periods:
- GET    /api/v2/finance/periods/ : List Accounting Periods
- POST   /api/v2/finance/periods/ : Create Financial Period
- GET    /api/v2/finance/periods/{id}/ : Retrieve Period Details
- GET    /api/v2/finance/periods/{id}/validate-integrity/ : Pre-Close Ledger Parity & Pending Checks
- POST   /api/v2/finance/periods/{id}/close/ : Close Period & Post Retained Earnings Closing Journal
- POST   /api/v2/finance/periods/{id}/reopen/ : Controlled Reopening (Owner Only)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.financial_period_service import FinancialPeriodService
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
                "detail": "Tenant account subscription is suspended. Financial period operations blocked.",
                "message_en": "Tenant account subscription is suspended. Financial period operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। আর্থিক পিরিয়ড কার্যক্রম বন্ধ রাখা হয়েছে।",
            }
        )


class FinancialPeriodViewSet(viewsets.ViewSet):
    """
    Dedicated ViewSet governing Financial Accounting Period close lifecycle,
    immutability locks, and audited reopening.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        period_status = request.query_params.get("status")
        branch_id = request.query_params.get("branch_id")

        periods = FinancialPeriodService.list_periods(
            tenant_id=tenant_id,
            status=period_status,
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response({"count": len(periods), "periods": periods}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        period = FinancialPeriodService.create_period(
            tenant_id=tenant_id,
            data=request.data,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Financial accounting period created successfully.",
                "message_bn": "আর্থিক হিসাব পিরিয়ড সফলভাবে তৈরি করা হয়েছে।",
                "period": period,
            },
            status=status.HTTP_201_CREATED,
        )

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        period = FinancialPeriodService.get_period(
            tenant_id=tenant_id,
            period_id=pk,
            actor_user=request.user,
        )
        return Response({"period": period}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="validate-integrity")
    def validate_integrity(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        report = FinancialPeriodService.validate_period_integrity(
            tenant_id=tenant_id,
            period_id=pk,
            actor_user=request.user,
        )
        return Response(report, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        notes = request.data.get("closing_notes", "") if isinstance(request.data, dict) else ""
        period = FinancialPeriodService.close_period(
            tenant_id=tenant_id,
            period_id=pk,
            closing_notes=notes,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Financial accounting period closed, sealed, and posted to Retained Earnings.",
                "message_bn": "আর্থিক পিরিয়ড সফলভাবে বন্ধ ও সিল করা হয়েছে এবং পুঞ্জীভূত মুনাফায় পোস্ট করা হয়েছে।",
                "period": period,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reason = request.data.get("reopen_reason", "") if isinstance(request.data, dict) else ""
        period = FinancialPeriodService.reopen_period(
            tenant_id=tenant_id,
            period_id=pk,
            reopen_reason=reason,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Financial accounting period reopened under audit control.",
                "message_bn": "আর্থিক পিরিয়ড অডিট নিয়ন্ত্রণে পুনরায় উন্মুক্ত করা হয়েছে।",
                "period": period,
            },
            status=status.HTTP_200_OK,
        )
