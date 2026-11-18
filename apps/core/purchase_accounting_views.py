"""
backend/apps/core/purchase_accounting_views.py
Architecture Decision: ADR-0284 (P284).
Phase 15: General Ledger, Operating Expenses & Profitability.

REST API ViewSet governing General Ledger Purchase Accounting Postings & Supplier Liabilities:
- Post GRN invoice recognition (Inventory Asset vs Accounts Payable).
- Post supplier payment disbursements (Accounts Payable vs Cash/Bank).
- Retrieve postings, detail, and multi-branch purchase accounting summary.
- Audited reversal of purchase accounting postings.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.purchase_accounting_service import PurchaseAccountingService

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
                "detail": "Tenant account subscription is suspended. Purchase accounting blocked.",
                "message_en": "Tenant account subscription is suspended. Purchase accounting blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ক্রয় অ্যাকাউন্টিং অপারেশন বন্ধ করা হয়েছে।",
            }
        )


class PurchaseAccountingViewSet(viewsets.ViewSet):
    """
    ViewSet governing General Ledger purchase accounting postings and supplier liabilities.
    Endpoints:
    - GET  /api/v2/finance/purchase-postings/
    - POST /api/v2/finance/purchase-postings/
    - GET  /api/v2/finance/purchase-postings/<posting_id>/
    - POST /api/v2/finance/purchase-postings/<posting_id>/reverse/
    - GET  /api/v2/finance/purchase-postings/summary/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        posting_type = request.query_params.get("posting_type")
        status_param = request.query_params.get("status")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        postings = PurchaseAccountingService.list_purchase_postings(
            tenant_id=tenant_id,
            branch_id=branch_id,
            posting_type=posting_type,
            status_param=status_param,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )
        return Response({"count": len(postings), "postings": postings}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        posting = PurchaseAccountingService.get_purchase_posting(
            tenant_id=tenant_id,
            posting_id=pk,
            actor_user=request.user,
        )
        return Response(posting, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        posting_type = request.data.get("posting_type", "INVOICE_RECOGNITION").upper().strip()

        if posting_type == "SUPPLIER_PAYMENT":
            supplier_id = request.data.get("supplier_id")
            payment_amount_bdt = request.data.get("payment_amount_bdt")
            payment_mode = request.data.get("payment_mode", "BANK")
            reference = request.data.get("reference", "")
            branch_id = request.data.get("branch_id")
            idempotency_key = request.data.get("idempotency_key", "")

            if not supplier_id or payment_amount_bdt is None:
                raise ValidationError(
                    {"error_code": "ERR_MISSING_PAYMENT_FIELDS", "detail": "supplier_id and payment_amount_bdt are required."}
                )

            result = PurchaseAccountingService.post_supplier_payment_disbursement(
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                payment_amount_bdt=payment_amount_bdt,
                payment_mode=payment_mode,
                reference=reference,
                branch_id=branch_id,
                idempotency_key=idempotency_key,
                actor_user=request.user,
            )
        else:
            # INVOICE_RECOGNITION
            grn_id = request.data.get("grn_id")
            supplier_id = request.data.get("supplier_id")
            total_cost_bdt = request.data.get("total_cost_bdt")
            challan_number = request.data.get("challan_number", "")
            supplier_invoice_number = request.data.get("supplier_invoice_number", "")
            purchase_order_number = request.data.get("purchase_order_number", "")
            branch_id = request.data.get("branch_id")
            paid_cash_bdt = request.data.get("paid_cash_bdt", 0)
            paid_bank_bdt = request.data.get("paid_bank_bdt", 0)
            idempotency_key = request.data.get("idempotency_key", "")

            if not grn_id and (not supplier_id or total_cost_bdt is None):
                raise ValidationError(
                    {"error_code": "ERR_MISSING_PURCHASE_REFERENCE", "detail": "Either grn_id or (supplier_id and total_cost_bdt) is required."}
                )

            result = PurchaseAccountingService.post_grn_invoice_recognition(
                tenant_id=tenant_id,
                grn_id=grn_id,
                supplier_id=supplier_id,
                total_cost_bdt=total_cost_bdt,
                challan_number=challan_number,
                supplier_invoice_number=supplier_invoice_number,
                purchase_order_number=purchase_order_number,
                branch_id=branch_id,
                paid_cash_bdt=paid_cash_bdt,
                paid_bank_bdt=paid_bank_bdt,
                idempotency_key=idempotency_key,
                actor_user=request.user,
            )

        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request, pk=None) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        reason = request.data.get("reason", "")
        result = PurchaseAccountingService.reverse_purchase_accounting(
            tenant_id=tenant_id,
            posting_id=pk,
            reason=reason,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request) -> Response:
        tenant_id = _get_request_tenant_id(request)
        _check_tenant_suspended(request, tenant_id)

        branch_id = request.query_params.get("branch_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        summary_data = PurchaseAccountingService.get_purchase_accounting_summary(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=request.user,
        )
        return Response(summary_data, status=status.HTTP_200_OK)
