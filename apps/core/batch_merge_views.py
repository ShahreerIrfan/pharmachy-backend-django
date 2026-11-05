"""
backend/apps/core/batch_merge_views.py
Architecture Decision: ADR-0178 (P178).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API Endpoints for Batch Merge Pre-Flight Compatibility Validation,
Consolidation Execution, and Audited Reversals.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.batch_merge_service import ALLOWED_MERGE_ROLES, BatchMergeService
from apps.core.models import BatchMergeRecord, BatchStock, Tenant
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class BatchMergeRecordSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True, default="")
    batch_number = serializers.CharField(source="target_batch.batch_number", read_only=True, default="")
    merged_by_name = serializers.CharField(source="merged_by.full_name", read_only=True, default="")

    class Meta:
        model = BatchMergeRecord
        fields = [
            "merge_id",
            "tenant_id",
            "branch_id",
            "product_id",
            "product_name",
            "source_batch_id",
            "target_batch_id",
            "batch_number",
            "merged_quantity_base_units",
            "source_cost_per_unit_before",
            "target_cost_per_unit_before",
            "target_cost_per_unit_after",
            "reason",
            "merged_by_id",
            "merged_by_name",
            "is_reversed",
            "reversal_reason",
            "reversed_at",
            "created_at",
            "updated_at",
        ]


class ExecuteBatchMergeRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    branch_id = serializers.CharField(required=False, allow_blank=True)
    source_batch_id = serializers.CharField(required=True)
    target_batch_id = serializers.CharField(required=True)
    reason = serializers.CharField(required=True, min_length=10)


class ValidateBatchMergeRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    branch_id = serializers.CharField(required=False, allow_blank=True)
    source_batch_id = serializers.CharField(required=True)
    target_batch_id = serializers.CharField(required=True)


class ReverseBatchMergeRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, min_length=10)


class BatchMergeViewSet(viewsets.ViewSet):
    """
    REST API governing pharmaceutical batch merges, compatibility validation,
    and audited ledger reversals. Direct mutation or deletion is barred (HTTP 405).
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant(self, request: Any) -> Tenant:
        user = request.user
        tenant_id = (
            request.data.get("tenant_id") if request.method == "POST" else request.query_params.get("tenant_id")
        )
        tenant_id = tenant_id or getattr(user, "tenant_id", None)

        if not tenant_id:
            raise ValidationError({"detail": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"})

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

        return tenant

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_merge(self, request: Any) -> Response:
        """Pre-flight compatibility dry run without database mutation."""
        self._get_tenant(request)
        serializer = ValidateBatchMergeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        source_id = serializer.validated_data["source_batch_id"]
        target_id = serializer.validated_data["target_batch_id"]

        source_batch = BatchStock.objects.select_related("tenant", "branch", "product").filter(batch_id=source_id).first()
        if not source_batch:
            raise NotFound({"detail": f"Source batch '{source_id}' not found.", "error_code": "ERR_SOURCE_BATCH_NOT_FOUND"})

        target_batch = BatchStock.objects.select_related("tenant", "branch", "product").filter(batch_id=target_id).first()
        if not target_batch:
            raise NotFound({"detail": f"Target batch '{target_id}' not found.", "error_code": "ERR_TARGET_BATCH_NOT_FOUND"})

        result = BatchMergeService.validate_merge_compatibility(source_batch, target_batch, raise_exception=False)
        return Response(result, status=status.HTTP_200_OK)

    def create(self, request: Any) -> Response:
        """Execute an audited batch merge."""
        self._get_tenant(request)
        user = request.user

        if getattr(user, "role", None) not in ALLOWED_MERGE_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Batch consolidation and merge authorization requires a branch manager or owner.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

        serializer = ExecuteBatchMergeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = BatchMergeService.execute_merge(
            user=user,
            source_batch_id=data["source_batch_id"],
            target_batch_id=data["target_batch_id"],
            reason=data["reason"],
        )
        return Response(result, status=status.HTTP_201_CREATED)

    def list(self, request: Any) -> Response:
        """List batch merge records scoped to tenant and optional branch."""
        tenant = self._get_tenant(request)
        qs = BatchMergeRecord.objects.filter(tenant=tenant).select_related(
            "product", "source_batch", "target_batch", "merged_by"
        )

        branch_id = request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)

        serializer = BatchMergeRecordSerializer(qs[:100], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """Retrieve details of a single batch merge record."""
        tenant = self._get_tenant(request)
        try:
            record = BatchMergeRecord.objects.select_related(
                "product", "source_batch", "target_batch", "merged_by"
            ).get(tenant=tenant, merge_id=pk)
        except BatchMergeRecord.DoesNotExist:
            raise NotFound({"detail": f"Batch merge record '{pk}' not found.", "error_code": "ERR_MERGE_RECORD_NOT_FOUND"})

        serializer = BatchMergeRecordSerializer(record)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request: Any, pk: str = None) -> Response:
        """Reverse an executed batch merge."""
        self._get_tenant(request)
        user = request.user

        serializer = ReverseBatchMergeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = BatchMergeService.reverse_merge(
            user=user,
            merge_id=pk,
            reason=serializer.validated_data["reason"],
        )
        return Response(result, status=status.HTTP_200_OK)

    def update(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed("PUT", detail="Posted batch merges cannot be modified.")

    def partial_update(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed("PATCH", detail="Posted batch merges cannot be modified.")

    def destroy(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed("DELETE", detail="Posted batch merges cannot be deleted.")

