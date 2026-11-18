"""
backend/apps/core/double_entry_journal_views.py
Architecture Decision: ADR-0282 (P282).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing General Ledger Double-Entry Journals:
- Post balanced double-entry journals.
- Retrieve and filter journal entries.
- Compensating reversal workflows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.double_entry_journal_service import DoubleEntryJournalService
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
                "detail": "Tenant account subscription is suspended. Journal operations blocked.",
                "message_en": "Tenant account subscription is suspended. Journal operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। জার্নাল অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class DoubleEntryJournalViewSet(viewsets.ViewSet):
    """
    ViewSet governing General Ledger double-entry journals.
    Endpoints:
    - GET  /api/v2/finance/journals/
    - POST /api/v2/finance/journals/
    - GET  /api/v2/finance/journals/<entry_id>/
    - POST /api/v2/finance/journals/<entry_id>/reverse/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        source_module = request.query_params.get("source_module")
        status_param = request.query_params.get("status")
        branch_id = request.query_params.get("branch_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        entries = DoubleEntryJournalService.list_journal_entries(
            tenant_id=tenant_id,
            source_module=source_module,
            status=status_param,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )
        return Response({"count": len(entries), "journals": entries}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        entry = DoubleEntryJournalService.get_journal_entry(
            tenant_id=tenant_id,
            entry_id=pk,
            actor_user=request.user,
        )
        return Response(entry, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        lines = request.data.get("lines")
        if not lines:
            raise ValidationError({"error_code": "ERR_MISSING_JOURNAL_LINES", "detail": "lines list is required."})

        posting_date = request.data.get("posting_date")
        source_module = request.data.get("source_module", "MANUAL")
        source_reference = request.data.get("source_reference", "")
        narration = request.data.get("narration", "")
        branch_id = request.data.get("branch_id")
        idempotency_key = request.data.get("idempotency_key", "")

        result = DoubleEntryJournalService.post_journal_entry(
            tenant_id=tenant_id,
            lines=lines,
            posting_date=posting_date,
            source_module=source_module,
            source_reference=source_reference,
            narration=narration,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reason = request.data.get("reason", "")
        result = DoubleEntryJournalService.reverse_journal_entry(
            tenant_id=tenant_id,
            entry_id=pk,
            reason=reason,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)
