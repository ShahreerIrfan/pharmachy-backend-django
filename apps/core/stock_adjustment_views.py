"""
backend/apps/core/stock_adjustment_views.py
Architecture Decision: ADR-0205 (P205).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Nontrivial Adjustment Approval Flow:
- Mandatory documented reason codes.
- Maker-Checker anti-self-approval enforcement.
- Immutable difference events with SHA-256 cryptographic verification hashes.
- Audited reversal workflow and Stock Movement Ledger integration.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    StockAdjustmentDifferenceEvent,
    StockAdjustmentReasonCode,
    StockAdjustmentRecord,
    StockCorrectionStatus,
)
from apps.core.stock_adjustment_service import StockAdjustmentService


class StockAdjustmentDifferenceEventSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)

    class Meta:
        model = StockAdjustmentDifferenceEvent
        fields = [
            "event_id",
            "branch",
            "adjustment",
            "product",
            "product_name",
            "batch",
            "batch_number",
            "pre_adjustment_quantity",
            "post_adjustment_quantity",
            "quantity_delta",
            "unit_cost_bdt",
            "total_cost_impact_bdt",
            "is_nontrivial",
            "reason_code",
            "reason_text",
            "proposed_by",
            "approved_by",
            "ledger_entry_id",
            "immutable_event_hash",
            "created_at",
        ]
        read_only_fields = fields


class StockAdjustmentRecordSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source="medicine.brand_name", read_only=True)
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True)
    difference_events = StockAdjustmentDifferenceEventSerializer(many=True, read_only=True)

    class Meta:
        model = StockAdjustmentRecord
        fields = [
            "adjustment_id",
            "branch",
            "medicine",
            "medicine_name",
            "batch",
            "batch_number",
            "adjustment_type",
            "quantity_delta",
            "unit_cost_bdt",
            "total_cost_impact",
            "reason",
            "reason_code",
            "is_nontrivial",
            "status",
            "proposed_by",
            "approved_by",
            "approved_at",
            "ledger_entry_id",
            "reversal_of",
            "is_reversed",
            "reversal_reason",
            "supporting_document_ref",
            "adjusted_by_username",
            "difference_events",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "adjustment_id",
            "total_cost_impact",
            "is_nontrivial",
            "status",
            "proposed_by",
            "approved_by",
            "approved_at",
            "ledger_entry_id",
            "reversal_of",
            "is_reversed",
            "reversal_reason",
            "adjusted_by_username",
            "created_at",
            "updated_at",
        ]


class StockAdjustmentViewSet(viewsets.ModelViewSet):
    """
    CRUD and Maker-Checker Workflow ViewSet for Stock Adjustments.
    """

    serializer_class = StockAdjustmentRecordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return StockAdjustmentRecord.objects.none()

        qs = (
            StockAdjustmentRecord.objects.filter(tenant_id=tenant_id)
            .select_related("medicine", "batch", "proposed_by", "approved_by")
            .prefetch_related("difference_events")
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        reason_code = self.request.query_params.get("reason_code")
        if reason_code:
            qs = qs.filter(reason_code=reason_code)

        is_nontrivial = self.request.query_params.get("is_nontrivial")
        if is_nontrivial is not None:
            qs = qs.filter(is_nontrivial=(str(is_nontrivial).lower() in ("true", "1")))

        return qs.order_by("-created_at")

    @action(detail=False, methods=["post"], url_path="propose")
    def propose(self, request):
        """
        Propose a stock adjustment.
        Payload:
        {
            "branch": "branch_id",
            "batch": "batch_id",
            "quantity_delta": -12,
            "reason_code": "EXPIRY_SPOILAGE",
            "reason_notes": "Expired ampoules identified during quarterly stock check.",
            "supporting_document_ref": "DOC-EXP-2026-09"
        }
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied("User does not have an active tenant.")

        branch_id = request.data.get("branch") or getattr(user, "branch_id", None)
        batch_id = request.data.get("batch")
        quantity_delta = request.data.get("quantity_delta")
        reason_code = request.data.get("reason_code", StockAdjustmentReasonCode.AUDIT_COUNT_VARIANCE)
        reason_notes = request.data.get("reason_notes", request.data.get("reason", ""))
        supporting_doc = request.data.get("supporting_document_ref", "")

        if not branch_id or not batch_id or quantity_delta is None:
            raise ValidationError(
                {"error": "branch, batch, and quantity_delta are mandatory fields."}
            )

        try:
            quantity_delta_int = int(quantity_delta)
        except (ValueError, TypeError):
            raise ValidationError({"quantity_delta": "quantity_delta must be an integer."})

        record = StockAdjustmentService.propose_adjustment(
            tenant_id=tenant_id,
            branch_id=branch_id,
            batch_id=batch_id,
            quantity_delta=quantity_delta_int,
            reason_code=reason_code,
            reason_notes=reason_notes,
            supporting_document_ref=supporting_doc,
            actor_user=user,
        )

        serializer = self.get_serializer(record)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """
        Approve proposed adjustment. Enforces Maker-Checker separation for nontrivial adjustments.
        """
        adjustment = self.get_object()
        user = request.user
        checker_notes = request.data.get("checker_notes", "")

        approved_record = StockAdjustmentService.approve_and_post_adjustment(
            adjustment_id=adjustment.adjustment_id,
            tenant_id=adjustment.tenant_id,
            checker_user=user,
            checker_notes=checker_notes,
        )

        serializer = self.get_serializer(approved_record)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """
        Reject proposed adjustment.
        """
        adjustment = self.get_object()
        user = request.user
        rejection_reason = request.data.get("rejection_reason", "")

        rejected_record = StockAdjustmentService.reject_adjustment(
            adjustment_id=adjustment.adjustment_id,
            tenant_id=adjustment.tenant_id,
            rejection_reason=rejection_reason,
            checker_user=user,
        )

        serializer = self.get_serializer(rejected_record)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request, pk=None):
        """
        Reverse approved adjustment with offsetting ledger entry and difference event.
        """
        adjustment = self.get_object()
        user = request.user
        reversal_reason = request.data.get("reversal_reason", "")

        reversal_record = StockAdjustmentService.reverse_approved_adjustment(
            adjustment_id=adjustment.adjustment_id,
            tenant_id=adjustment.tenant_id,
            reversal_reason=reversal_reason,
            actor_user=user,
        )

        serializer = self.get_serializer(reversal_record)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="difference-events")
    def difference_events(self, request, pk=None):
        """
        Returns immutable difference events associated with this adjustment.
        """
        adjustment = self.get_object()
        events = StockAdjustmentDifferenceEvent.objects.filter(
            tenant_id=adjustment.tenant_id,
            adjustment=adjustment,
        ).order_by("-created_at")
        serializer = StockAdjustmentDifferenceEventSerializer(events, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="all-difference-events")
    def all_difference_events(self, request):
        """
        Returns all difference events for tenant audit.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return Response([])

        events = StockAdjustmentDifferenceEvent.objects.filter(tenant_id=tenant_id)
        branch_id = request.query_params.get("branch")
        if branch_id:
            events = events.filter(branch_id=branch_id)

        serializer = StockAdjustmentDifferenceEventSerializer(events[:100], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

