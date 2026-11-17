"""
backend/apps/core/stock_segregation_views.py
Architecture Decision: ADR-0207 (P207).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Damaged/Returned Stock Segregation.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    SegregationCategory,
    SegregationStatus,
    StockSegregationRecord,
)
from apps.core.stock_segregation_service import StockSegregationService


class StockSegregationSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)
    segregated_by_email = serializers.EmailField(source="segregated_by.email", read_only=True)
    inspected_by_email = serializers.EmailField(source="inspected_by.email", read_only=True)
    actioned_by_email = serializers.EmailField(source="actioned_by.email", read_only=True)

    class Meta:
        model = StockSegregationRecord
        fields = [
            "segregation_id",
            "branch",
            "batch",
            "product",
            "product_name",
            "batch_number",
            "segregated_quantity",
            "unit_cost_bdt",
            "total_cost_impact",
            "category",
            "status",
            "segregation_location",
            "reason_notes",
            "segregated_by",
            "segregated_by_email",
            "segregated_at",
            "inspected_by",
            "inspected_by_email",
            "inspected_at",
            "inspection_notes",
            "release_justification",
            "actioned_by",
            "actioned_by_email",
            "actioned_at",
            "disposition_reference",
            "ledger_entry_id",
            "immutable_hash",
            "updated_at",
        ]
        read_only_fields = [
            "segregation_id",
            "batch_number",
            "unit_cost_bdt",
            "total_cost_impact",
            "status",
            "segregated_by",
            "segregated_at",
            "inspected_by",
            "inspected_at",
            "actioned_by",
            "actioned_at",
            "ledger_entry_id",
            "immutable_hash",
            "updated_at",
        ]


class StockSegregationViewSet(viewsets.ModelViewSet):
    """
    CRUD and Inspection Workflows for Segregated Inventory Stocks.
    """

    serializer_class = StockSegregationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return StockSegregationRecord.objects.none()

        qs = StockSegregationRecord.objects.filter(tenant_id=tenant_id).select_related(
            "product", "batch", "segregated_by", "inspected_by", "actioned_by"
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        category_param = self.request.query_params.get("category")
        if category_param:
            qs = qs.filter(category=category_param)

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        return qs.order_by("-segregated_at")

    @action(detail=False, methods=["post"], url_path="segregate")
    def segregate(self, request):
        """
        Deducts units from available sellable inventory into segregation holding.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        branch_id = request.data.get("branch") or getattr(user, "branch_id", None)
        batch_id = request.data.get("batch")
        quantity = request.data.get("quantity")
        category = request.data.get("category", SegregationCategory.DAMAGED)
        reason_notes = request.data.get("reason_notes", "")
        location = request.data.get("segregation_location", "SEGREGATION-HOLD")

        if not branch_id or not batch_id or not quantity:
            raise ValidationError({"error": "branch, batch, and quantity are required."})

        try:
            qty_int = int(quantity)
        except (ValueError, TypeError):
            raise ValidationError({"quantity": "Quantity must be an integer."})

        record = StockSegregationService.segregate_stock(
            tenant_id=tenant_id,
            branch_id=branch_id,
            batch_id=batch_id,
            quantity=qty_int,
            category=category,
            reason_notes=reason_notes,
            segregation_location=location,
            actor_user=user,
        )
        return Response(self.get_serializer(record).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="release-to-sellable")
    def release_to_sellable(self, request, pk=None):
        """
        QA pharmacist inspects and releases verified stock back to sellable inventory.
        """
        user = request.user
        justification = request.data.get("release_justification", "")
        new_loc = request.data.get("new_storage_location", "SHELF-ACTIVE")

        record = StockSegregationService.release_to_sellable(
            segregation_id=pk,
            tenant_id=user.tenant_id,
            justification=justification,
            inspector_user=user,
            new_storage_location=new_loc,
        )
        return Response(self.get_serializer(record).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="dispose-or-return")
    def dispose_or_return(self, request, pk=None):
        """
        Disposes/destroys stock or returns to supplier with ledger deduction.
        """
        user = request.user
        action_type = request.data.get("action_type", SegregationStatus.DISPOSED_WRITTEN_OFF)
        disp_ref = request.data.get("disposition_reference", "")
        action_notes = request.data.get("action_notes", "")

        record = StockSegregationService.dispose_or_return_stock(
            segregation_id=pk,
            tenant_id=user.tenant_id,
            action_type=action_type,
            disposition_reference=disp_ref,
            action_notes=action_notes,
            actor_user=user,
        )
        return Response(self.get_serializer(record).data, status=status.HTTP_200_OK)
