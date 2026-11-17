"""
test_p211_temperature_sensors_logs.py
Architecture Decision: ADR-0211 (P211).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Acceptance Criteria:
1. Valid Scenario:
   - Register temperature sensor/device for cold-chain fridge.
   - Record manual reading within thresholds (2.0°C to 8.0°C) -> Safe, audit hash generated.
   - Ingest sensor batch containing in-range, warning excursion, and critical excursion readings.
2. Invalid Input:
   - Inverted thresholds (min >= max) raises ValidationError.
3. Unauthorized & Cross-Tenant:
   - Cashier role barred (PermissionDenied / HTTP 403).
   - Cross-tenant access barred (NotFound / HTTP 404).
4. REST API Integration:
   - Endpoints /cold-chain/temperature-logs/ and /import-batch/ verify end-to-end functionality.
"""

from decimal import Decimal
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from apps.core.models import (
    Tenant,
    Branch,
    Warehouse,
    StorageLocation,
    WarehouseType,
    StorageLocationType,
    TemperatureMonitoringDevice,
    TemperatureLogReading,
    SensorDeviceType,
    TemperatureLogSourceType,
    ExcursionSeverity,
)
from apps.core.temperature_monitoring_service import TemperatureMonitoringService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p211-{uid}",
        name="Popular Diagnostic Pharmacy",
        subdomain=f"popular-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-cross-{uid}",
        name="Ibn Sina Pharmacy",
        subdomain=f"ibnsina-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def branch_a(db, tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        is_active=True,
    )


@pytest.fixture
def branch_b(db, tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:6]}",
        tenant=tenant_b,
        name="Mirpur Branch",
        is_active=True,
    )


@pytest.fixture
def pharmacist_user(db, tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_coldchain@popular.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_user(db, tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="cashier_coldchain@popular.com",
        password="ValidPassword123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def fridge_location(db, tenant_a, branch_a):
    wh = Warehouse.objects.create(
        warehouse_id=f"wh-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        name="Main Cold Room",
        code="WH-COLD-01",
        warehouse_type=WarehouseType.COLD_ROOM,
        is_cold_storage=True,
    )
    return StorageLocation.objects.create(
        location_id=f"loc-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        warehouse=wh,
        code="FRIDGE-BIOL-01",
        name="Vaccine & Biologicals Main Fridge",
        location_type=StorageLocationType.FRIDGE,
    )


@pytest.mark.django_db
def test_manual_reading_and_batch_import_excursion_detection(tenant_a, branch_a, pharmacist_user, fridge_location):
    """
    Test 1:
    - Register IoT wireless sensor.
    - Log single manual reading within 2°C to 8°C -> Normal, audit hash generated.
    - Ingest batch of readings:
      * 4.2°C -> Normal
      * 9.5°C -> Warning excursion
      * 18.0°C -> Critical excursion
    """
    # 1. Register device
    device = TemperatureMonitoringService.register_device(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        storage_location_id=fridge_location.location_id,
        device_name="Elitech GSP-6 Continuous Logger",
        device_type=SensorDeviceType.IOT_WIRELESS,
        serial_number="SN-ELITECH-9872",
        calibration_date=timezone.now().date() - timezone.timedelta(days=60),
        calibration_due_date=timezone.now().date() + timezone.timedelta(days=300),
        actor_user=pharmacist_user,
    )
    assert device.device_id.startswith("dev-")
    assert device.is_active is True

    # 2. Record single manual reading
    reading = TemperatureMonitoringService.record_reading(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        storage_location_id=fridge_location.location_id,
        temperature_celsius=Decimal("4.50"),
        humidity_percentage=Decimal("52.00"),
        min_threshold_celsius=Decimal("2.00"),
        max_threshold_celsius=Decimal("8.00"),
        device_id=device.device_id,
        source_type=TemperatureLogSourceType.MANUAL_ENTRY,
        notes="Daily morning physical check",
        actor_user=pharmacist_user,
    )
    assert reading.is_excursion is False
    assert reading.excursion_severity == ExcursionSeverity.NONE
    assert len(reading.audit_hash) == 64

    # 3. Ingest sensor batch
    now = timezone.now()
    batch_payload = [
        {"temperature_celsius": "4.20", "humidity_percentage": "50.00", "recorded_at": (now - timezone.timedelta(hours=3)).isoformat()},
        {"temperature_celsius": "9.50", "humidity_percentage": "62.00", "recorded_at": (now - timezone.timedelta(hours=2)).isoformat()},
        {"temperature_celsius": "18.00", "humidity_percentage": "70.00", "recorded_at": (now - timezone.timedelta(hours=1)).isoformat()},
    ]
    imported = TemperatureMonitoringService.import_sensor_batch(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        storage_location_id=fridge_location.location_id,
        readings_payload=batch_payload,
        device_id=device.device_id,
        actor_user=pharmacist_user,
    )
    assert len(imported) == 3

    # Safe reading
    assert imported[0].is_excursion is False
    assert imported[0].excursion_severity == ExcursionSeverity.NONE

    # Warning reading (9.5°C > 8.0°C by <5°C)
    assert imported[1].is_excursion is True
    assert imported[1].excursion_severity == ExcursionSeverity.WARNING

    # Critical reading (18.0°C >= 15°C)
    assert imported[2].is_excursion is True
    assert imported[2].excursion_severity == ExcursionSeverity.CRITICAL


@pytest.mark.django_db
def test_inverted_thresholds_rejected(tenant_a, branch_a, pharmacist_user, fridge_location):
    """Test 2: Min threshold >= Max threshold raises ValidationError."""
    with pytest.raises(ValidationError) as exc:
        TemperatureMonitoringService.record_reading(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            storage_location_id=fridge_location.location_id,
            temperature_celsius=Decimal("5.00"),
            min_threshold_celsius=Decimal("10.00"),
            max_threshold_celsius=Decimal("2.00"),
            actor_user=pharmacist_user,
        )
    assert "Min threshold must be strictly less than max threshold" in str(exc.value)


@pytest.mark.django_db
def test_cashier_denied_and_cross_tenant_rejected(tenant_a, tenant_b, branch_a, cashier_user, pharmacist_user, fridge_location):
    """Test 3: Cashier role denied (PermissionDenied), cross-tenant denied (NotFound)."""
    # Cashier denied
    with pytest.raises(PermissionDenied):
        TemperatureMonitoringService.record_reading(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            storage_location_id=fridge_location.location_id,
            temperature_celsius=Decimal("4.50"),
            actor_user=cashier_user,
        )

    # Cross-tenant denied
    with pytest.raises(NotFound):
        TemperatureMonitoringService.record_reading(
            tenant_id=tenant_b.tenant_id,
            branch_id=branch_a.branch_id,
            storage_location_id=fridge_location.location_id,
            temperature_celsius=Decimal("4.50"),
            actor_user=pharmacist_user,
        )


@pytest.mark.django_db
def test_rest_api_reading_and_batch_endpoints(tenant_a, branch_a, pharmacist_user, fridge_location):
    """Test 4: Full REST API workflow for single log creation and batch import."""
    client = APIClient()
    client.force_authenticate(user=pharmacist_user)

    # 1. Single reading POST
    res_single = client.post(
        "/api/v2/inventory/cold-chain/temperature-logs/",
        {
            "branch": branch_a.branch_id,
            "storage_location": fridge_location.location_id,
            "temperature_celsius": "4.80",
            "humidity_percentage": "55.00",
            "min_threshold_celsius": "2.00",
            "max_threshold_celsius": "8.00",
            "notes": "REST API automated check",
        },
        format="json",
    )
    assert res_single.status_code == status.HTTP_201_CREATED
    data_single = res_single.json()
    assert data_single["is_excursion"] is False
    assert data_single["storage_location_code"] == "FRIDGE-BIOL-01"

    # 2. Batch import POST
    res_batch = client.post(
        "/api/v2/inventory/cold-chain/temperature-logs/import-batch/",
        {
            "branch": branch_a.branch_id,
            "storage_location": fridge_location.location_id,
            "readings": [
                {"temperature_celsius": "3.50", "humidity_percentage": "48.00"},
                {"temperature_celsius": "12.00", "humidity_percentage": "65.00"}, # Excursion
            ],
            "notes": "Bulk USB data sync",
        },
        format="json",
    )
    assert res_batch.status_code == status.HTTP_201_CREATED
    data_batch = res_batch.json()
    assert len(data_batch) == 2

    # 3. Query excursions only
    res_filter = client.get("/api/v2/inventory/cold-chain/temperature-logs/?excursions_only=true")
    assert res_filter.status_code == status.HTTP_200_OK
    results = res_filter.json()
    # At least 1 excursion exists
    items = results.get("results", results) if isinstance(results, dict) else results
    assert all(r["is_excursion"] is True for r in items)

