"""
Supplier Operations Dashboard REST API Views (ADR-0199 / P199)
Exposes endpoints for dynamic dashboard metrics and formal auditable snapshots.
"""

from typing import Any
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from apps.core.models import SupplierDashboardSnapshot
from apps.core.supplier_dashboard_service import SupplierDashboardService


class SupplierDashboardViewSet(viewsets.ViewSet):
    """
    REST API ViewSet serving supplier operations dashboard metrics and snapshots.
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
                    "detail": "Tenant identification is required.",
                }
            )
        return str(tenant_id)

    def _serialize_snapshot(self, snap: SupplierDashboardSnapshot, include_metrics: bool = False) -> dict[str, Any]:
        data = {
            "snapshot_id": snap.snapshot_id,
            "snapshot_date": str(snap.snapshot_date),
            "branch_id": snap.branch_id,
            "open_orders_count": snap.open_orders_count,
            "open_orders_value": str(snap.open_orders_value),
            "overdue_deliveries_count": snap.overdue_deliveries_count,
            "overdue_deliveries_value": str(snap.overdue_deliveries_value),
            "total_due_balance": str(snap.total_due_balance),
            "overdue_due_balance": str(snap.overdue_due_balance),
            "low_stock_opportunities_count": snap.low_stock_opportunities_count,
            "estimated_reorder_cost": str(snap.estimated_reorder_cost),
            "active_suppliers_count": snap.active_suppliers_count,
            "immutable_verification_hash": snap.immutable_verification_hash,
            "notes": snap.notes,
            "generated_by_id": str(snap.generated_by_id) if snap.generated_by_id else None,
            "generated_at": snap.generated_at.isoformat() if snap.generated_at else None,
        }
        if include_metrics:
            data["dashboard_metrics"] = snap.dashboard_metrics
        return data

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request: Request) -> Response:
        """
        Dynamically computes real-time supplier operations dashboard summary.
        Query params: branch_id (optional), supplier_id (optional)
        """
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        supplier_id = request.query_params.get("supplier_id")

        summary_data = SupplierDashboardService.get_dashboard_summary(
            tenant_id=tenant_id,
            actor_user=request.user,
            branch_id=branch_id,
            supplier_id=supplier_id,
        )
        return Response(summary_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="snapshot")
    def create_snapshot(self, request: Request) -> Response:
        """
        Persists a formal audited dashboard snapshot with SHA-256 hash.
        Body: branch_id (optional), notes (optional)
        """
        tenant_id = self._get_tenant_id(request)
        branch_id = request.data.get("branch_id")
        notes = request.data.get("notes", "")

        snapshot = SupplierDashboardService.create_dashboard_snapshot(
            tenant_id=tenant_id,
            actor_user=request.user,
            branch_id=branch_id,
            notes=notes,
        )
        return Response(self._serialize_snapshot(snapshot, include_metrics=True), status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="snapshots")
    def list_snapshots(self, request: Request) -> Response:
        """
        Lists saved dashboard snapshots for the tenant/branch.
        Query params: branch_id (optional)
        """
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")

        snapshots = SupplierDashboardService.list_snapshots(
            tenant_id=tenant_id,
            actor_user=request.user,
            branch_id=branch_id,
        )
        return Response([self._serialize_snapshot(s) for s in snapshots], status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """
        Retrieves a single dashboard snapshot with its full detailed JSON metrics.
        """
        tenant_id = self._get_tenant_id(request)
        snapshot = SupplierDashboardService.get_snapshot(
            tenant_id=tenant_id,
            actor_user=request.user,
            snapshot_id=pk,
        )
        return Response(self._serialize_snapshot(snapshot, include_metrics=True), status=status.HTTP_200_OK)

