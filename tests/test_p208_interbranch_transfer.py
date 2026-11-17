"""
backend/tests/test_p208_interbranch_transfer.py
Architecture Decision: ADR-0208 (P208).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Comprehensive Test Suite for Inter-Branch Transfer Requests:
1. Full lifecycle: Request -> Approve -> Dispatch (TRANSFER_OUT) -> Receive (TRANSFER_IN).
2. Stock reservation: sender stock reserved immediately; unreserved availability reduced.
3. Cancellation release: cancelling transfer releases reserved sender units.
4. Cross-tenant and same-branch transfer rejection guards.
5. Cashier RBAC rejection (ERR_STAFF_ROLE_DENIED / 403 Forbidden).
6. DRF REST API endpoints: request-transfer, approve, dispatch, and receive.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.interbranch_transfer_service import InterBranchTransferService
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InterBranchTransferPriority,
    InterBranchTransferRequest,
    InterBranchTransferStatus,
    MedicineProduct,
    StockLedgerEntry,
    StockMovementType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Evercare Pharmacy Chain",
        subdomain=f"evercare-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="United Health Khulna",
        subdomain=f"united-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_source(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Bashundhara Hub Branch",
    )


@pytest.fixture
def branch_dest(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Uttara Express Branch",
    )


@pytest.fixture
def branch_b(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Khulna Main Branch",
    )


@pytest.fixture
def pharmacist_user_a(tenant_a, branch_source):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700800001",
        email="pharmacist@evercare.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_source.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )


@pytest.fixture
def manager_user_a(tenant_a, branch_source):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700800002",
        email="manager@evercare.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_source.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def cashier_user_a(tenant_a, branch_source):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700800003",
        email="cashier@evercare.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_source.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def product_paracetamol():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Napa 500mg Tablet",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="500mg",
        cost_unit=Decimal("1.2000"),
        mrp_unit=Decimal("1.5000"),
    )


@pytest.fixture
def batch_source_stock(tenant_a, branch_source, product_paracetamol):
    return BatchStock.objects.create(
        tenant=tenant_a,
        branch=branch_source,
        product=product_paracetamol,
        batch_id=f"batch-{uuid.uuid4().hex[:8]}",
        batch_number="BATCH-NAPA-2026-X1",
        purchase_date=timezone.now().date() - timezone.timedelta(days=20),
        expiry_date=timezone.now().date() + timezone.timedelta(days=400),
        quantity_base_units=100,
        cost_per_unit=Decimal("1.20"),
        storage_location="SHELF-A1",
    )


@pytest.mark.django_db
def test_full_interbranch_transfer_lifecycle(
    tenant_a, branch_source, branch_dest, pharmacist_user_a, manager_user_a, batch_source_stock
):
    """
    Test 1: Full transfer lifecycle:
    1. Request 40 units -> sender reservation increases to 40, available drops to 60.
    2. Manager approves -> status APPROVED.
    3. Dispatch -> physical sender stock becomes 60, reserved becomes 0. TRANSFER_OUT posted.
    4. Receive -> receiver branch stock created/credited with 40 units. TRANSFER_IN posted.
    """
    assert batch_source_stock.available_quantity_base_units == 100

    # 1. Create transfer request
    payload = [
        {
            "batch_id": batch_source_stock.batch_id,
            "quantity_requested": 40,
            "destination_storage_location": "SHELF-UTTARA-01",
        }
    ]
    trf = InterBranchTransferService.create_transfer_request(
        tenant_id=tenant_a.tenant_id,
        source_branch_id=branch_source.branch_id,
        destination_branch_id=branch_dest.branch_id,
        items_payload=payload,
        priority=InterBranchTransferPriority.URGENT,
        notes="Urgent replenishment for evening rush.",
        actor_user=pharmacist_user_a,
    )

    assert trf.status == InterBranchTransferStatus.REQUESTED
    assert trf.total_units_requested == 40
    assert trf.total_valuation_bdt == Decimal("48.0000")

    batch_source_stock.refresh_from_db()
    assert batch_source_stock.reserved_quantity_base_units == 40
    assert batch_source_stock.available_quantity_base_units == 60

    # 2. Manager approves
    trf = InterBranchTransferService.approve_transfer(
        transfer_id=trf.transfer_id,
        tenant_id=tenant_a.tenant_id,
        actor_user=manager_user_a,
    )
    assert trf.status == InterBranchTransferStatus.APPROVED

    # 3. Dispatch
    trf = InterBranchTransferService.dispatch_transfer(
        transfer_id=trf.transfer_id,
        tenant_id=tenant_a.tenant_id,
        carrier_notes="Dispatched via internal pharmacy courier bike 04.",
        actor_user=pharmacist_user_a,
    )
    assert trf.status == InterBranchTransferStatus.DISPATCHED
    assert trf.total_units_dispatched == 40

    batch_source_stock.refresh_from_db()
    assert batch_source_stock.quantity_base_units == 60
    assert batch_source_stock.reserved_quantity_base_units == 0

    item = trf.items.first()
    assert item.dispatch_ledger_entry_id != ""
    ledger_out = StockLedgerEntry.objects.get(entry_id=item.dispatch_ledger_entry_id)
    assert ledger_out.quantity_delta == -40
    assert ledger_out.movement_type == StockMovementType.TRANSFER_OUT
    assert ledger_out.balance_after == 60

    # 4. Receive at destination branch
    trf = InterBranchTransferService.receive_transfer(
        transfer_id=trf.transfer_id,
        tenant_id=tenant_a.tenant_id,
        receiving_notes="Verified 40 units received intact.",
        actor_user=manager_user_a,
    )
    assert trf.status == InterBranchTransferStatus.RECEIVED
    assert trf.total_units_received == 40

    # Verify destination branch batch was created and credited
    dest_batch = BatchStock.objects.get(
        tenant=tenant_a,
        branch=branch_dest,
        batch_number=batch_source_stock.batch_number,
    )
    assert dest_batch.quantity_base_units == 40
    assert dest_batch.storage_location == "SHELF-UTTARA-01"

    item.refresh_from_db()
    assert item.receipt_ledger_entry_id != ""
    ledger_in = StockLedgerEntry.objects.get(entry_id=item.receipt_ledger_entry_id)
    assert ledger_in.quantity_delta == 40
    assert ledger_in.movement_type == StockMovementType.TRANSFER_IN
    assert ledger_in.balance_after == 40


@pytest.mark.django_db
def test_transfer_cancellation_releases_reservation(
    tenant_a, branch_source, branch_dest, pharmacist_user_a, batch_source_stock
):
    """
    Test 2: Cancelling transfer safely releases reserved sender stock.
    """
    trf = InterBranchTransferService.create_transfer_request(
        tenant_id=tenant_a.tenant_id,
        source_branch_id=branch_source.branch_id,
        destination_branch_id=branch_dest.branch_id,
        items_payload=[{"batch_id": batch_source_stock.batch_id, "quantity_requested": 25}],
        actor_user=pharmacist_user_a,
    )

    batch_source_stock.refresh_from_db()
    assert batch_source_stock.reserved_quantity_base_units == 25

    cancelled = InterBranchTransferService.cancel_transfer(
        transfer_id=trf.transfer_id,
        tenant_id=tenant_a.tenant_id,
        cancellation_reason="Destination branch stock found in auxiliary cabinet.",
        actor_user=pharmacist_user_a,
    )

    assert cancelled.status == InterBranchTransferStatus.CANCELLED
    batch_source_stock.refresh_from_db()
    assert batch_source_stock.reserved_quantity_base_units == 0
    assert batch_source_stock.available_quantity_base_units == 100


@pytest.mark.django_db
def test_same_branch_or_cross_tenant_rejection(
    tenant_a, tenant_b, branch_source, branch_b, pharmacist_user_a, batch_source_stock
):
    """
    Test 3: Same-branch transfers and cross-tenant transfers are rejected.
    """
    # 1. Same branch transfer
    with pytest.raises(ValidationError) as exc_same:
        InterBranchTransferService.create_transfer_request(
            tenant_id=tenant_a.tenant_id,
            source_branch_id=branch_source.branch_id,
            destination_branch_id=branch_source.branch_id,
            items_payload=[{"batch_id": batch_source_stock.batch_id, "quantity_requested": 10}],
            actor_user=pharmacist_user_a,
        )
    assert "ERR_SAME_BRANCH_TRANSFER" in str(exc_same.value)

    # 2. Cross-tenant transfer
    with pytest.raises(NotFound) as exc_cross:
        InterBranchTransferService.create_transfer_request(
            tenant_id=tenant_a.tenant_id,
            source_branch_id=branch_source.branch_id,
            destination_branch_id=branch_b.branch_id,
            items_payload=[{"batch_id": batch_source_stock.batch_id, "quantity_requested": 10}],
            actor_user=pharmacist_user_a,
        )
    assert "ERR_CROSS_TENANT_TRANSFER" in str(exc_cross.value)


@pytest.mark.django_db
def test_cashier_rbac_denied(
    tenant_a, branch_source, branch_dest, cashier_user_a, batch_source_stock
):
    """
    Test 4: Cashiers are strictly denied from creating transfers.
    """
    with pytest.raises(PermissionDenied) as exc:
        InterBranchTransferService.create_transfer_request(
            tenant_id=tenant_a.tenant_id,
            source_branch_id=branch_source.branch_id,
            destination_branch_id=branch_dest.branch_id,
            items_payload=[{"batch_id": batch_source_stock.batch_id, "quantity_requested": 10}],
            actor_user=cashier_user_a,
        )
    assert "ERR_STAFF_ROLE_DENIED" in str(exc.value)


@pytest.mark.django_db
def test_rest_api_request_approve_dispatch_receive(
    tenant_a, branch_source, branch_dest, pharmacist_user_a, manager_user_a, batch_source_stock
):
    """
    Test 5: Full REST API workflow: request -> approve -> dispatch -> receive.
    """
    client_pharm = APIClient()
    client_pharm.force_authenticate(user=pharmacist_user_a)

    # 1. Request via REST
    payload = {
        "source_branch": branch_source.branch_id,
        "destination_branch": branch_dest.branch_id,
        "priority": "URGENT",
        "notes": "Urgent API transfer request.",
        "items": [
            {
                "batch_id": batch_source_stock.batch_id,
                "quantity_requested": 20,
                "destination_storage_location": "SHELF-API",
            }
        ],
    }
    res_req = client_pharm.post("/api/v2/inventory/transfers/request-transfer/", payload, format="json")
    assert res_req.status_code == status.HTTP_201_CREATED
    trf_id = res_req.data["transfer_id"]

    # 2. Approve via REST
    client_mgr = APIClient()
    client_mgr.force_authenticate(user=manager_user_a)

    res_app = client_mgr.post(f"/api/v2/inventory/transfers/{trf_id}/approve/", format="json")
    assert res_app.status_code == status.HTTP_200_OK
    assert res_app.data["status"] == "APPROVED"

    # 3. Dispatch via REST
    res_disp = client_pharm.post(
        f"/api/v2/inventory/transfers/{trf_id}/dispatch/",
        {"dispatch_carrier_notes": "Van-102"},
        format="json",
    )
    assert res_disp.status_code == status.HTTP_200_OK
    assert res_disp.data["status"] == "DISPATCHED"

    # 4. Receive via REST
    res_recv = client_mgr.post(
        f"/api/v2/inventory/transfers/{trf_id}/receive/",
        {"receiving_notes": "Received completely."},
        format="json",
    )
    assert res_recv.status_code == status.HTTP_200_OK
    assert res_recv.data["status"] == "RECEIVED"
