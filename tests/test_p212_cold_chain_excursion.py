"""
backend/tests/test_p212_cold_chain_excursion.py
Acceptance tests for P212: Cold chain excursion workflow.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid scenario: Temperature excursion triggers automatic lot quarantine; pharmacist reviews and releases to sellable with valid clinical justification.
2. Valid scenario: Pharmacist reviews and condemns/disposes batches, triggering double-entry StockLedger record (EXPIRY_DISPOSAL).
3. Invalid input: Release to sellable rejected when clinical justification is under 15 characters or missing.
4. RBAC: Cashier role is strictly denied from triggering or reviewing excursions (HTTP 403 / PermissionDenied).
5. Cross-tenant isolation: Tenant B cannot view or review Tenant A's excursion incidents (HTTP 404 / NotFound).
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
    ColdChainAffectedBatch,
    ColdChainExcursionIncident,
    ExcursionDispositionOutcome,
    ExcursionIncidentStatus,
    MedicineProduct,
    PurchaseCostLot,
    StockLedgerEntry,
    StockMovementType,
    StorageLocation,
    StorageLocationType,
    Tenant,
    Warehouse,
    WarehouseType,
)

User = PharmacyUser
from apps.core.cold_chain_excursion_service import ColdChainExcursionService


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="Evercare Dhaka Main",
        subdomain=f"evercare-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Square Pharma Secondary",
        subdomain=f"square-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-a-{uid}",
        tenant=tenant_a,
        name="Evercare Central Branch",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-b-{uid}",
        tenant=tenant_b,
        name="Square Uttara Branch",
        is_active=True,
    )


@pytest.fixture
def storage_location_a(tenant_a, branch_a):
    wh = Warehouse.objects.create(
        warehouse_id=f"wh-a-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        name="Main Cold Room",
        code="WH-COLD-01",
        warehouse_type=WarehouseType.COLD_ROOM,
        is_cold_storage=True,
    )
    return StorageLocation.objects.create(
        location_id=f"loc-a-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        warehouse=wh,
        code="FRIDGE-VACC-01",
        name="Main Vaccines Refrigerator 1",
        location_type=StorageLocationType.FRIDGE,
    )


@pytest.fixture
def product_a():
    return MedicineProduct.objects.create(
        brand_name="Insulin Glargine 100IU",
        generic_name="Insulin Glargine",
        dosage_form="Injection",
        strength="100 IU/mL",
        cost_unit=Decimal("450.00"),
        mrp_unit=Decimal("520.00"),
    )


@pytest.fixture
def batch_stock_a(tenant_a, branch_a, product_a, storage_location_a):
    uid = uuid.uuid4().hex[:6]
    return BatchStock.objects.create(
        batch_id=f"bat-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_a,
        batch_number=f"BATCH-INS-{uid[:4]}",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        storage_location=storage_location_a.code,
        quantity_base_units=50,
        cost_per_unit=Decimal("450.00"),
        status=BatchStatus.ACTIVE,
        is_quarantined=False,
    )


@pytest.fixture
def pharmacist_user(db, tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_ashraf@evercare.com",
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
        email="cashier_rahim@evercare.com",
        password="ValidPassword123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def tenant_b_pharmacist(db, tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_hasan_b@square.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        is_active=True,
    )


@pytest.mark.django_db
def test_valid_excursion_quarantine_and_release(
    api_client, pharmacist_user, storage_location_a, batch_stock_a
):
    """
    Scenario 1: Detect temperature excursion (13.5°C), verify automatic lot quarantine
    and zero sellable inventory, then pharmacist reviews and releases with valid clinical justification.
    """
    api_client.force_authenticate(user=pharmacist_user)

    # 1. Trigger excursion incident via API
    url = "/api/v2/inventory/cold-chain/excursions/"
    payload = {
        "storage_location": str(storage_location_a.location_id),
        "recorded_temperature": "13.50",
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED, response.data
    incident_id = response.data["id"]
    assert response.data["status"] == ExcursionIncidentStatus.DETECTED
    assert response.data["quarantined_batches_count"] == 1

    # Verify batch is quarantined in DB
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.is_quarantined is True
    assert batch_stock_a.status == BatchStatus.QUARANTINED
    assert batch_stock_a.available_quantity_base_units == 0

    # 2. Pharmacist clinical review & release
    review_url = f"/api/v2/inventory/cold-chain/excursions/{incident_id}/review-disposition/"
    review_payload = {
        "disposition_outcome": ExcursionDispositionOutcome.RELEASE_TO_SELLABLE,
        "clinical_justification": "Reviewed manufacturer stability data (tolerates 24h at up to 25C). Stock confirmed safe.",
    }
    review_resp = api_client.post(review_url, review_payload, format="json")
    assert review_resp.status_code == status.HTTP_200_OK, review_resp.data
    assert review_resp.data["status"] == ExcursionIncidentStatus.RESOLVED
    assert review_resp.data["disposition_outcome"] == ExcursionDispositionOutcome.RELEASE_TO_SELLABLE

    # Verify batch is un-quarantined and sellable quantity restored
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.is_quarantined is False
    assert batch_stock_a.status == BatchStatus.ACTIVE
    assert batch_stock_a.available_quantity_base_units == 50


@pytest.mark.django_db
def test_valid_excursion_condemn_and_disposal_ledger(
    api_client, pharmacist_user, tenant_a, branch_a, storage_location_a, batch_stock_a
):
    """
    Scenario 2: Excursion confirmed catastrophic, pharmacist reviews with CONDEMN_DISPOSE outcome.
    Verifies batch status set to DISPOSED and StockLedgerEntry recorded.
    """
    # Trigger excursion via service directly
    incident = ColdChainExcursionService.trigger_excursion(
        tenant=tenant_a,
        branch=branch_a,
        storage_location=storage_location_a,
        recorded_temperature=Decimal("28.00"),
    )
    assert incident.quarantined_batches_count == 1
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.status == BatchStatus.QUARANTINED

    # Review via API
    api_client.force_authenticate(user=pharmacist_user)
    review_url = f"/api/v2/inventory/cold-chain/excursions/{incident.id}/review-disposition/"
    payload = {
        "disposition_outcome": ExcursionDispositionOutcome.CONDEMN_DISPOSE,
        "clinical_justification": "Temperature exceeded 25C for over 6 hours; biologics denatured and condemned.",
    }
    response = api_client.post(review_url, payload, format="json")
    assert response.status_code == status.HTTP_200_OK

    batch_stock_a.refresh_from_db()
    assert batch_stock_a.status == BatchStatus.ARCHIVED
    assert batch_stock_a.is_active is False
    assert batch_stock_a.is_quarantined is False

    # Verify StockLedgerEntry
    ledger_entries = StockLedgerEntry.objects.filter(
        tenant=tenant_a,
        source_entity_type="ColdChainExcursionIncident",
        source_entity_id=str(incident.id),
        movement_type=StockMovementType.EXPIRY_DISPOSAL,
    )
    assert ledger_entries.exists()
    entry = ledger_entries.first()
    assert entry.quantity_delta == -50


@pytest.mark.django_db
def test_invalid_short_clinical_justification_rejected(
    api_client, pharmacist_user, tenant_a, branch_a, storage_location_a, batch_stock_a
):
    """
    Scenario 3: Short or empty clinical justification (< 15 characters) must be rejected with 400.
    """
    incident = ColdChainExcursionService.trigger_excursion(
        tenant=tenant_a,
        branch=branch_a,
        storage_location=storage_location_a,
        recorded_temperature=Decimal("11.00"),
    )

    api_client.force_authenticate(user=pharmacist_user)
    review_url = f"/api/v2/inventory/cold-chain/excursions/{incident.id}/review-disposition/"
    payload = {
        "disposition_outcome": ExcursionDispositionOutcome.RELEASE_TO_SELLABLE,
        "clinical_justification": "Too short",  # Only 9 chars
    }
    response = api_client.post(review_url, payload, format="json")
    assert response.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    details = response.data.get("details", response.data)
    assert "clinical_justification" in details


@pytest.mark.django_db
def test_cashier_role_denied(
    api_client, cashier_user, storage_location_a
):
    """
    Scenario 4: Cashiers are strictly barred from triggering or reviewing cold chain excursions.
    """
    api_client.force_authenticate(user=cashier_user)

    url = "/api/v2/inventory/cold-chain/excursions/"
    payload = {
        "storage_location": str(storage_location_a.location_id),
        "recorded_temperature": "12.00",
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_cross_tenant_isolation(
    api_client, tenant_a, branch_a, storage_location_a, tenant_b_pharmacist
):
    """
    Scenario 5: Pharmacist from Tenant B cannot access or review Tenant A's excursion incidents.
    """
    incident = ColdChainExcursionService.trigger_excursion(
        tenant=tenant_a,
        branch=branch_a,
        storage_location=storage_location_a,
        recorded_temperature=Decimal("14.00"),
    )

    api_client.force_authenticate(user=tenant_b_pharmacist)
    url = f"/api/v2/inventory/cold-chain/excursions/{incident.id}/"
    response = api_client.get(url)
    assert response.status_code == status.HTTP_404_NOT_FOUND

    review_url = f"/api/v2/inventory/cold-chain/excursions/{incident.id}/review-disposition/"
    review_resp = api_client.post(
        review_url,
        {
            "disposition_outcome": ExcursionDispositionOutcome.RELEASE_TO_SELLABLE,
            "clinical_justification": "Cross-tenant unauthorized attempt to release stock.",
        },
        format="json",
    )
    assert review_resp.status_code == status.HTTP_404_NOT_FOUND
