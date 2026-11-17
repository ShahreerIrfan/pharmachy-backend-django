"""
backend/apps/core/inventory_fault_stress_views.py
Architecture Decision: ADR-0220 (P220).
Phase 11 Capstone: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSet exposing Phase 11 Fault, Stress, Concurrency & Conservation Verification APIs:
- GET  /api/v2/inventory/qa-stress/
- GET  /api/v2/inventory/qa-stress/{run_id}/
- POST /api/v2/inventory/qa-stress/verify-conservation/
- POST /api/v2/inventory/qa-stress/run-high-volume/
- POST /api/v2/inventory/qa-stress/run-concurrency/
- POST /api/v2/inventory/qa-stress/verify-fault-isolation/
- PUT/PATCH/DELETE barred with HTTP 405 Method Not Allowed (immutable audit evidence).
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.inventory_fault_stress_service import InventoryFaultStressService
from apps.core.models import InventoryFaultStressRun, Tenant


class InventoryFaultStressRunSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True, default="")
    test_type_display = serializers.CharField(source="get_test_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    executed_by_email = serializers.CharField(source="executed_by.email", read_only=True, default=None)

    class Meta:
        model = InventoryFaultStressRun
        fields = [
            "run_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "test_type",
            "test_type_display",
            "status",
            "status_display",
            "total_operations",
            "successful_operations",
            "rejected_faults",
            "initial_stock_units",
            "final_stock_units",
            "net_ledger_delta_units",
            "conservation_verified",
            "branch_isolation_verified",
            "execution_time_ms",
            "metrics_payload",
            "verification_hash",
            "executed_by_email",
            "executed_at",
        ]
        read_only_fields = fields


class InventoryFaultStressViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Immutable verification audit endpoint for Phase 11 Fault & Stress testing.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = InventoryFaultStressRunSerializer
    lookup_field = "run_id"

    def get_queryset(self):
        user = self.request.user
        InventoryFaultStressService.assert_qa_authority(user)

        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return InventoryFaultStressRun.objects.none()

        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id
        InventoryFaultStressService.assert_subscription_active(tenant)

        qs = (
            InventoryFaultStressRun.objects.filter(tenant=tenant)
            .select_related("branch", "executed_by")
            .order_by("-executed_at", "-run_id")
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        test_type = self.request.query_params.get("test_type")
        if test_type:
            qs = qs.filter(test_type=test_type)

        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        return qs

    @action(detail=False, methods=["post"], url_path="verify-conservation")
    def verify_conservation(self, request: Request) -> Response:
        """POST /api/v2/inventory/qa-stress/verify-conservation/"""
        branch_id = request.data.get("branch") or getattr(request.user, "branch_id", None)
        dest_branch_id = request.data.get("dest_branch")
        try:
            initial_units = int(request.data.get("initial_units", 200))
        except (TypeError, ValueError):
            raise ValidationError({"initial_units": "initial_units must be an integer."})

        run = InventoryFaultStressService.verify_conservation_pipeline(
            user=request.user,
            branch_id=branch_id,
            dest_branch_id=dest_branch_id,
            initial_units=initial_units,
        )
        return Response(InventoryFaultStressRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="run-high-volume")
    def run_high_volume(self, request: Request) -> Response:
        """POST /api/v2/inventory/qa-stress/run-high-volume/"""
        branch_id = request.data.get("branch") or getattr(request.user, "branch_id", None)
        try:
            operation_count = int(request.data.get("operation_count", 100))
        except (TypeError, ValueError):
            raise ValidationError({"operation_count": "operation_count must be an integer."})

        run = InventoryFaultStressService.run_high_volume_stress(
            user=request.user,
            branch_id=branch_id,
            operation_count=operation_count,
        )
        return Response(InventoryFaultStressRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="run-concurrency")
    def run_concurrency(self, request: Request) -> Response:
        """POST /api/v2/inventory/qa-stress/run-concurrency/"""
        branch_id = request.data.get("branch") or getattr(request.user, "branch_id", None)
        try:
            initial_units = int(request.data.get("initial_units", 50))
            concurrency_level = int(request.data.get("concurrency_level", 20))
            units_per_request = int(request.data.get("units_per_request", 5))
        except (TypeError, ValueError):
            raise ValidationError({"detail": "Concurrency stress parameters must be integers."})

        run = InventoryFaultStressService.run_concurrent_mutation_stress(
            user=request.user,
            branch_id=branch_id,
            initial_units=initial_units,
            concurrency_level=concurrency_level,
            units_per_request=units_per_request,
        )
        return Response(InventoryFaultStressRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="verify-fault-isolation")
    def verify_fault_isolation(self, request: Request) -> Response:
        """POST /api/v2/inventory/qa-stress/verify-fault-isolation/"""
        branch_id = request.data.get("branch") or getattr(request.user, "branch_id", None)
        sibling_branch_id = request.data.get("sibling_branch")
        run = InventoryFaultStressService.verify_fault_and_isolation_gate(
            user=request.user,
            branch_id=branch_id,
            sibling_branch_id=sibling_branch_id,
        )
        return Response(InventoryFaultStressRunSerializer(run).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PUT", detail="Inventory fault and stress verification runs are immutable.")

    def partial_update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PATCH", detail="Inventory fault and stress verification runs are immutable.")

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="Inventory fault and stress verification runs are immutable.")

