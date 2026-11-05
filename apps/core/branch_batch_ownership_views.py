"""
backend/apps/core/branch_batch_ownership_views.py
Architecture Decision: ADR-0175 (P175).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API Endpoints for Per-Branch Batch Ownership, Isolation,
and Cross-Branch Stock Mixing Prevention.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.branch_batch_ownership_service import BranchBatchOwnershipService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class ValidateDispenseItemSerializer(serializers.Serializer):
    batch_id = serializers.CharField(required=True)
    quantity = serializers.IntegerField(required=True, min_value=1)


class ValidateDispenseRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    branch_id = serializers.CharField(required=True)
    items = ValidateDispenseItemSerializer(many=True, required=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Items list cannot be empty.")
        return value


class InterBranchTransferRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    source_branch_id = serializers.CharField(required=True)
    target_branch_id = serializers.CharField(required=True)
    batch_id = serializers.CharField(required=True)
    quantity = serializers.IntegerField(required=True, min_value=1)
    reason = serializers.CharField(required=True, min_length=10)


class BranchBatchOwnershipViewSet(viewsets.ViewSet):
    """
    API endpoints governing branch-scoped batch lookups, cross-branch dispense isolation,
    multi-branch segregated summaries, and inter-branch transfers.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request: Any) -> str:
        user = request.user
        tenant_id = (
            request.query_params.get("tenant_id")
            if request.method == "GET"
            else request.data.get("tenant_id")
        )
        tenant_id = tenant_id or getattr(user, "tenant_id", None)

        if not tenant_id:
            raise ValidationError(
                {"detail": "tenant_id is required.", "error_code": "ERR_MISSING_TENANT"}
            )

        if (
            getattr(user, "tenant_id", None)
            and str(user.tenant_id) != str(tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {"detail": "Cross-tenant access is strictly prohibited.", "error_code": "ERR_CROSS_TENANT_ACCESS"}
            )

        return str(tenant_id)

    def list(self, request: Any) -> Response:
        """List batches scoped to the user's authorized branch context."""
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        product_id = request.query_params.get("product_id")
        status_filter = request.query_params.get("status")
        search = request.query_params.get("search")
        dispensable_only = str(request.query_params.get("dispensable_only", "")).lower() in ("true", "1")

        batches = BranchBatchOwnershipService.list_branch_batches(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=branch_id,
            product_id=product_id,
            status_filter=status_filter,
            search=search,
            dispensable_only=dispensable_only,
        )
        return Response(batches, status=status.HTTP_200_OK)

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """Retrieve a single batch, strictly validating branch ownership."""
        tenant_id = self._get_tenant_id(request)
        branch_id = request.query_params.get("branch_id")

        batch_data = BranchBatchOwnershipService.get_branch_batch(
            user=request.user,
            tenant_id=tenant_id,
            batch_id=pk,
            branch_id=branch_id,
        )
        return Response(batch_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-dispense")
    def validate_dispense(self, request: Any) -> Response:
        """Pre-flight POS checkout check validating branch isolation and unreserved stock."""
        tenant_id = self._get_tenant_id(request)
        serializer = ValidateDispenseRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = BranchBatchOwnershipService.validate_branch_dispense(
            user=request.user,
            tenant_id=tenant_id,
            branch_id=data["branch_id"],
            items=data["items"],
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get", "post"], url_path="branch-summary")
    def branch_summary(self, request: Any) -> Response:
        """Segregated multi-branch inventory breakdown for a medicine."""
        tenant_id = self._get_tenant_id(request)
        product_id = (
            request.query_params.get("product_id")
            if request.method == "GET"
            else request.data.get("product_id")
        )
        if not product_id:
            raise ValidationError(
                {"detail": "product_id is required.", "error_code": "ERR_PRODUCT_REQUIRED"}
            )

        summary = BranchBatchOwnershipService.get_medicine_per_branch_summary(
            user=request.user,
            tenant_id=tenant_id,
            product_id=product_id,
        )
        return Response(summary, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="transfer")
    def transfer(self, request: Any) -> Response:
        """Audited inter-branch stock transfer between two branches of the same tenant."""
        tenant_id = self._get_tenant_id(request)
        serializer = InterBranchTransferRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        transfer_result = BranchBatchOwnershipService.transfer_stock_between_branches(
            user=request.user,
            tenant_id=tenant_id,
            source_branch_id=data["source_branch_id"],
            target_branch_id=data["target_branch_id"],
            batch_id=data["batch_id"],
            quantity=data["quantity"],
            reason=data["reason"],
        )
        return Response(transfer_result, status=status.HTTP_200_OK)
