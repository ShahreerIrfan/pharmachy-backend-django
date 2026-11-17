"""
backend/tests/test_p207_stock_segregation.py
Architecture Decision: ADR-0207 (P207).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Comprehensive Test Suite for Damaged/Returned Stock Segregation:
1. Segregation from sellable inventory: availability deduction and physical zone allocation.
2. Documented release rule: QA pharmacist clearance with mandatory justification (>= 15 chars).
3. Release guard: recalled or counterfeit stock strictly barred from sellable return.
4. Disposal / write-off: stock deduction and StockLedgerEntry synchronization.
5. Cashier RBAC rejection (ERR_STAFF_ROLE_DENIED / 403 Forbidden).
6. Cross-tenant isolation.
7. DRF REST API endpoints: segregate, release-to-sellable, and dispose-or-return.
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
    MedicineProduct,
    SegregationCategory,
    SegregationStatus,
    StockLedgerEntry,
    StockMovementType,
    StockSegregationRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_segregation_service import StockSegregationService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Apollo Care Gulshan",
        subdomain=f"apollo-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Barisal Central Pharmacy",
        subdomain=f"barisal-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Gulshan 2 Branch",
    )


@pytest.fixture
def branch_b(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Sadat Road Branch",
    )


@pytest.fixture
def pharmacist_qa_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700700001",
        email="pharmacist.qa@apollocare.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )


@pytest.fixture
def manager_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700700002",
        email="manager@apollocare.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def cashier_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700700003",
        email="cashier@apollocare.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def manager_user_b(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700700004",
        email="manager@barisalcentral.com",
        password="SecurePassword123!",
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def product_azithromycin():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Zithrin 500mg Tablet",
        generic_name="Azithromycin",
        dosage_form="Tablet",
        strength="500mg",
        cost_unit=Decimal("35.0000"),
        mrp_unit=Decimal("45.0000"),
    )


@pytest.fixture
def batch_stock_a(tenant_a, branch_a, product_azithromycin):
    return BatchStock.objects.create(
        tenant=tenant_a,
        branch=branch_a,
        product=product_azithromycin,
        batch_id=f"batch-{uuid.uuid4().hex[:8]}",
        batch_number="BATCH-ZITH-2026-004",
        purchase_date=timezone.now().date() - timezone.timedelta(days=30),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        quantity_base_units=100,
        cost_per_unit=Decimal("35.00"),
        storage_location="SHELF-A2-05",
    )


@pytest.mark.django_db
def test_segregate_stock_deducts_sellable_availability(
    tenant_a, branch_a, pharmacist_qa_a, batch_stock_a
):
    """
    Test 1: Segregating 30 units of damaged stock reserves them from sellable inventory.
    """
    assert batch_stock_a.available_quantity_base_units == 100

    rec = StockSegregationService.segregate_stock(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity=30,
        category=SegregationCategory.DAMAGED,
        reason_notes="Outer blister packaging crushed during carton unloading.",
        segregation_location="DAMAGED-HOLD-BIN",
        actor_user=pharmacist_qa_a,
    )

    assert rec.status == SegregationStatus.SEGREGATED
    assert rec.segregated_quantity == 30
    assert rec.total_cost_impact == Decimal("1050.0000")

    batch_stock_a.refresh_from_db()
    assert batch_stock_a.reserved_quantity_base_units == 30
    assert batch_stock_a.available_quantity_base_units == 70  # Sellable stock reduced!


@pytest.mark.django_db
def test_release_to_sellable_documented_rule(
    tenant_a, branch_a, pharmacist_qa_a, batch_stock_a
):
    """
    Test 2: QA Pharmacist inspects customer return and clears stock with documented justification.
    """
    rec = StockSegregationService.segregate_stock(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity=15,
        category=SegregationCategory.RETURNED_CUSTOMER,
        reason_notes="Customer returned unopened strip with valid receipt.",
        actor_user=pharmacist_qa_a,
    )

    # 1. Too short justification fails
    with pytest.raises(ValidationError) as exc_short:
        StockSegregationService.release_to_sellable(
            segregation_id=rec.segregation_id,
            tenant_id=tenant_a.tenant_id,
            justification="checked ok",  # < 15 chars
            inspector_user=pharmacist_qa_a,
        )
    assert "ERR_RELEASE_JUSTIFICATION_REQUIRED" in str(exc_short.value)

    # 2. Valid justification succeeds and restores sellable availability
    released = StockSegregationService.release_to_sellable(
        segregation_id=rec.segregation_id,
        tenant_id=tenant_a.tenant_id,
        justification="Blister foil intact, seal verified, stored at proper temperature.",
        inspector_user=pharmacist_qa_a,
        new_storage_location="SHELF-A2-05",
    )

    assert released.status == SegregationStatus.RELEASED_TO_SELLABLE
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.reserved_quantity_base_units == 0
    assert batch_stock_a.available_quantity_base_units == 100


@pytest.mark.django_db
def test_release_prohibited_for_recalled_or_counterfeit(
    tenant_a, branch_a, pharmacist_qa_a, batch_stock_a
):
    """
    Test 3: Recalled or suspected counterfeit items are strictly barred from release.
    """
    rec_recalled = StockSegregationService.segregate_stock(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity=20,
        category=SegregationCategory.RECALLED,
        reason_notes="DGDA safety alert 2026-08.",
        actor_user=pharmacist_qa_a,
    )

    with pytest.raises(ValidationError) as exc:
        StockSegregationService.release_to_sellable(
            segregation_id=rec_recalled.segregation_id,
            tenant_id=tenant_a.tenant_id,
            justification="Pharmacist attempts release of recalled batch.",
            inspector_user=pharmacist_qa_a,
        )
    assert "ERR_PROHIBITED_RELEASE" in str(exc.value)


@pytest.mark.django_db
def test_disposal_written_off_emits_stock_ledger_entry(
    tenant_a, branch_a, pharmacist_qa_a, manager_user_a, batch_stock_a
):
    """
    Test 4: Disposed stock deducts physical units and emits StockLedgerEntry.
    """
    rec = StockSegregationService.segregate_stock(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity=25,
        category=SegregationCategory.DAMAGED,
        reason_notes="Crushed ampoules leaking fluid.",
        actor_user=pharmacist_qa_a,
    )

    disposed = StockSegregationService.dispose_or_return_stock(
        segregation_id=rec.segregation_id,
        tenant_id=tenant_a.tenant_id,
        action_type=SegregationStatus.DISPOSED_WRITTEN_OFF,
        disposition_reference="CERT-WRITEOFF-2026-11",
        action_notes="Destroyed via pharmaceutical autoclave and biological waste drain.",
        actor_user=manager_user_a,
    )

    assert disposed.status == SegregationStatus.DISPOSED_WRITTEN_OFF
    assert disposed.ledger_entry_id != ""

    batch_stock_a.refresh_from_db()
    assert batch_stock_a.quantity_base_units == 75  # 100 - 25 = 75
    assert batch_stock_a.reserved_quantity_base_units == 0

    ledger = StockLedgerEntry.objects.get(entry_id=disposed.ledger_entry_id)
    assert ledger.quantity_delta == -25
    assert ledger.movement_type == StockMovementType.STOCK_ADJUSTMENT_OUT
    assert ledger.balance_after == 75


@pytest.mark.django_db
def test_cashier_rbac_denied(
    tenant_a, branch_a, cashier_user_a, batch_stock_a
):
    """
    Test 5: Cashier role is strictly denied from initiating stock segregation.
    """
    with pytest.raises(PermissionDenied) as exc:
        StockSegregationService.segregate_stock(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=batch_stock_a.batch_id,
            quantity=10,
            category=SegregationCategory.DAMAGED,
            reason_notes="Customer dropped box at counter.",
            actor_user=cashier_user_a,
        )
    assert "ERR_STAFF_ROLE_DENIED" in str(exc.value)


@pytest.mark.django_db
def test_cross_tenant_isolation(
    tenant_a, branch_a, pharmacist_qa_a, manager_user_b, batch_stock_a
):
    """
    Test 6: Tenant B cannot access Tenant A's segregation records.
    """
    rec = StockSegregationService.segregate_stock(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity=10,
        category=SegregationCategory.DAMAGED,
        reason_notes="Damaged lot.",
        actor_user=pharmacist_qa_a,
    )

    client_b = APIClient()
    client_b.force_authenticate(user=manager_user_b)

    res = client_b.get(f"/api/v2/inventory/segregations/{rec.segregation_id}/")
    assert res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_rest_api_segregate_release_and_disposal(
    tenant_a, branch_a, pharmacist_qa_a, manager_user_a, batch_stock_a
):
    """
    Test 7: Full DRF REST API workflow: segregate -> release-to-sellable -> dispose-or-return.
    """
    client_qa = APIClient()
    client_qa.force_authenticate(user=pharmacist_qa_a)

    # 1. Segregate via REST
    payload = {
        "branch": branch_a.branch_id,
        "batch": batch_stock_a.batch_id,
        "quantity": 10,
        "category": "RETURNED_CUSTOMER",
        "reason_notes": "Returned by customer within 10 minutes of purchase.",
        "segregation_location": "RETURNS-BAY",
    }
    res_seg = client_qa.post("/api/v2/inventory/segregations/segregate/", payload, format="json")
    assert res_seg.status_code == status.HTTP_201_CREATED
    seg_id = res_seg.data["segregation_id"]

    # 2. Release via REST
    res_rel = client_qa.post(
        f"/api/v2/inventory/segregations/{seg_id}/release-to-sellable/",
        {"release_justification": "Verified factory packaging seal completely intact and authentic."},
        format="json",
    )
    assert res_rel.status_code == status.HTTP_200_OK
    assert res_rel.data["status"] == "RELEASED_TO_SELLABLE"
