"""
backend/apps/core/stock_correction_views.py
Architecture Decision: ADR-0174 (P174).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API Endpoints for Stock Correction Restrictions,
Two-Person Approval Workflows, and Reversible Ledger Entries.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    BatchStock,
    Branch,
    PurchaseCostLot,
    StockAdjustmentRecord,
    StockAdjustmentType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_correction_service import (
    APPROVE_REVERSE_ALLOWED_ROLES,
    PROPOSE_ALLOWED_ROLES,
    StockCorrectionService,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class StockAdjustmentRecordSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)
    branch_id = serializers.CharField(source="branch.branch_id", read_only=True)
    product_id = serializers.CharField(source="medicine.product_id", read_only=True)
    batch_id = serializers.CharField(source="batch.batch_id", read_only=True)
    cost_lot_id = serializers.CharField(source="cost_lot.lot_id", read_only=True, default=None)
    proposed_by_id = serializers.CharField(source="proposed_by.user_id", read_only=True, default=None)
    approved_by_id = serializers.CharField(source="approved_by.user_id", read_only=True, default=None)
    reversal_of_id = serializers.CharField(source="reversal_of.adjustment_id", read_only=True, default=None)

    class Meta:
        model = StockAdjustmentRecord
        fields = [
            "adjustment_id",
            "tenant_id",
            "branch_id",
            "product_id",
            "batch_id",
            "cost_lot_id",
            "adjustment_type",
            "quantity_delta",
            "unit_cost_bdt",
            "total_cost_impact",
            "reason",
            "status",
            "proposed_by_id",
            "approved_by_id",
            "approved_at",
            "reversal_of_id",
            "is_reversed",
            "reversal_reason",
            "supporting_document_ref",
            "adjusted_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "adjustment_id",
            "tenant_id",
            "branch_id",
            "product_id",
            "batch_id",
            "cost_lot_id",
            "unit_cost_bdt",
            "total_cost_impact",
            "status",
            "proposed_by_id",
            "approved_by_id",
            "approved_at",
            "reversal_of_id",
            "is_reversed",
            "created_at",
            "updated_at",
        ]


class ProposeAdjustmentRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    branch_id = serializers.CharField(required=True)
    batch_id = serializers.CharField(required=True)
    cost_lot_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    adjustment_type = serializers.ChoiceField(choices=StockAdjustmentType.choices)
    quantity_delta = serializers.IntegerField(required=True)
    reason = serializers.CharField(required=True, min_length=10)
    supporting_document_ref = serializers.CharField(required=False, allow_blank=True, default="")
    auto_approve = serializers.BooleanField(required=False, default=False)

    def validate_quantity_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("Quantity delta cannot be zero.")
        return value


class RejectAdjustmentRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, min_length=10)


class ReverseAdjustmentRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, min_length=10)


class StockCorrectionViewSet(viewsets.ViewSet):
    """
    ViewSet governing audited stock corrections, approval workflows,
    and reversible ledger entries. Direct balance edits are strictly prohibited.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant(self, request: Any) -> Tenant:
        user = request.user
        tenant_id = request.data.get("tenant_id") if request.method == "POST" else request.query_params.get("tenant_id")
        tenant_id = tenant_id or getattr(user, "tenant_id", None)

        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

        if (
            getattr(user, "tenant_id", None)
            and str(user.tenant_id) != str(tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {"detail": "Cross-tenant access is strictly prohibited.", "error_code": "ERR_CROSS_TENANT_ACCESS"}
            )

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"detail": f"Tenant '{tenant_id}' not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        if getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED):
            raise PermissionDenied(
                {
                    "detail": "Tenant operations are suspended due to subscription status.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

        return tenant

    def list(self, request: Any) -> Response:
        """List stock adjustments scoped to tenant and optional branch/batch/status."""
        tenant = self._get_tenant(request)
        user = request.user
        role = getattr(user, "role", None)

        if role not in PROPOSE_ALLOWED_ROLES:
            raise PermissionDenied(
                {"detail": "Role is not authorized to view stock adjustments.", "error_code": "ERR_ROLE_UNAUTHORIZED"}
            )

        qs = StockAdjustmentRecord.objects.filter(tenant=tenant)

        branch_id = request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        elif getattr(user, "branch_id", None) and role not in (UserRole.PHARMACY_OWNER, UserRole.SUPERADMIN, "OWNER_ADMIN"):
            qs = qs.filter(branch__branch_id=user.branch_id)

        batch_id = request.query_params.get("batch_id")
        if batch_id:
            qs = qs.filter(batch__batch_id=batch_id)

        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())

        is_reversed_param = request.query_params.get("is_reversed")
        if is_reversed_param is not None:
            qs = qs.filter(is_reversed=str(is_reversed_param).lower() in ("true", "1"))

        serializer = StockAdjustmentRecordSerializer(qs[:100], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """Retrieve a single stock adjustment record."""
        tenant = self._get_tenant(request)
        try:
            record = StockAdjustmentRecord.objects.get(tenant=tenant, adjustment_id=pk)
        except StockAdjustmentRecord.DoesNotExist:
            raise NotFound(
                {"detail": f"Stock adjustment '{pk}' not found.", "error_code": "ERR_ADJUSTMENT_NOT_FOUND"}
            )
        serializer = StockAdjustmentRecordSerializer(record)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def create(self, request: Any) -> Response:
        """Create / propose a stock adjustment."""
        return self.propose(request)

    @action(detail=False, methods=["post"], url_path="propose")
    def propose(self, request: Any) -> Response:
        """Propose a reasoned inventory adjustment."""
        tenant = self._get_tenant(request)
        user = request.user

        serializer = ProposeAdjustmentRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch_id = data["branch_id"]
        try:
            branch = Branch.objects.get(tenant=tenant, branch_id=branch_id)
        except Branch.DoesNotExist:
            raise NotFound({"detail": f"Branch '{branch_id}' not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        batch_id = data["batch_id"]
        try:
            batch = BatchStock.objects.get(tenant=tenant, branch=branch, batch_id=batch_id)
        except BatchStock.DoesNotExist:
            raise NotFound(
                {
                    "detail": f"Batch '{batch_id}' not found in specified branch.",
                    "error_code": "ERR_BATCH_NOT_FOUND",
                }
            )

        cost_lot = None
        cost_lot_id = data.get("cost_lot_id")
        if cost_lot_id:
            try:
                cost_lot = PurchaseCostLot.objects.get(
                    tenant=tenant, branch=branch, batch=batch, lot_id=cost_lot_id
                )
            except PurchaseCostLot.DoesNotExist:
                raise NotFound(
                    {
                        "detail": f"Cost lot '{cost_lot_id}' not found for batch '{batch_id}'.",
                        "error_code": "ERR_COST_LOT_NOT_FOUND",
                    }
                )

        try:
            record = StockCorrectionService.propose_adjustment(
                user=user,
                tenant=tenant,
                branch=branch,
                batch=batch,
                adjustment_type=data["adjustment_type"],
                quantity_delta=data["quantity_delta"],
                reason=data["reason"],
                cost_lot=cost_lot,
                supporting_document_ref=data.get("supporting_document_ref", ""),
                auto_approve=data.get("auto_approve", False),
            )
        except DjangoValidationError as exc:
            raise ValidationError(
                {"detail": str(exc), "error_code": "ERR_VALIDATION_FAILED"}
            )

        out_serializer = StockAdjustmentRecordSerializer(record)
        return Response(out_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request: Any, pk: str = None) -> Response:
        """Approve and post a pending stock adjustment."""
        user = request.user
        role = getattr(user, "role", None)

        if role not in APPROVE_REVERSE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Branch Managers or Pharmacy Owners can approve adjustments.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

        record = StockCorrectionService.approve_and_post_adjustment(user=user, adjustment_id=pk)
        out_serializer = StockAdjustmentRecordSerializer(record)
        return Response(out_serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request: Any, pk: str = None) -> Response:
        """Reject a pending stock adjustment."""
        user = request.user
        role = getattr(user, "role", None)

        if role not in APPROVE_REVERSE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Branch Managers or Pharmacy Owners can reject adjustments.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

        serializer = RejectAdjustmentRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        record = StockCorrectionService.reject_adjustment(
            user=user,
            adjustment_id=pk,
            rejection_reason=serializer.validated_data["reason"],
        )
        out_serializer = StockAdjustmentRecordSerializer(record)
        return Response(out_serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request: Any, pk: str = None) -> Response:
        """Reverse an approved stock adjustment and restore physical and availability balances."""
        user = request.user
        role = getattr(user, "role", None)

        if role not in APPROVE_REVERSE_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Only Branch Managers or Pharmacy Owners can reverse adjustments.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

        serializer = ReverseAdjustmentRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reversal_record = StockCorrectionService.reverse_adjustment(
            user=user,
            adjustment_id=pk,
            reversal_reason=serializer.validated_data["reason"],
        )
        out_serializer = StockAdjustmentRecordSerializer(reversal_record)
        return Response(out_serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request: Any, pk: str = None) -> Response:
        """Direct balance edits and updates to historical ledger records are strictly forbidden."""
        raise MethodNotAllowed(
            "PUT",
            detail="Direct edits to stock adjustments are prohibited. Use reverse action to post compensating entries.",
        )

    def partial_update(self, request: Any, pk: str = None) -> Response:
        """Direct balance edits and updates to historical ledger records are strictly forbidden."""
        raise MethodNotAllowed(
            "PATCH",
            detail="Direct edits to stock adjustments are prohibited. Use reverse action to post compensating entries.",
        )

    def destroy(self, request: Any, pk: str = None) -> Response:
        """Direct deletion of historical ledger records is strictly forbidden."""
        raise MethodNotAllowed(
            "DELETE",
            detail="Deletion of stock adjustments is prohibited. Use reverse action to post compensating entries.",
        )
