"""
backend/apps/core/purchase_return_views.py
Architecture Decision: ADR-0191 (P191).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing Supplier Purchase Returns & Debit Notes:
- Issues returns tied to original physical batches and cost lots.
- Guards against returning already-dispensed or POS-reserved stock.
- Posts AP debit notes to SupplierLedgerEntry reducing payable balance.
- Supports cancelation of unposted returns.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.purchase_return_service import PurchaseReturnService

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
    """Blocks suspended tenants from mutating or accessing purchase return resources."""
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Purchase return processing locked.",
            }
        )


class PurchaseReturnViewSet(viewsets.ViewSet):
    """
    REST API ViewSet governing Supplier Purchase Returns & Debit Notes.
    Routes:
      - GET    /api/v2/procurement/purchase-returns/
      - POST   /api/v2/procurement/purchase-returns/
      - GET    /api/v2/procurement/purchase-returns/{pk}/
      - POST   /api/v2/procurement/purchase-returns/{pk}/post-return/
      - POST   /api/v2/procurement/purchase-returns/{pk}/cancel-return/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/purchase-returns/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.query_params.get("supplier_id")
        status_filter = request.query_params.get("status")
        branch_id = request.query_params.get("branch_id") or _get_request_branch_id(request)

        try:
            returns = PurchaseReturnService.list_purchase_returns(
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                status_filter=status_filter,
                branch_id=branch_id,
            )
            return Response({"count": len(returns), "results": returns}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/purchase-returns/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            pret = PurchaseReturnService.get_purchase_return(
                tenant_id=tenant_id,
                return_id=pk,
            )
            return Response(pret, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/purchase-returns/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        auto_post = request.query_params.get("auto_post", "").lower() in ("true", "1")
        try:
            pret = PurchaseReturnService.create_purchase_return(
                tenant_id=tenant_id,
                actor_user=request.user,
                data=request.data,
                auto_post=auto_post,
            )
            return Response(pret, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error creating purchase return: %s", exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="post-return")
    def post_return(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/purchase-returns/{pk}/post-return/
        Decrements BatchStock, updates PurchaseCostLot, posts SupplierLedgerEntry debit note.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            pret = PurchaseReturnService.post_purchase_return(
                tenant_id=tenant_id,
                actor_user=request.user,
                return_id=pk,
            )
            return Response(pret, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error posting purchase return %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="cancel-return")
    def cancel_return(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/purchase-returns/{pk}/cancel-return/
        Cancels an unposted draft purchase return.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "")
        try:
            pret = PurchaseReturnService.cancel_purchase_return(
                tenant_id=tenant_id,
                actor_user=request.user,
                return_id=pk,
                reason=reason,
            )
            return Response(pret, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error cancelling purchase return %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

