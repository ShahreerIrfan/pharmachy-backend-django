"""
backend/tests/test_p167_purchase_cost_lot.py
Architecture Decision: ADR-0167 (P167).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Purchase Cost Lot Entity,
COGS Valuation Separation from Batch Identity, and Inventory Valuation.
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    MedicineProduct,
    PlanTier,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestPurchaseCostLotIntegration:
    """Integration test suite for P167 Purchase Cost Lot in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        # 1. Tenant A & Branch A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p167-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p167-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p167-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Tenant B & Branch B (cross-tenant tests)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p167-b-{self.suffix}",
            name=f"Beximco Pharmacy {self.suffix}",
            subdomain=f"beximco-p167-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p167-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p167-susp-{self.suffix}",
            name="Suspended Tenant",
            subdomain=f"susp-p167-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p167-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Users
        self.pharmacist_user = PharmacyUser.objects.create_user(
            email=f"pharmacist-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacist User",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier User",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.user_b = PharmacyUser.objects.create_user(
            email=f"userb-{self.suffix}@pharmab.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="User B Manager",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )

        self.user_susp = PharmacyUser.objects.create_user(
            email=f"susp-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Suspended User",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

        # 5. Product & Batch
        self.product = MedicineProduct.objects.create(
            product_id=f"med-p167-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.0000"),
            mrp_unit=Decimal("3.0000"),
        )

        self.batch = BatchStock.objects.create(
            batch_id=f"bat-p167-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"B-101-{self.suffix}",
            quantity_base_units=0,
            cost_per_unit=Decimal("2.00"),
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=365),
            status=BatchStatus.ACTIVE,
        )

        # 6. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-p167-{self.suffix}",
            tenant=self.tenant_a,
            name="Beximco Distribution Centre",
            contact_person="Kabir Ahmed",
            phone="+8801711223344",
        )

    def test_purchase_lot_creation_success(self):
        """Pharmacist receives stock into a new purchase lot; increments batch stock and audits."""
        self.client.force_authenticate(user=self.pharmacist_user)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_id": self.batch.batch_id,
            "supplier_id": self.supplier.supplier_id,
            "purchase_order_number": "PO-2026-001",
            "grn_number": "GRN-2026-881",
            "invoice_reference": "INV-BX-990",
            "purchase_date": self.today.isoformat(),
            "unit_cost": "2.1000",
            "initial_quantity": 100,
        }

        resp = self.client.post("/api/v2/inventory/purchase-cost-lots/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data
        assert data["unit_cost"] == "2.1000"
        assert data["initial_quantity"] == 100
        assert data["remaining_quantity"] == 100
        assert data["status"] == "ACTIVE"

        # Verify batch stock incremented
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 100

        # Verify audit log
        audit = AuditLogEntry.objects.filter(
            tenant_id=self.tenant_a.tenant_id,
            action="PURCHASE_COST_LOT_CREATED",
            resource_id=data["lot_id"],
        ).first()
        assert audit is not None
        assert audit.actor_role == UserRole.STAFF_PHARMACIST

    def test_identical_batch_different_prices_multi_tier_fifo_cogs(self):
        """
        Receives two purchase lots of the identical batch at different prices (2.10 BDT and 2.35 BDT).
        Allocates 120 units across lots: exact unblended COGS must equal 257.00 BDT.
        """
        # Lot 1: 100 units @ 2.1000 BDT (purchased 10 days ago)
        lot1 = PurchaseCostLot.objects.create(
            lot_id=f"lot1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            supplier=self.supplier,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("2.1000"),
            initial_quantity=100,
            remaining_quantity=100,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # Lot 2: 50 units @ 2.3500 BDT (purchased today) for SAME batch!
        lot2 = PurchaseCostLot.objects.create(
            lot_id=f"lot2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            supplier=self.supplier,
            purchase_date=self.today,
            unit_cost=Decimal("2.3500"),
            initial_quantity=50,
            remaining_quantity=50,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # Batch has 150 units in total
        self.batch.quantity_base_units = 150
        self.batch.save()

        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "quantity": 120,
        }

        resp = self.client.post("/api/v2/inventory/purchase-cost-lots/allocate-cogs/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK, resp.data
        data = resp.data
        assert data["total_units"] == 120
        # 100 * 2.10 = 210.00; 20 * 2.35 = 47.00; Total = 257.0000 BDT!
        assert data["total_cogs"] == "257.0000"
        assert data["effective_unit_cogs"] == "2.1417"

        # Verify lot state changes
        lot1.refresh_from_db()
        lot2.refresh_from_db()
        assert lot1.remaining_quantity == 0
        assert lot1.status == PurchaseCostLotStatus.DEPLETED
        assert lot2.remaining_quantity == 30
        assert lot2.status == PurchaseCostLotStatus.ACTIVE

        # Verify batch stock decremented
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 30

    def test_reverse_cogs_allocation_restores_lots(self):
        """Reversing previous allocations restores lot stock and reactivates depleted lot."""
        lot1 = PurchaseCostLot.objects.create(
            lot_id=f"lot-rev1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            purchase_date=self.today - datetime.timedelta(days=5),
            unit_cost=Decimal("2.1000"),
            initial_quantity=100,
            remaining_quantity=0,
            status=PurchaseCostLotStatus.DEPLETED,
        )

        self.batch.quantity_base_units = 0
        self.batch.status = BatchStatus.DEPLETED
        self.batch.save()

        self.client.force_authenticate(user=self.pharmacist_user)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "allocations": [
                {"lot_id": lot1.lot_id, "units_allocated": 25},
            ],
            "reason": "Customer Return",
        }

        resp = self.client.post("/api/v2/inventory/purchase-cost-lots/reverse-cogs/", payload, format="json")
        assert resp.status_code == status.HTTP_200_OK, resp.data
        data = resp.data
        assert data["total_restored_units"] == 25
        assert data["total_restored_cogs"] == "52.5000"

        lot1.refresh_from_db()
        assert lot1.remaining_quantity == 25
        assert lot1.status == PurchaseCostLotStatus.ACTIVE

        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 25
        assert self.batch.status == BatchStatus.ACTIVE

    def test_inventory_valuation_endpoint(self):
        """Valuation endpoint computes sum(remaining_quantity * unit_cost) accurately."""
        # Lot 1: 50 remaining @ 2.0000 = 100.0000 BDT
        PurchaseCostLot.objects.create(
            lot_id=f"lot-val1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            purchase_date=self.today,
            unit_cost=Decimal("2.0000"),
            initial_quantity=100,
            remaining_quantity=50,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # Lot 2: 40 remaining @ 2.5000 = 100.0000 BDT
        PurchaseCostLot.objects.create(
            lot_id=f"lot-val2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            purchase_date=self.today,
            unit_cost=Decimal("2.5000"),
            initial_quantity=100,
            remaining_quantity=40,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.pharmacist_user)
        resp = self.client.get(
            f"/api/v2/inventory/purchase-cost-lots/valuation/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["total_quantity_base_units"] == 90
        assert data["total_inventory_valuation_bdt"] == "200.0000"

    def test_invalid_input_rejected(self):
        """Rejects zero/negative quantity and negative acquisition unit cost."""
        self.client.force_authenticate(user=self.pharmacist_user)

        # 1. Zero quantity
        payload_zero = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_id": self.batch.batch_id,
            "unit_cost": "2.1000",
            "initial_quantity": 0,
        }
        r1 = self.client.post("/api/v2/inventory/purchase-cost-lots/", payload_zero, format="json")
        assert r1.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert r1.data.get("error_code") == "ERR_INVALID_QUANTITY"

        # 2. Negative cost
        payload_neg_cost = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_id": self.batch.batch_id,
            "unit_cost": "-5.0000",
            "initial_quantity": 10,
        }
        r2 = self.client.post("/api/v2/inventory/purchase-cost-lots/", payload_neg_cost, format="json")
        assert r2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert r2.data.get("error_code") == "ERR_INVALID_COST"

    def test_cannot_receive_into_expired_batch(self):
        """Receiving stock into an expired batch is strictly prohibited."""
        expired_batch = BatchStock.objects.create(
            batch_id=f"bat-exp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"EXP-{self.suffix}",
            quantity_base_units=0,
            cost_per_unit=Decimal("2.00"),
            purchase_date=self.today - datetime.timedelta(days=200),
            expiry_date=self.today - datetime.timedelta(days=5),
            status=BatchStatus.EXPIRED,
        )

        self.client.force_authenticate(user=self.pharmacist_user)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_id": expired_batch.batch_id,
            "unit_cost": "2.0000",
            "initial_quantity": 50,
        }

        resp = self.client.post("/api/v2/inventory/purchase-cost-lots/", payload, format="json")
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp.data.get("error_code") == "ERR_CANNOT_RECEIVE_INTO_EXPIRED_BATCH"

    def test_insufficient_cost_lot_stock_fails(self):
        """Requesting more units than total available cost lots raises ERR_INSUFFICIENT_COST_LOT_STOCK."""
        PurchaseCostLot.objects.create(
            lot_id=f"lot-small-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            purchase_date=self.today,
            unit_cost=Decimal("2.1000"),
            initial_quantity=20,
            remaining_quantity=10,
            status=PurchaseCostLotStatus.ACTIVE,
        )
        self.batch.quantity_base_units = 10
        self.batch.save()

        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "quantity": 50,  # exceeds 10 available
        }

        resp = self.client.post("/api/v2/inventory/purchase-cost-lots/allocate-cogs/", payload, format="json")
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert resp.data.get("error_code") == "ERR_INSUFFICIENT_COST_LOT_STOCK"

    def test_cashier_role_unauthorized_to_create_or_reverse(self):
        """Cashiers cannot create purchase cost lots or trigger reversals."""
        self.client.force_authenticate(user=self.cashier_user)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "batch_id": self.batch.batch_id,
            "unit_cost": "2.1000",
            "initial_quantity": 50,
        }

        resp = self.client.post("/api/v2/inventory/purchase-cost-lots/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data.get("error_code") == "ERR_ROLE_UNAUTHORIZED"

    def test_cross_tenant_isolation(self):
        """Tenant B user cannot access or allocate Tenant A cost lots."""
        lot = PurchaseCostLot.objects.create(
            lot_id=f"lot-iso-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch,
            purchase_date=self.today,
            unit_cost=Decimal("2.1000"),
            initial_quantity=50,
            remaining_quantity=50,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.user_b)
        resp = self.client.get(
            f"/api/v2/inventory/purchase-cost-lots/{lot.lot_id}/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data.get("error_code") in ("ERR_TENANT_MISMATCH", "ERR_CROSS_TENANT_VIOLATION") or "tenant" in str(data).lower()

    def test_suspended_tenant_blocked(self):
        """Suspended tenants cannot create purchase cost lots."""
        self.client.force_authenticate(user=self.user_susp)

        payload = {
            "tenant_id": self.tenant_suspended.tenant_id,
            "branch_id": self.branch_suspended.branch_id,
            "product_id": self.product.product_id,
            "batch_id": self.batch.batch_id,
            "unit_cost": "2.1000",
            "initial_quantity": 50,
        }

        resp = self.client.post("/api/v2/inventory/purchase-cost-lots/", payload, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(data).lower()
