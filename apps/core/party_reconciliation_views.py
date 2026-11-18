"""
backend/apps/core/party_reconciliation_views.py
Architecture Decision: ADR-0280 (P280).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet governing End-to-End Party Reconciliation:
- Customer Accounts Receivable (AR) reconciliation.
- Supplier Accounts Payable (AP) reconciliation.
- Tenant portfolio balance summary and authoritative statements.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.party_reconciliation_service import PartyReconciliationService

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
    body_tenant = request.data.get("tenant_id") if hasattr(request, "data") else None
    if body_tenant:
        return str(body_tenant)
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
                "detail": "Tenant account subscription is suspended. Reconciliation blocked.",
                "message_en": "Tenant account subscription is suspended. Reconciliation blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। রিকনসিলিয়েশন বন্ধ করা হয়েছে।",
            }
        )


class PartyReconciliationViewSet(viewsets.ViewSet):
    """
    ViewSet governing party accounts receivable and accounts payable reconciliation.
    Endpoints:
    - POST /api/v2/parties/reconciliation/reconcile-customer/
    - POST /api/v2/parties/reconciliation/reconcile-supplier/
    - POST /api/v2/parties/reconciliation/portfolio-summary/
    - GET  /api/v2/parties/reconciliation/statement/?reconciliation_id=<id>
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="reconcile-customer")
    def reconcile_customer(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        customer_id = request.data.get("customer_id")
        if not customer_id:
            raise ValidationError({"error_code": "ERR_MISSING_CUSTOMER_ID", "detail": "customer_id is required."})

        auto_adjust = bool(request.data.get("auto_adjust", False))
        adjustment_reason = request.data.get("adjustment_reason", "")

        result = PartyReconciliationService.reconcile_customer_ar(
            tenant_id=tenant_id,
            customer_id=customer_id,
            actor_user=request.user,
            auto_adjust=auto_adjust,
            adjustment_reason=adjustment_reason,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="reconcile-supplier")
    def reconcile_supplier(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        supplier_id = request.data.get("supplier_id")
        if not supplier_id:
            raise ValidationError({"error_code": "ERR_MISSING_SUPPLIER_ID", "detail": "supplier_id is required."})

        auto_adjust = bool(request.data.get("auto_adjust", False))
        adjustment_reason = request.data.get("adjustment_reason", "")

        result = PartyReconciliationService.reconcile_supplier_ap(
            tenant_id=tenant_id,
            supplier_id=supplier_id,
            actor_user=request.user,
            auto_adjust=auto_adjust,
            adjustment_reason=adjustment_reason,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post", "get"], url_path="portfolio-summary")
    def portfolio_summary(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        result = PartyReconciliationService.reconcile_tenant_portfolio(
            tenant_id=tenant_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="statement")
    def statement(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reconciliation_id = request.query_params.get("reconciliation_id")
        if not reconciliation_id:
            raise ValidationError({"error_code": "ERR_MISSING_RECONCILIATION_ID", "detail": "reconciliation_id is required."})

        result = PartyReconciliationService.get_reconciliation_statement(
            tenant_id=tenant_id,
            reconciliation_id=reconciliation_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)
