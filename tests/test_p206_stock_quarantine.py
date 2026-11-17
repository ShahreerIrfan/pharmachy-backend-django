"""
backend/tests/test_p206_stock_quarantine.py
Architecture Decision: ADR-0206 (P206).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Comprehensive Test Suite for Expired Stock Quarantine:
1. Batch quarantine, storage relocation, and sale prohibition (ERR_EXPIRED_STOCK_SALE_PROHIBITED).
2. Automated expired batch sweep and quarantine.
3. Manager disposal approval, physical stock deduction, and ledger synchronization.
4. Release guard: expired batches cannot be released back to active inventory.
5. Cashier RBAC rejection (ERR_STAFF_ROLE_DENIED / 403 Forbidden).
6. Cross-tenant isolation.
7. DRF REST API endpoints: quarantine-batch, sweep-expired, validate-sale, and approve-disposal.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    ExpiredStockQuarantineReason,
    ExpiredStockQuarantineRecord,
    ExpiredStockQuarantineStatus,
    MedicineProduct,
    StockLedgerEntry,
    StockMovementType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_quarantine_service import ExpiredStockQuarantineService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Mirpur Healthcare",
        subdomain=f"mirpur-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Sylhet Pharma Corp",
        subdomain=f"sylhet-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Mirpur 10 Branch",
    )


@pytest.fixture
def branch_b(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Zindabazar Branch",
    )


@pytest.fixture
def pharmacist_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700600001",
        email="pharmacist@mirpurhealth.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )


@pytest.fixture
def manager_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700600002",
        email="manager@mirpurhealth.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def cashier_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700600003",
        email="cashier@mirpurhealth.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def manager_user_b(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700600004",
        email="manager@sylhetpharma.com",
        password="SecurePassword123!",
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def product_insulin():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Humalog Mix 50/50 KwikPen",
        generic_name="Insulin Lispro",
        dosage_form="Injectable",
        strength="100 IU/ml",
        cost_unit=Decimal("450.0000"),
        mrp_unit=Decimal("600.0000"),
        requires_cold_chain=True,
    )


@pytest.fixture
def active_batch_a(tenant_a, branch_a, product_insulin):
    return BatchStock.objects.create(
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch_id=f"batch-{uuid.uuid4().hex[:8]}",
        batch_number="BATCH-INS-2026-09",
        purchase_date=timezone.now().date() - timezone.timedelta(days=60),
        expiry_date=timezone.now().date() + timezone.timedelta(days=120),
        quantity_base_units=40,
        cost_per_unit=Decimal("450.00"),
        storage_location="FRIDGE-COLD-01",
    )


@pytest.fixture
def expired_batch_a(tenant_a, branch_a, product_insulin):
    return BatchStock.objects.create(
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch_id=f"batch-{uuid.uuid4().hex[:8]}",
        batch_number="BATCH-INS-EXP-2026",
        purchase_date=timezone.now().date() - timezone.timedelta(days=400),
        expiry_date=timezone.now().date() - timezone.timedelta(days=10),  # Expired 10 days ago
        quantity_base_units=25,
        cost_per_unit=Decimal("450.00"),
        storage_location="SHELF-GENERAL",
    )


@pytest.mark.django_db
def test_quarantine_batch_and_prohibit_sale(
    tenant_a, branch_a, pharmacist_user_a, active_batch_a
):
    """
    Test 1: Manual quarantine moves batch, changes status to QUARANTINED,
    creates quarantine record, and strictly prohibits sale.
    """
    # Initially sale is permitted
    ExpiredStockQuarantineService.assert_sale_allowed(active_batch_a)

    # Pharmacist quarantines batch
    record = ExpiredStockQuarantineService.quarantine_batch(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=active_batch_a.batch_id,
        reason=ExpiredStockQuarantineReason.COLD_CHAIN_EXCURSION,
        notes="Temperature monitor recorded 18C excursion for 3 hours.",
        quarantine_location="QUARANTINE-COLD-BOX",
        actor_user=pharmacist_user_a,
    )

    assert record.status == ExpiredStockQuarantineStatus.QUARANTINED
    assert record.quarantined_quantity == 40
    assert record.total_cost_impact == Decimal("18000.0000")
    assert record.immutable_hash != ""

    active_batch_a.refresh_from_db()
    assert active_batch_a.is_quarantined is True
    assert active_batch_a.status == BatchStatus.QUARANTINED
    assert active_batch_a.storage_location == "QUARANTINE-COLD-BOX"
    assert active_batch_a.is_dispensable is False

    # Sale is now strictly prohibited
    with pytest.raises(ValidationError) as exc:
        ExpiredStockQuarantineService.assert_sale_allowed(active_batch_a)
    assert "ERR_EXPIRED_STOCK_SALE_PROHIBITED" in str(exc.value)

    # Duplicate quarantine attempt fails
    with pytest.raises(ValidationError) as exc_dup:
        ExpiredStockQuarantineService.quarantine_batch(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=active_batch_a.batch_id,
            actor_user=pharmacist_user_a,
        )
    assert "ERR_ALREADY_QUARANTINED" in str(exc_dup.value)


@pytest.mark.django_db
def test_sweep_and_quarantine_expired_batches(
    tenant_a, branch_a, pharmacist_user_a, expired_batch_a
):
    """
    Test 2: Automated sweep scans branch and quarantines all expired stock batches.
    """
    assert expired_batch_a.is_quarantined is False
    swept = ExpiredStockQuarantineService.sweep_and_quarantine_expired_batches(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        actor_user=pharmacist_user_a,
    )

    assert len(swept) == 1
    assert swept[0].batch_number == "BATCH-INS-EXP-2026"
    assert swept[0].quarantine_reason == ExpiredStockQuarantineReason.BATCH_EXPIRED

    expired_batch_a.refresh_from_db()
    assert expired_batch_a.is_quarantined is True
    assert expired_batch_a.status == BatchStatus.QUARANTINED


@pytest.mark.django_db
def test_disposal_approval_and_stock_ledger_deduction(
    tenant_a, branch_a, pharmacist_user_a, manager_user_a, expired_batch_a
):
    """
    Test 3: Manager approves disposal, physical inventory reduced to 0, StockLedgerEntry posted.
    """
    record = ExpiredStockQuarantineService.quarantine_batch(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=expired_batch_a.batch_id,
        reason=ExpiredStockQuarantineReason.BATCH_EXPIRED,
        notes="Expired insulin batch",
        actor_user=pharmacist_user_a,
    )

    actioned = ExpiredStockQuarantineService.approve_disposal_or_return(
        quarantine_id=record.quarantine_id,
        tenant_id=tenant_a.tenant_id,
        action_type=ExpiredStockQuarantineStatus.DISPOSED,
        disposal_certificate_ref="CERT-DISP-2026-991",
        action_notes="Incinerated at authorized pharmaceutical disposal facility per DGDA guidelines.",
        actor_user=manager_user_a,
    )

    assert actioned.status == ExpiredStockQuarantineStatus.DISPOSED
    assert actioned.disposal_certificate_ref == "CERT-DISP-2026-991"

    # Batch units reduced to 0 and DEPLETED
    expired_batch_a.refresh_from_db()
    assert expired_batch_a.quantity_base_units == 0
    assert expired_batch_a.status == BatchStatus.DEPLETED

    # Ledger entry check
    ledger = StockLedgerEntry.objects.get(entry_id=actioned.ledger_entry_id)
    assert ledger.quantity_delta == -25
    assert ledger.movement_type == StockMovementType.STOCK_ADJUSTMENT_OUT
    assert ledger.balance_after == 0


@pytest.mark.django_db
def test_release_guard_prohibits_expired_batch(
    tenant_a, branch_a, pharmacist_user_a, manager_user_a, active_batch_a, expired_batch_a
):
    """
    Test 4: Expired batch cannot be released back to active stock.
    Non-expired batch can be released with valid justification.
    """
    # 1. Quarantined expired batch cannot be released
    rec_exp = ExpiredStockQuarantineService.quarantine_batch(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=expired_batch_a.batch_id,
        actor_user=pharmacist_user_a,
    )
    with pytest.raises(ValidationError) as exc:
        ExpiredStockQuarantineService.release_quarantine(
            quarantine_id=rec_exp.quarantine_id,
            tenant_id=tenant_a.tenant_id,
            release_reason="Attempting to release expired batch.",
            new_storage_location="SHELF-A1",
            actor_user=manager_user_a,
        )
    assert "ERR_EXPIRED_BATCH_CANNOT_BE_RELEASED" in str(exc.value)

    # 2. Non-expired batch can be released
    rec_act = ExpiredStockQuarantineService.quarantine_batch(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=active_batch_a.batch_id,
        reason=ExpiredStockQuarantineReason.QUALITY_DEFECT,
        notes="Blister foil checked for moisture seal.",
        actor_user=pharmacist_user_a,
    )

    released = ExpiredStockQuarantineService.release_quarantine(
        quarantine_id=rec_act.quarantine_id,
        tenant_id=tenant_a.tenant_id,
        release_reason="Quality inspection passed by chief pharmacist with lab clearance.",
        new_storage_location="FRIDGE-COLD-01",
        actor_user=manager_user_a,
    )

    assert released.status == ExpiredStockQuarantineStatus.RELEASED
    active_batch_a.refresh_from_db()
    assert active_batch_a.is_quarantined is False
    assert active_batch_a.status == BatchStatus.ACTIVE
    assert active_batch_a.is_dispensable is True


@pytest.mark.django_db
def test_cashier_rbac_denied(
    tenant_a, branch_a, cashier_user_a, active_batch_a
):
    """
    Test 5: Cashier role is strictly denied from quarantine operations.
    """
    with pytest.raises(PermissionDenied) as exc:
        ExpiredStockQuarantineService.quarantine_batch(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=active_batch_a.batch_id,
            actor_user=cashier_user_a,
        )
    assert "ERR_STAFF_ROLE_DENIED" in str(exc.value)


@pytest.mark.django_db
def test_cross_tenant_isolation(
    tenant_a, branch_a, pharmacist_user_a, manager_user_b, active_batch_a
):
    """
    Test 6: Tenant B cannot access or action Tenant A's quarantine records.
    """
    record = ExpiredStockQuarantineService.quarantine_batch(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=active_batch_a.batch_id,
        actor_user=pharmacist_user_a,
    )

    client_b = APIClient()
    client_b.force_authenticate(user=manager_user_b)

    res = client_b.get(f"/api/v2/inventory/quarantines/{record.quarantine_id}/")
    assert res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_rest_api_quarantine_sweep_and_validate_sale(
    tenant_a, branch_a, pharmacist_user_a, manager_user_a, active_batch_a, expired_batch_a
):
    """
    Test 7: Full REST API workflow: quarantine-batch, validate-sale, sweep-expired, and approve-disposal.
    """
    client_pharm = APIClient()
    client_pharm.force_authenticate(user=pharmacist_user_a)

    # 1. Manual quarantine via REST
    payload = {
        "branch": branch_a.branch_id,
        "batch": active_batch_a.batch_id,
        "reason": "COLD_CHAIN_EXCURSION",
        "notes": "Sensor alarm triggered during power outage.",
        "quarantine_location": "COLD-QUARANTINE",
    }
    res_q = client_pharm.post("/api/v2/inventory/quarantines/quarantine-batch/", payload, format="json")
    assert res_q.status_code == status.HTTP_201_CREATED
    quar_id = res_q.data["quarantine_id"]

    # 2. validate-sale fails for quarantined batch
    res_val = client_pharm.post("/api/v2/inventory/quarantines/validate-sale/", {"batch": active_batch_a.batch_id}, format="json")
    assert res_val.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 3. Sweep expired via REST
    res_sweep = client_pharm.post("/api/v2/inventory/quarantines/sweep-expired/", {"branch": branch_a.branch_id}, format="json")
    assert res_sweep.status_code == status.HTTP_200_OK
    assert res_sweep.data["swept_count"] >= 1

    # 4. Manager approves disposal
    client_mgr = APIClient()
    client_mgr.force_authenticate(user=manager_user_a)

    res_disp = client_mgr.post(
        f"/api/v2/inventory/quarantines/{quar_id}/approve-disposal/",
        {
            "action_type": "DISPOSED",
            "disposal_certificate_ref": "DISP-REST-2026",
            "action_notes": "Safely incinerated.",
        },
        format="json",
    )
    assert res_disp.status_code == status.HTTP_200_OK
    assert res_disp.data["status"] == "DISPOSED"
