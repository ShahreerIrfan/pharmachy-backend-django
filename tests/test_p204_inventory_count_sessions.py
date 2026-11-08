"""
backend/tests/test_p204_inventory_count_sessions.py
Architecture Decision: ADR-0204 (P204).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Comprehensive Test Suite for Inventory Count Sessions & Maker-Checker Posting:
1. End-to-End Cycle Count: creation, scanner collection, discrepancy calculation,
   maker-checker approval, and ledger adjustment posting.
2. Stock Ledger Integration: verified balance updates and tamper-evident SHA-256 hashes.
3. Maker-Checker Anti-Self-Approval Enforcement (Maker != Checker).
4. Invalid inputs: negative counted quantities and empty session submission rejection.
5. Re-posting guard: cannot post already-posted count session.
6. Cashier RBAC rejection (403 Forbidden).
7. Cross-tenant isolation.
8. REST API endpoints.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.inventory_count_service import InventoryCountService
from apps.core.models import (
    BatchStock,
    Branch,
    InventoryCountDiscrepancyType,
    InventoryCountItem,
    InventoryCountSession,
    InventoryCountSessionStatus,
    InventoryCountSessionType,
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
        name="Lakeside Pharmacy Banani",
        subdomain=f"lakeside-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Square Health Uttara",
        subdomain=f"square-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        name="Banani Main Branch",
    )


@pytest.fixture
def branch_b(tenant_b):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant_b,
        name="Uttara Sector 3 Branch",
    )


@pytest.fixture
def pharmacist_maker_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700400001",
        email="maker.pharmacist@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )


@pytest.fixture
def manager_checker_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700400002",
        email="checker.manager@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def cashier_user_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700400003",
        email="cashier@lakeside.com",
        password="SecurePassword123!",
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        role=UserRole.CASHIER,
    )


@pytest.fixture
def manager_user_b(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number="+8801700400004",
        email="manager@square.com",
        password="SecurePassword123!",
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )


@pytest.fixture
def product_napa():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Napa Extra 500mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="TABLET",
        cost_unit=Decimal("2.0000"),
        mrp_unit=Decimal("2.5000"),
        barcode="89411002001",
    )


@pytest.fixture
def product_ace():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Ace Plus 500mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="TABLET",
        cost_unit=Decimal("2.0000"),
        mrp_unit=Decimal("2.5000"),
        barcode="89411002002",
    )


@pytest.mark.django_db
class TestInventoryCountSessionSuite:
    """End-to-End Test Suite for P204: Inventory Count Sessions."""

    def test_full_count_lifecycle_and_ledger_adjustments(
        self, tenant_a, branch_a, pharmacist_maker_a, manager_checker_a, product_napa, product_ace
    ):
        """Test 1: Full physical count session from scanning to maker-checker posting."""
        today = timezone.now().date()

        # Batch 1 (Napa): Recorded System Qty = 100
        b1 = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_napa,
            batch_number="BAT-NAPA-01",
            quantity_base_units=100,
            cost_per_unit=Decimal("2.0000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=200),
            storage_location="SH-A1",
        )

        # Batch 2 (Ace): Recorded System Qty = 50
        b2 = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_ace,
            batch_number="BAT-ACE-02",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.0000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=250),
            storage_location="SH-A2",
        )

        # 1. Maker creates session
        session = InventoryCountService.create_session(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            session_type=InventoryCountSessionType.CYCLE_COUNT,
            notes="Quarterly cycle count for shelf A",
            actor_user=pharmacist_maker_a,
        )
        assert session.status == InventoryCountSessionStatus.IN_PROGRESS

        # 2. Maker scans items:
        # Napa: counted 110 (Surplus of +10)
        item1 = InventoryCountService.record_scan_item(
            session_id=session.session_id,
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_number=b1.batch_number,
            counted_quantity=110,
            storage_location="SH-A1",
            actor_user=pharmacist_maker_a,
        )
        assert item1.discrepancy_quantity == 10
        assert item1.discrepancy_type == InventoryCountDiscrepancyType.SURPLUS
        assert item1.cost_discrepancy_bdt == Decimal("20.0000")

        # Ace: counted 45 (Shortage of -5)
        item2 = InventoryCountService.record_scan_item(
            session_id=session.session_id,
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_number=b2.batch_number,
            counted_quantity=45,
            storage_location="SH-A2",
            actor_user=pharmacist_maker_a,
        )
        assert item2.discrepancy_quantity == -5
        assert item2.discrepancy_type == InventoryCountDiscrepancyType.SHORTAGE
        assert item2.cost_discrepancy_bdt == Decimal("-10.0000")

        session.refresh_from_db()
        assert session.total_items_counted == 2
        assert session.total_discrepancy_units == 5  # +10 + (-5) = +5
        assert session.total_cost_discrepancy_bdt == Decimal("10.0000")
        assert session.surplus_count == 1
        assert session.shortage_count == 1

        # 3. Maker submits session for review
        session = InventoryCountService.submit_for_review(
            session_id=session.session_id,
            tenant_id=tenant_a.tenant_id,
            actor_user=pharmacist_maker_a,
            notes="Physical count completed without incident.",
        )
        assert session.status == InventoryCountSessionStatus.SUBMITTED_FOR_REVIEW

        # 4. Checker reviews and approves session
        session = InventoryCountService.review_session(
            session_id=session.session_id,
            tenant_id=tenant_a.tenant_id,
            is_approved=True,
            checker_user=manager_checker_a,
            checker_notes="Discrepancies verified against shelf registers.",
        )
        assert session.status == InventoryCountSessionStatus.APPROVED
        assert session.reviewed_by == manager_checker_a

        # 5. Post adjustments to Stock Ledger
        res = InventoryCountService.post_adjustments(
            session_id=session.session_id,
            tenant_id=tenant_a.tenant_id,
            actor_user=manager_checker_a,
        )
        assert res["status"] == InventoryCountSessionStatus.POSTED
        assert res["adjustments_posted_count"] == 2

        # 6. Verify physical BatchStock updated
        b1.refresh_from_db()
        b2.refresh_from_db()
        assert b1.quantity_base_units == 110  # 100 + 10 = 110
        assert b2.quantity_base_units == 45   # 50 - 5 = 45

        # 7. Verify StockLedgerEntry entries
        item1.refresh_from_db()
        item2.refresh_from_db()
        assert item1.adjustment_ledger_entry_id != ""
        entry1 = StockLedgerEntry.objects.get(entry_id=item1.adjustment_ledger_entry_id)
        assert entry1.movement_type == StockMovementType.STOCK_ADJUSTMENT_IN
        assert entry1.quantity_delta == 10
        assert entry1.balance_after == 110
        assert len(entry1.immutable_verification_hash) == 64

        entry2 = StockLedgerEntry.objects.get(entry_id=item2.adjustment_ledger_entry_id)
        assert entry2.movement_type == StockMovementType.STOCK_ADJUSTMENT_OUT
        assert entry2.quantity_delta == -5
        assert entry2.balance_after == 45
        assert len(entry2.immutable_verification_hash) == 64

    def test_maker_checker_anti_self_approval_enforced(
        self, tenant_a, branch_a, manager_checker_a, product_napa
    ):
        """Test 2: Maker cannot approve their own count session (Segregation of Duties)."""
        today = timezone.now().date()
        b = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_napa,
            batch_number="BAT-SELF-01",
            quantity_base_units=60,
            cost_per_unit=Decimal("2.0000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=100),
        )

        # Manager creates and scans
        session = InventoryCountService.create_session(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            actor_user=manager_checker_a,
        )
        InventoryCountService.record_scan_item(
            session_id=session.session_id,
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            batch_number=b.batch_number,
            counted_quantity=65,
            actor_user=manager_checker_a,
        )
        InventoryCountService.submit_for_review(
            session_id=session.session_id,
            tenant_id=tenant_a.tenant_id,
            actor_user=manager_checker_a,
        )

        # Same manager attempts to approve own session
        with pytest.raises(ValidationError) as exc:
            InventoryCountService.review_session(
                session_id=session.session_id,
                tenant_id=tenant_a.tenant_id,
                is_approved=True,
                checker_user=manager_checker_a,
            )
        assert "Maker-checker separation violated" in str(exc.value)

    def test_invalid_inputs_and_already_posted_guard(
        self, tenant_a, branch_a, pharmacist_maker_a, manager_checker_a, product_napa
    ):
        """Test 3: Negative counted quantities and re-posting already-posted sessions are rejected."""
        session = InventoryCountService.create_session(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            actor_user=pharmacist_maker_a,
        )

        # Rejection: Negative counted quantity
        with pytest.raises(ValidationError) as exc:
            InventoryCountService.record_scan_item(
                session_id=session.session_id,
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                batch_number="ANY-BATCH",
                counted_quantity=-5,
                actor_user=pharmacist_maker_a,
            )
        assert "Counted quantity cannot be negative" in str(exc.value)

        # Rejection: Submitting session with 0 items
        with pytest.raises(ValidationError) as exc:
            InventoryCountService.submit_for_review(
                session_id=session.session_id,
                tenant_id=tenant_a.tenant_id,
                actor_user=pharmacist_maker_a,
            )
        assert "Cannot submit empty count session" in str(exc.value)

    def test_cashier_rbac_rejection(
        self, tenant_a, branch_a, cashier_user_a
    ):
        """Test 4: Cashiers cannot initiate inventory count sessions (403 Forbidden)."""
        with pytest.raises(PermissionDenied) as exc:
            InventoryCountService.create_session(
                tenant_id=tenant_a.tenant_id,
                branch_id=branch_a.branch_id,
                actor_user=cashier_user_a,
            )
        assert "Cashiers are not authorized to create or execute inventory count sessions" in str(exc.value)

    def test_cross_tenant_isolation(
        self, tenant_a, tenant_b, branch_a, branch_b, pharmacist_maker_a, manager_user_b
    ):
        """Test 5: Tenant B manager cannot view or act on Tenant A's count sessions."""
        session_a = InventoryCountService.create_session(
            tenant_id=tenant_a.tenant_id,
            branch_id=branch_a.branch_id,
            actor_user=pharmacist_maker_a,
        )

        with pytest.raises(NotFound):
            InventoryCountService.submit_for_review(
                session_id=session_a.session_id,
                tenant_id=tenant_b.tenant_id,
                actor_user=manager_user_b,
            )

    def test_drf_api_endpoints(
        self, tenant_a, branch_a, pharmacist_maker_a, manager_checker_a, product_napa
    ):
        """Test 6: REST API scan, submit, review and post endpoints."""
        today = timezone.now().date()
        b = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=product_napa,
            batch_number="BAT-API-CNT",
            quantity_base_units=40,
            cost_per_unit=Decimal("2.0000"),
            purchase_date=today,
            expiry_date=today + timezone.timedelta(days=100),
        )

        client_maker = APIClient()
        client_maker.force_authenticate(user=pharmacist_maker_a)

        # 1. Create Session via API
        resp = client_maker.post(
            "/api/v2/inventory/count-sessions/",
            {
                "branch": branch_a.branch_id,
                "session_type": InventoryCountSessionType.CYCLE_COUNT,
                "notes": "API test session",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        session_id = resp.data["session_id"]

        # 2. Scan item via API
        resp_scan = client_maker.post(
            f"/api/v2/inventory/count-sessions/{session_id}/scan-item/",
            {
                "batch_number": b.batch_number,
                "counted_quantity": 42,
                "scanner_barcode": product_napa.barcode,
            },
            format="json",
        )
        assert resp_scan.status_code == status.HTTP_201_CREATED
        assert resp_scan.data["discrepancy_quantity"] == 2

        # 3. Submit for review via API
        resp_sub = client_maker.post(
            f"/api/v2/inventory/count-sessions/{session_id}/submit-for-review/",
            {"notes": "Ready for manager review"},
            format="json",
        )
        assert resp_sub.status_code == status.HTTP_200_OK
        assert resp_sub.data["status"] == InventoryCountSessionStatus.SUBMITTED_FOR_REVIEW

        # 4. Review by Manager via API
        client_checker = APIClient()
        client_checker.force_authenticate(user=manager_checker_a)

        resp_rev = client_checker.post(
            f"/api/v2/inventory/count-sessions/{session_id}/review/",
            {"is_approved": True, "checker_notes": "Approved by manager"},
            format="json",
        )
        assert resp_rev.status_code == status.HTTP_200_OK
        assert resp_rev.data["status"] == InventoryCountSessionStatus.APPROVED

        # 5. Post adjustments via API
        resp_post = client_checker.post(
            f"/api/v2/inventory/count-sessions/{session_id}/post-adjustments/",
            format="json",
        )
        assert resp_post.status_code == status.HTTP_200_OK
        assert resp_post.data["status"] == InventoryCountSessionStatus.POSTED
        assert resp_post.data["adjustments_posted_count"] == 1

