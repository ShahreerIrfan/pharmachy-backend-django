"""
backend/apps/core/stock_movement_correction_views.py
Architecture Decision: ADR-0217 (P217).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSet for Stock Movement Corrections:
- Reverses incorrect posted inventory moves using auditable compensating entries.
- Bars direct row overwrites and deletions on StockLedgerEntry.
- Enforces double-reversal prevention and negative stock guards.
- Restricts Cashiers (HTTP 403) and isolates tenants (HTTP 404).
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import (
    Branch,
    MovementCorrectionReason,
    MovementCorrectionStatus,
    StockMovementCorrection,
    Tenant,
)
from apps.core.stock_movement_correction_service import StockMovementCorrectionService


class StockMovementCorrectionSerializer(serializers.ModelSerializer):
    reason_display = serializers.CharField(source="get_correction_reason_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    product_name = serializers.CharField(source="product.brand_name", read_only=True)
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True)
    original_entry_id = serializers.CharField(source="original_entry.entry_id", read_only=True)
    compensating_entry_id = serializers.CharField(source="compensating_entry.entry_id", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = StockMovementCorrection
        fields = [
            "correction_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "original_entry_id",
            "compensating_entry_id",
            "product_id",
            "product_name",
            "batch_number",
            "correction_reason",
            "reason_display",
            "explanation",
            "reversed_quantity_delta",
            "unit_cost_bdt",
            "total_cost_impact_bdt",
            "status",
            "status_display",
            "created_at",
        ]
        read_only_fields = fields


class CreateMovementCorrectionRequestSerializer(serializers.Serializer):
    branch = serializers.CharField(required=True, help_text="Branch ID where original entry was posted")
    original_entry = serializers.CharField(required=True, help_text="StockLedgerEntry ID to reverse")
    reason = serializers.ChoiceField(
        choices=MovementCorrectionReason.choices,
        default=MovementCorrectionReason.MISTYPED_QUANTITY,
    )
    explanation = serializers.CharField(required=True, min_length=5, help_text="Mandatory detailed reason")


class StockMovementCorrectionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for viewing and executing auditable stock movement corrections.
    """
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "correction_id"
    serializer_class = StockMovementCorrectionSerializer

    def get_queryset(self):
        user = self.request.user
        StockMovementCorrectionService.assert_correction_role(user)

        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return StockMovementCorrection.objects.none()

        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        qs = (
            StockMovementCorrection.objects.filter(tenant=tenant)
            .select_related("branch", "product", "batch", "original_entry", "compensating_entry")
            .order_by("-created_at")
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        return qs

    def create(self, request: Request) -> Response:
        """
        Executes a compensating reversal for an erroneous posted StockLedgerEntry.
        POST /api/v2/inventory/movement-corrections/
        """
        user = request.user
        StockMovementCorrectionService.assert_correction_role(user)

        branch_id = request.data.get("branch")
        if not branch_id:
            raise ValidationError({"branch": "Branch ID is required."})

        original_entry_id = request.data.get("original_entry")
        if not original_entry_id:
            raise ValidationError({"original_entry": "Original StockLedgerEntry ID is required."})

        reason = request.data.get("reason", MovementCorrectionReason.MISTYPED_QUANTITY)
        if reason not in MovementCorrectionReason.values:
            raise ValidationError({"reason": f"Invalid correction reason '{reason}'."})

        explanation = request.data.get("explanation", "")
        if not explanation or len(explanation.strip()) < 5:
            raise ValidationError({"explanation": "A descriptive explanation of at least 5 characters is required."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User must have a tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise NotFound({"detail": f"Branch '{branch_id}' not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        correction = StockMovementCorrectionService.execute_compensating_correction(
            tenant=tenant,
            branch=branch,
            original_entry_id=original_entry_id,
            reason=reason,
            explanation=explanation,
            user=user,
        )

        serializer = StockMovementCorrectionSerializer(correction)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PUT", detail="Stock movement corrections are immutable financial records.")

    def partial_update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PATCH", detail="Stock movement corrections are immutable financial records.")

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="Stock movement corrections are immutable financial records.")
