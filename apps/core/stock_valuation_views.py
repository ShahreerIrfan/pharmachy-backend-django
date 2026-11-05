"""
backend/apps/core/stock_valuation_views.py
Architecture Decision: ADR-0179 (P179).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

DRF ViewSet providing live stock valuation, FIFO lot vs weighted average costing,
5-tier aging analysis, realized margin from immutable lot allocations,
and immutable snapshot archives.
"""

from __future__ import annotations

import datetime
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import StockCostingMethod, StockValuationSnapshot
from apps.core.stock_valuation_service import StockValuationService


class StockValuationViewSet(viewsets.ViewSet):
    """
    Authoritative API ViewSet for Stock Valuation, Aging Buckets, and Margin Derivations.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request: Request) -> str:
        tenant_id = request.query_params.get("tenant_id") or getattr(request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError(
                {"detail": "tenant_id query parameter or user tenant association is required.", "error_code": "ERR_TENANT_REQUIRED"}
            )
        return str(tenant_id)

    def list(self, request: Request) -> Response:
        """
        Calculates live inventory valuation and margin metrics on-demand.
        GET /api/v2/inventory/valuation/
        """
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        product_id = request.query_params.get("product_id")
        costing_method = request.query_params.get("costing_method", StockCostingMethod.FIFO_ACTUAL)

        as_of_str = request.query_params.get("as_of_date")
        start_str = request.query_params.get("start_date")
        end_str = request.query_params.get("end_date")

        try:
            as_of_date = datetime.date.fromisoformat(as_of_str) if as_of_str else None
        except ValueError:
            raise ValidationError({"detail": "Invalid as_of_date format. Use YYYY-MM-DD.", "error_code": "ERR_INVALID_VALUATION_INPUT"})

        try:
            start_date = datetime.date.fromisoformat(start_str) if start_str else None
            end_date = datetime.date.fromisoformat(end_str) if end_str else None
        except ValueError:
            raise ValidationError({"detail": "Invalid date range format. Use YYYY-MM-DD.", "error_code": "ERR_INVALID_VALUATION_INPUT"})

        valuation = StockValuationService.calculate_valuation(
            tenant_id=tenant_id,
            branch_id=branch_id,
            product_id=product_id,
            costing_method=costing_method,
            as_of_date=as_of_date,
            start_date=start_date,
            end_date=end_date,
            user=request.user,
        )
        return Response(valuation, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """
        Creates and persists an immutable valuation snapshot.
        POST /api/v2/inventory/valuation/
        """
        tenant_id = request.data.get("tenant_id") or getattr(request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"detail": "tenant_id is required.", "error_code": "ERR_TENANT_REQUIRED"})

        branch_id = request.data.get("branch_id")
        if not branch_id:
            raise ValidationError({"detail": "branch_id is required to anchor a valuation snapshot.", "error_code": "ERR_BRANCH_REQUIRED"})

        costing_method = request.data.get("costing_method", StockCostingMethod.FIFO_ACTUAL)
        notes = request.data.get("notes", "")

        as_of_str = request.data.get("as_of_date")
        start_str = request.data.get("start_date")
        end_str = request.data.get("end_date")

        try:
            as_of_date = datetime.date.fromisoformat(as_of_str) if as_of_str else None
            start_date = datetime.date.fromisoformat(start_str) if start_str else None
            end_date = datetime.date.fromisoformat(end_str) if end_str else None
        except ValueError:
            raise ValidationError({"detail": "Invalid date format. Use YYYY-MM-DD.", "error_code": "ERR_INVALID_VALUATION_INPUT"})

        snapshot = StockValuationService.create_snapshot(
            user=request.user,
            tenant_id=str(tenant_id),
            branch_id=str(branch_id),
            costing_method=costing_method,
            as_of_date=as_of_date,
            start_date=start_date,
            end_date=end_date,
            notes=notes,
        )
        return Response(snapshot, status=status.HTTP_201_CREATED)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """
        Retrieves a persisted stock valuation snapshot by ID.
        GET /api/v2/inventory/valuation/{id}/
        """
        StockValuationService._assert_manage_role(request.user)
        snapshot = StockValuationSnapshot.objects.select_related("branch").filter(snapshot_id=pk).first()
        if not snapshot:
            raise NotFound({"detail": f"Valuation snapshot '{pk}' not found.", "error_code": "ERR_SNAPSHOT_NOT_FOUND"})

        user_tenant = getattr(request.user, "tenant_id", None)
        if user_tenant and str(user_tenant) != str(snapshot.tenant_id) and getattr(request.user, "role", None) != "SUPERADMIN":
            raise PermissionDenied({"detail": "Cross-tenant access prohibited.", "error_code": "ERR_CROSS_TENANT_ACCESS"})

        return Response(StockValuationService.serialize_snapshot(snapshot), status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="aging")
    def aging(self, request: Request) -> Response:
        """
        Returns dedicated 5-tier inventory aging breakdown.
        GET /api/v2/inventory/valuation/aging/
        """
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        product_id = request.query_params.get("product_id")

        valuation = StockValuationService.calculate_valuation(
            tenant_id=tenant_id,
            branch_id=branch_id,
            product_id=product_id,
            costing_method=StockCostingMethod.FIFO_ACTUAL,
            user=request.user,
        )
        return Response(
            {
                "tenant_id": valuation["tenant_id"],
                "branch_id": valuation["branch_id"],
                "as_of_date": valuation["as_of_date"],
                "total_quantity_base_units": valuation["total_quantity_base_units"],
                "total_cost_valuation_bdt": valuation["total_cost_valuation_bdt"],
                "aging_summary": valuation["aging_summary"],
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="history")
    def history(self, request: Request) -> Response:
        """
        Lists historical valuation snapshots for the tenant and branch.
        GET /api/v2/inventory/valuation/history/
        """
        StockValuationService._assert_manage_role(request.user)
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")

        qs = StockValuationSnapshot.objects.select_related("branch").filter(tenant_id=tenant_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        results = [StockValuationService.serialize_snapshot(snap) for snap in qs[:50]]
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def update(self, request: Request, pk: Any = None) -> Response:
        raise MethodNotAllowed("PUT", detail="Stock valuation snapshots are immutable financial audit records.")

    def partial_update(self, request: Request, pk: Any = None) -> Response:
        raise MethodNotAllowed("PATCH", detail="Stock valuation snapshots are immutable financial audit records.")

    def destroy(self, request: Request, pk: Any = None) -> Response:
        raise MethodNotAllowed("DELETE", detail="Stock valuation snapshots are immutable financial audit records.")

