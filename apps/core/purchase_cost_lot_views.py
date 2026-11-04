"""
Architecture Decision: ADR-0167 (P167)
DRF ViewSet for Purchase Cost Lot Entity & COGS Valuation Separation.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import Tenant, TenantLifecycleStatus
from apps.core.purchase_cost_lot_service import PurchaseCostLotService

logger = logging.getLogger(__name__)


class PurchaseCostLotViewSet(viewsets.ViewSet):
    """
    API ViewSet for managing purchase cost lots, COGS allocations,
    cost reversals, and inventory balance sheet valuation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant(self, request) -> Tenant:
        user = request.user
        tenant_id = (
            request.query_params.get("tenant_id")
            or request.data.get("tenant_id")
            or getattr(user, "tenant_id", None)
        )
        if not tenant_id:
            raise ValidationError(
                {"error_code": "ERR_TENANT_REQUIRED", "detail": "tenant_id query param or payload field is required."}
            )

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != "SUPERADMIN":
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_MISMATCH", "detail": "Cross-tenant access prohibited."}
            )

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": "Tenant not found."})

        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription suspended."}
            )

        return tenant

    def list(self, request) -> Response:
        """Lists purchase cost lots for tenant in FIFO order."""
        try:
            tenant = self._get_tenant(request)
            branch_id = request.query_params.get("branch_id")
            product_id = request.query_params.get("product_id")
            batch_id = request.query_params.get("batch_id")
            status_filter = request.query_params.get("status")

            lots = PurchaseCostLotService.list_purchase_lots(
                user=request.user,
                tenant_id=tenant.tenant_id,
                branch_id=branch_id,
                product_id=product_id,
                batch_id=batch_id,
                status_filter=status_filter,
            )
            return Response(lots, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """Retrieves a single purchase cost lot record."""
        try:
            tenant = self._get_tenant(request)
            lot = PurchaseCostLotService.get_purchase_lot(
                user=request.user,
                tenant_id=tenant.tenant_id,
                lot_id=pk,
            )
            return Response(lot, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """Receives purchase stock into a distinct cost lot and increments batch stock."""
        try:
            tenant = self._get_tenant(request)
            payload = dict(request.data)
            payload["tenant_id"] = tenant.tenant_id

            lot = PurchaseCostLotService.create_purchase_lot(
                user=request.user,
                data=payload,
            )
            return Response(lot, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="allocate-cogs")
    def allocate_cogs(self, request) -> Response:
        """
        Allocates requested quantity across active cost lots in FEFO/FIFO order.
        Derives unblended COGS and decrements lot stock atomically.
        """
        try:
            tenant = self._get_tenant(request)
            branch_id = request.data.get("branch_id")
            product_id = request.data.get("product_id")
            quantity = int(request.data.get("quantity", 0))
            batch_id = request.data.get("batch_id")

            if not branch_id or not product_id or quantity <= 0:
                return Response(
                    {
                        "error_code": "ERR_INVALID_PARAMS",
                        "detail": "branch_id, product_id, and positive quantity are required.",
                    },
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            result = PurchaseCostLotService.calculate_and_allocate_cogs(
                user=request.user,
                tenant_id=tenant.tenant_id,
                branch_id=branch_id,
                product_id=product_id,
                quantity=quantity,
                batch_id=batch_id,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="reverse-cogs")
    def reverse_cogs(self, request) -> Response:
        """
        Restores stock back into originating purchase cost lots and reverses COGS.
        """
        try:
            tenant = self._get_tenant(request)
            allocations = request.data.get("allocations", [])
            reason = request.data.get("reason", "Customer Return / Void")

            result = PurchaseCostLotService.reverse_purchase_lot_allocation(
                user=request.user,
                tenant_id=tenant.tenant_id,
                allocations=allocations,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["get"], url_path="valuation")
    def valuation(self, request) -> Response:
        """
        Computes aggregate balance sheet inventory valuation across active cost lots.
        """
        try:
            tenant = self._get_tenant(request)
            branch_id = request.query_params.get("branch_id")
            product_id = request.query_params.get("product_id")

            result = PurchaseCostLotService.get_inventory_valuation(
                user=request.user,
                tenant_id=tenant.tenant_id,
                branch_id=branch_id,
                product_id=product_id,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

