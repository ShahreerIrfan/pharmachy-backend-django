"""
temperature_monitoring_views.py
Architecture Decision: ADR-0211 (P211).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.
"""

from decimal import Decimal
from rest_framework import viewsets, permissions, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import (
    TemperatureMonitoringDevice,
    TemperatureLogReading,
    SensorDeviceType,
    TemperatureLogSourceType,
    ExcursionSeverity,
)
from apps.core.temperature_monitoring_service import TemperatureMonitoringService


class TemperatureMonitoringDeviceSerializer(serializers.ModelSerializer):
    storage_location_code = serializers.CharField(source="storage_location.code", read_only=True)
    storage_location_name = serializers.CharField(source="storage_location.name", read_only=True)

    class Meta:
        model = TemperatureMonitoringDevice
        fields = [
            "device_id",
            "branch",
            "storage_location",
            "storage_location_code",
            "storage_location_name",
            "device_name",
            "device_type",
            "serial_number",
            "calibration_date",
            "calibration_due_date",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["device_id", "created_at", "updated_at"]


class TemperatureLogReadingSerializer(serializers.ModelSerializer):
    storage_location_code = serializers.CharField(source="storage_location.code", read_only=True)
    device_name = serializers.CharField(source="device.device_name", read_only=True)
    recorded_by_name = serializers.CharField(source="recorded_by.get_full_name", read_only=True)

    class Meta:
        model = TemperatureLogReading
        fields = [
            "log_id",
            "branch",
            "storage_location",
            "storage_location_code",
            "device",
            "device_name",
            "source_type",
            "recorded_at",
            "temperature_celsius",
            "humidity_percentage",
            "min_threshold_celsius",
            "max_threshold_celsius",
            "is_excursion",
            "excursion_severity",
            "notes",
            "recorded_by",
            "recorded_by_name",
            "audit_hash",
            "created_at",
        ]
        read_only_fields = ["log_id", "is_excursion", "excursion_severity", "audit_hash", "created_at"]


class TemperatureMonitoringDeviceViewSet(viewsets.ModelViewSet):
    """CRUD for cold-chain temperature sensors and loggers."""

    serializer_class = TemperatureMonitoringDeviceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return TemperatureMonitoringDevice.objects.none()

        qs = TemperatureMonitoringDevice.objects.filter(tenant_id=tenant_id).select_related(
            "branch", "storage_location"
        )
        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        TemperatureMonitoringService.assert_staff_authority(user)
        serializer.save(tenant_id=user.tenant_id)


class TemperatureLogReadingViewSet(viewsets.ModelViewSet):
    """ViewSet for temperature telemetry, manual log sheets, and excursion queries."""

    serializer_class = TemperatureLogReadingSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        tenant_id = getattr(user, "tenant_id", None)
        if not tenant_id:
            return TemperatureLogReading.objects.none()

        qs = TemperatureLogReading.objects.filter(tenant_id=tenant_id).select_related(
            "branch", "storage_location", "device", "recorded_by"
        )

        location_id = self.request.query_params.get("storage_location")
        if location_id:
            qs = qs.filter(storage_location_id=location_id)

        excursions_only = self.request.query_params.get("excursions_only")
        if excursions_only and excursions_only.lower() in ["true", "1"]:
            qs = qs.filter(is_excursion=True)

        return qs

    def create(self, request, *args, **kwargs):
        """Creates a single manual reading or API ingestion."""
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        data = request.data

        reading = TemperatureMonitoringService.record_reading(
            tenant_id=tenant_id,
            branch_id=data.get("branch"),
            storage_location_id=data.get("storage_location"),
            temperature_celsius=Decimal(str(data.get("temperature_celsius", "0.00"))),
            humidity_percentage=Decimal(str(data["humidity_percentage"])) if data.get("humidity_percentage") is not None else None,
            min_threshold_celsius=Decimal(str(data.get("min_threshold_celsius", "2.00"))),
            max_threshold_celsius=Decimal(str(data.get("max_threshold_celsius", "8.00"))),
            device_id=data.get("device"),
            source_type=data.get("source_type", TemperatureLogSourceType.MANUAL_ENTRY),
            notes=data.get("notes", ""),
            actor_user=user,
        )
        return Response(self.get_serializer(reading).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="import-batch")
    def import_batch(self, request):
        """Imports a batch of readings from a USB data logger or file dump."""
        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        data = request.data

        readings = TemperatureMonitoringService.import_sensor_batch(
            tenant_id=tenant_id,
            branch_id=data.get("branch"),
            storage_location_id=data.get("storage_location"),
            readings_payload=data.get("readings", []),
            device_id=data.get("device"),
            source_type=data.get("source_type", TemperatureLogSourceType.USB_LOGGER_BATCH),
            batch_notes=data.get("notes", ""),
            actor_user=user,
        )
        return Response(self.get_serializer(readings, many=True).data, status=status.HTTP_201_CREATED)

