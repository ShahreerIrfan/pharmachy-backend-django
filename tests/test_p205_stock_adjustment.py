"""
backend/tests/test_p205_stock_adjustment.py
Architecture Decision: ADR-0205 (P205).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Comprehensive Test Suite for Nontrivial Adjustment Approval Flow:
1. Nontrivial proposal, manager approval, stock ledger posting, and difference event creation.
2. Maker-checker anti-self-approval enforcement (ERR_MAKER_CHECKER_SELF_APPROVAL).
3. Mandatory reason validation (>= 10 chars, valid reason code, non-zero delta, stock availability).
4. Reversal of approved adjustment emitting offsetting ledger entry and restoring stock.
5. Cashier RBAC rejection (403 Forbidden / ERR_STAFF_ROLE_DENIED).
6. Cross-tenant isolation.
7. DRF REST API endpoints: propose, approve, reverse, and difference-events.
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
    BatchStock,
    Branch,
    MedicineProduct,
    StockAdjustmentDifferenceEvent,
    StockAdjustmentReasonCode,
    StockAdjustmentRecord,
    StockCorrectionStatus,
    StockLedgerEntry,
    StockMovementType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.stock_adjustment_service import StockAdjustmentService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def tenant_a():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Dhaka Health Care",
        subdomain=f"dhaka-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Chittagong Pharma",
        subdomain=f"ctg-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Dhanmondi Branch",
    )


@pytest.fixture
def branch_b(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Agrabad Branch",
    )


@pytest.fixture
def pharmacist_maker_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700500001",
        email="maker.pharmacist@dhakahealth.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )


@pytest.fixture
def manager_checker_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700500002",
        email="checker.manager@dhakahealth.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def cashier_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700500003",
        email="cashier@dhakahealth.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def manager_user_b(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700500004",
        email="manager@chittagongpharma.com",
        password="SecurePassword123!",
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def product_amoxicillin():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Moxacil 500mg Capsule",
        generic_name="Amoxicillin Trihydrate",
        dosage_form="Capsule",
        strength="500mg",
        cost_unit=Decimal("4.0000"),
        mrp_unit=Decimal("5.0000"),
        is_antibiotic=True,
    )


@pytest.fixture
def batch_stock_a(tenant_a, branch_a, product_amoxicillin):
    return BatchStock.objects.create(
        tenant=tenant_a,
        branch=branch_a,
        product=product_amoxicillin,
        batch_id=f"batch-{uuid.uuid4().hex[:8]}",
        batch_number="BATCH-MOX-2026-001",
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
        quantity_base_units=50,
        cost_per_unit=Decimal("45.00"),
        storage_location="SHELF-A1-01",
    )


@pytest.mark.django_db
def test_nontrivial_stock_adjustment_proposal_and_approval(
    tenant_a, branch_a, pharmacist_maker_a, manager_checker_a, batch_stock_a
):
    """
    Test 1: Propose nontrivial adjustment (-15 units, >10 threshold and 15*45 = 675 BDT > 500 threshold).
    Manager approves, emits ledger entry and cryptographic difference event.
    """
    adj = StockAdjustmentService.propose_adjustment(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity_delta=-15,
        reason_code=StockAdjustmentReasonCode.EXPIRY_SPOILAGE,
        reason_notes="Damaged blister foils during warehouse handling inspection.",
        supporting_document_ref="DOC-SPOILAGE-2026-88",
        actor_user=pharmacist_maker_a,
    )

    assert adj.is_nontrivial is True
    assert adj.status == StockCorrectionStatus.PENDING
    assert adj.proposed_by == pharmacist_maker_a
    assert adj.quantity_delta == -15
    assert adj.total_cost_impact == Decimal("675.0000")

    # Stock should NOT be modified before approval
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.quantity_base_units == 50

    # Checker approves
    approved_adj = StockAdjustmentService.approve_and_post_adjustment(
        adjustment_id=adj.adjustment_id,
        tenant_id=tenant_a.tenant_id,
        checker_user=manager_checker_a,
        checker_notes="Physical breakage verified and approved for write-off.",
    )

    assert approved_adj.status == StockCorrectionStatus.APPROVED
    assert approved_adj.approved_by == manager_checker_a
    assert approved_adj.ledger_entry_id != ""

    # Stock should be reduced
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.quantity_base_units == 35

    # Difference event check
    diff_event = StockAdjustmentDifferenceEvent.objects.filter(adjustment=approved_adj).first()
    assert diff_event is not None
    assert diff_event.pre_adjustment_quantity == 50
    assert diff_event.post_adjustment_quantity == 35
    assert diff_event.quantity_delta == -15
    assert diff_event.is_nontrivial is True
    assert diff_event.immutable_event_hash != ""

    # Verification of SHA-256 hash
    expected_hash = diff_event.compute_hash()
    assert diff_event.immutable_event_hash == expected_hash

    # Check StockLedgerEntry
    ledger_entry = StockLedgerEntry.objects.get(entry_id=approved_adj.ledger_entry_id)
    assert ledger_entry.quantity_delta == -15
    assert ledger_entry.movement_type == StockMovementType.STOCK_ADJUSTMENT_OUT
    assert ledger_entry.balance_after == 35


@pytest.mark.django_db
def test_maker_checker_anti_self_approval_enforcement(
    tenant_a, branch_a, manager_checker_a, batch_stock_a
):
    """
    Test 2: Maker-Checker separation of duties - proposer cannot approve their own nontrivial adjustment.
    """
    adj = StockAdjustmentService.propose_adjustment(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity_delta=-12,
        reason_code=StockAdjustmentReasonCode.THEFT_PILFERAGE,
        reason_notes="Discrepancy identified during internal security sweep.",
        actor_user=manager_checker_a,
    )

    assert adj.is_nontrivial is True

    # Same manager attempts to approve
    with pytest.raises(ValidationError) as exc_info:
        StockAdjustmentService.approve_and_post_adjustment(
            adjustment_id=adj.adjustment_id,
            tenant_id=tenant_a.tenant_id,
            checker_user=manager_checker_a,
        )

    assert "ERR_MAKER_CHECKER_SELF_APPROVAL" in str(exc_info.value)


@pytest.mark.django_db
def test_mandatory_reason_and_delta_validation(
    tenant_a, branch_a, pharmacist_maker_a, batch_stock_a
):
    """
    Test 3: Mandatory reason explanation (>=10 chars), non-zero delta, stock availability.
    """
    # 1. Reason too short (< 10 chars)
    with pytest.raises(ValidationError) as exc_info:
        StockAdjustmentService.propose_adjustment(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=batch_stock_a.batch_id,
            quantity_delta=-2,
            reason_code=StockAdjustmentReasonCode.BREAKAGE_SPILLAGE,
            reason_notes="broken",  # 6 chars
            actor_user=pharmacist_maker_a,
        )
    assert "ERR_MANDATORY_REASON_REQUIRED" in str(exc_info.value)

    # 2. Zero delta
    with pytest.raises(ValidationError) as exc_info:
        StockAdjustmentService.propose_adjustment(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=batch_stock_a.batch_id,
            quantity_delta=0,
            reason_code=StockAdjustmentReasonCode.BREAKAGE_SPILLAGE,
            reason_notes="Valid explanation text exceeding ten chars",
            actor_user=pharmacist_maker_a,
        )
    assert "ERR_ZERO_DELTA" in str(exc_info.value)

    # 3. Reducing more than available stock
    with pytest.raises(ValidationError) as exc_info:
        StockAdjustmentService.propose_adjustment(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=batch_stock_a.batch_id,
            quantity_delta=-999,  # Only 50 available
            reason_code=StockAdjustmentReasonCode.AUDIT_COUNT_VARIANCE,
            reason_notes="Major discrepancy noted during count cycle",
            actor_user=pharmacist_maker_a,
        )
    assert "ERR_INSUFFICIENT_STOCK" in str(exc_info.value)


@pytest.mark.django_db
def test_reversal_workflow(
    tenant_a, branch_a, pharmacist_maker_a, manager_checker_a, batch_stock_a
):
    """
    Test 4: Reversing an approved adjustment creates offsetting ledger entry and restores batch balance.
    """
    adj = StockAdjustmentService.propose_adjustment(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity_delta=-10,
        reason_code=StockAdjustmentReasonCode.DISPENSING_ERROR_REVERSAL,
        reason_notes="Misplaced stock later found in incorrect cabinet bin.",
        actor_user=pharmacist_maker_a,
    )

    StockAdjustmentService.approve_and_post_adjustment(
        adjustment_id=adj.adjustment_id,
        tenant_id=tenant_a.tenant_id,
        checker_user=manager_checker_a,
    )

    batch_stock_a.refresh_from_db()
    assert batch_stock_a.quantity_base_units == 40

    # Reverse the approved adjustment
    rev_adj = StockAdjustmentService.reverse_approved_adjustment(
        adjustment_id=adj.adjustment_id,
        tenant_id=tenant_a.tenant_id,
        reversal_reason="Misplaced bottles returned to shelf after audit.",
        actor_user=manager_checker_a,
    )

    adj.refresh_from_db()
    assert adj.is_reversed is True
    assert rev_adj.quantity_delta == 10
    assert rev_adj.reversal_of == adj

    # Batch stock should be restored to 50
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.quantity_base_units == 50

    # Offsetting difference event check
    rev_event = StockAdjustmentDifferenceEvent.objects.filter(adjustment=rev_adj).first()
    assert rev_event is not None
    assert rev_event.quantity_delta == 10
    assert rev_event.post_adjustment_quantity == 50

    # Cannot reverse twice
    with pytest.raises(ValidationError) as exc_info:
        StockAdjustmentService.reverse_approved_adjustment(
            adjustment_id=adj.adjustment_id,
            tenant_id=tenant_a.tenant_id,
            reversal_reason="Attempting second reversal of same adjustment.",
            actor_user=manager_checker_a,
        )
    assert "ERR_ALREADY_REVERSED" in str(exc_info.value)


@pytest.mark.django_db
def test_cashier_rbac_rejection(
    tenant_a, branch_a, cashier_user_a, batch_stock_a
):
    """
    Test 5: Cashiers are strictly denied from proposing adjustments.
    """
    with pytest.raises(PermissionDenied) as exc_info:
        StockAdjustmentService.propose_adjustment(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_id=batch_stock_a.batch_id,
            quantity_delta=-5,
            reason_code=StockAdjustmentReasonCode.BREAKAGE_SPILLAGE,
            reason_notes="Accidental drop by cashier during packing.",
            actor_user=cashier_user_a,
        )
    assert "ERR_STAFF_ROLE_DENIED" in str(exc_info.value)


@pytest.mark.django_db
def test_cross_tenant_isolation(
    tenant_a, branch_a, tenant_b, pharmacist_maker_a, manager_user_b, batch_stock_a
):
    """
    Test 6: Cross-tenant isolation - Tenant B manager cannot approve or view Tenant A's adjustment.
    """
    adj = StockAdjustmentService.propose_adjustment(
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        batch_id=batch_stock_a.batch_id,
        quantity_delta=-10,
        reason_code=StockAdjustmentReasonCode.EXPIRY_SPOILAGE,
        reason_notes="Quarterly audit found compromised foil strips.",
        actor_user=pharmacist_maker_a,
    )

    client_b = APIClient()
    client_b.force_authenticate(user=manager_user_b)

    # Tenant B tries to get detail of Tenant A's adjustment
    url = f"/api/v2/inventory/adjustments/{adj.adjustment_id}/"
    res = client_b.get(url)
    assert res.status_code == status.HTTP_404_NOT_FOUND

    # Tenant B tries to approve Tenant A's adjustment
    res_approve = client_b.post(f"/api/v2/inventory/adjustments/{adj.adjustment_id}/approve/")
    assert res_approve.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_rest_api_propose_approve_and_difference_events(
    tenant_a, branch_a, pharmacist_maker_a, manager_checker_a, batch_stock_a
):
    """
    Test 7: Full DRF REST API flow: propose -> approve -> inspect difference-events -> reverse.
    """
    client_maker = APIClient()
    client_maker.force_authenticate(user=pharmacist_maker_a)

    # 1. Propose via REST
    propose_payload = {
        "branch": branch_a.branch_id,
        "batch": batch_stock_a.batch_id,
        "quantity_delta": -12,
        "reason_code": "EXPIRY_SPOILAGE",
        "reason_notes": "Expired stock removed from display unit.",
        "supporting_document_ref": "REF-API-EXP-01",
    }
    res_propose = client_maker.post("/api/v2/inventory/adjustments/propose/", propose_payload, format="json")
    assert res_propose.status_code == status.HTTP_201_CREATED
    adj_id = res_propose.data["adjustment_id"]
    assert res_propose.data["status"] == "PENDING"
    assert res_propose.data["is_nontrivial"] is True

    # 2. Pharmacist maker tries approval via REST -> 403 Forbidden (approver role denied)
    res_self = client_maker.post(f"/api/v2/inventory/adjustments/{adj_id}/approve/", format="json")
    assert res_self.status_code == status.HTTP_403_FORBIDDEN

    # 3. Manager approves via REST
    client_manager = APIClient()
    client_manager.force_authenticate(user=manager_checker_a)

    res_approve = client_manager.post(
        f"/api/v2/inventory/adjustments/{adj_id}/approve/",
        {"checker_notes": "Verified expired bottles."},
        format="json",
    )
    assert res_approve.status_code == status.HTTP_200_OK
    assert res_approve.data["status"] == "APPROVED"
    assert res_approve.data["ledger_entry_id"] != ""

    # 4. Difference events endpoint
    res_events = client_manager.get(f"/api/v2/inventory/adjustments/{adj_id}/difference-events/")
    assert res_events.status_code == status.HTTP_200_OK
    assert len(res_events.data) == 1
    assert res_events.data[0]["quantity_delta"] == -12
    assert res_events.data[0]["pre_adjustment_quantity"] == 50
    assert res_events.data[0]["post_adjustment_quantity"] == 38
    assert res_events.data[0]["immutable_event_hash"] != ""

    # 5. Reverse via REST
    res_reverse = client_manager.post(
        f"/api/v2/inventory/adjustments/{adj_id}/reverse/",
        {"reversal_reason": "Stock returned after DGDA audit clearance."},
        format="json",
    )
    assert res_reverse.status_code == status.HTTP_201_CREATED
    assert res_reverse.data["quantity_delta"] == 12

    # Stock should be back to 50
    batch_stock_a.refresh_from_db()
    assert batch_stock_a.quantity_base_units == 50
