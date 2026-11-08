"""
backend/apps/core/inventory_count_views.py
Architecture Decision: ADR-0204 (P204).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Inventory Count Sessions & Maker-Checker Posting.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.inventory_count_service import InventoryCountService
from apps.core.models import (
    InventoryCountItem,
    InventoryCountSession,
    InventoryCountSessionStatus,
    InventoryCountSessionType,
)


class InventoryCountItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)

    class Meta:
        model = InventoryCountItem
        fields = [
            "item_id",
            "product",
            "product_name",
            "batch_number",
            "storage_location",
            "system_quantity",
            "counted_quantity",
            "discrepancy_quantity",
            "unit_cost",
            "cost_discrepancy_bdt",
            "discrepancy_type",
            "scanner_barcode",
            "adjustment_ledger_entry_id",
            "notes",
        ]
        read_only_fields = [
            "item_id",
            "system_quantity",
            "discrepancy_quantity",
            "cost_discrepancy_bdt",
            "discrepancy_type",
            "adjustment_ledger_entry_id",
        ]


class InventoryCountSessionSerializer(serializers.ModelSerializer):
    items = InventoryCountItemSerializer(many=True, read_only=True)

    class Meta:
        model = InventoryCountSession
        fields = [
            "session_id",
            "branch",
            "session_number",
            "session_type",
            "status",
            "warehouse",
            "location",
            "notes",
            "total_items_counted",
            "total_discrepancy_units",
            "total_cost_discrepancy_bdt",
            "surplus_count",
            "shortage_count",
            "matched_count",
            "created_by",
            "counted_by",
            "submitted_at",
            "reviewed_by",
            "reviewed_at",
            "checker_notes",
            "posted_at",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "session_id",
            "session_number",
            "status",
            "total_items_counted",
            "total_discrepancy_units",
            "total_cost_discrepancy_bdt",
            "surplus_count",
            "shortage_count",
            "matched_count",
            "created_by",
            "counted_by",
            "submitted_at",
            "reviewed_by",
            "reviewed_at",
            "posted_at",
            "created_at",
            "updated_at",
        ]


class InventoryCountSessionViewSet(viewsets.ModelViewSet):
    """
    CRUD and Maker-Checker Workflow ViewSet for Inventory Count Sessions.
    """

    serializer_class = InventoryCountSessionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return InventoryCountSession.objects.none()
        qs = InventoryCountSession.objects.filter(tenant_id=tenant_id).prefetch_related("items", "items__product")
        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        user = self.request.user
        InventoryCountService.assert_maker_role(user)
        session = InventoryCountService.create_session(
            tenant_id=user.tenant_id,
            branch_id=self.request.data.get("branch") or getattr(user, "branch_id", None),
            session_type=self.request.data.get("session_type", InventoryCountSessionType.CYCLE_COUNT),
            warehouse_id=self.request.data.get("warehouse"),
            location_id=self.request.data.get("location"),
            notes=self.request.data.get("notes", ""),
            actor_user=user,
        )
        serializer.instance = session

    @action(detail=True, methods=["post"], url_path="scan-item")
    def scan_item(self, request, pk=None):
        """Records scanned or counted batch into the active session."""
        session = self.get_object()
        user = request.user
        batch_number = request.data.get("batch_number", "")
        counted_quantity = int(request.data.get("counted_quantity", 0))
        barcode = request.data.get("scanner_barcode", "")
        storage_location = request.data.get("storage_location", "")
        notes = request.data.get("notes", "")

        item = InventoryCountService.record_scan_item(
            session_id=session.session_id,
            tenant_id=user.tenant_id,
            branch_id=session.branch_id,
            batch_number=batch_number,
            counted_quantity=counted_quantity,
            scanner_barcode=barcode,
            storage_location=storage_location,
            notes=notes,
            actor_user=user,
        )
        return Response(InventoryCountItemSerializer(item).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="submit-for-review")
    def submit_for_review(self, request, pk=None):
        """Maker submits count session for review."""
        session = self.get_object()
        user = request.user
        notes = request.data.get("notes", "")
        updated = InventoryCountService.submit_for_review(
            session_id=session.session_id,
            tenant_id=user.tenant_id,
            actor_user=user,
            notes=notes,
        )
        return Response(InventoryCountSessionSerializer(updated).data)

    @action(detail=True, methods=["post"], url_path="review")
    def review(self, request, pk=None):
        """Checker approves or rejects count sheet."""
        session = self.get_object()
        user = request.user
        is_approved = bool(request.data.get("is_approved", True))
        checker_notes = request.data.get("checker_notes", "")

        updated = InventoryCountService.review_session(
            session_id=session.session_id,
            tenant_id=user.tenant_id,
            is_approved=is_approved,
            checker_user=user,
            checker_notes=checker_notes,
        )
        return Response(InventoryCountSessionSerializer(updated).data)

    @action(detail=True, methods=["post"], url_path="post-adjustments")
    def post_adjustments(self, request, pk=None):
        """Posts discrepancy adjustments to the immutable Stock Ledger."""
        session = self.get_object()
        user = request.user
        res = InventoryCountService.post_adjustments(
            session_id=session.session_id,
            tenant_id=user.tenant_id,
            actor_user=user,
        )
        return Response(res, status=status.HTTP_200_OK)

