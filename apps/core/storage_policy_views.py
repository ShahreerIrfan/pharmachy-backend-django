"""
backend/apps/core/storage_policy_views.py
Architecture Decision: ADR-0213 (P213).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Storage Policy Enforcement:
- Policy configuration (Cold-chain zone protection, strict temperature limits).
- Validation endpoint for pre-check before assigning or moving batches.
- Audited batch relocation endpoint.
- Branch-wide storage compliance audit & reconciliation.
"""

from __future__ import annotations

from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework import serializers

from apps.core.models import (
    BatchStock,
    BatchStorageAssignmentLog,
    Branch,
    MedicineProduct,
    StorageLocation,
    StorageZonePolicy,
    Tenant,
)
from apps.core.storage_policy_service import StoragePolicyService


class StorageZonePolicySerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = StorageZonePolicy
        fields = [
            "policy_id",
            "tenant",
            "branch",
            "branch_name",
            "name",
            "enforce_cold_chain",
            "allow_ambient_override",
            "strict_temperature_boundary",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["policy_id", "tenant", "created_at", "updated_at"]


class BatchStorageAssignmentLogSerializer(serializers.ModelSerializer):
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True)
    product_name = serializers.CharField(source="product.brand_name", read_only=True)
    assigned_by_name = serializers.CharField(source="assigned_by.username", read_only=True, default=None)

    class Meta:
        model = BatchStorageAssignmentLog
        fields = [
            "id",
            "tenant",
            "branch",
            "batch",
            "batch_number",
            "product",
            "product_name",
            "from_location_code",
            "to_location_code",
            "quantity_units",
            "is_cold_chain_product",
            "is_compliant",
            "justification",
            "assigned_by",
            "assigned_by_name",
            "assigned_at",
        ]
        read_only_fields = fields


class StoragePolicyViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing storage zone policies and enforcing cold chain storage compliance.
    """
    serializer_class = StorageZonePolicySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return StorageZonePolicy.objects.none()

        qs = StorageZonePolicy.objects.filter(tenant=tenant_id).select_related("branch")
        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs

    def perform_create(self, serializer):
        StoragePolicyService.assert_inventory_role(self.request.user)
        tenant_id = getattr(self.request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "Authenticated user must have an active tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id
        serializer.save(tenant=tenant)

    @action(detail=False, methods=["post"], url_path="validate-assignment")
    def validate_assignment(self, request):
        """
        Pre-validation endpoint before saving or moving a medicine batch in UI.
        """
        tenant_id = getattr(request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User must have a tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        product_id = request.data.get("product_id")
        storage_location_id = request.data.get("storage_location_id")
        override_justification = request.data.get("override_justification", "")

        if not product_id:
            raise ValidationError({"product_id": "product_id is required."})
        if not storage_location_id:
            raise ValidationError({"storage_location_id": "storage_location_id is required."})

        try:
            product = MedicineProduct.objects.get(pk=product_id)
        except MedicineProduct.DoesNotExist:
            raise NotFound(f"Product {product_id} not found.")

        try:
            storage_loc = StorageLocation.objects.select_related("warehouse").get(pk=storage_location_id, tenant=tenant)
        except StorageLocation.DoesNotExist:
            raise NotFound(f"StorageLocation {storage_location_id} not found for this tenant.")

        policy = StorageZonePolicy.objects.filter(tenant=tenant, branch=storage_loc.branch, is_active=True).first()

        is_compliant, reason = StoragePolicyService.validate_storage_assignment(
            product=product,
            storage_location=storage_loc,
            policy=policy,
            override_justification=override_justification,
        )

        return Response(
            {
                "is_compliant": is_compliant,
                "reason": reason,
                "product_name": product.brand_name,
                "requires_cold_chain": getattr(product, "requires_cold_chain", False),
                "storage_location_code": storage_loc.code,
                "storage_location_name": storage_loc.name,
                "location_type": storage_loc.location_type,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="relocate-batch")
    def relocate_batch(self, request):
        """
        Executes an audited stock relocation to a verified compliant zone.
        """
        StoragePolicyService.assert_inventory_role(request.user)

        tenant_id = getattr(request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User must have a tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        batch_id = request.data.get("batch_id")
        target_location_id = request.data.get("target_location_id")
        override_justification = request.data.get("override_justification", "")

        if not batch_id:
            raise ValidationError({"batch_id": "batch_id is required."})
        if not target_location_id:
            raise ValidationError({"target_location_id": "target_location_id is required."})

        try:
            batch = BatchStock.objects.select_related("product", "branch").get(pk=batch_id, tenant=tenant)
        except BatchStock.DoesNotExist:
            raise NotFound(f"Batch {batch_id} not found for this tenant.")

        try:
            target_loc = StorageLocation.objects.select_related("warehouse", "branch").get(
                pk=target_location_id, tenant=tenant
            )
        except StorageLocation.DoesNotExist:
            raise NotFound(f"StorageLocation {target_location_id} not found for this tenant.")

        log_entry = StoragePolicyService.assign_or_relocate_batch(
            tenant=tenant,
            branch=batch.branch,
            batch=batch,
            target_location=target_loc,
            actor_user=request.user,
            override_justification=override_justification,
        )

        serializer = BatchStorageAssignmentLogSerializer(log_entry)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="audit-reconciliation")
    def audit_reconciliation(self, request):
        """
        Audits branch stock against active storage policy.
        """
        StoragePolicyService.assert_inventory_role(request.user)

        tenant_id = getattr(request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "User must have a tenant."})
        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id

        branch_id = request.query_params.get("branch") or getattr(request.user, "branch_id", None)
        if not branch_id:
            raise ValidationError({"branch": "Branch parameter is required."})

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise NotFound(f"Branch {branch_id} not found for this tenant.")

        result = StoragePolicyService.audit_branch_storage_compliance(tenant=tenant, branch=branch)
        return Response(result, status=status.HTTP_200_OK)

