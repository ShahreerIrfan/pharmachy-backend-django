"""
backend/apps/core/multi_supplier_comparison_views.py
Architecture Decision: ADR-0195 (P195).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet for Multi-Supplier Comparison:
- Compares suppliers by product cost, delivery reliability, lead time, pack size, and approved terms.
- Evaluates composite rankings and recommended vendors.
- Allows managerial PO conversion upon audited supplier selection.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.multi_supplier_comparison_service import MultiSupplierComparisonService

logger = logging.getLogger(__name__)


class MultiSupplierComparisonViewSet(viewsets.ViewSet):
    """
    ViewSet for evaluating and managing multi-supplier procurement comparisons.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request: Request) -> str:
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id and hasattr(request.user, "tenant_id"):
            tenant_id = str(request.user.tenant_id)
        if not tenant_id:
            raise ValidationError(
                {
                    "error_code": "ERR_TENANT_REQUIRED",
                    "detail": "Tenant identification is required to access supplier comparisons.",
                }
            )
        return str(tenant_id)

    @action(detail=False, methods=["get"], url_path="compare")
    def compare(self, request: Request) -> Response:
        """
        Dynamically compares active suppliers for a given product without persisting.
        Query params: product_id (required), target_quantity (optional, default 100), branch_id (optional)
        """
        tenant_id = self._get_tenant_id(request)
        product_id = request.query_params.get("product_id")
        if not product_id:
            raise ValidationError(
                {
                    "error_code": "ERR_PRODUCT_REQUIRED",
                    "detail": "Query parameter 'product_id' is required for comparison.",
                }
            )

        target_qty_str = request.query_params.get("target_quantity", "100")
        try:
            target_qty = int(target_qty_str)
        except ValueError:
            target_qty = 100

        branch_id = request.query_params.get("branch_id")

        comparison = MultiSupplierComparisonService.compare_suppliers_for_product(
            tenant_id=tenant_id,
            actor_user=request.user,
            product_id=product_id,
            target_quantity=target_qty,
            branch_id=branch_id,
        )
        # Strip internal object references before JSON render
        clean_lines = []
        for line in comparison.get("comparison_lines", []):
            line_copy = dict(line)
            line_copy.pop("_supplier_obj", None)
            clean_lines.append(line_copy)
        comparison["comparison_lines"] = clean_lines
        return Response(comparison, status=status.HTTP_200_OK)

    def list(self, request: Request) -> Response:
        """
        Lists saved supplier comparison evaluations.
        """
        tenant_id = self._get_tenant_id(request)
        product_id = request.query_params.get("product_id")
        status_filter = request.query_params.get("status")

        evaluations = MultiSupplierComparisonService.list_evaluations(
            tenant_id=tenant_id,
            actor_user=request.user,
            product_id=product_id,
            status=status_filter,
        )
        return Response({"count": len(evaluations), "results": evaluations}, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """
        Creates and saves a formal supplier comparison evaluation snapshot.
        Body: product_id (required), target_order_quantity (optional), branch_id (optional), notes (optional)
        """
        tenant_id = self._get_tenant_id(request)
        product_id = request.data.get("product_id")
        if not product_id:
            raise ValidationError(
                {
                    "error_code": "ERR_PRODUCT_REQUIRED",
                    "detail": "Field 'product_id' is required to create comparison evaluation.",
                }
            )

        target_qty = int(request.data.get("target_order_quantity", 100) or 100)
        branch_id = request.data.get("branch_id")
        notes = request.data.get("notes", "")

        evaluation = MultiSupplierComparisonService.create_comparison_evaluation(
            tenant_id=tenant_id,
            actor_user=request.user,
            product_id=product_id,
            target_quantity=target_qty,
            branch_id=branch_id,
            notes=notes,
        )
        return Response(evaluation, status=status.HTTP_201_CREATED)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """
        Retrieves a single comparison evaluation by ID.
        """
        tenant_id = self._get_tenant_id(request)
        evaluation = MultiSupplierComparisonService.get_evaluation(
            tenant_id=tenant_id,
            actor_user=request.user,
            evaluation_id=pk,
        )
        return Response(evaluation, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="select-supplier")
    def select_supplier(self, request: Request, pk: str = None) -> Response:
        """
        Formally selects a supplier from the comparison and generates a Purchase Order.
        Body: selected_supplier_id (required), selection_reason (required), order_quantity (optional)
        """
        tenant_id = self._get_tenant_id(request)
        selected_supplier_id = request.data.get("selected_supplier_id")
        if not selected_supplier_id:
            raise ValidationError(
                {
                    "error_code": "ERR_SUPPLIER_REQUIRED",
                    "detail": "Field 'selected_supplier_id' is required.",
                }
            )

        selection_reason = request.data.get("selection_reason", "")
        if not selection_reason.strip():
            raise ValidationError(
                {
                    "error_code": "ERR_SELECTION_REASON_REQUIRED",
                    "detail": "Audited selection_reason is required to commit vendor procurement.",
                }
            )

        order_qty = request.data.get("order_quantity")
        if order_qty is not None:
            order_qty = int(order_qty)

        result = MultiSupplierComparisonService.select_supplier_and_create_po(
            tenant_id=tenant_id,
            actor_user=request.user,
            evaluation_id=pk,
            selected_supplier_id=selected_supplier_id,
            selection_reason=selection_reason,
            order_quantity=order_qty,
        )
        return Response(result, status=status.HTTP_200_OK)
