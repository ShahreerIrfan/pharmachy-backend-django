"""
backend/apps/core/cold_chain_excursion_views.py
Architecture Decision: ADR-0212 (P212).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSets and Serializers for Cold Chain Excursion Workflow:
- Excursion incident tracking and listing with tenant isolation.
- Automatic quarantine triggers.
- Pharmacist clinical disposition review (Release / Dispose / Supplier Return).
- Audit trails and strict cashier access prohibition.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework import serializers

from apps.core.models import (
    Branch,
    ColdChainAffectedBatch,
    ColdChainExcursionIncident,
    ExcursionDispositionOutcome,
    ExcursionIncidentStatus,
    StorageLocation,
    TemperatureLogReading,
    Tenant,
)
from apps.core.cold_chain_excursion_service import ColdChainExcursionService


class ColdChainAffectedBatchSerializer(serializers.ModelSerializer):
    batch_number = serializers.CharField(source="batch.batch_number", read_only=True)
    product_name = serializers.CharField(source="batch.product.brand_name", read_only=True)

    class Meta:
        model = ColdChainAffectedBatch
        fields = [
            "id",
            "batch",
            "batch_number",
            "product_name",
            "initial_quantity",
            "unit_cost",
            "total_cost",
            "disposition_outcome",
            "created_at",
        ]
        read_only_fields = fields


class ColdChainExcursionIncidentSerializer(serializers.ModelSerializer):
    storage_location_name = serializers.CharField(source="storage_location.name", read_only=True)
    storage_location_code = serializers.CharField(source="storage_location.code", read_only=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.username", read_only=True, default=None)
    affected_batches = ColdChainAffectedBatchSerializer(many=True, read_only=True)

    class Meta:
        model = ColdChainExcursionIncident
        fields = [
            "id",
            "incident_number",
            "tenant",
            "branch",
            "storage_location",
            "storage_location_name",
            "storage_location_code",
            "trigger_reading",
            "status",
            "disposition_outcome",
            "recorded_temperature_celsius",
            "min_threshold_celsius",
            "max_threshold_celsius",
            "severity",
            "quarantined_batches_count",
            "quarantined_total_value",
            "clinical_justification",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "affected_batches",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "incident_number",
            "tenant",
            "status",
            "disposition_outcome",
            "severity",
            "quarantined_batches_count",
            "quarantined_total_value",
            "reviewed_by",
            "reviewed_at",
            "affected_batches",
            "created_at",
            "updated_at",
        ]


class ColdChainExcursionViewSet(viewsets.ModelViewSet):
    """
    API endpoint for monitoring cold chain excursion incidents and executing
    quarantine/pharmacist disposition actions.
    """
    serializer_class = ColdChainExcursionIncidentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return ColdChainExcursionIncident.objects.none()

        qs = ColdChainExcursionIncident.objects.filter(tenant=tenant_id).select_related(
            "storage_location", "reviewed_by", "branch"
        ).prefetch_related("affected_batches__batch__product")

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        incident_status = self.request.query_params.get("status")
        if incident_status:
            qs = qs.filter(status=incident_status)

        return qs

    def create(self, request, *args, **kwargs):
        """
        Trigger an excursion incident and quarantine affected stock.
        """
        ColdChainExcursionService.assert_review_permission(request.user)

        tenant_id = getattr(request.user, "tenant_id", None) or getattr(request.user, "tenant", None)
        if not tenant_id:
            raise ValidationError({"tenant": "Authenticated user must have an active tenant."})

        if isinstance(tenant_id, Tenant):
            tenant = tenant_id
        else:
            tenant = Tenant.objects.get(tenant_id=tenant_id)

        data = request.data
        storage_location_id = data.get("storage_location")
        if not storage_location_id:
            raise ValidationError({"storage_location": "storage_location is required."})

        try:
            storage_loc = StorageLocation.objects.get(pk=storage_location_id, tenant=tenant)
        except StorageLocation.DoesNotExist:
            raise NotFound(f"StorageLocation {storage_location_id} not found for this tenant.")

        branch = storage_loc.branch
        recorded_temp_val = data.get("recorded_temperature")
        if recorded_temp_val is None:
            raise ValidationError({"recorded_temperature": "recorded_temperature is required."})

        try:
            recorded_temp = Decimal(str(recorded_temp_val))
        except Exception:
            raise ValidationError({"recorded_temperature": "Invalid temperature numeric value."})

        min_t = None
        if data.get("min_threshold") is not None:
            min_t = Decimal(str(data["min_threshold"]))

        max_t = None
        if data.get("max_threshold") is not None:
            max_t = Decimal(str(data["max_threshold"]))

        trigger_reading = None
        reading_id = data.get("trigger_reading")
        if reading_id:
            trigger_reading = TemperatureLogReading.objects.filter(id=reading_id, tenant=tenant).first()

        incident = ColdChainExcursionService.trigger_excursion(
            tenant=tenant,
            branch=branch,
            storage_location=storage_loc,
            recorded_temperature=recorded_temp,
            min_threshold=min_t,
            max_threshold=max_t,
            trigger_reading=trigger_reading,
            created_by=request.user,
        )

        serializer = self.get_serializer(incident)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="review-disposition")
    def review_disposition(self, request, pk=None):
        """
        Pharmacist review action to resolve excursion and dispose quarantined stock.
        """
        incident = self.get_object()
        outcome = request.data.get("disposition_outcome")
        clinical_justification = request.data.get("clinical_justification", "")

        updated_incident = ColdChainExcursionService.review_disposition(
            incident=incident,
            reviewer_user=request.user,
            outcome=outcome,
            clinical_justification=clinical_justification,
        )

        serializer = self.get_serializer(updated_incident)
        return Response(serializer.data, status=status.HTTP_200_OK)
