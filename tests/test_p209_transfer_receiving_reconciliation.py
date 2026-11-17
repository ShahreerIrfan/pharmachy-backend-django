"""
test_p209_transfer_receiving_reconciliation.py
Architecture Decision: ADR-0208, ADR-0209 (P209).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Acceptance Criteria:
1. Valid partial & damaged receipt reconciliation:
   - 70 good units credited to sellable stock.
   - 20 damaged units quarantined (BatchStatus.QUARANTINED, zero sellable availability).
   - 10 lost units documented in transit loss ledger.
   - Total conservation: 70 + 20 + 10 == 100 dispatched. Zero duplicate or lost quantities.
   - Status transitions to RECONCILED.
2. Invalid or incomplete input:
   - Discrepancy mismatch (good + damaged + lost != dispatched) raises ValidationError.
3. Unauthorized and cross-tenant guard:
   - Cashier role barred from reconciliation (PermissionDenied / HTTP 403).
   - Cross-tenant user barred (NotFound / HTTP 404).
4. Duplicate / Idempotency protection:
   - Duplicate receive/reconcile call on already received transfer rejected with ERR_TRANSFER_ALREADY_RECEIVED.
5. Full REST API flow test.
"""

from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from apps.core.models import (
    Tenant,
    Branch,
    MedicineProduct,
    BatchStock,
    BatchStatus,
    InterBranchTransferRequest,
    InterBranchTransferItem,
    InterBranchTransferStatus,
    InterBranchTransferPriority,
    StockLedgerEntry,
    StockMovementType,
)
from apps.core.interbranch_transfer_service import InterBranchTransferService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(
        tenant_id="ten-recon-01",
        name="Evercare Pharmacy Chain",
        subdomain="evercare-recon-01",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(
        tenant_id="ten-recon-02",
        name="Square Pharmacy Chain",
        subdomain="square-recon-02",
        status="ACTIVE",
    )


@pytest.fixture
def branch_source(db, tenant_a):
    return Branch.objects.create(
        branch_id="br-recon-src",
        tenant=tenant_a,
        name="Central Warehouse Branch",
        is_active=True,
    )


@pytest.fixture
def branch_dest(db, tenant_a):
    return Branch.objects.create(
        branch_id="br-recon-dest",
        tenant=tenant_a,
        name="Dhanmondi Retail Branch",
        is_active=True,
    )


@pytest.fixture
def branch_cross(db, tenant_b):
    return Branch.objects.create(
        branch_id="br-recon-cross",
        tenant=tenant_b,
        name="Square Gulshan Branch",
        is_active=True,
    )


@pytest.fixture
def pharmacist_user_a(db, tenant_a, branch_dest):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700900001",
        email="pharm_recon@evercare.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_dest.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_user_a(db, tenant_a, branch_dest):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700900002",
        email="cashier_recon@evercare.com",
        password="ValidPassword123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_dest.branch_id,
        is_active=True,
    )


@pytest.fixture
def pharmacist_user_b(db, tenant_b, branch_cross):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700900003",
        email="pharm_cross@square.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_cross.branch_id,
        is_active=True,
    )


@pytest.fixture
def product_azithro(db):
    return MedicineProduct.objects.create(
        product_id="med-recon-azithro",
        brand_name="Zimax 500mg",
        generic_name="Azithromycin",
        dosage_form="Tablet",
        strength="500mg",
        cost_unit=Decimal("25.0000"),
        mrp_unit=Decimal("35.0000"),
    )


@pytest.fixture
def batch_source(db, tenant_a, branch_source, product_azithro):
    return BatchStock.objects.create(
        batch_id="batch-recon-src-01",
        tenant=tenant_a,
        branch=branch_source,
        product=product_azithro,
        batch_number="BATCH-ZIM-2026-X1",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=700),
        quantity_base_units=100,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("25.0000"),
        storage_location="WAREHOUSE-BAY-1",
        status=BatchStatus.ACTIVE,
    )


@pytest.fixture
def dispatched_transfer(tenant_a, branch_source, branch_dest, pharmacist_user_a, batch_source):
    """Creates, approves, and dispatches an interbranch transfer of 100 units."""
    transfer = InterBranchTransferService.create_transfer_request(
        tenant_id=tenant_a.tenant_id,
        source_branch_id=branch_source.branch_id,
        destination_branch_id=branch_dest.branch_id,
        priority=InterBranchTransferPriority.URGENT,
        items_payload=[
            {
                "batch_id": batch_source.batch_id,
                "quantity_requested": 100,
                "destination_storage_location": "SHELF-MAIN",
            }
        ],
        notes="Urgent replenishment for Dhanmondi clinic.",
        actor_user=pharmacist_user_a,
    )
    transfer = InterBranchTransferService.approve_transfer(
        transfer_id=transfer.transfer_id,
        tenant_id=tenant_a.tenant_id,
        actor_user=pharmacist_user_a,
    )
    transfer = InterBranchTransferService.dispatch_transfer(
        transfer_id=transfer.transfer_id,
        tenant_id=tenant_a.tenant_id,
        carrier_notes="Shipped via DHL Secure ColdVan.",
        actor_user=pharmacist_user_a,
    )
    return transfer


@pytest.mark.django_db
def test_partial_damaged_receipt_reconciliation(dispatched_transfer, tenant_a, branch_dest, pharmacist_user_a, product_azithro):
    """
    Scenario 1: Dispatched 100 units.
    Reconciles: 70 good units, 20 damaged units, 10 lost in transit units.
    Validates conservation, quarantine isolation, zero ghost units, and RECONCILED status.
    """
    item = dispatched_transfer.items.first()
    reconciled = InterBranchTransferService.receive_transfer(
        transfer_id=dispatched_transfer.transfer_id,
        tenant_id=tenant_a.tenant_id,
        received_items_payload=[
            {
                "item_id": item.item_id,
                "quantity_received_good": 70,
                "quantity_received_damaged": 20,
                "quantity_lost": 10,
                "discrepancy_reason": "Outer packaging water damage during transit; 10 units crushed/lost.",
                "damage_storage_location": "QUARANTINE-DAMAGED",
            }
        ],
        receiving_notes="Inspection completed by Pharmacist on arrival.",
        actor_user=pharmacist_user_a,
    )

    assert reconciled.status == InterBranchTransferStatus.RECONCILED
    assert reconciled.total_units_received == 90  # 70 good + 20 damaged
    assert reconciled.total_units_received_good == 70
    assert reconciled.total_units_received_damaged == 20
    assert reconciled.total_units_lost == 10

    # Line item checks
    item.refresh_from_db()
    assert item.quantity_received_good == 70
    assert item.quantity_received_damaged == 20
    assert item.quantity_lost == 10
    assert item.is_reconciled is True
    assert item.receipt_ledger_entry_id != ""
    assert item.damage_ledger_entry_id != ""

    # Verify destination sellable batch has exactly 70 units
    dest_good_batch = BatchStock.objects.get(
        tenant=tenant_a,
        branch=branch_dest,
        product=product_azithro,
        batch_number=item.batch_number,
        status=BatchStatus.ACTIVE,
    )
    assert dest_good_batch.quantity_base_units == 70
    assert dest_good_batch.available_quantity_base_units == 70

    # Verify quarantined damaged batch has exactly 20 units and status is QUARANTINED
    dest_damaged_batch = BatchStock.objects.get(
        tenant=tenant_a,
        branch=branch_dest,
        product=product_azithro,
        batch_number=f"{item.batch_number}-DMG",
        status=BatchStatus.QUARANTINED,
    )
    assert dest_damaged_batch.quantity_base_units == 20
    assert dest_damaged_batch.storage_location == "QUARANTINE-DAMAGED"

    # Verify StockLedger entries
    good_movement = StockLedgerEntry.objects.get(entry_id=item.receipt_ledger_entry_id)
    assert good_movement.movement_type == StockMovementType.TRANSFER_IN
    assert good_movement.quantity_delta == 70

    damage_movement = StockLedgerEntry.objects.get(entry_id=item.damage_ledger_entry_id)
    assert damage_movement.movement_type == StockMovementType.TRANSFER_IN
    assert damage_movement.quantity_delta == 20


@pytest.mark.django_db
def test_discrepancy_conservation_mismatch_rejected(dispatched_transfer, tenant_a, pharmacist_user_a):
    """
    Scenario 2: Dispatched 100 units.
    Reconciliation payload gives 60 good + 10 damaged + 5 lost = 75 != 100.
    System must reject with ValidationError (ERR_DISCREPANCY_CONSERVATION).
    """
    item = dispatched_transfer.items.first()
    with pytest.raises(ValidationError) as exc:
        InterBranchTransferService.receive_transfer(
            transfer_id=dispatched_transfer.transfer_id,
            tenant_id=tenant_a.tenant_id,
            received_items_payload=[
                {
                    "item_id": item.item_id,
                    "quantity_received_good": 60,
                    "quantity_received_damaged": 10,
                    "quantity_lost": 5,
                    "discrepancy_reason": "Incomplete count",
                }
            ],
            actor_user=pharmacist_user_a,
        )
    assert "Discrepancy conservation failure" in str(exc.value)


@pytest.mark.django_db
def test_unauthorized_and_cross_tenant_reconciliation_rejected(
    dispatched_transfer, tenant_a, tenant_b, cashier_user_a, pharmacist_user_b
):
    """
    Scenario 3:
    - Cashier role cannot reconcile transfers (PermissionDenied).
    - Cross-tenant user cannot reconcile another tenant's transfer (NotFound).
    """
    item = dispatched_transfer.items.first()
    payload = [
        {
            "item_id": item.item_id,
            "quantity_received_good": 100,
            "quantity_received_damaged": 0,
            "quantity_lost": 0,
        }
    ]

    # Cashier attempt
    with pytest.raises(PermissionDenied):
        InterBranchTransferService.receive_transfer(
            transfer_id=dispatched_transfer.transfer_id,
            tenant_id=tenant_a.tenant_id,
            received_items_payload=payload,
            actor_user=cashier_user_a,
        )

    # Cross-tenant attempt
    with pytest.raises(NotFound):
        InterBranchTransferService.receive_transfer(
            transfer_id=dispatched_transfer.transfer_id,
            tenant_id=tenant_b.tenant_id,
            received_items_payload=payload,
            actor_user=pharmacist_user_b,
        )


@pytest.mark.django_db
def test_duplicate_reconciliation_prevented(dispatched_transfer, tenant_a, pharmacist_user_a):
    """
    Scenario 4: Re-receiving or duplicate reconciliation attempt must fail with
    ERR_TRANSFER_ALREADY_RECEIVED, preventing double stock posting.
    """
    item = dispatched_transfer.items.first()
    payload = [
        {
            "item_id": item.item_id,
            "quantity_received_good": 100,
            "quantity_received_damaged": 0,
            "quantity_lost": 0,
        }
    ]

    # First receive succeeds
    InterBranchTransferService.receive_transfer(
        transfer_id=dispatched_transfer.transfer_id,
        tenant_id=tenant_a.tenant_id,
        received_items_payload=payload,
        actor_user=pharmacist_user_a,
    )

    # Second receive attempt fails
    with pytest.raises(ValidationError) as exc:
        InterBranchTransferService.receive_transfer(
            transfer_id=dispatched_transfer.transfer_id,
            tenant_id=tenant_a.tenant_id,
            received_items_payload=payload,
            actor_user=pharmacist_user_a,
        )
    assert "ERR_TRANSFER_ALREADY_RECEIVED" in str(exc.value)


@pytest.mark.django_db
def test_rest_api_reconcile_endpoint(dispatched_transfer, pharmacist_user_a):
    """
    Scenario 5: Full REST API invocation for /reconcile/ endpoint with partial/damaged breakdown.
    """
    client = APIClient()
    client.force_authenticate(user=pharmacist_user_a)

    item = dispatched_transfer.items.first()
    url = f"/api/v2/inventory/transfers/{dispatched_transfer.transfer_id}/reconcile/"
    res = client.post(
        url,
        {
            "received_items": [
                {
                    "item_id": item.item_id,
                    "quantity_received_good": 80,
                    "quantity_received_damaged": 15,
                    "quantity_lost": 5,
                    "discrepancy_reason": "Broken vials during transport",
                }
            ],
            "reconciliation_notes": "API Reconciled by branch pharmacist.",
        },
        format="json",
    )
    assert res.status_code == status.HTTP_200_OK
    data = res.json()
    assert data["status"] == "RECONCILED"
    assert data["total_units_received_good"] == 80
    assert data["total_units_received_damaged"] == 15
    assert data["total_units_lost"] == 5
