"""
backend/apps/core/inventory_snapshot_views.py
Architecture Decision: ADR-0215 (P215).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Inventory Snapshots:
- Fast cacheable read models derived strictly from canonical stock ledger movements.
- On-demand and EOD checkpoint generation.
- Tamper-evident SHA-256 cryptographic integrity verification endpoint.
- Multi-tenant isolation and strict cashier denial.
"""

from __future__ import annotations

from typing import Any

from django.utils.dateparse import parse_datetime
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework import serializers

from apps.core.models import (
    Branch,
    InventorySnapshotCheckpoint,
    InventorySnapshotLineItem,
    SnapshotCheckpointType,
    Tenant,
)
from apps.core.inventory_snapshot_service import InventorySnapshotService


class InventorySnapshotLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventorySnapshotLineItem
        fields = [
            "id",
            "product",
            "product_name",
            "batch",
            "batch_number",
            "storage_location_code",
            "quantity_base_units",
            "unit_cost",
            "total_valuation_bdt",
            "is_cold_chain",
            "ledger_delta_sum",
            "reconciled_discrepancy",
        ]
        read_only_fields = fields


class InventorySnapshotCheckpointSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.email", read_only=True, default=None)
    line_items = InventorySnapshotLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = InventorySnapshotCheckpoint
        fields = [
            "snapshot_id",
            "tenant",
            "branch",
            "branch_name",
            "checkpoint_type",
            "as_of_timestamp",
            "total_products_count",
            "total_batches_count",
            "total_quantity_base_units",
            "total_valuation_bdt",
            "ledger_entries_processed",
            "sha256_checksum",
            "is_canonical_reconciled",
            "created_by",
            "created_by_name",
            "created_at",
            "line_items",
        ]
        read_only_fields = fields


class InventorySnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for querying and generating canonical inventory snapshot read models.
    """
    serializer_class = InventorySnapshotCheckpointSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return InventorySnapshotCheckpoint.objects.none()

        qs = InventorySnapshotCheckpoint.objects.filter(tenant=tenant_id).select_related(
            "branch", "created_by"
        ).prefetch_related("line_items__product")

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        checkpoint_type = self.request.query_params.get("checkpoint_type")
        if checkpoint_type:
            qs = qs.filter(checkpoint_type=checkpoint_type)

        return qs

    @action(detail=False, methods=["post"], url_path="generate")
    def generate(self, request):
        """
        Generates a new point-in-time inventory snapshot derived from the stock ledger.
        """
        InventorySnapshotService.assert_inventory_role(request.user)

        tenant_id = getattr(request.user, "tenant_id", None) or getattr(request.user, "tenant", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User must have a tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        branch_id = request.data.get("branch") or getattr(request.user, "branch_id", None)
        if not branch_id:
            raise ValidationError({"branch": "Branch ID is required."})

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise NotFound(f"Branch {branch_id} not found for this tenant.")

        checkpoint_type = request.data.get("checkpoint_type", SnapshotCheckpointType.ON_DEMAND)
        as_of_str = request.data.get("as_of_timestamp")
        as_of_time = parse_datetime(as_of_str) if as_of_str else None

        checkpoint = InventorySnapshotService.generate_snapshot(
            tenant=tenant,
            branch=branch,
            checkpoint_type=checkpoint_type,
            as_of_timestamp=as_of_time,
            actor_user=request.user,
        )

        serializer = self.get_serializer(checkpoint)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="verify-integrity")
    def verify_integrity(self, request, pk=None):
        """
        Cryptographically verifies SHA-256 checksum of line items against stored header.
        """
        snapshot = self.get_object()
        matches, recomputed = InventorySnapshotService.verify_snapshot_integrity(snapshot)
        return Response(
            {
                "snapshot_id": snapshot.snapshot_id,
                "is_valid": matches,
                "stored_checksum": snapshot.sha256_checksum,
                "recomputed_checksum": recomputed,
            },
            status=status.HTTP_200_OK,
        )
