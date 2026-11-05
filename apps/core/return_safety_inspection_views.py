"""
backend/apps/core/return_safety_inspection_views.py
Architecture Decision: ADR-0177 (P177).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API Endpoints for Sales Returns Safety Inspections,
Exact Batch/Cost Lot Restocking, and Clinical Quarantine Management.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    ReturnInspectionDisposition,
    ReturnSafetyInspection,
    Tenant,
)
from apps.core.return_safety_inspection_service import (
    INSPECTION_ALLOWED_ROLES,
    ReturnSafetyInspectionService,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class ReturnSafetyInspectionSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True, default="")
    product_name = serializers.CharField(source="product.brand_name", read_only=True, default="")
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True, default="")
    inspector_name = serializers.CharField(source="inspector.full_name", read_only=True, default="")

    class Meta:
        model = ReturnSafetyInspection
        fields = [
            "inspection_id",
            "tenant_id",
            "branch_id",
            "allocation_id",
            "invoice_id",
            "invoice_number",
            "product_id",
            "product_name",
            "batch_id",
            "batch_number",
            "cost_lot_id",
            "returned_quantity_base_units",
            "disposition",
            "is_seal_intact",
            "is_packaging_undamaged",
            "is_storage_condition_verified",
            "is_unexpired",
            "temperature_reading_celsius",
            "inspector_id",
            "inspector_name",
            "customer_reason",
            "inspection_notes",
            "refund_amount",
            "restocked_cogs",
            "is_reversed",
            "reversal_reason",
            "reversed_at",
            "created_at",
            "updated_at",
        ]


class CreateReturnInspectionRequestSerializer(serializers.Serializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    branch_id = serializers.CharField(required=False, allow_blank=True)
    allocation_id = serializers.CharField(required=True)
    returned_quantity_base_units = serializers.IntegerField(min_value=1)
    disposition = serializers.ChoiceField(choices=ReturnInspectionDisposition.choices)
    is_seal_intact = serializers.BooleanField(default=True)
    is_packaging_undamaged = serializers.BooleanField(default=True)
    is_storage_condition_verified = serializers.BooleanField(default=True)
    is_unexpired = serializers.BooleanField(default=True)
    temperature_reading_celsius = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    customer_reason = serializers.CharField(required=False, allow_blank=True, default="")
    inspection_notes = serializers.CharField(required=False, allow_blank=True, default="")


class ReverseReturnInspectionRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, min_length=10)


class ReturnSafetyInspectionViewSet(viewsets.ViewSet):
    """
    REST API governing sales return safety inspections,
    lot-specific restocking, and quarantine holds.
    Direct mutation or deletion is strictly prohibited (HTTP 405).
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

    def create(self, request: Any) -> Response:
        """Submit a safety inspection and process return to lot/batch."""
        self._get_tenant(request)
        user = request.user

        # RBAC Check: Pharmacist or Manager required
        if getattr(user, "role", None) not in INSPECTION_ALLOWED_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Safety inspection and return disposition requires a registered pharmacist or manager.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

        serializer = CreateReturnInspectionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = ReturnSafetyInspectionService.inspect_and_process_return(
            user=user,
            data=serializer.validated_data,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    def list(self, request: Any) -> Response:
        """List return safety inspections scoped to tenant and optional branch."""
        tenant = self._get_tenant(request)
        qs = ReturnSafetyInspection.objects.filter(tenant=tenant).select_related(
            "product", "batch", "cost_lot", "invoice", "inspector"
        )

        branch_id = request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        disposition = request.query_params.get("disposition")
        if disposition:
            qs = qs.filter(disposition=disposition)

        allocation_id = request.query_params.get("allocation_id")
        if allocation_id:
            qs = qs.filter(allocation_id=allocation_id)

        serializer = ReturnSafetyInspectionSerializer(qs[:100], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """Retrieve details of a single return safety inspection."""
        tenant = self._get_tenant(request)
        try:
            insp = ReturnSafetyInspection.objects.select_related(
                "product", "batch", "cost_lot", "invoice", "inspector"
            ).get(tenant=tenant, inspection_id=pk)
        except ReturnSafetyInspection.DoesNotExist:
            raise NotFound(
                {"detail": f"Return inspection '{pk}' not found.", "error_code": "ERR_INSPECTION_NOT_FOUND"}
            )

        serializer = ReturnSafetyInspectionSerializer(insp)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request: Any, pk: str = None) -> Response:
        """Reverse a posted return safety inspection."""
        self._get_tenant(request)
        user = request.user

        serializer = ReverseReturnInspectionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = ReturnSafetyInspectionService.reverse_return_inspection(
            user=user,
            inspection_id=pk,
            reason=serializer.validated_data["reason"],
        )
        return Response(result, status=status.HTTP_200_OK)

    def update(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed("PUT", detail="Posted return inspections cannot be modified.")

    def partial_update(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed("PATCH", detail="Posted return inspections cannot be modified.")

    def destroy(self, request: Any, pk: str = None) -> Response:
        raise MethodNotAllowed("DELETE", detail="Posted return inspections cannot be deleted.")

