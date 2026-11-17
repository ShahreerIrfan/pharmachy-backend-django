"""
backend/apps/core/barcode_inventory_views.py
Architecture Decision: ADR-0218 (P218).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSet for Barcode Inventory Workflows:
- Receive, Put-Away, Move, Count, and Verify workflows with bilingual operator prompts.
- Enforces storage zone policy (e.g. cold-chain in fridge).
- Multi-tenant isolation and strict role checks.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.barcode_inventory_service import BarcodeInventoryService
from apps.core.models import (
    BarcodeInventoryAuditLog,
    BarcodeWorkflowType,
    Branch,
    Tenant,
)


class BarcodeInventoryAuditLogSerializer(serializers.ModelSerializer):
    workflow_display = serializers.CharField(source="get_workflow_type_display", read_only=True)
    product_name = serializers.CharField(source="product.brand_name", read_only=True)
    batch_number_str = serializers.CharField(source="batch.batch_number", read_only=True)

    class Meta:
        model = BarcodeInventoryAuditLog
        fields = [
            "audit_id",
            "tenant_id",
            "branch_id",
            "workflow_type",
            "workflow_display",
            "scanned_barcode",
            "product_id",
            "product_name",
            "batch_number_str",
            "source_location",
            "destination_location",
            "expected_quantity",
            "scanned_quantity",
            "discrepancy",
            "operator_prompt_en",
            "operator_prompt_bn",
            "is_success",
            "error_code",
            "created_at",
        ]


class BarcodeInventoryViewSet(viewsets.ViewSet):
    """
    ViewSet for Barcode-driven inventory workflows.
    """
    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant_and_branch(self, request: Request) -> tuple[Tenant, Branch]:
        user = request.user
        BarcodeInventoryService.assert_barcode_role(user)

        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User has no tenant association."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        branch_id = request.data.get("branch") or request.query_params.get("branch") or getattr(user, "branch_id", None)
        if not branch_id:
            raise ValidationError({"branch": "Branch ID is required."})

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise NotFound({"detail": f"Branch '{branch_id}' not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        return tenant, branch

    @action(detail=False, methods=["get"], url_path="audit-logs")
    def audit_logs(self, request: Request) -> Response:
        tenant, branch = self._resolve_tenant_and_branch(request)
        qs = BarcodeInventoryAuditLog.objects.filter(tenant=tenant, branch=branch).select_related("product", "batch")[:50]
        serializer = BarcodeInventoryAuditLogSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="receive")
    def receive(self, request: Request) -> Response:
        tenant, branch = self._resolve_tenant_and_branch(request)
        barcode = request.data.get("barcode")
        if not barcode:
            raise ValidationError({"barcode": "Barcode string is required."})

        result = BarcodeInventoryService.execute_receive(tenant, branch, barcode, user=request.user)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="put-away")
    def put_away(self, request: Request) -> Response:
        tenant, branch = self._resolve_tenant_and_branch(request)
        batch_number = request.data.get("batch_number") or request.data.get("barcode")
        destination_location = request.data.get("destination_location") or request.data.get("location")

        if not batch_number:
            raise ValidationError({"batch_number": "Batch number or barcode is required."})
        if not destination_location:
            raise ValidationError({"destination_location": "Destination location barcode is required."})

        result = BarcodeInventoryService.execute_put_away(
            tenant, branch, batch_number, destination_location, user=request.user
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="move")
    def move(self, request: Request) -> Response:
        tenant, branch = self._resolve_tenant_and_branch(request)
        batch_number = request.data.get("batch_number")
        from_location = request.data.get("from_location", "")
        to_location = request.data.get("to_location")

        if not batch_number:
            raise ValidationError({"batch_number": "Batch number is required."})
        if not to_location:
            raise ValidationError({"to_location": "Target location is required."})

        result = BarcodeInventoryService.execute_move(
            tenant, branch, batch_number, from_location, to_location, user=request.user
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="count")
    def count(self, request: Request) -> Response:
        tenant, branch = self._resolve_tenant_and_branch(request)
        barcode = request.data.get("barcode")
        location = request.data.get("location", "")
        counted_quantity = request.data.get("counted_quantity")

        if not barcode:
            raise ValidationError({"barcode": "Barcode is required."})
        if counted_quantity is None:
            raise ValidationError({"counted_quantity": "Counted quantity is required."})

        try:
            qty = int(counted_quantity)
            if qty < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError({"counted_quantity": "Counted quantity must be a non-negative integer."})

        result = BarcodeInventoryService.execute_count(
            tenant, branch, barcode, location, qty, user=request.user
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="verify")
    def verify(self, request: Request) -> Response:
        tenant, branch = self._resolve_tenant_and_branch(request)
        barcode = request.data.get("barcode")
        if not barcode:
            raise ValidationError({"barcode": "Barcode is required."})

        result = BarcodeInventoryService.execute_verify(tenant, branch, barcode, user=request.user)
        return Response(result, status=status.HTTP_200_OK)
