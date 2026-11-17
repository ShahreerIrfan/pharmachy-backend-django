"""
backend/apps/core/supply_chain_alert_views.py
Architecture Decision: ADR-0214 (P214).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Supply Chain Stock Alerts:
- Real-time stockout, reorder, overstock, negative availability, and temperature risk notifications.
- Automatic deduplication with occurrence counters.
- Actions: acknowledge, resolve, dismiss, scan branch inventory.
- RBAC: Cashiers prohibited from managing alerts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework import serializers

from apps.core.models import (
    Branch,
    MedicineProduct,
    StorageLocation,
    SupplyChainStockAlert,
    Tenant,
)
from apps.core.supply_chain_alert_service import SupplyChainAlertService


class SupplyChainStockAlertSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True, default=None)
    product_generic = serializers.CharField(source="product.generic_name", read_only=True, default=None)
    storage_location_name = serializers.CharField(source="storage_location.name", read_only=True, default=None)
    storage_location_code = serializers.CharField(source="storage_location.code", read_only=True, default=None)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    acknowledged_by_name = serializers.CharField(source="acknowledged_by.email", read_only=True, default=None)
    resolved_by_name = serializers.CharField(source="resolved_by.email", read_only=True, default=None)

    class Meta:
        model = SupplyChainStockAlert
        fields = [
            "id",
            "tenant",
            "branch",
            "branch_name",
            "product",
            "product_name",
            "product_generic",
            "storage_location",
            "storage_location_name",
            "storage_location_code",
            "alert_type",
            "severity",
            "status",
            "title",
            "message",
            "message_bn",
            "current_quantity",
            "threshold_quantity",
            "current_temperature",
            "dedup_fingerprint",
            "occurred_count",
            "first_triggered_at",
            "last_triggered_at",
            "acknowledged_by",
            "acknowledged_by_name",
            "acknowledged_at",
            "resolved_by",
            "resolved_by_name",
            "resolved_at",
            "resolution_notes",
        ]
        read_only_fields = fields


class SupplyChainAlertViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing and managing supply chain inventory alerts.
    """
    serializer_class = SupplyChainStockAlertSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return SupplyChainStockAlert.objects.none()

        qs = SupplyChainStockAlert.objects.filter(tenant=tenant_id).select_related(
            "product", "storage_location", "branch", "acknowledged_by", "resolved_by"
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        alert_status = self.request.query_params.get("status")
        if alert_status:
            qs = qs.filter(status=alert_status)

        alert_type = self.request.query_params.get("alert_type")
        if alert_type:
            qs = qs.filter(alert_type=alert_type)

        severity = self.request.query_params.get("severity")
        if severity:
            qs = qs.filter(severity=severity)

        return qs

    @action(detail=False, methods=["post"], url_path="scan")
    def scan_branch(self, request):
        """
        Triggers a full branch inventory scan to evaluate and deduplicate stock alerts.
        """
        SupplyChainAlertService.assert_alert_management_role(request.user)

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

        result = SupplyChainAlertService.scan_branch_inventory(tenant=tenant, branch=branch)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="evaluate")
    def evaluate(self, request):
        """
        Evaluates a specific product stock level or storage temperature reading.
        """
        SupplyChainAlertService.assert_alert_management_role(request.user)

        tenant_id = getattr(request.user, "tenant_id", None) or getattr(request.user, "tenant", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User must have a tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        data = request.data
        branch_id = data.get("branch") or getattr(request.user, "branch_id", None)
        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise NotFound(f"Branch {branch_id} not found.")

        # Product evaluation
        product_id = data.get("product_id")
        if product_id:
            try:
                product = MedicineProduct.objects.get(pk=product_id)
            except MedicineProduct.DoesNotExist:
                raise NotFound(f"Product {product_id} not found.")

            qty = int(data.get("quantity_on_hand", 0))
            reorder = int(data.get("reorder_level", 0))
            max_lvl = data.get("max_level")
            max_val = int(max_lvl) if max_lvl is not None else None

            alert = SupplyChainAlertService.evaluate_stock_level(
                tenant=tenant,
                branch=branch,
                product=product,
                quantity_on_hand=qty,
                reorder_level=reorder,
                max_level=max_val,
            )
            if alert:
                serializer = self.get_serializer(alert)
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response({"status": "OK", "message": "Stock level is safe and compliant."}, status=status.HTTP_200_OK)

        # Temperature evaluation
        storage_location_id = data.get("storage_location_id")
        if storage_location_id:
            try:
                loc = StorageLocation.objects.get(pk=storage_location_id, tenant=tenant)
            except StorageLocation.DoesNotExist:
                raise NotFound(f"StorageLocation {storage_location_id} not found.")

            temp = Decimal(str(data.get("temperature", "4.00")))
            min_t = Decimal(str(data.get("min_threshold", "2.00")))
            max_t = Decimal(str(data.get("max_threshold", "8.00")))

            alert = SupplyChainAlertService.evaluate_temperature_risk(
                tenant=tenant,
                branch=branch,
                storage_location=loc,
                recorded_temp=temp,
                min_threshold=min_t,
                max_threshold=max_t,
            )
            if alert:
                serializer = self.get_serializer(alert)
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response({"status": "OK", "message": "Temperature is compliant."}, status=status.HTTP_200_OK)

        raise ValidationError({"error": "Either product_id or storage_location_id must be provided."})

    @action(detail=True, methods=["post"], url_path="acknowledge")
    def acknowledge(self, request, pk=None):
        alert = self.get_object()
        notes = request.data.get("notes", "")
        updated = SupplyChainAlertService.acknowledge_alert(alert=alert, actor_user=request.user, notes=notes)
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="resolve")
    def resolve(self, request, pk=None):
        alert = self.get_object()
        notes = request.data.get("resolution_notes", "")
        updated = SupplyChainAlertService.resolve_alert(alert=alert, actor_user=request.user, resolution_notes=notes)
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="dismiss")
    def dismiss(self, request, pk=None):
        alert = self.get_object()
        notes = request.data.get("notes", "")
        updated = SupplyChainAlertService.dismiss_alert(alert=alert, actor_user=request.user, notes=notes)
        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)
