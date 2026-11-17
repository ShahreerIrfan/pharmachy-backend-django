"""
temperature_monitoring_service.py
Architecture Decision: ADR-0211 (P211).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Domain Service for Temperature & Humidity Monitoring:
- Registering IoT, Bluetooth, and Manual Logging Devices.
- Recording point-in-time sensor readings or manual log sheets.
- Bulk batch importing of USB data logger CSV/JSON files.
- Real-time excursion classification (Safe, Warning, Critical).
- SHA-256 cryptographic audit hash generation for compliance integrity.
- Strict multi-tenant isolation and role-based access control.
"""

from decimal import Decimal
import uuid
from typing import Any, Dict, List, Optional
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from apps.core.models import (
    Tenant,
    Branch,
    StorageLocation,
    TemperatureMonitoringDevice,
    TemperatureLogReading,
    SensorDeviceType,
    TemperatureLogSourceType,
    ExcursionSeverity,
)
from apps.identity.models import UserRole


class TemperatureMonitoringService:
    """Service managing cold chain temperature monitoring and excursion detection."""

    ALLOWED_ROLES = {
        UserRole.STAFF_PHARMACIST,
        UserRole.BRANCH_MANAGER,
        UserRole.PHARMACY_OWNER,
        UserRole.SUPERADMIN,
    }

    @classmethod
    def assert_staff_authority(cls, actor_user: Any) -> None:
        """Validates that actor user has authorized staff role."""
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in cls.ALLOWED_ROLES:
            raise PermissionDenied("ERR_STAFF_ROLE_DENIED: Only Pharmacists or Managers can log or import temperature telemetry.")

    @classmethod
    @transaction.atomic
    def register_device(
        cls,
        tenant_id: str,
        branch_id: str,
        storage_location_id: Optional[str],
        device_name: str,
        device_type: str = SensorDeviceType.MANUAL_DIGITAL,
        serial_number: str = "",
        calibration_date: Any = None,
        calibration_due_date: Any = None,
        actor_user: Any = None,
    ) -> TemperatureMonitoringDevice:
        """Registers a sensor or logger device."""
        cls.assert_staff_authority(actor_user)

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound("Tenant not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound("Branch not found or belongs to a different tenant (ERR_CROSS_TENANT_ACCESS).")

        location = None
        if storage_location_id:
            location = StorageLocation.objects.filter(tenant=tenant, branch=branch, location_id=storage_location_id).first()
            if not location:
                raise NotFound(f"Storage location '{storage_location_id}' not found in specified branch.")

        device_id = f"dev-{uuid.uuid4().hex[:10]}"
        device = TemperatureMonitoringDevice.objects.create(
            device_id=device_id,
            tenant=tenant,
            branch=branch,
            storage_location=location,
            device_name=device_name.strip(),
            device_type=device_type,
            serial_number=serial_number.strip(),
            calibration_date=calibration_date,
            calibration_due_date=calibration_due_date,
            is_active=True,
        )
        return device

    @classmethod
    @transaction.atomic
    def record_reading(
        cls,
        tenant_id: str,
        branch_id: str,
        storage_location_id: str,
        temperature_celsius: Decimal,
        humidity_percentage: Optional[Decimal] = None,
        min_threshold_celsius: Decimal = Decimal("2.00"),
        max_threshold_celsius: Decimal = Decimal("8.00"),
        device_id: Optional[str] = None,
        source_type: str = TemperatureLogSourceType.MANUAL_ENTRY,
        recorded_at: Optional[timezone.datetime] = None,
        notes: str = "",
        actor_user: Any = None,
    ) -> TemperatureLogReading:
        """
        Records a single point-in-time temperature reading.
        Computes excursions and cryptographic audit hash.
        """
        cls.assert_staff_authority(actor_user)

        if min_threshold_celsius >= max_threshold_celsius:
            raise ValidationError({"thresholds": "Min threshold must be strictly less than max threshold."})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound("Tenant not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound("Branch not found or belongs to a different tenant (ERR_CROSS_TENANT_ACCESS).")

        location = StorageLocation.objects.filter(tenant=tenant, branch=branch, location_id=storage_location_id).first()
        if not location:
            raise NotFound(f"Storage location '{storage_location_id}' not found in branch {branch_id}.")

        device = None
        if device_id:
            device = TemperatureMonitoringDevice.objects.filter(tenant=tenant, branch=branch, device_id=device_id).first()
            if not device:
                raise NotFound(f"Device '{device_id}' not found in branch.")

        reading_id = f"tlog-{uuid.uuid4().hex[:10]}"
        reading = TemperatureLogReading(
            log_id=reading_id,
            tenant=tenant,
            branch=branch,
            storage_location=location,
            device=device,
            source_type=source_type,
            recorded_at=recorded_at or timezone.now(),
            temperature_celsius=temperature_celsius,
            humidity_percentage=humidity_percentage,
            min_threshold_celsius=min_threshold_celsius,
            max_threshold_celsius=max_threshold_celsius,
            notes=notes.strip(),
            recorded_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )
        reading.save()
        return reading

    @classmethod
    @transaction.atomic
    def import_sensor_batch(
        cls,
        tenant_id: str,
        branch_id: str,
        storage_location_id: str,
        readings_payload: List[Dict[str, Any]],
        device_id: Optional[str] = None,
        source_type: str = TemperatureLogSourceType.USB_LOGGER_BATCH,
        default_min_threshold: Decimal = Decimal("2.00"),
        default_max_threshold: Decimal = Decimal("8.00"),
        batch_notes: str = "",
        actor_user: Any = None,
    ) -> List[TemperatureLogReading]:
        """
        Ingests a batch of readings (e.g. from a USB or Bluetooth temperature logger).
        """
        cls.assert_staff_authority(actor_user)

        if not readings_payload:
            raise ValidationError({"readings": "At least one reading must be provided in the import batch."})

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound("Tenant not found.")

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            raise NotFound("Branch not found (ERR_CROSS_TENANT_ACCESS).")

        location = StorageLocation.objects.filter(tenant=tenant, branch=branch, location_id=storage_location_id).first()
        if not location:
            raise NotFound(f"Storage location '{storage_location_id}' not found in branch.")

        device = None
        if device_id:
            device = TemperatureMonitoringDevice.objects.filter(tenant=tenant, branch=branch, device_id=device_id).first()

        created_readings = []
        for r in readings_payload:
            temp = Decimal(str(r.get("temperature_celsius", "0.00")))
            hum = Decimal(str(r["humidity_percentage"])) if r.get("humidity_percentage") is not None else None
            min_t = Decimal(str(r.get("min_threshold_celsius", default_min_threshold)))
            max_t = Decimal(str(r.get("max_threshold_celsius", default_max_threshold)))

            ts = r.get("recorded_at")
            if isinstance(ts, str):
                from django.utils.dateparse import parse_datetime
                ts_parsed = parse_datetime(ts)
            else:
                ts_parsed = ts

            reading = TemperatureLogReading(
                log_id=f"tlog-{uuid.uuid4().hex[:10]}",
                tenant=tenant,
                branch=branch,
                storage_location=location,
                device=device,
                source_type=source_type,
                recorded_at=ts_parsed or timezone.now(),
                temperature_celsius=temp,
                humidity_percentage=hum,
                min_threshold_celsius=min_t,
                max_threshold_celsius=max_t,
                notes=r.get("notes", batch_notes),
                recorded_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            )
            reading.save()
            created_readings.append(reading)

        return created_readings
