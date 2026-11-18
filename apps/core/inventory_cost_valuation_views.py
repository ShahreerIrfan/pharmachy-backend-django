"""
backend/apps/core/inventory_cost_valuation_views.py
Architecture Decision: ADR-0295 (P295).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSets governing Inventory Cost Valuation & GL Reconciliation:
- GET /api/v2/finance/inventory-valuation/ : Reconcile physical purchase lots against GL 1030
- POST /api/v2/finance/inventory-valuation/adjust/ : Post audited GL valuation adjustment (write-down/up)
- GET /api/v2/finance/inventory-valuation/export/ : Export valuation reconciliation report in CSV format
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from django.http import HttpResponse
from rest_framework import permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.inventory_cost_valuation_service import InventoryCostValuationService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger(__name__)


class InventoryValuationCsvExportRenderer(renderers.BaseRenderer):
    media_type = "text/csv"
    format = "csv"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, str):
            return data.encode("utf-8")
        return data


def _get_request_tenant_id(request) -> str:
    qp_tenant = getattr(request, "query_params", {}).get("tenant_id") if hasattr(request, "query_params") else request.GET.get("tenant_id")
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


def _check_tenant_suspended(request, tenant_id: str):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Inventory valuation blocked.",
                "message_en": "Tenant account subscription is suspended. Inventory valuation blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ইনভেন্টরি মূল্যায়ন দেখা সম্ভব নয়।",
            }
        )


class InventoryCostValuationViewSet(viewsets.ViewSet):
    """
    Dedicated ViewSet for Inventory Cost Valuation, Loss Disposition, and GL Reconciliation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        as_of_date = request.query_params.get("as_of_date")

        report = InventoryCostValuationService.generate_inventory_valuation_report(
            tenant_id=tenant_id,
            branch_id=branch_id,
            as_of_date=as_of_date,
            actor_user=request.user,
        )
        return Response(report, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="adjust")
    def adjust(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        payload = request.data or {}
        branch_id = payload.get("branch_id")
        adjustment_type = payload.get("adjustment_type")
        amount_bdt = payload.get("amount_bdt")
        reason = payload.get("reason")
        audit_notes = payload.get("audit_notes", "")

        result = InventoryCostValuationService.post_valuation_adjustment(
            tenant_id=tenant_id,
            branch_id=branch_id,
            adjustment_type=adjustment_type,
            amount_bdt=amount_bdt,
            reason=reason,
            audit_notes=audit_notes,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(
        detail=False,
        methods=["get"],
        url_path="export",
        renderer_classes=[InventoryValuationCsvExportRenderer, renderers.JSONRenderer],
    )
    def export(self, request) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        as_of_date = request.query_params.get("as_of_date")

        csv_content = InventoryCostValuationService.export_inventory_valuation_csv(
            tenant_id=tenant_id,
            branch_id=branch_id,
            as_of_date=as_of_date,
            actor_user=request.user,
        )

        response = HttpResponse(csv_content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="inventory_cost_valuation.csv"'
        return response
