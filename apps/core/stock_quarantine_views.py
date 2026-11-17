"""
backend/apps/core/stock_quarantine_views.py
Architecture Decision: ADR-0206 (P206).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Expired Stock Quarantine:
- Quarantine batch manual & automated sweeps.
- Prohibit sale or reservation validation endpoint.
- Disposal / supplier return / destruction workflows.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    BatchStock,
    ExpiredStockQuarantineReason,
    ExpiredStockQuarantineRecord,
    ExpiredStockQuarantineStatus,
)
from apps.core.stock_quarantine_service import ExpiredStockQuarantineService


class ExpiredStockQuarantineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)
    quarantined_by_email = serializers.EmailField(source="quarantined_by.email", read_only=True)
    actioned_by_email = serializers.EmailField(source="actioned_by.email", read_only=True)

    class Meta:
        model = ExpiredStockQuarantineRecord
        fields = [
            "quarantine_id",
            "branch",
            "batch",
            "product",
            "product_name",
            "batch_number",
            "quarantined_quantity",
            "unit_cost_bdt",
            "total_cost_impact",
            "quarantine_reason",
            "status",
            "quarantine_notes",
            "quarantine_location",
            "quarantined_by",
            "quarantined_by_email",
            "quarantined_at",
            "actioned_by",
            "actioned_by_email",
            "actioned_at",
            "action_notes",
            "disposal_certificate_ref",
            "ledger_entry_id",
            "immutable_hash",
            "updated_at",
        ]
        read_only_fields = [
            "quarantine_id",
            "batch_number",
            "quarantined_quantity",
            "unit_cost_bdt",
            "total_cost_impact",
            "status",
            "quarantined_by",
            "quarantined_at",
            "actioned_by",
            "actioned_at",
            "ledger_entry_id",
            "immutable_hash",
            "updated_at",
        ]


class ExpiredStockQuarantineViewSet(viewsets.ModelViewSet):
    """
    CRUD and Operational Workflows for Expired and Defective Stock Quarantine.
    """

    serializer_class = ExpiredStockQuarantineSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return ExpiredStockQuarantineRecord.objects.none()

        qs = ExpiredStockQuarantineRecord.objects.filter(tenant_id=tenant_id).select_related(
            "product", "batch", "quarantined_by", "actioned_by"
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        reason_param = self.request.query_params.get("reason")
        if reason_param:
            qs = qs.filter(quarantine_reason=reason_param)

        return qs.order_by("-quarantined_at")

    @action(detail=False, methods=["post"], url_path="quarantine-batch")
    def quarantine_batch(self, request):
        """
        Manually quarantines a batch.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        branch_id = request.data.get("branch") or getattr(user, "branch_id", None)
        batch_id = request.data.get("batch")
        reason = request.data.get("reason", ExpiredStockQuarantineReason.BATCH_EXPIRED)
        notes = request.data.get("notes", "")
        location = request.data.get("quarantine_location", "QUARANTINE-ZONE")

        if not branch_id or not batch_id:
            raise ValidationError({"error": "branch and batch are required."})

        record = ExpiredStockQuarantineService.quarantine_batch(
            tenant_id=tenant_id,
            branch_id=branch_id,
            batch_id=batch_id,
            reason=reason,
            notes=notes,
            quarantine_location=location,
            actor_user=user,
        )
        return Response(self.get_serializer(record).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="sweep-expired")
    def sweep_expired(self, request):
        """
        Automated sweep: identifies all expired batches in the branch and quarantines them.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        branch_id = request.data.get("branch") or getattr(user, "branch_id", None)

        if not branch_id:
            raise ValidationError({"branch": "Branch ID is required for sweep."})

        quarantined = ExpiredStockQuarantineService.sweep_and_quarantine_expired_batches(
            tenant_id=tenant_id,
            branch_id=branch_id,
            actor_user=user,
        )
        serializer = self.get_serializer(quarantined, many=True)
        return Response(
            {"swept_count": len(quarantined), "records": serializer.data},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="approve-disposal")
    def approve_disposal(self, request, pk=None):
        """
        Approve disposal, vendor return, or destruction of quarantined batch.
        """
        user = request.user
        action_type = request.data.get("action_type", ExpiredStockQuarantineStatus.DISPOSED)
        cert_ref = request.data.get("disposal_certificate_ref", "")
        action_notes = request.data.get("action_notes", "")

        record = ExpiredStockQuarantineService.approve_disposal_or_return(
            quarantine_id=pk,
            tenant_id=user.tenant_id,
            action_type=action_type,
            disposal_certificate_ref=cert_ref,
            action_notes=action_notes,
            actor_user=user,
        )
        return Response(self.get_serializer(record).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, pk=None):
        """
        Release a quarantined batch back to active inventory (only if NOT expired).
        """
        user = request.user
        release_reason = request.data.get("release_reason", "")
        new_loc = request.data.get("storage_location", "SHELF-ACTIVE")

        record = ExpiredStockQuarantineService.release_quarantine(
            quarantine_id=pk,
            tenant_id=user.tenant_id,
            release_reason=release_reason,
            new_storage_location=new_loc,
            actor_user=user,
        )
        return Response(self.get_serializer(record).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-sale")
    def validate_sale(self, request):
        """
        Pre-dispense POS check: prohibits sale of expired or quarantined batches.
        """
        user = request.user
        batch_id = request.data.get("batch")
        if not batch_id:
            raise ValidationError({"batch": "Batch ID is required."})

        batch = BatchStock.objects.filter(tenant_id=user.tenant_id, batch_id=batch_id).first()
        if not batch:
            raise ValidationError({"batch": "Batch not found."})

        ExpiredStockQuarantineService.assert_sale_allowed(batch)
        return Response({"status": "SALE_PERMITTED", "batch_id": batch.batch_id, "is_dispensable": True})

