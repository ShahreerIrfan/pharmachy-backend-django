"""
backend/apps/core/landed_cost_views.py
Architecture Decision: ADR-0176 (P176).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API Endpoints for Inward Landed Cost Allocation,
Inventory Capitalization, and Audited Reversals.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.landed_cost_service import LandedCostService
from apps.core.models import (
    Branch,
    LandedCostAllocationLine,
    LandedCostAllocationMethod,
    LandedCostExpense,
    LandedCostExpenseType,
    PurchaseCostLot,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class LandedCostAllocationLineSerializer(serializers.ModelSerializer):
    cost_lot_id = serializers.CharField(source="cost_lot.lot_id", read_only=True)
    batch_id = serializers.CharField(source="batch.batch_id", read_only=True)
    product_id = serializers.CharField(source="product.product_id", read_only=True)
    product_name = serializers.CharField(source="product.brand_name", read_only=True)

    class Meta:
        model = LandedCostAllocationLine
        fields = [
            "allocation_id",
            "cost_lot_id",
            "batch_id",
            "product_id",
            "product_name",
            "lot_base_unit_cost_before",
            "allocated_expense_amount",
            "allocated_cost_per_unit",
            "lot_landed_unit_cost_after",
            "created_at",
        ]


class LandedCostExpenseSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)
    branch_id = serializers.CharField(source="branch.branch_id", read_only=True)
    grn_id = serializers.CharField(source="grn.grn_id", read_only=True, default=None)
    recorded_by_id = serializers.CharField(source="recorded_by.user_id", read_only=True, default=None)
    lines = LandedCostAllocationLineSerializer(many=True, read_only=True)

    class Meta:
        model = LandedCostExpense
        fields = [
            "expense_id",
            "tenant_id",
            "branch_id",
            "grn_id",
            "expense_type",
            "allocation_method",
            "total_expense_bdt",
            "invoice_ref",
            "reason",
            "status",
            "recorded_by_id",
            "posted_at",
            "reversal_reason",
            "lines",
            "created_at",
            "updated_at",
        ]


class PreviewAllocationRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    branch_id = serializers.CharField(required=True)
    total_expense_bdt = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=Decimal("0.0100"))
    allocation_method = serializers.ChoiceField(choices=LandedCostAllocationMethod.choices)
    cost_lot_ids = serializers.ListField(child=serializers.CharField(), min_length=1)


class CreateLandedCostRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    branch_id = serializers.CharField(required=True)
    grn_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    expense_type = serializers.ChoiceField(choices=LandedCostExpenseType.choices)
    allocation_method = serializers.ChoiceField(choices=LandedCostAllocationMethod.choices)
    total_expense_bdt = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=Decimal("0.0100"))
    invoice_ref = serializers.CharField(required=False, allow_blank=True, default="")
    reason = serializers.CharField(required=True, min_length=10)
    cost_lot_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)


class ReverseLandedCostRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, min_length=10)


class LandedCostExpenseViewSet(viewsets.ViewSet):
    """
    API endpoints governing landed cost expenses, pro-rata lot allocations,
    and audited valuation reversals. Direct mutation or deletion is prohibited.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant(self, request: Any) -> Tenant:
        user = request.user
        tenant_id = (
            request.data.get("tenant_id") if request.method == "POST" else request.query_params.get("tenant_id")
        )
        tenant_id = tenant_id or getattr(user, "tenant_id", None)

        if not tenant_id:
            raise ValidationError({"detail": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        if (
            getattr(user, "tenant_id", None)
            and str(user.tenant_id) != str(tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {"detail": "Cross-tenant access is strictly prohibited.", "error_code": "ERR_CROSS_TENANT_ACCESS"}
            )

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"detail": f"Tenant '{tenant_id}' not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        if getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED):
            raise PermissionDenied(
                {
                    "detail": "Tenant operations are suspended due to subscription status.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

        return tenant

    def list(self, request: Any) -> Response:
        """List landed cost expenses scoped to tenant and optional branch/grn."""
        tenant = self._get_tenant(request)
        user = request.user
        role = getattr(user, "role", None)

        qs = LandedCostExpense.objects.filter(tenant=tenant).prefetch_related("lines")

        branch_id = request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        elif getattr(user, "branch_id", None) and role not in (UserRole.PHARMACY_OWNER, UserRole.SUPERADMIN, "OWNER_ADMIN"):
            qs = qs.filter(branch__branch_id=user.branch_id)

        grn_id = request.query_params.get("grn_id")
        if grn_id:
            qs = qs.filter(grn__grn_id=grn_id)

        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())

        serializer = LandedCostExpenseSerializer(qs[:100], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """Retrieve a single landed cost expense with complete allocation lines."""
        tenant = self._get_tenant(request)
        try:
            expense = LandedCostExpense.objects.prefetch_related("lines").get(tenant=tenant, expense_id=pk)
        except LandedCostExpense.DoesNotExist:
            raise NotFound(
                {"detail": f"Landed cost expense '{pk}' not found.", "error_code": "ERR_EXPENSE_NOT_FOUND"}
            )
        serializer = LandedCostExpenseSerializer(expense)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def create(self, request: Any) -> Response:
        """Create and post a landed cost expense, capitalizing costs to target lots."""
        tenant = self._get_tenant(request)
        user = request.user

        serializer = CreateLandedCostRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch_id = data["branch_id"]
        try:
            branch = Branch.objects.get(tenant=tenant, branch_id=branch_id)
        except Branch.DoesNotExist:
            raise NotFound({"detail": f"Branch '{branch_id}' not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        expense = LandedCostService.create_and_post_landed_cost(
            user=user,
            tenant=tenant,
            branch=branch,
            expense_type=data["expense_type"],
            allocation_method=data["allocation_method"],
            total_expense_bdt=data["total_expense_bdt"],
            reason=data["reason"],
            cost_lot_ids=data["cost_lot_ids"],
            grn_id=data.get("grn_id"),
            invoice_ref=data.get("invoice_ref", ""),
        )

        out_serializer = LandedCostExpenseSerializer(expense)
        return Response(out_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="preview")
    def preview(self, request: Any) -> Response:
        """Preview landed cost allocation breakdown across lots without persisting."""
        tenant = self._get_tenant(request)
        serializer = PreviewAllocationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch_id = data["branch_id"]
        try:
            branch = Branch.objects.get(tenant=tenant, branch_id=branch_id)
        except Branch.DoesNotExist:
            raise NotFound({"detail": f"Branch '{branch_id}' not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        cost_lots = list(
            PurchaseCostLot.objects.select_related("product", "batch").filter(
                tenant=tenant, branch=branch, lot_id__in=data["cost_lot_ids"]
            )
        )
        if len(cost_lots) != len(data["cost_lot_ids"]):
            cross_branch_lots = PurchaseCostLot.objects.filter(
                tenant=tenant, lot_id__in=data["cost_lot_ids"]
            ).exclude(branch=branch)
            if cross_branch_lots.exists():
                raise ValidationError(
                    {
                        "detail": "Cross-branch cost lot allocation is strictly prohibited. All cost lots must belong to the active branch.",
                        "error_code": "ERR_CROSS_BRANCH_LOT",
                    }
                )
            raise NotFound(
                {
                    "detail": "One or more cost lots not found in specified branch/tenant.",
                    "error_code": "ERR_COST_LOT_NOT_FOUND",
                }
            )

        allocations = LandedCostService.calculate_allocations(
            total_expense=data["total_expense_bdt"],
            allocation_method=data["allocation_method"],
            cost_lots=cost_lots,
        )

        preview_data = []
        for a in allocations:
            preview_data.append(
                {
                    "cost_lot_id": a["cost_lot_id"],
                    "batch_id": a["batch_id"],
                    "product_id": a["product_id"],
                    "product_name": a["product_name"],
                    "initial_quantity": a["initial_quantity"],
                    "lot_base_unit_cost_before": str(a["lot_base_unit_cost_before"]),
                    "allocated_expense_amount": str(a["allocated_expense_amount"]),
                    "allocated_cost_per_unit": str(a["allocated_cost_per_unit"]),
                    "lot_landed_unit_cost_after": str(a["lot_landed_unit_cost_after"]),
                }
            )

        return Response(
            {
                "total_expense_bdt": str(data["total_expense_bdt"]),
                "allocation_method": data["allocation_method"],
                "lot_count": len(preview_data),
                "allocations": preview_data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request: Any, pk: str = None) -> Response:
        """Reverse a posted landed cost expense and restore original base acquisition costs."""
        serializer = ReverseLandedCostRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reversed_expense = LandedCostService.reverse_landed_cost(
            user=request.user,
            expense_id=pk,
            reversal_reason=serializer.validated_data["reason"],
        )
        out_serializer = LandedCostExpenseSerializer(reversed_expense)
        return Response(out_serializer.data, status=status.HTTP_200_OK)

    def update(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed(
            "PUT",
            detail="Direct edits to landed cost records are prohibited. Use reverse action to undo.",
        )

    def partial_update(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed(
            "PATCH",
            detail="Direct edits to landed cost records are prohibited. Use reverse action to undo.",
        )

    def destroy(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed(
            "DELETE",
            detail="Direct deletion of landed cost records is prohibited. Use reverse action to undo.",
        )

