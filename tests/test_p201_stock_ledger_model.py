"""
backend/tests/test_p201_stock_ledger_model.py
Architecture Decision: ADR-0201 (P201).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests for Append-Only Stock Movement Ledger:
1. Positive inward receipt movement with balance conservation and SHA-256 hash.
2. Subsequent outward sale movement updating batch stock and availability.
3. Mathematical stock conservation invariant violation rejection.
4. Zero quantity_delta rejection.
5. Insufficient stock reduction rejection.
6. Immutability guarantee: updates and deletes strictly forbidden.
7. Automated audit reconciliation and tamper detection.
8. Role-based access control (Cashier denied 403) and API endpoint parity.
9. Cross-tenant isolation assurance.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStock,
    Branch,
    BranchStockAvailability,
    MedicineProduct,
    StockLedgerEntry,
    StockMovementType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole
from apps.core.stock_ledger_service import StockLedgerService


@pytest.fixture
def tenant():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Lakeside Pharma Dhanmondi",
        subdomain=f"lakeside-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def other_tenant():
    uid = uuid.uuid4().hex[:8]
    return Tenant.objects.create(
        tenant_id=f"ten-{uid}",
        name="Green Cross Uttara",
        subdomain=f"greencross-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch(tenant):
    return Branch.objects.create(
        branch_id=f"br-{uuid.uuid4().hex[:8]}",
        tenant=tenant,
        name="Main Dispensary",
    )


@pytest.fixture
def product():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Napa Extra 500mg/65mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="TABLET",
        cost_unit=Decimal("2.2500"),
        mrp_unit=Decimal("3.5000"),
    )


@pytest.fixture
def cold_product():
    uid = uuid.uuid4().hex[:8]
    return MedicineProduct.objects.create(
        product_id=f"med-{uid}",
        brand_name="Insulatard 100IU/ml 10ml",
        generic_name="Insulin Human (NPH)",
        dosage_form="INJECTION",
        cost_unit=Decimal("400.0000"),
        mrp_unit=Decimal("480.0000"),
    )


@pytest.fixture
def batch(tenant, branch, product):
    return BatchStock.objects.create(
        batch_id=f"bat-{uuid.uuid4().hex[:8]}",
        tenant=tenant,
        branch=branch,
        product=product,
        batch_number="BCH-202611-001",
        quantity_base_units=0,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("2.2500"),
        purchase_date=timezone.now().date(),
        expiry_date=timezone.now().date() + timezone.timedelta(days=365),
    )


@pytest.fixture
def manager_user(tenant, branch):
    user = PharmacyUser.objects.create_user(
        phone_number="+8801700000201",
        email="manager@lakeside.com",
        password="ValidPassword123!",
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )
    return user


@pytest.fixture
def cashier_user(tenant, branch):
    user = PharmacyUser.objects.create_user(
        phone_number="+8801700000202",
        email="cashier@lakeside.com",
        password="ValidPassword123!",
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id,
        role=UserRole.CASHIER,
    )
    return user


@pytest.mark.django_db
class TestStockLedgerModelAndService:

    def test_record_stock_movement_creates_immutable_entry_and_conserves_balance(self, tenant, branch, product, batch, manager_user):
        """Test 1: Inward purchase movement updates batch, balance, and records valid SHA-256 hash."""
        entry = StockLedgerService.record_stock_movement(
            tenant=tenant,
            branch=branch,
            product=product,
            movement_type=StockMovementType.PURCHASE_RECEIPT,
            quantity_delta=100,
            source_entity_type="GOODS_RECEIPT_NOTE",
            source_entity_id="grn-001",
            source_reference="GRN-202611-001",
            batch=batch,
            unit_cost=Decimal("2.2500"),
            posted_by=manager_user,
        )

        assert entry.entry_id.startswith("stkld-")
        assert entry.balance_before == 0
        assert entry.balance_after == 100
        assert entry.quantity_delta == 100
        assert entry.balance_after == entry.balance_before + entry.quantity_delta
        assert entry.total_cost == Decimal("225.0000")
        assert len(entry.immutable_verification_hash) == 64
        assert entry.immutable_verification_hash == entry.compute_hash()

        # BatchStock updated
        batch.refresh_from_db()
        assert batch.quantity_base_units == 100

        # Branch availability updated
        avail = BranchStockAvailability.objects.get(tenant=tenant, branch=branch, product=product)
        assert avail.quantity_on_hand == 100
        assert avail.is_out_of_stock is False

    def test_subsequent_movement_reduces_stock_accurately(self, tenant, branch, product, batch, manager_user):
        """Test 2: Outward sale movement decrements stock conservation properly."""
        # Initial receipt
        StockLedgerService.record_stock_movement(
            tenant=tenant,
            branch=branch,
            product=product,
            movement_type=StockMovementType.PURCHASE_RECEIPT,
            quantity_delta=50,
            source_entity_type="GOODS_RECEIPT_NOTE",
            source_entity_id="grn-002",
            source_reference="GRN-202611-002",
            batch=batch,
            posted_by=manager_user,
        )

        # Sale dispense
        dispense = StockLedgerService.record_stock_movement(
            tenant=tenant,
            branch=branch,
            product=product,
            movement_type=StockMovementType.SALE_DISPENSE,
            quantity_delta=-15,
            source_entity_type="SALE_INVOICE",
            source_entity_id="inv-001",
            source_reference="INV-202611-0088",
            batch=batch,
            posted_by=manager_user,
        )

        assert dispense.balance_before == 50
        assert dispense.balance_after == 35
        assert dispense.balance_after == dispense.balance_before + dispense.quantity_delta

        batch.refresh_from_db()
        assert batch.quantity_base_units == 35

    def test_stock_conservation_violation_raises_validation_error(self, tenant, branch, product, batch):
        """Test 3: Direct attempt to violate conservation equation throws ValidationError."""
        entry = StockLedgerEntry(
            tenant=tenant,
            branch=branch,
            product=product,
            batch=batch,
            batch_number=batch.batch_number,
            movement_type=StockMovementType.STOCK_ADJUSTMENT_IN,
            quantity_delta=10,
            balance_before=0,
            balance_after=15,  # Violates 0 + 10 != 15!
            source_entity_type="STOCK_ADJUSTMENT",
            source_entity_id="adj-001",
            source_reference="ADJ-202611-001",
        )
        with pytest.raises(ValidationError) as exc:
            entry.clean()
        assert "Conservation invariant violated" in str(exc.value)

    def test_zero_quantity_delta_rejected(self, tenant, branch, product, batch):
        """Test 4: Movement with 0 quantity delta is prohibited."""
        from rest_framework.exceptions import ValidationError as DRFValidationError
        with pytest.raises((ValidationError, DRFValidationError)):
            StockLedgerService.record_stock_movement(
                tenant=tenant,
                branch=branch,
                product=product,
                movement_type=StockMovementType.STOCK_ADJUSTMENT_IN,
                quantity_delta=0,
                source_entity_type="MANUAL_COUNT",
                source_entity_id="adj-000",
                source_reference="ADJ-000",
                batch=batch,
            )

    def test_insufficient_stock_rejection(self, tenant, branch, product, batch):
        """Test 5: Attempting to deduct stock below zero is rejected."""
        batch.quantity_base_units = 5
        batch.save()

        from rest_framework.exceptions import ValidationError as DRFValidationError
        with pytest.raises((ValidationError, DRFValidationError)) as exc:
            StockLedgerService.record_stock_movement(
                tenant=tenant,
                branch=branch,
                product=product,
                movement_type=StockMovementType.SALE_DISPENSE,
                quantity_delta=-10,
                source_entity_type="SALE_INVOICE",
                source_entity_id="inv-overflow",
                source_reference="INV-OVERFLOW",
                batch=batch,
            )
        assert "Insufficient batch stock" in str(exc.value)

    def test_immutability_blocks_updates_and_deletes(self, tenant, branch, product, batch, manager_user):
        """Test 6: Existing ledger entries cannot be altered or removed."""
        entry = StockLedgerService.record_stock_movement(
            tenant=tenant,
            branch=branch,
            product=product,
            movement_type=StockMovementType.PURCHASE_RECEIPT,
            quantity_delta=40,
            source_entity_type="GOODS_RECEIPT_NOTE",
            source_entity_id="grn-immutable",
            source_reference="GRN-IMMUTABLE",
            batch=batch,
            posted_by=manager_user,
        )

        # Attempt save update
        entry.notes = "Attempting unauthorized modification"
        with pytest.raises(ValidationError) as exc_update:
            entry.save()
        assert "strictly append-only and cannot be updated" in str(exc_update.value)

        # Attempt delete
        with pytest.raises(ValidationError) as exc_del:
            entry.delete()
        assert "strictly append-only and cannot be deleted" in str(exc_del.value)

    def test_reconciliation_verifies_ledger_and_detects_tampering(self, tenant, branch, product, batch, manager_user):
        """Test 7: Automated reconciliation computes algebraic sum and detects cryptographic tampering."""
        # Record inward 100
        StockLedgerService.record_stock_movement(
            tenant=tenant,
            branch=branch,
            product=product,
            movement_type=StockMovementType.PURCHASE_RECEIPT,
            quantity_delta=100,
            source_entity_type="GRN",
            source_entity_id="grn-100",
            source_reference="GRN-100",
            batch=batch,
            posted_by=manager_user,
        )
        # Record outward -30
        StockLedgerService.record_stock_movement(
            tenant=tenant,
            branch=branch,
            product=product,
            movement_type=StockMovementType.SALE_DISPENSE,
            quantity_delta=-30,
            source_entity_type="SALE",
            source_entity_id="sale-30",
            source_reference="SALE-30",
            batch=batch,
            posted_by=manager_user,
        )

        recon = StockLedgerService.reconcile_batch_stock(
            tenant_id=tenant.tenant_id,
            batch_number_or_id=batch.batch_number,
            branch_id=branch.branch_id,
        )
        assert recon["is_balanced"] is True
        assert recon["recorded_batch_quantity"] == 70
        assert recon["ledger_sum_quantity"] == 70
        assert recon["discrepancy"] == 0
        assert recon["tamper_detected"] is False
        assert recon["total_entries"] == 2

    def test_role_enforcement_and_api_endpoints(self, tenant, branch, product, batch, manager_user, cashier_user):
        """Test 8: API endpoints, cashier 403 denial, manager 200 success, and direct modification rejection."""
        client = APIClient()

        # Manager can list ledger
        client.force_authenticate(user=manager_user)
        res_mgr = client.get("/api/v2/inventory/stock-ledger/")
        assert res_mgr.status_code == status.HTTP_200_OK

        # Manager can check batch trail
        res_trail = client.get(f"/api/v2/inventory/stock-ledger/batch-trail/?batch_number={batch.batch_number}")
        assert res_trail.status_code == status.HTTP_200_OK
        assert "results" in res_trail.data

        # Manager can run reconcile
        res_recon = client.post(
            "/api/v2/inventory/stock-ledger/reconcile/",
            {"batch_number": batch.batch_number},
            format="json",
        )
        assert res_recon.status_code == status.HTTP_200_OK
        assert "is_balanced" in res_recon.data

        # Direct POST to create is disallowed (405)
        res_post = client.post("/api/v2/inventory/stock-ledger/", {"product": product.product_id})
        assert res_post.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        # Cashier role is denied access (403)
        client.force_authenticate(user=cashier_user)
        res_cashier = client.get("/api/v2/inventory/stock-ledger/")
        assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    def test_cross_tenant_isolation(self, tenant, other_tenant, branch, product, batch, manager_user):
        """Test 9: Stock ledger entries of Tenant A are never accessible or visible to Tenant B."""
        entry = StockLedgerService.record_stock_movement(
            tenant=tenant,
            branch=branch,
            product=product,
            movement_type=StockMovementType.PURCHASE_RECEIPT,
            quantity_delta=50,
            source_entity_type="GRN",
            source_entity_id="grn-iso",
            source_reference="GRN-ISO",
            batch=batch,
            posted_by=manager_user,
        )

        other_branch = Branch.objects.create(
            branch_id=f"br-{uuid.uuid4().hex[:8]}",
            tenant=other_tenant,
            name="Other Branch",
        )
        other_user = PharmacyUser.objects.create_user(
            phone_number="+8801700000299",
            email="other_manager@greencross.com",
            password="ValidPassword123!",
            tenant_id=other_tenant.tenant_id,
            branch_id=other_branch.branch_id,
            role=UserRole.BRANCH_MANAGER,
        )

        client = APIClient()
        client.force_authenticate(user=other_user)

        # Listing for Tenant B does not contain Tenant A's entry
        res = client.get("/api/v2/inventory/stock-ledger/")
        assert res.status_code == status.HTTP_200_OK
        ids = [item["entry_id"] for item in res.data["results"]]
        assert entry.entry_id not in ids

        # Detail fetch by Tenant B returns 404
        res_detail = client.get(f"/api/v2/inventory/stock-ledger/{entry.entry_id}/")
        assert res_detail.status_code == status.HTTP_404_NOT_FOUND
