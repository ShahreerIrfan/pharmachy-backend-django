"""
backend/apps/core/tax_configuration_views.py
Architecture Decision: ADR-0297 (P297).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing Tax Configuration & Invoice Presentation:
- GET    /api/v2/finance/tax-config/                          : List configured tax rules
- POST   /api/v2/finance/tax-config/                          : Create DRAFT tax rule
- GET    /api/v2/finance/tax-config/{id}/                     : Retrieve rule detail
- PATCH  /api/v2/finance/tax-config/{id}/                     : Edit DRAFT rule
- DELETE /api/v2/finance/tax-config/{id}/                     : Soft-archive tax rule
- POST   /api/v2/finance/tax-config/{id}/review-activate/     : Audited DRAFT -> REVIEWED_ACTIVE transition
- GET/PUT /api/v2/finance/tax-config/presentation/            : Get or update invoice tax presentation profile
- POST   /api/v2/finance/tax-config/calculate-invoice/        : Calculate invoice taxes (+ optional snapshot & GL posting)
- POST   /api/v2/finance/tax-config/reverse-snapshot/         : Reverse finalized tax snapshot + GL entry
- GET    /api/v2/finance/tax-config/export/?format=csv        : Export tax configuration CSV
"""

from __future__ import annotations

import logging

from django.http import HttpResponse
from rest_framework import permissions, renderers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.tax_configuration_service import TaxConfigurationService

logger = logging.getLogger(__name__)


class TaxConfigCsvExportRenderer(renderers.BaseRenderer):
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
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    if hasattr(request, "data") and isinstance(request.data, dict) and request.data.get("tenant_id"):
        return str(request.data.get("tenant_id"))
    return ""


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant subscription is suspended. Tax configuration blocked.",
                "message_en": "Tenant subscription is suspended. Tax configuration blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ট্যাক্স কনফিগারেশন বন্ধ করা হয়েছে।",
            }
        )


class TaxConfigurationViewSet(viewsets.ViewSet):
    """
    REST ViewSet for Tenant-Reviewed Tax Configuration & Invoice Presentation (P297).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        result = TaxConfigurationService.list_tax_rules(
            tenant_id=tenant_id,
            status_filter=request.query_params.get("status"),
            branch_id=request.query_params.get("branch_id"),
            applies_to_scope=request.query_params.get("applies_to_scope"),
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        rule = TaxConfigurationService.get_tax_rule(tenant_id=tenant_id, rule_id=pk, actor_user=request.user)
        return Response(rule, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        rule = TaxConfigurationService.create_tax_rule(
            tenant_id=tenant_id, data=request.data or {}, actor_user=request.user
        )
        return Response(rule, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        rule = TaxConfigurationService.update_tax_rule_draft(
            tenant_id=tenant_id, rule_id=pk, data=request.data or {}, actor_user=request.user
        )
        return Response(rule, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        reason = str((request.data or {}).get("reason", "")) if isinstance(request.data, dict) else ""
        result = TaxConfigurationService.archive_tax_rule(
            tenant_id=tenant_id, rule_id=pk, reason=reason, actor_user=request.user
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="review-activate")
    def review_activate(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        result = TaxConfigurationService.review_and_activate_rule(
            tenant_id=tenant_id,
            rule_id=pk,
            review_reference=payload.get("review_reference", ""),
            review_notes=payload.get("review_notes", ""),
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get", "put", "patch"], url_path="presentation")
    def presentation(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        if request.method == "GET":
            res = TaxConfigurationService.get_or_default_presentation_profile(
                tenant_id=tenant_id,
                branch_id=request.query_params.get("branch_id"),
                actor_user=request.user,
            )
            return Response(res, status=status.HTTP_200_OK)
        res = TaxConfigurationService.upsert_presentation_profile(
            tenant_id=tenant_id, data=request.data or {}, actor_user=request.user
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="calculate-invoice")
    def calculate_invoice(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = TaxConfigurationService.calculate_invoice_taxes(
            tenant_id=tenant_id,
            branch_id=str(payload.get("branch_id") or getattr(request.user, "branch_id", "")),
            items=payload.get("items", []),
            invoice_reference=payload.get("invoice_reference", ""),
            invoice_date=payload.get("invoice_date"),
            persist_snapshot=bool(payload.get("persist_snapshot", False)),
            post_gl=bool(payload.get("post_gl", False)),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="reverse-snapshot")
    def reverse_snapshot(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        payload = request.data or {}
        res = TaxConfigurationService.reverse_tax_snapshot(
            tenant_id=tenant_id,
            snapshot_id=str(payload.get("snapshot_id", "")),
            reason=str(payload.get("reason", "")),
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["get"],
        url_path="export",
        renderer_classes=[renderers.JSONRenderer, TaxConfigCsvExportRenderer],
    )
    def export_csv(self, request) -> HttpResponse:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request)
        csv_body = TaxConfigurationService.export_tax_rules_csv(tenant_id=tenant_id, actor_user=request.user)
        response = HttpResponse(csv_body, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="tax_rules_configuration.csv"'
        return response
