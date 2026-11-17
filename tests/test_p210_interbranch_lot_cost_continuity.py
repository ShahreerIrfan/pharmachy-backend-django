"""
test_p210_interbranch_lot_cost_continuity.py
Architecture Decision: ADR-0208, ADR-0209, ADR-0210 (P210).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Acceptance Criteria:
1. Valid Scenario:
   - Carries original batch, manufacturer expiry date, lot reference, and PurchaseCostLot lineage across branches.
   - Applies transfer cost policy (e.g. LANDED_MARKUP: 10% on 20.0000 -> 22.0000).
   - Destination batch and derived PurchaseCostLot accurately preserve acquisition history.
2. Expiry Alteration Guard:
   - Transfer cannot modify/extend pharmaceutical manufacturer expiry date (ERR_EXPIRY_DATE_ALTERATION_PROHIBITED).
3. Unauthorized & Cross-Tenant Guards:
   - Cashier role denied.
   - Cross-tenant transfer denied.
4. Weighted Average Cost & Decimal Precision:
   - Incoming transfer into existing batch recalculates weighted average unit cost without precision distortion.
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
    MedicineProduct,
    BatchStock,
    BatchStatus,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    InterBranchTransferRequest,
    InterBranchTransferItem,
    InterBranchTransferStatus,
    InterBranchTransferPriority,
    TransferCostPolicy,
    StockLedgerEntry,
    StockMovementType,
)
from apps.core.interbranch_transfer_service import InterBranchTransferService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p210-{uid}",
        name="Labaid Pharmacy Network",
        subdomain=f"labaid-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-cross-{uid}",
        name="Apollo Pharmacy Chain",
        subdomain=f"apollo-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def branch_source(db, tenant_a):
    return Branch.objects.create(
        branch_id=f"br-src-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        name="Central Depot Hub",
        is_active=True,
    )


@pytest.fixture
def branch_dest(db, tenant_a):
    return Branch.objects.create(
        branch_id=f"br-dest-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        name="Banani Branch",
        is_active=True,
    )


@pytest.fixture
def pharmacist_user(db, tenant_a, branch_source):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharm_p210@labaid.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_source.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_user(db, tenant_a, branch_source):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="cashier_p210@labaid.com",
        password="ValidPassword123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_source.branch_id,
        is_active=True,
    )


@pytest.fixture
def product_insulin(db):
    return MedicineProduct.objects.create(
        product_id=f"med-{uuid.uuid4().hex[:8]}",
        brand_name="Mixtard 30 HM Penfill",
        generic_name="Biphasic Isophane Insulin",
        dosage_form="Injection",
        strength="100 IU/ml",
        cost_unit=Decimal("450.0000"),
        mrp_unit=Decimal("520.0000"),
    )


@pytest.fixture
def source_batch_with_lot(db, tenant_a, branch_source, product_insulin):
    exp_date = timezone.now().date() + timezone.timedelta(days=365)
    purch_date = timezone.now().date() - timezone.timedelta(days=30)
    batch = BatchStock.objects.create(
        batch_id=f"batch-src-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_source,
        product=product_insulin,
        batch_number="BATCH-INS-2026-A1",
        purchase_date=purch_date,
        expiry_date=exp_date,
        quantity_base_units=100,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("450.0000"),
        lot_reference="LOT-NOVO-2026-X8",
        storage_location="FRIDGE-COLD-1",
        status=BatchStatus.ACTIVE,
    )

    cost_lot = PurchaseCostLot.objects.create(
        lot_id=f"lot-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        branch=branch_source,
        product=product_insulin,
        batch=batch,
        purchase_date=purch_date,
        unit_cost=Decimal("450.0000"),
        initial_quantity=100,
        remaining_quantity=100,
        purchase_order_number="PO-2026-9011",
        supplier_name="Novo Nordisk Pharma",
        status=PurchaseCostLotStatus.ACTIVE,
    )
    return batch, cost_lot


@pytest.mark.django_db
def test_interbranch_lot_cost_continuity_and_markup_policy(
    tenant_a, branch_source, branch_dest, pharmacist_user, product_insulin, source_batch_with_lot
):
    """
    Test 1: Valid transfer applying LANDED_MARKUP policy (5% handling markup on 450.00 -> 472.50).
    Verifies that destination batch and PurchaseCostLot inherit complete lineage,
    lot reference, original cost, and immutable expiry date.
    """
    batch, cost_lot = source_batch_with_lot

    # 1. Create transfer with LANDED_MARKUP 5%
    transfer = InterBranchTransferService.create_transfer_request(
        tenant_id=tenant_a.tenant_id,
        source_branch_id=branch_source.branch_id,
        destination_branch_id=branch_dest.branch_id,
        items_payload=[
            {
                "batch_id": batch.batch_id,
                "quantity_requested": 40,
                "destination_storage_location": "FRIDGE-RETAIL",
            }
        ],
        transfer_cost_policy="LANDED_MARKUP",
        markup_percentage=Decimal("5.00"),
        notes="Insulin transfer with cold-chain markup",
        actor_user=pharmacist_user,
    )

    item = transfer.items.first()
    assert item.original_cost_per_unit == Decimal("450.0000")
    # 450 + (450 * 0.05) = 472.5000
    assert item.transferred_cost_per_unit == Decimal("472.5000")
    assert item.original_lot_reference == "LOT-NOVO-2026-X8"
    assert item.source_cost_lot == cost_lot

    # 2. Approve and Dispatch
    InterBranchTransferService.approve_transfer(transfer.transfer_id, tenant_a.tenant_id, pharmacist_user)
    InterBranchTransferService.dispatch_transfer(transfer.transfer_id, tenant_a.tenant_id, "ColdVan Courier", pharmacist_user)

    # 3. Receive at Destination
    reconciled = InterBranchTransferService.receive_transfer(
        transfer_id=transfer.transfer_id,
        tenant_id=tenant_a.tenant_id,
        received_items_payload=[
            {
                "item_id": item.item_id,
                "quantity_received_good": 40,
                "quantity_received_damaged": 0,
                "quantity_lost": 0,
            }
        ],
        receiving_notes="Cold chain intact (4.2 C verified).",
        actor_user=pharmacist_user,
    )

    assert reconciled.status == InterBranchTransferStatus.RECEIVED

    # 4. Verify Destination BatchStock Lineage
    dest_batch = BatchStock.objects.get(
        tenant=tenant_a,
        branch=branch_dest,
        product=product_insulin,
        batch_number=batch.batch_number,
    )
    assert dest_batch.quantity_base_units == 40
    assert dest_batch.origin_batch == batch
    assert dest_batch.origin_branch == branch_source
    assert dest_batch.expiry_date == batch.expiry_date  # Expiry date continuity!
    assert dest_batch.lot_reference == "LOT-NOVO-2026-X8"
    assert dest_batch.original_cost_per_unit == Decimal("450.0000")
    assert dest_batch.cost_per_unit == Decimal("472.50")
    assert dest_batch.transfer_cost_markup_bdt == Decimal("22.5000")
    assert dest_batch.storage_location == "FRIDGE-RETAIL"

    # 5. Verify Destination PurchaseCostLot Lineage
    dest_lot = PurchaseCostLot.objects.get(
        tenant=tenant_a,
        branch=branch_dest,
        batch=dest_batch,
    )
    assert dest_lot.unit_cost == Decimal("472.5000")
    assert dest_lot.purchase_date == cost_lot.purchase_date
    assert dest_lot.supplier_name == "Novo Nordisk Pharma"
    assert dest_lot.remaining_quantity == 40


@pytest.mark.django_db
def test_expiry_date_alteration_prohibited(
    tenant_a, branch_source, branch_dest, pharmacist_user, product_insulin, source_batch_with_lot
):
    """
    Test 2: Expiry alteration guard.
    If destination branch already has this batch number but with a conflicting expiry date,
    receiving must fail with ERR_EXPIRY_DATE_ALTERATION_PROHIBITED.
    """
    batch, _ = source_batch_with_lot

    # Pre-create a conflicting batch in destination with different expiry date
    BatchStock.objects.create(
        batch_id=f"batch-conflict-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_dest,
        product=product_insulin,
        batch_number=batch.batch_number,
        purchase_date=timezone.now().date(),
        expiry_date=batch.expiry_date + timezone.timedelta(days=90),  # Conflicting!
        quantity_base_units=10,
        cost_per_unit=Decimal("450.00"),
        status=BatchStatus.ACTIVE,
    )

    transfer = InterBranchTransferService.create_transfer_request(
        tenant_id=tenant_a.tenant_id,
        source_branch_id=branch_source.branch_id,
        destination_branch_id=branch_dest.branch_id,
        items_payload=[{"batch_id": batch.batch_id, "quantity_requested": 10}],
        actor_user=pharmacist_user,
    )
    InterBranchTransferService.approve_transfer(transfer.transfer_id, tenant_a.tenant_id, pharmacist_user)
    InterBranchTransferService.dispatch_transfer(transfer.transfer_id, tenant_a.tenant_id, "", pharmacist_user)

    item = transfer.items.first()
    with pytest.raises(ValidationError) as exc:
        InterBranchTransferService.receive_transfer(
            transfer_id=transfer.transfer_id,
            tenant_id=tenant_a.tenant_id,
            received_items_payload=[{"item_id": item.item_id, "quantity_received_good": 10}],
            actor_user=pharmacist_user,
        )
    assert "ERR_EXPIRY_DATE_ALTERATION_PROHIBITED" in str(exc.value)


@pytest.mark.django_db
def test_weighted_average_cost_continuity_on_existing_batch(
    tenant_a, branch_source, branch_dest, pharmacist_user, product_insulin, source_batch_with_lot
):
    """
    Test 3: Weighted average cost continuity.
    Destination branch has 50 units @ 450.00. Receives 50 units @ 470.00.
    Weighted average = (50*450 + 50*470) / 100 = 460.00.
    """
    batch, _ = source_batch_with_lot

    # Pre-create destination batch with 50 units @ 450.00
    dest_existing = BatchStock.objects.create(
        batch_id=f"batch-dest-exist-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_dest,
        product=product_insulin,
        batch_number=batch.batch_number,
        purchase_date=batch.purchase_date,
        expiry_date=batch.expiry_date,
        quantity_base_units=50,
        cost_per_unit=Decimal("450.00"),
        status=BatchStatus.ACTIVE,
    )

    # Transfer 50 units with FIXED_FEE 1000 total (20 per unit -> 470.00 transferred cost)
    transfer = InterBranchTransferService.create_transfer_request(
        tenant_id=tenant_a.tenant_id,
        source_branch_id=branch_source.branch_id,
        destination_branch_id=branch_dest.branch_id,
        items_payload=[{"batch_id": batch.batch_id, "quantity_requested": 50}],
        transfer_cost_policy="FIXED_FEE",
        logistics_handling_fee_bdt=Decimal("1000.0000"),
        actor_user=pharmacist_user,
    )
    InterBranchTransferService.approve_transfer(transfer.transfer_id, tenant_a.tenant_id, pharmacist_user)
    InterBranchTransferService.dispatch_transfer(transfer.transfer_id, tenant_a.tenant_id, "", pharmacist_user)

    item = transfer.items.first()
    assert item.transferred_cost_per_unit == Decimal("470.0000")

    InterBranchTransferService.receive_transfer(
        transfer_id=transfer.transfer_id,
        tenant_id=tenant_a.tenant_id,
        received_items_payload=[{"item_id": item.item_id, "quantity_received_good": 50}],
        actor_user=pharmacist_user,
    )

    dest_existing.refresh_from_db()
    # (50*450 + 50*470) / 100 = 460.00
    assert dest_existing.cost_per_unit == Decimal("460.00")
    assert dest_existing.quantity_base_units == 100


@pytest.mark.django_db
def test_cashier_denied_transfer_creation(
    tenant_a, branch_source, branch_dest, cashier_user, source_batch_with_lot
):
    """Test 4: Cashier cannot create transfers (RBAC / PermissionDenied)."""
    batch, _ = source_batch_with_lot
    with pytest.raises(PermissionDenied):
        InterBranchTransferService.create_transfer_request(
            tenant_id=tenant_a.tenant_id,
            source_branch_id=branch_source.branch_id,
            destination_branch_id=branch_dest.branch_id,
            items_payload=[{"batch_id": batch.batch_id, "quantity_requested": 10}],
            actor_user=cashier_user,
        )
