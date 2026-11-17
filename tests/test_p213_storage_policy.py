"""
backend/tests/test_p213_storage_policy.py
Acceptance tests for P213: Storage policy enforcement.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid scenario: Cold-chain medicine assigned to a refrigerated storage zone succeeds.
2. Invalid scenario: Cold-chain medicine assignment to ambient shelf rejected (ValidationError / HTTP 422).
3. Pre-validation endpoint: Accurately checks compatibility prior to relocation.
4. RBAC: Cashier role is strictly denied from relocating or assigning stock (HTTP 403 Forbidden).
5. Cross-tenant isolation: Tenant B cannot access or relocate Tenant A's batches (HTTP 404 NotFound).
6. Cross-branch isolation: Assigning batch to another branch's storage location rejected.
7. Audit reconciliation: Audits active branch inventory for storage policy compliance.
"""

import uuid
from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.identity.models import PharmacyUser, UserRole
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    StorageLocation,
    StorageLocationType,
    StorageZonePolicy,
    Tenant,
    Warehouse,
    WarehouseType,
)
from apps.core.storage_policy_service import StoragePolicyService


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="Evercare Central Hospital",
        subdomain=f"evercare-pol-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Square Hospital Group",
        subdomain=f"square-pol-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def branch_a1(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-a1-{uid}",
        tenant=tenant_a,
        name="Main Pharmacy Branch 1",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-a2-{uid}",
        tenant=tenant_a,
        name="OPD Pharmacy Branch 2",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-b-{uid}",
        tenant=tenant_b,
        name="Square Gulshan Branch",
        is_active=True,
    )


@pytest.fixture
def pharmacist_user(tenant_a, branch_a1):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_storage@evercare.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a1.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_user(tenant_a, branch_a1):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="cashier_storage@evercare.com",
        password="ValidPassword123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a1.branch_id,
        is_active=True,
    )


@pytest.fixture
def tenant_b_pharmacist(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_b@square.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        is_active=True,
    )


@pytest.fixture
def cold_warehouse(tenant_a, branch_a1):
    return Warehouse.objects.create(
        warehouse_id=f"wh-cold-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a1,
        name="Main Cold Chain Warehouse",
        code="WH-COLD-01",
        warehouse_type=WarehouseType.COLD_ROOM,
        is_cold_storage=True,
    )


@pytest.fixture
def ambient_warehouse(tenant_a, branch_a1):
    return Warehouse.objects.create(
        warehouse_id=f"wh-amb-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a1,
        name="Central Ambient Dispensary",
        code="WH-AMB-01",
        warehouse_type=WarehouseType.MAIN_DISPENSARY,
        is_cold_storage=False,
    )


@pytest.fixture
def fridge_location(tenant_a, branch_a1, cold_warehouse):
    return StorageLocation.objects.create(
        location_id=f"loc-fridge-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a1,
        warehouse=cold_warehouse,
        code="FRIDGE-BIOL-01",
        name="Biologicals & Insulin Refrigerator",
        location_type=StorageLocationType.FRIDGE,
        temperature_monitored=True,
        min_temp_celsius=Decimal("2.00"),
        max_temp_celsius=Decimal("8.00"),
    )


@pytest.fixture
def ambient_shelf_location(tenant_a, branch_a1, ambient_warehouse):
    return StorageLocation.objects.create(
        location_id=f"loc-shelf-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a1,
        warehouse=ambient_warehouse,
        code="SHELF-GEN-01",
        name="General Ambient Medicine Shelf",
        location_type=StorageLocationType.SHELF,
        temperature_monitored=False,
    )


@pytest.fixture
def other_branch_location(tenant_a, branch_a2):
    wh = Warehouse.objects.create(
        warehouse_id=f"wh-b2-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a2,
        name="OPD Dispensary",
        code="WH-OPD-02",
        warehouse_type=WarehouseType.MAIN_DISPENSARY,
        is_cold_storage=False,
    )
    return StorageLocation.objects.create(
        location_id=f"loc-b2-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a2,
        warehouse=wh,
        code="SHELF-OPD-01",
        name="OPD Shelf",
        location_type=StorageLocationType.SHELF,
    )


@pytest.fixture
def cold_medicine(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"prod-cold-{uid}",
        brand_name="Erythropoietin 4000IU",
        generic_name="Erythropoietin",
        dosage_form="Prefilled Syringe",
        strength="4000 IU/0.4 mL",
        cost_unit=Decimal("950.00"),
        mrp_unit=Decimal("1100.00"),
        requires_cold_chain=True,
        min_temp_celsius=Decimal("2.00"),
        max_temp_celsius=Decimal("8.00"),
    )


@pytest.fixture
def ambient_medicine(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"prod-amb-{uid}",
        brand_name="Paracetamol 500mg",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="500 mg",
        cost_unit=Decimal("0.80"),
        mrp_unit=Decimal("1.20"),
        requires_cold_chain=False,
    )


@pytest.fixture
def cold_batch_stock(tenant_a, branch_a1, cold_medicine, fridge_location):
    uid = uuid.uuid4().hex[:6]
    return BatchStock.objects.create(
        batch_id=f"bat-cold-{uid}",
        tenant=tenant_a,
        branch=branch_a1,
        product=cold_medicine,
        batch_number=f"EPO-{uid[:4]}",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        storage_location=fridge_location.code,
        quantity_base_units=60,
        cost_per_unit=Decimal("950.00"),
        status=BatchStatus.ACTIVE,
        is_quarantined=False,
    )


@pytest.mark.django_db
def test_valid_cold_chain_relocation(
    api_client, pharmacist_user, cold_batch_stock, fridge_location
):
    """
    Scenario 1: Valid relocation of cold-chain medicine to compliant fridge.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/storage-policies/relocate-batch/"
    payload = {
        "batch_id": cold_batch_stock.batch_id,
        "target_location_id": fridge_location.location_id,
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_200_OK, response.data
    assert response.data["to_location_code"] == fridge_location.code
    assert response.data["is_compliant"] is True


@pytest.mark.django_db
def test_invalid_cold_chain_assignment_to_ambient_shelf_rejected(
    api_client, pharmacist_user, cold_batch_stock, ambient_shelf_location
):
    """
    Scenario 2: Assigning cold-chain medicine to ambient shelf is rejected with validation error.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/storage-policies/relocate-batch/"
    payload = {
        "batch_id": cold_batch_stock.batch_id,
        "target_location_id": ambient_shelf_location.location_id,
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    details = response.data.get("details", response.data)
    assert "storage_location" in details


@pytest.mark.django_db
def test_pre_validation_endpoint(
    api_client, pharmacist_user, cold_medicine, fridge_location, ambient_shelf_location
):
    """
    Scenario 3: Pre-validation endpoint verifies compliance before user attempts move.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/storage-policies/validate-assignment/"

    # Cold fridge check -> True
    resp1 = api_client.post(url, {
        "product_id": str(cold_medicine.product_id),
        "storage_location_id": fridge_location.location_id,
    }, format="json")
    assert resp1.status_code == status.HTTP_200_OK, resp1.data
    assert resp1.data["is_compliant"] is True

    # Ambient shelf check -> False
    resp2 = api_client.post(url, {
        "product_id": str(cold_medicine.product_id),
        "storage_location_id": ambient_shelf_location.location_id,
    }, format="json")
    assert resp2.status_code == status.HTTP_200_OK
    assert resp2.data["is_compliant"] is False
    assert "requires cold-chain" in resp2.data["reason"]


@pytest.mark.django_db
def test_cashier_role_denied(
    api_client, cashier_user, cold_batch_stock, fridge_location
):
    """
    Scenario 4: Cashiers are strictly barred from relocating medicine stock.
    """
    api_client.force_authenticate(user=cashier_user)
    url = "/api/v2/inventory/storage-policies/relocate-batch/"
    payload = {
        "batch_id": cold_batch_stock.batch_id,
        "target_location_id": fridge_location.location_id,
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_cross_tenant_isolation(
    api_client, tenant_b_pharmacist, cold_batch_stock, fridge_location
):
    """
    Scenario 5: Pharmacist from Tenant B cannot access or relocate Tenant A's batches.
    """
    api_client.force_authenticate(user=tenant_b_pharmacist)
    url = "/api/v2/inventory/storage-policies/relocate-batch/"
    payload = {
        "batch_id": cold_batch_stock.batch_id,
        "target_location_id": fridge_location.location_id,
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_cross_branch_relocation_rejected(
    api_client, pharmacist_user, cold_batch_stock, other_branch_location
):
    """
    Scenario 6: Attempting to assign batch to a location belonging to another branch is rejected.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/storage-policies/relocate-batch/"
    payload = {
        "batch_id": cold_batch_stock.batch_id,
        "target_location_id": other_branch_location.location_id,
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    details = response.data.get("details", response.data)
    assert "storage_location" in details


@pytest.mark.django_db
def test_branch_audit_reconciliation(
    api_client, pharmacist_user, branch_a1, cold_batch_stock
):
    """
    Scenario 7: Audit reconciliation reports compliance statistics for branch stock.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = f"/api/v2/inventory/storage-policies/audit-reconciliation/?branch={branch_a1.branch_id}"
    response = api_client.get(url)
    assert response.status_code == status.HTTP_200_OK
    assert response.data["total_active_batches"] >= 1
    assert response.data["compliant_batches_count"] >= 1
    assert response.data["violation_batches_count"] == 0
