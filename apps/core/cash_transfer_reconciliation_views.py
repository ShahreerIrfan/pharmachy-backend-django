"""
backend/apps/core/cash_transfer_reconciliation_views.py
Architecture Decision: ADR-0290 (P290).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSets governing Treasury Transfers and Bank Reconciliations:
- POST /api/v2/finance/treasury/transfers/ : Execute Cash-Bank Treasury Transfer
- GET  /api/v2/finance/treasury/transfers/ : List Transfers
- POST /api/v2/finance/treasury/reconciliations/ : Execute Bank Reconciliation Run
- GET  /api/v2/finance/treasury/reconciliations/ : List Bank Reconciliations
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.cash_transfer_reconciliation_service import CashTransferReconciliationService
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
                "detail": "Tenant account subscription is suspended. Treasury operations blocked.",
                "message_en": "Tenant account subscription is suspended. Treasury operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ট্রেজারি অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class CashBankTransferViewSet(viewsets.ViewSet):
    """
    ViewSet governing internal treasury transfers (Cash to Bank, Bank to Cash, Inter-bank).
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        tx_type = request.query_params.get("transfer_type")
        account_id = request.query_params.get("account_id")

        transfers = CashTransferReconciliationService.list_transfers(
            tenant_id=tenant_id,
            transfer_type=tx_type,
            account_id=account_id,
            actor_user=request.user,
        )
        return Response({"count": len(transfers), "transfers": transfers}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        transfer = CashTransferReconciliationService.execute_cash_bank_transfer(
            tenant_id=tenant_id,
            data=request.data,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Treasury fund transfer executed and posted to General Ledger.",
                "message_bn": "ট্রেজারি ফান্ড ট্রান্সফার সম্পন্ন এবং জেনারেল লেজারে পোস্ট করা হয়েছে।",
                "transfer": transfer,
            },
            status=status.HTTP_201_CREATED,
        )


class BankReconciliationViewSet(viewsets.ViewSet):
    """
    ViewSet governing Bank Statement Reconciliations.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        bank_acc_id = request.query_params.get("bank_account_id")
        reconciliations = CashTransferReconciliationService.list_reconciliations(
            tenant_id=tenant_id,
            bank_account_id=bank_acc_id,
            actor_user=request.user,
        )
        return Response({"count": len(reconciliations), "reconciliations": reconciliations}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        batch = CashTransferReconciliationService.create_reconciliation_batch(
            tenant_id=tenant_id,
            data=request.data,
            actor_user=request.user,
        )
        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Bank reconciliation batch processed.",
                "message_bn": "ব্যাংক রিকনসিলিয়েশন ব্যাচ সম্পন্ন হয়েছে।",
                "reconciliation": batch,
            },
            status=status.HTTP_201_CREATED,
        )
