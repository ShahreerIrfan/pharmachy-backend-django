"""
backend/tests/test_p215_inventory_snapshots.py
Acceptance tests for P215: Inventory snapshots.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid scenario: Snapshot generation strictly derived from canonical StockLedgerEntry transactions.
2. Mathematical reconciliation: Ledger delta sum matches physical batch balance.
3. Cryptographic integrity: SHA-256 checksum over line items verified.
4. Incremental / Reversal movements: Additional adjustments roll up accurately into new checkpoints.
5. RBAC: Cashier role is strictly denied from generating snapshots (HTTP 403 Forbidden).
6. Cross-tenant isolation: Tenant B cannot access or generate Tenant A's snapshots (HTTP 404 NotFound).
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
    InventorySnapshotCheckpoint,
    InventorySnapshotLineItem,
    MedicineProduct,
    SnapshotCheckpointType,
    StockLedgerEntry,
    StockMovementType,
    Tenant,
)
from apps.core.inventory_snapshot_service import InventorySnapshotService


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="United Hospital Pharmacy",
        subdomain=f"united-snap-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Apollo Dhaka Dispensary",
        subdomain=f"apollo-snap-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-a-{uid}",
        tenant=tenant_a,
        name="Central Inpatient Pharmacy",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-b-{uid}",
        tenant=tenant_b,
        name="Apollo Gulshan Branch",
        is_active=True,
    )


@pytest.fixture
def pharmacist_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_snapshot@united.com",
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
        email="cashier_snapshot@united.com",
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
        email="pharmacist_b@apollo.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        is_active=True,
    )


@pytest.fixture
def product_insulin(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"prod-ins-{uid}",
        brand_name="Mixtard 30 100IU",
        generic_name="Biphasic Isophane Insulin",
        dosage_form="Vial",
        strength="100 IU/mL",
        cost_unit=Decimal("420.00"),
        mrp_unit=Decimal("480.00"),
        requires_cold_chain=True,
    )


@pytest.fixture
def batch_insulin(tenant_a, branch_a, product_insulin):
    uid = uuid.uuid4().hex[:6]
    return BatchStock.objects.create(
        batch_id=f"bat-ins-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch_number=f"MIX-{uid[:4]}",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        storage_location="FRIDGE-01",
        quantity_base_units=40,
        cost_per_unit=Decimal("420.00"),
        status=BatchStatus.ACTIVE,
        is_quarantined=False,
    )


@pytest.fixture
def ledger_entries(tenant_a, branch_a, product_insulin, batch_insulin, pharmacist_user):
    """
    Creates canonical StockLedgerEntry rows:
    +50 received, -10 sold -> balance 40.
    """
    e1 = StockLedgerEntry.objects.create(
        entry_id=f"sle-{uuid.uuid4().hex[:12]}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch=batch_insulin,
        batch_number=batch_insulin.batch_number,
        movement_type=StockMovementType.PURCHASE_RECEIPT,
        quantity_delta=50,
        balance_before=0,
        balance_after=50,
        unit_cost=Decimal("420.00"),
        total_cost=Decimal("21000.00"),
        source_entity_type="GoodsReceiptNote",
        source_entity_id="GRN-001",
        source_reference="PO-001",
        immutable_verification_hash="hash-e1",
        posted_by=pharmacist_user,
    )
    e2 = StockLedgerEntry.objects.create(
        entry_id=f"sle-{uuid.uuid4().hex[:12]}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch=batch_insulin,
        batch_number=batch_insulin.batch_number,
        movement_type=StockMovementType.SALE_DISPENSE,
        quantity_delta=-10,
        balance_before=50,
        balance_after=40,
        unit_cost=Decimal("420.00"),
        total_cost=Decimal("4200.00"),
        source_entity_type="Invoice",
        source_entity_id="INV-001",
        source_reference="SALE-001",
        immutable_verification_hash="hash-e2",
        posted_by=pharmacist_user,
    )
    return [e1, e2]


@pytest.mark.django_db
def test_valid_snapshot_generation_and_reconciliation(
    api_client, pharmacist_user, branch_a, batch_insulin, ledger_entries
):
    """
    Scenario 1: Generates inventory snapshot strictly derived from stock ledger.
    Validates total quantity = 40, total valuation = 40 * 420 = 16800.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/snapshots/generate/"
    payload = {
        "branch": branch_a.branch_id,
        "checkpoint_type": SnapshotCheckpointType.DAILY_EOD,
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED, response.data
    data = response.data

    assert data["branch"] == branch_a.branch_id
    assert data["checkpoint_type"] == SnapshotCheckpointType.DAILY_EOD
    assert data["total_quantity_base_units"] == 40
    assert Decimal(data["total_valuation_bdt"]) == Decimal("16800.00")
    assert data["ledger_entries_processed"] == 2
    assert data["is_canonical_reconciled"] is True
    assert len(data["sha256_checksum"]) == 64

    # Verify line items
    lines = data["line_items"]
    assert len(lines) == 1
    assert lines[0]["batch_number"] == batch_insulin.batch_number
    assert lines[0]["quantity_base_units"] == 40
    assert lines[0]["ledger_delta_sum"] == 40
    assert lines[0]["reconciled_discrepancy"] == 0


@pytest.mark.django_db
def test_snapshot_cryptographic_integrity_verification(
    api_client, pharmacist_user, tenant_a, branch_a, batch_insulin, ledger_entries
):
    """
    Scenario 2: Verifies SHA-256 checksum integrity of generated snapshot.
    """
    snapshot = InventorySnapshotService.generate_snapshot(
        tenant=tenant_a,
        branch=branch_a,
        actor_user=pharmacist_user,
    )

    api_client.force_authenticate(user=pharmacist_user)
    url = f"/api/v2/inventory/snapshots/{snapshot.snapshot_id}/verify-integrity/"
    response = api_client.post(url, {}, format="json")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["is_valid"] is True
    assert response.data["stored_checksum"] == snapshot.sha256_checksum


@pytest.mark.django_db
def test_incremental_reversal_movement_snapshot(
    api_client, pharmacist_user, tenant_a, branch_a, product_insulin, batch_insulin, ledger_entries
):
    """
    Scenario 3: An adjustment movement (+5) is recorded; snapshot captures new total (45).
    """
    # Record stock adjustment in ledger and update physical batch
    StockLedgerEntry.objects.create(
        entry_id=f"sle-{uuid.uuid4().hex[:12]}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch=batch_insulin,
        batch_number=batch_insulin.batch_number,
        movement_type=StockMovementType.STOCK_ADJUSTMENT_IN,
        quantity_delta=5,
        balance_before=40,
        balance_after=45,
        unit_cost=Decimal("420.00"),
        total_cost=Decimal("2100.00"),
        source_entity_type="StockAdjustment",
        source_entity_id="ADJ-001",
        source_reference="COUNT-001",
        immutable_verification_hash="hash-adj",
        posted_by=pharmacist_user,
    )
    batch_insulin.quantity_base_units = 45
    batch_insulin.save(update_fields=["quantity_base_units"])

    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/snapshots/generate/"
    response = api_client.post(url, {"branch": branch_a.branch_id}, format="json")
    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["total_quantity_base_units"] == 45
    assert Decimal(response.data["total_valuation_bdt"]) == Decimal("18900.00") # 45 * 420
    assert response.data["is_canonical_reconciled"] is True


@pytest.mark.django_db
def test_cashier_role_denied(
    api_client, cashier_user, branch_a
):
    """
    Scenario 4: Cashiers are strictly barred from generating inventory snapshots.
    """
    api_client.force_authenticate(user=cashier_user)
    url = "/api/v2/inventory/snapshots/generate/"
    response = api_client.post(url, {"branch": branch_a.branch_id}, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
def test_cross_tenant_isolation(
    api_client, tenant_b_pharmacist, tenant_a, branch_a, batch_insulin, ledger_entries
):
    """
    Scenario 5: Tenant B cannot access or generate Tenant A's snapshots.
    """
    snapshot = InventorySnapshotService.generate_snapshot(
        tenant=tenant_a,
        branch=branch_a,
    )

    api_client.force_authenticate(user=tenant_b_pharmacist)
    url = f"/api/v2/inventory/snapshots/{snapshot.snapshot_id}/"
    response = api_client.get(url)
    assert response.status_code == status.HTTP_404_NOT_FOUND
