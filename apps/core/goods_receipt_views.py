"""
backend/apps/core/goods_receipt_views.py
Architecture Decision: ADR-0187 (P187).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing Goods Receipt Notes (GRN), PO matching,
cold-chain inspections, stock-posting commands, and AP liability confirmation.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.goods_receipt_service import GoodsReceiptService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    """Extracts tenant ID from authenticated user, request tenant, or header."""
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    return ""


def _get_request_branch_id(request) -> str:
    """Extracts branch ID from authenticated user or header."""
    user = getattr(request, "user", None)
    if user and getattr(user, "branch_id", None):
        return str(user.branch_id)
    header = request.META.get("HTTP_X_BRANCH_ID")
    if header:
        return str(header)
    return ""


def _check_tenant_suspended(request):
    """Blocks suspended tenants from mutating or accessing goods receipt resources."""
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Goods receipt access blocked.",
                "message_en": "Tenant account subscription is suspended. Goods receipt access blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। গুডস রিসিট মডিউল বন্ধ।",
            }
        )


class GoodsReceiptViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for pharmaceutical Goods Received Notes (GRN).
    Enforces role authorization, PO line matching, cold-chain checks,
    idempotent stock posting, and reversal safety.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/goods-receipts/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        po_id = request.query_params.get("purchase_order_id")
        supplier_id = request.query_params.get("supplier_id")
        status_filter = request.query_params.get("status")

        try:
            results = GoodsReceiptService.list_grns(
                tenant_id=tenant_id,
                branch_id=branch_id,
                po_id=po_id,
                supplier_id=supplier_id,
                status=status_filter,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/goods-receipts/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            grn_data = GoodsReceiptService.get_grn(tenant_id=tenant_id, grn_id=pk)
            return Response(grn_data, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/goods-receipts/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        target_tenant = request.data.get("tenant_id") or tenant_id
        if target_tenant != tenant_id and not getattr(request.user, "is_superuser", False):
            return Response(
                {
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "detail": "Cross-tenant goods receipt creation forbidden.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            created = GoodsReceiptService.create_grn(
                tenant_id=target_tenant,
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="post-receipt")
    def post_receipt(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/goods-receipts/{pk}/post-receipt/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            posted = GoodsReceiptService.post_grn_to_stock(
                tenant_id=tenant_id,
                actor_user=request.user,
                grn_id=pk,
            )
            return Response(posted, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="reverse-receipt")
    def reverse_receipt(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/goods-receipts/{pk}/reverse-receipt/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "Goods Return / Invoice Cancellation")
        try:
            reversed_grn = GoodsReceiptService.reverse_grn(
                tenant_id=tenant_id,
                actor_user=request.user,
                grn_id=pk,
                reason=reason,
            )
            return Response(reversed_grn, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["get"], url_path="cold-chain-evidence")
    def cold_chain_evidence(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/goods-receipts/{pk}/cold-chain-evidence/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            evidences = GoodsReceiptService.get_cold_chain_evidences(
                tenant_id=tenant_id,
                grn_id=pk,
            )
            return Response({"count": len(evidences), "results": evidences}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["get"], url_path="cold-chain-quarantine")
    def cold_chain_quarantine(self, request) -> Response:
        """GET /api/v2/procurement/goods-receipts/cold-chain-quarantine/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        status_filter = request.query_params.get("quarantine_status", "PENDING_REVIEW")
        try:
            evidences = GoodsReceiptService.get_cold_chain_evidences(
                tenant_id=tenant_id,
                quarantine_status=status_filter if status_filter != "ALL" else None,
                branch_id=branch_id,
            )
            return Response({"count": len(evidences), "results": evidences}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="resolve-quarantine")
    def resolve_quarantine(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/goods-receipts/{pk}/resolve-quarantine/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        evidence_id = request.data.get("evidence_id")
        resolution_action = request.data.get("resolution_action")
        justification = request.data.get("justification", "")
        notes = request.data.get("notes", "")

        try:
            resolved = GoodsReceiptService.resolve_cold_chain_quarantine(
                tenant_id=tenant_id,
                actor_user=request.user,
                evidence_id=evidence_id,
                resolution_action=resolution_action,
                justification=justification,
                notes=notes,
            )
            return Response(resolved, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

