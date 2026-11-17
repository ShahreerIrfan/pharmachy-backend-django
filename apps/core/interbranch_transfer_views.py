"""
backend/apps/core/interbranch_transfer_views.py
Architecture Decision: ADR-0208 (P208).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Inter-Branch Inventory Transfers.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.interbranch_transfer_service import InterBranchTransferService
from apps.core.models import (
    InterBranchTransferItem,
    InterBranchTransferPriority,
    InterBranchTransferRequest,
    InterBranchTransferStatus,
)


class InterBranchTransferItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)

    class Meta:
        model = InterBranchTransferItem
        fields = [
            "item_id",
            "product",
            "product_name",
            "source_batch",
            "batch_number",
            "quantity_requested",
            "quantity_dispatched",
            "quantity_received",
            "quantity_received_good",
            "quantity_received_damaged",
            "quantity_lost",
            "discrepancy_reason",
            "damage_storage_location",
            "damage_ledger_entry_id",
            "loss_ledger_entry_id",
            "is_reconciled",
            "original_cost_per_unit",
            "transferred_cost_per_unit",
            "original_lot_reference",
            "source_cost_lot",
            "destination_cost_lot",
            "unit_cost_bdt",
            "total_cost_bdt",
            "expiry_date",
            "source_storage_location",
            "destination_storage_location",
            "dispatch_ledger_entry_id",
            "receipt_ledger_entry_id",
            "item_notes",
        ]
        read_only_fields = [
            "item_id",
            "quantity_dispatched",
            "quantity_received",
            "quantity_received_good",
            "quantity_received_damaged",
            "quantity_lost",
            "damage_ledger_entry_id",
            "loss_ledger_entry_id",
            "is_reconciled",
            "original_cost_per_unit",
            "transferred_cost_per_unit",
            "original_lot_reference",
            "source_cost_lot",
            "destination_cost_lot",
            "unit_cost_bdt",
            "total_cost_bdt",
            "dispatch_ledger_entry_id",
            "receipt_ledger_entry_id",
        ]


class InterBranchTransferRequestSerializer(serializers.ModelSerializer):
    source_branch_name = serializers.CharField(source="source_branch.name", read_only=True)
    destination_branch_name = serializers.CharField(source="destination_branch.name", read_only=True)
    items = InterBranchTransferItemSerializer(many=True, read_only=True)

    class Meta:
        model = InterBranchTransferRequest
        fields = [
            "transfer_id",
            "transfer_number",
            "source_branch",
            "source_branch_name",
            "destination_branch",
            "destination_branch_name",
            "status",
            "priority",
            "transfer_cost_policy",
            "markup_percentage",
            "logistics_handling_fee_bdt",
            "notes",
            "requested_by",
            "requested_at",
            "approved_by",
            "approved_at",
            "dispatched_by",
            "dispatched_at",
            "dispatch_carrier_notes",
            "received_by",
            "received_at",
            "receiving_notes",
            "rejection_reason",
            "total_items_count",
            "total_units_requested",
            "total_units_dispatched",
            "total_units_received",
            "total_units_received_good",
            "total_units_received_damaged",
            "total_units_lost",
            "reconciliation_notes",
            "reconciled_by",
            "reconciled_at",
            "total_valuation_bdt",
            "immutable_hash",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "transfer_id",
            "transfer_number",
            "status",
            "requested_by",
            "requested_at",
            "approved_by",
            "approved_at",
            "dispatched_by",
            "dispatched_at",
            "received_by",
            "received_at",
            "reconciled_by",
            "reconciled_at",
            "total_items_count",
            "total_units_requested",
            "total_units_dispatched",
            "total_units_received",
            "total_units_received_good",
            "total_units_received_damaged",
            "total_units_lost",
            "total_valuation_bdt",
            "immutable_hash",
            "created_at",
            "updated_at",
        ]


class InterBranchTransferViewSet(viewsets.ModelViewSet):
    """
    CRUD and State Workflow ViewSet for Inter-Branch Transfers.
    """

    serializer_class = InterBranchTransferRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return InterBranchTransferRequest.objects.none()

        qs = InterBranchTransferRequest.objects.filter(tenant_id=tenant_id).select_related(
            "source_branch", "destination_branch"
        ).prefetch_related("items", "items__product")

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            from django.db.models import Q
            qs = qs.filter(Q(source_branch_id=branch_id) | Q(destination_branch_id=branch_id))

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        return qs.order_by("-created_at")

    @action(detail=False, methods=["post"], url_path="request-transfer")
    def request_transfer(self, request):
        """
        Creates a transfer request and reserves stock at the sender branch.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        src_branch = request.data.get("source_branch")
        dst_branch = request.data.get("destination_branch")
        items = request.data.get("items", [])
        priority = request.data.get("priority", InterBranchTransferPriority.NORMAL)
        notes = request.data.get("notes", "")

        if not src_branch or not dst_branch or not items:
            raise ValidationError({"error": "source_branch, destination_branch, and items list are required."})

        transfer = InterBranchTransferService.create_transfer_request(
            tenant_id=tenant_id,
            source_branch_id=src_branch,
            destination_branch_id=dst_branch,
            items_payload=items,
            priority=priority,
            notes=notes,
            actor_user=user,
        )
        return Response(self.get_serializer(transfer).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """Approves transfer request."""
        user = request.user
        transfer = InterBranchTransferService.approve_transfer(
            transfer_id=pk,
            tenant_id=user.tenant_id,
            actor_user=user,
        )
        return Response(self.get_serializer(transfer).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="dispatch")
    def dispatch_transfer(self, request, pk=None):
        """Dispatches transfer, deducting stock from source branch ledger."""
        user = request.user
        carrier_notes = request.data.get("dispatch_carrier_notes", "")
        transfer = InterBranchTransferService.dispatch_transfer(
            transfer_id=pk,
            tenant_id=user.tenant_id,
            carrier_notes=carrier_notes,
            actor_user=user,
        )
        return Response(self.get_serializer(transfer).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="receive")
    def receive(self, request, pk=None):
        """Receives transfer at destination branch, crediting destination ledger."""
        user = request.user
        recv_items = request.data.get("received_items")
        recv_notes = request.data.get("receiving_notes", "")
        transfer = InterBranchTransferService.receive_transfer(
            transfer_id=pk,
            tenant_id=user.tenant_id,
            received_items_payload=recv_items,
            receiving_notes=recv_notes,
            actor_user=user,
        )
        return Response(self.get_serializer(transfer).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reconcile")
    def reconcile(self, request, pk=None):
        """Reconciles partial/damaged received items and records transit discrepancy."""
        user = request.user
        recv_items = request.data.get("received_items")
        reconcile_notes = request.data.get("reconciliation_notes", "")
        transfer = InterBranchTransferService.receive_transfer(
            transfer_id=pk,
            tenant_id=user.tenant_id,
            received_items_payload=recv_items,
            receiving_notes=reconcile_notes,
            actor_user=user,
        )
        return Response(self.get_serializer(transfer).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """Cancels transfer prior to dispatch and releases sender reservations."""
        user = request.user
        reason = request.data.get("cancellation_reason", "")
        transfer = InterBranchTransferService.cancel_transfer(
            transfer_id=pk,
            tenant_id=user.tenant_id,
            cancellation_reason=reason,
            actor_user=user,
        )
        return Response(self.get_serializer(transfer).data, status=status.HTTP_200_OK)

