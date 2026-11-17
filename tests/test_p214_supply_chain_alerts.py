"""
backend/tests/test_p214_supply_chain_alerts.py
Acceptance tests for P214: Supply chain stock alerts.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid scenario: Stockout and Reorder alert generation with proper severity.
2. Deduplication: Repeated evaluations increment occurred_count without duplicating open alerts.
3. Negative availability: Negative stock generates critical discrepancy alert.
4. Temperature risk: Temperature excursions generate temperature risk alert.
5. Lifecycle: Acknowledge and resolve transitions with audit stamps.
6. Auto-resolution: Replenishing stock into safe range auto-resolves open alerts.
7. RBAC: Cashier role is strictly denied from managing alerts (HTTP 403 Forbidden).
8. Cross-tenant isolation: Tenant B cannot access or modify Tenant A's alerts (HTTP 404 NotFound).
"""

import uuid
from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.identity.models import PharmacyUser, UserRole
from apps.core.models import (
    AlertSeverity,
    AlertStatus,
    Branch,
    BranchStockAvailability,
    MedicineProduct,
    StockAlertType,
    StorageLocation,
    StorageLocationType,
    SupplyChainStockAlert,
    Tenant,
    Warehouse,
    WarehouseType,
)
from apps.core.supply_chain_alert_service import SupplyChainAlertService


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="Labaid Central Pharmacy",
        subdomain=f"labaid-alert-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Popular Pharmacy Group",
        subdomain=f"popular-alert-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-a-{uid}",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-b-{uid}",
        tenant=tenant_b,
        name="Mirpur Branch",
        is_active=True,
    )


@pytest.fixture
def pharmacist_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_alert@labaid.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="cashier_alert@labaid.com",
        password="ValidPassword123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def tenant_b_pharmacist(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_b@popular.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        is_active=True,
    )


@pytest.fixture
def medicine_napa(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"prod-napa-{uid}",
        brand_name="Napa Extra 500/65mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500 mg + 65 mg",
        cost_unit=Decimal("2.00"),
        mrp_unit=Decimal("2.50"),
    )


@pytest.fixture
def fridge_location(tenant_a, branch_a):
    wh = Warehouse.objects.create(
        warehouse_id=f"wh-cold-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        name="Main Cold Storage",
        code="WH-COLD-01",
        warehouse_type=WarehouseType.COLD_ROOM,
        is_cold_storage=True,
    )
    return StorageLocation.objects.create(
        location_id=f"loc-fridge-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        warehouse=wh,
        code="FRIDGE-VACC-01",
        name="Vaccine Storage Unit 1",
        location_type=StorageLocationType.FRIDGE,
    )


@pytest.mark.django_db
def test_stockout_and_reorder_alert_generation(
    tenant_a, branch_a, medicine_napa
):
    """
    Scenario 1: Evaluates stock levels, verifying stockout and reorder alerts.
    """
    # 1. Stockout alert
    alert_stockout = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=0,
        reorder_level=20,
    )
    assert alert_stockout is not None
    assert alert_stockout.alert_type == StockAlertType.STOCKOUT
    assert alert_stockout.severity == AlertSeverity.CRITICAL
    assert alert_stockout.status == AlertStatus.OPEN
    assert alert_stockout.occurred_count == 1

    # Resolve stockout first so reorder can trigger
    alert_stockout.status = AlertStatus.RESOLVED
    alert_stockout.save()

    # 2. Reorder alert
    alert_reorder = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=15,
        reorder_level=20,
    )
    assert alert_reorder is not None
    assert alert_reorder.alert_type == StockAlertType.REORDER_REQUIRED
    assert alert_reorder.severity == AlertSeverity.WARNING


@pytest.mark.django_db
def test_alert_deduplication(
    tenant_a, branch_a, medicine_napa
):
    """
    Scenario 2: Repeated evaluation of same condition deduplicates into existing alert.
    """
    alert1 = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=0,
        reorder_level=30,
    )
    assert alert1.occurred_count == 1

    # Second evaluation while alert is still OPEN
    alert2 = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=0,
        reorder_level=30,
    )
    assert alert2.id == alert1.id
    assert alert2.occurred_count == 2

    # Verify only 1 open record in DB
    open_alerts = SupplyChainStockAlert.objects.filter(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        status=AlertStatus.OPEN,
    )
    assert open_alerts.count() == 1


@pytest.mark.django_db
def test_negative_availability_anomaly(
    tenant_a, branch_a, medicine_napa
):
    """
    Scenario 3: Negative stock creates critical anomaly alert.
    """
    alert = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=-10,
        reorder_level=20,
    )
    assert alert is not None
    assert alert.alert_type == StockAlertType.NEGATIVE_AVAILABILITY
    assert alert.severity == AlertSeverity.CRITICAL


@pytest.mark.django_db
def test_temperature_risk_alert_and_dedup(
    tenant_a, branch_a, fridge_location
):
    """
    Scenario 4: Temperature breach triggers deduplicated temperature risk alert.
    """
    alert1 = SupplyChainAlertService.evaluate_temperature_risk(
        tenant=tenant_a,
        branch=branch_a,
        storage_location=fridge_location,
        recorded_temp=Decimal("14.00"),
    )
    assert alert1 is not None
    assert alert1.alert_type == StockAlertType.TEMPERATURE_RISK
    assert alert1.severity in [AlertSeverity.CRITICAL, AlertSeverity.EMERGENCY]

    # Repeat excursion reading
    alert2 = SupplyChainAlertService.evaluate_temperature_risk(
        tenant=tenant_a,
        branch=branch_a,
        storage_location=fridge_location,
        recorded_temp=Decimal("15.50"),
    )
    assert alert2.id == alert1.id
    assert alert2.occurred_count == 2


@pytest.mark.django_db
def test_alert_lifecycle_api(
    api_client, pharmacist_user, tenant_a, branch_a, medicine_napa
):
    """
    Scenario 5: Pharmacist acknowledges and resolves alert via API endpoints.
    """
    alert = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=5,
        reorder_level=20,
    )

    api_client.force_authenticate(user=pharmacist_user)

    # 1. Acknowledge
    ack_url = f"/api/v2/inventory/alerts/{alert.id}/acknowledge/"
    ack_resp = api_client.post(ack_url, {"notes": "PO placed with supplier Square"}, format="json")
    assert ack_resp.status_code == status.HTTP_200_OK
    assert ack_resp.data["status"] == AlertStatus.ACKNOWLEDGED
    assert ack_resp.data["acknowledged_by_name"] == pharmacist_user.email

    # 2. Resolve
    res_url = f"/api/v2/inventory/alerts/{alert.id}/resolve/"
    res_resp = api_client.post(res_url, {"resolution_notes": "Stock received and replenished."}, format="json")
    assert res_resp.status_code == status.HTTP_200_OK
    assert res_resp.data["status"] == AlertStatus.RESOLVED


@pytest.mark.django_db
def test_auto_resolution_on_replenishment(
    tenant_a, branch_a, medicine_napa
):
    """
    Scenario 6: Replenishing stock into safe range auto-resolves open alerts.
    """
    alert = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=5,
        reorder_level=20,
    )
    assert alert.status == AlertStatus.OPEN

    # Replenished to 100 units (> 20)
    result = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=100,
        reorder_level=20,
    )
    assert result is None

    alert.refresh_from_db()
    assert alert.status == AlertStatus.RESOLVED
    assert "Auto-resolved" in alert.resolution_notes


@pytest.mark.django_db
def test_cashier_role_denied(
    api_client, cashier_user, tenant_a, branch_a, medicine_napa
):
    """
    Scenario 7: Cashiers cannot acknowledge or scan alerts.
    """
    alert = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=0,
        reorder_level=20,
    )

    api_client.force_authenticate(user=cashier_user)
    url = f"/api/v2/inventory/alerts/{alert.id}/acknowledge/"
    response = api_client.post(url, {}, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_cross_tenant_isolation(
    api_client, tenant_b_pharmacist, tenant_a, branch_a, medicine_napa
):
    """
    Scenario 8: Tenant B cannot access Tenant A's alerts.
    """
    alert = SupplyChainAlertService.evaluate_stock_level(
        tenant=tenant_a,
        branch=branch_a,
        product=medicine_napa,
        quantity_on_hand=0,
        reorder_level=20,
    )

    api_client.force_authenticate(user=tenant_b_pharmacist)
    url = f"/api/v2/inventory/alerts/{alert.id}/"
    response = api_client.get(url)
    assert response.status_code == status.HTTP_404_NOT_FOUND
