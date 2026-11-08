"""
backend/apps/core/warehouse_location_views.py
Architecture Decision: ADR-0202 (P202).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF Serializers and ViewSets for Warehouses, Storage Locations,
Cold Chain Placement and Controlled Access Rule Enforcement.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    BatchLocationAssignment,
    BatchStock,
    Branch,
    ControlledLocationAccessRule,
    StorageLocation,
    StorageLocationType,
    Tenant,
    Warehouse,
    WarehouseType,
)
from apps.core.warehouse_location_service import WarehouseLocationService


# ------------------------------------------------------------------------------
# Serializers
# ------------------------------------------------------------------------------


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = [
            "warehouse_id",
            "branch",
            "name",
            "name_bn",
            "code",
            "warehouse_type",
            "is_cold_storage",
            "is_active",
            "address",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["warehouse_id", "created_at", "updated_at"]


class ControlledLocationAccessRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ControlledLocationAccessRule
        fields = [
            "rule_id",
            "branch",
            "location",
            "allowed_role",
            "requires_dual_authorization",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["rule_id", "created_at", "updated_at"]


class StorageLocationSerializer(serializers.ModelSerializer):
    access_rules = ControlledLocationAccessRuleSerializer(many=True, read_only=True)

    class Meta:
        model = StorageLocation
        fields = [
            "location_id",
            "branch",
            "warehouse",
            "parent_location",
            "location_type",
            "code",
            "name",
            "name_bn",
            "capacity_pack_limit",
            "current_pack_count",
            "temperature_monitored",
            "min_temp_celsius",
            "max_temp_celsius",
            "is_controlled_access",
            "is_active",
            "access_rules",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["location_id", "current_pack_count", "created_at", "updated_at"]


class BatchLocationAssignmentSerializer(serializers.ModelSerializer):
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True)
    location_code = serializers.CharField(source="location.code", read_only=True)
    product_name = serializers.CharField(source="batch.product.brand_name", read_only=True)

    class Meta:
        model = BatchLocationAssignment
        fields = [
            "assignment_id",
            "branch",
            "batch",
            "batch_number",
            "product_name",
            "location",
            "location_code",
            "quantity",
            "assigned_by",
            "assigned_at",
            "notes",
        ]
        read_only_fields = ["assignment_id", "assigned_at"]


# ------------------------------------------------------------------------------
# ViewSets
# ------------------------------------------------------------------------------


class WarehouseViewSet(viewsets.ModelViewSet):
    """
    CRUD API for branch warehouses and storage facilities.
    Only Managers and Owners can modify facility definitions.
    """

    serializer_class = WarehouseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return Warehouse.objects.none()
        qs = Warehouse.objects.filter(tenant_id=tenant_id)
        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs.order_by("code")

    def perform_create(self, serializer):
        WarehouseLocationService.assert_warehouse_admin(self.request.user)
        user = self.request.user
        serializer.save(tenant_id=user.tenant_id)

    def perform_update(self, serializer):
        WarehouseLocationService.assert_warehouse_admin(self.request.user)
        serializer.save()

    def perform_destroy(self, instance):
        WarehouseLocationService.assert_warehouse_admin(self.request.user)
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])


class StorageLocationViewSet(viewsets.ModelViewSet):
    """
    CRUD API for Shelves, Bins, Cold-Chain Fridges, Freezers, Quarantine Zones,
    and Narcotics Vaults, including physical batch assignments and location transfers.
    """

    serializer_class = StorageLocationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return StorageLocation.objects.none()
        qs = StorageLocation.objects.filter(tenant_id=tenant_id)
        warehouse_id = self.request.query_params.get("warehouse")
        if warehouse_id:
            qs = qs.filter(warehouse_id=warehouse_id)
        location_type = self.request.query_params.get("location_type")
        if location_type:
            qs = qs.filter(location_type=location_type)
        return qs.order_by("code")

    def perform_create(self, serializer):
        WarehouseLocationService.assert_warehouse_admin(self.request.user)
        user = self.request.user
        serializer.save(tenant_id=user.tenant_id)

    def perform_update(self, serializer):
        WarehouseLocationService.assert_warehouse_admin(self.request.user)
        serializer.save()

    def perform_destroy(self, instance):
        WarehouseLocationService.assert_warehouse_admin(self.request.user)
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])

    @action(detail=True, methods=["post"], url_path="assign-batch")
    def assign_batch(self, request, pk=None):
        """Assign batch stock into this storage location."""
        location = self.get_object()
        user = request.user
        batch_id = request.data.get("batch_id")
        quantity = int(request.data.get("quantity", 0))
        notes = request.data.get("notes", "")

        supervisor_user = None
        supervisor_id = request.data.get("supervisor_user_id")
        if supervisor_id:
            from apps.identity.models import PharmacyUser
            supervisor_user = PharmacyUser.objects.filter(pk=supervisor_id).first()

        assignment = WarehouseLocationService.assign_batch_stock_location(
            tenant_id=user.tenant_id,
            branch_id=location.branch_id,
            batch_id=batch_id,
            location_id=location.location_id,
            quantity=quantity,
            actor_user=user,
            supervisor_user=supervisor_user,
            notes=notes,
        )
        return Response(
            BatchLocationAssignmentSerializer(assignment).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="transfer-batch")
    def transfer_batch(self, request):
        """Move batch stock from a source location to a target location."""
        user = request.user
        batch_id = request.data.get("batch_id")
        branch_id = request.data.get("branch_id")
        source_loc_id = request.data.get("source_location_id")
        target_loc_id = request.data.get("target_location_id")
        quantity = int(request.data.get("quantity", 0))
        notes = request.data.get("notes", "")

        supervisor_user = None
        supervisor_id = request.data.get("supervisor_user_id")
        if supervisor_id:
            from apps.identity.models import PharmacyUser
            supervisor_user = PharmacyUser.objects.filter(pk=supervisor_id).first()

        res = WarehouseLocationService.transfer_batch_location(
            tenant_id=user.tenant_id,
            branch_id=branch_id,
            batch_id=batch_id,
            source_location_id=source_loc_id,
            target_location_id=target_loc_id,
            quantity=quantity,
            actor_user=user,
            supervisor_user=supervisor_user,
            notes=notes,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="assigned-batches")
    def assigned_batches(self, request, pk=None):
        """Lists batches physically stored in this location."""
        location = self.get_object()
        assignments = BatchLocationAssignment.objects.filter(
            tenant_id=request.user.tenant_id,
            location=location,
            quantity__gt=0,
        ).select_related("batch", "batch__product")
        serializer = BatchLocationAssignmentSerializer(assignments, many=True)
        return Response(serializer.data)

