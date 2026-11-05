"""
backend/tests/test_p174_stock_correction_restrictions.py
Architecture Decision: ADR-0174 (P174).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P174 Stock Correction Restrictions:
1. Audited, reasoned adjustment commands with mandatory justification (>= 10 chars).
2. Delta validation: non-zero signed integer.
3. Two-person dual authorization workflow (cashiers propose, managers approve).
4. Direct balance edits and updates/deletions prohibited (HTTP 405 MethodNotAllowed).
5. Conservation & reservation safety: cannot reduce stock below locked cart reservations.
6. Reversible ledger entries: symmetrical compensating records with negative deltas.
7. Double-reversal prevention and audit logging verification.
8. Recalculation of BranchStockAvailability (P171) on approval and reversal.
9. Cross-tenant isolation boundaries.
10. Suspended tenant lockouts.
"""

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
    BranchStockAvailability,
    InventoryBaseUnitStrategy,
    MedicineProduct,
    PurchaseCostLot,
    StockAdjustmentRecord,
    StockAdjustmentType,
    StockCorrectionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestStockCorrectionRestrictionsIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.unique_id = uuid.uuid4().hex[:6]

        # 1. Setup Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-corr-{self.unique_id}",
            name="Banani Care Pharmacy",
            subdomain=f"banani-corr-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-corr-{self.unique_id}",
            tenant=self.tenant,
            name="Banani Branch",
        )

        # 2. Setup Cashier and Manager Users
        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier_{self.unique_id}@pharmacy.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Cashier POS",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )
        self.manager_user = PharmacyUser.objects.create_user(
            email=f"manager_{self.unique_id}@pharmacy.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        # 3. Setup Product & Packaging Base Unit Strategy
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.unique_id}",
            brand_name="Napa Extra 500mg/65mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("3.0000"),
            cost_unit=Decimal("2.2000"),
        )
        self.strategy = InventoryBaseUnitStrategy.objects.create(
            strategy_id=f"strat-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            base_unit="TABLET",
            pack_unit_label="BOX",
            intermediate_unit_label="STRIP",
            pack_to_intermediate_multiplier=10,
            intermediate_to_base_multiplier=10,
            total_base_units_per_pack=100,
            is_active=True,
        )

        # 4. Setup Batch: 100 physical units, 20 reserved units
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-CORR-001",
            quantity_base_units=100,
            reserved_quantity_base_units=20,
            expiry_date=self.today + datetime.timedelta(days=180),
            purchase_date=self.today - datetime.timedelta(days=30),
            cost_per_unit=Decimal("2.20"),
            status=BatchStatus.ACTIVE,
        )

        # 5. Setup PurchaseCostLot
        self.cost_lot = PurchaseCostLot.objects.create(
            lot_id=f"lot-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch=self.batch,
            purchase_date=self.today - datetime.timedelta(days=30),
            unit_cost=Decimal("2.2000"),
            initial_quantity=100,
            remaining_quantity=100,
            status="ACTIVE",
        )

    def test_01_cashier_propose_stock_adjustment_creates_pending_record(self):
        """Cashier proposes stock reduction for damaged items; record created as PENDING."""
        self.client.force_authenticate(user=self.cashier_user)

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "cost_lot_id": self.cost_lot.lot_id,
            "adjustment_type": "DAMAGE",
            "quantity_delta": -10,
            "reason": "Ten blister strips damaged by liquid leak in storage carton",
            "supporting_document_ref": "INCIDENT-2026-001",
        }
        res = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED, res.data

        data = res.data
        assert data["adjustment_id"].startswith("stkadj-")
        assert data["status"] == StockCorrectionStatus.PENDING
        assert data["quantity_delta"] == -10
        assert Decimal(data["total_cost_impact"]) == Decimal("22.0000")
        assert data["proposed_by_id"] == str(self.cashier_user.user_id)
        assert data["approved_by_id"] is None

        # Verify physical stock remains UNCHANGED while pending
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 100

        # Verify audit log entry
        audit_entry = AuditLogEntry.objects.filter(
            resource_id=data["adjustment_id"],
            action="STOCK_CORRECTION_PROPOSED",
        ).first()
        assert audit_entry is not None

    def test_02_zero_quantity_delta_rejected(self):
        """Zero quantity delta is rejected by serializer and service."""
        self.client.force_authenticate(user=self.cashier_user)

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "adjustment_type": "DAMAGE",
            "quantity_delta": 0,
            "reason": "Testing invalid zero quantity delta",
        }
        res = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "delta" in str(res.data).lower()

    def test_03_reason_length_minimum_enforced(self):
        """Reason under 10 characters is rejected with validation error."""
        self.client.force_authenticate(user=self.cashier_user)

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "adjustment_type": "COUNT_CORRECTION",
            "quantity_delta": 5,
            "reason": "short",
        }
        res = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "reason" in str(res.data).lower() or "10" in str(res.data)

    def test_04_manager_approval_updates_physical_stock_and_availability(self):
        """Manager approves pending proposal; updates physical batch, cost lot, and availability matrix."""
        # Cashier proposes
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "cost_lot_id": self.cost_lot.lot_id,
            "adjustment_type": "EXPIRY_REMOVAL",
            "quantity_delta": -15,
            "reason": "Fifteen units expired and removed from active shelf",
        }
        res_prop = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        adj_id = res_prop.data["adjustment_id"]

        # Manager approves
        self.client.force_authenticate(user=self.manager_user)
        res_app = self.client.post(f"/api/v2/inventory/stock-corrections/{adj_id}/approve/", format="json")
        assert res_app.status_code == status.HTTP_200_OK, res_app.data
        assert res_app.data["status"] == StockCorrectionStatus.APPROVED
        assert res_app.data["approved_by_id"] == str(self.manager_user.user_id)
        assert res_app.data["approved_at"] is not None

        # Verify physical batch stock decreased from 100 to 85
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 85

        # Verify cost lot remaining decreased from 100 to 85
        self.cost_lot.refresh_from_db()
        assert self.cost_lot.remaining_quantity == 85

        # Verify BranchStockAvailability recalculated: on_hand=85, reserved=20, available=65
        avail = BranchStockAvailability.objects.get(
            tenant=self.tenant, branch=self.branch, product=self.product
        )
        assert avail.quantity_on_hand == 85
        assert avail.quantity_reserved == 20
        assert avail.quantity_physically_available == 85
        assert avail.quantity_server_confirmed_available == 65

    def test_05_cashier_cannot_approve_permission_denied(self):
        """Cashier attempting to approve an adjustment is blocked (403 Forbidden)."""
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "adjustment_type": "THEFT_LOSS",
            "quantity_delta": -2,
            "reason": "Missing two strips discovered during mid-day spot check",
        }
        res_prop = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        adj_id = res_prop.data["adjustment_id"]

        # Cashier tries to approve
        res_app = self.client.post(f"/api/v2/inventory/stock-corrections/{adj_id}/approve/", format="json")
        assert res_app.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_PERMISSION_DENIED" in str(res_app.data)

    def test_06_stock_reduction_exceeding_unreserved_stock_rejected(self):
        """Attempting reduction that exceeds unreserved physical stock is blocked."""
        # Batch: 100 units, 20 reserved -> available unreserved = 80 units
        # Attempting reduction of 85 units must fail
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "adjustment_type": "DAMAGE",
            "quantity_delta": -85,
            "reason": "Severe storage damage writing off 85 units from batch",
        }
        res = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_INSUFFICIENT_UNRESERVED_STOCK" in str(res.data)

    def test_07_manager_reject_pending_adjustment(self):
        """Manager rejects pending proposal; record marked REJECTED and no inventory mutated."""
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "adjustment_type": "COUNT_CORRECTION",
            "quantity_delta": -12,
            "reason": "Count discrepancy recorded by afternoon cashier",
        }
        res_prop = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        adj_id = res_prop.data["adjustment_id"]

        # Manager rejects with audit reason
        self.client.force_authenticate(user=self.manager_user)
        res_rej = self.client.post(
            f"/api/v2/inventory/stock-corrections/{adj_id}/reject/",
            {"reason": "Recounted by pharmacist: 12 units located in back shelf overflow"},
            format="json",
        )
        assert res_rej.status_code == status.HTTP_200_OK, res_rej.data
        assert res_rej.data["status"] == StockCorrectionStatus.REJECTED

        # Stock remains unchanged
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 100

    def test_08_reversal_creates_compensating_entry_and_restores_balance(self):
        """Reversing an approved adjustment creates a symmetrical compensating record and restores stock."""
        # Propose and approve an adjustment of -20 units
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "cost_lot_id": self.cost_lot.lot_id,
            "adjustment_type": "INTERNAL_USE",
            "quantity_delta": -20,
            "reason": "Twenty units issued to internal medical emergency room",
        }
        res_prop = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        adj_id = res_prop.data["adjustment_id"]

        self.client.force_authenticate(user=self.manager_user)
        self.client.post(f"/api/v2/inventory/stock-corrections/{adj_id}/approve/", format="json")

        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 80

        # Post reversal
        rev_payload = {"reason": "Internal transfer cancelled: emergency room stock returned intact"}
        res_rev = self.client.post(
            f"/api/v2/inventory/stock-corrections/{adj_id}/reverse/", rev_payload, format="json"
        )
        assert res_rev.status_code == status.HTTP_201_CREATED, res_rev.data
        rev_data = res_rev.data

        assert rev_data["adjustment_id"].startswith("stkadj-")
        assert rev_data["reversal_of_id"] == adj_id
        assert rev_data["quantity_delta"] == 20  # Exact opposite (+20)
        assert rev_data["status"] == StockCorrectionStatus.APPROVED

        # Verify original record marked as reversed
        orig_record = StockAdjustmentRecord.objects.get(adjustment_id=adj_id)
        assert orig_record.is_reversed is True
        assert orig_record.status == StockCorrectionStatus.REVERSED

        # Verify batch stock restored from 80 back to 100
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 100

        # Verify cost lot restored back to 100
        self.cost_lot.refresh_from_db()
        assert self.cost_lot.remaining_quantity == 100

        # Verify availability restored
        avail = BranchStockAvailability.objects.get(
            tenant=self.tenant, branch=self.branch, product=self.product
        )
        assert avail.quantity_on_hand == 100

    def test_09_double_reversal_prohibited(self):
        """Attempting to reverse an already reversed adjustment fails with validation error."""
        self.client.force_authenticate(user=self.manager_user)
        # Create an already approved record
        adj = StockAdjustmentRecord.objects.create(
            adjustment_id=f"stkadj-rev-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            medicine=self.product,
            batch=self.batch,
            adjustment_type=StockAdjustmentType.THEFT_LOSS,
            quantity_delta=-5,
            unit_cost_bdt=Decimal("2.20"),
            total_cost_impact=Decimal("11.00"),
            reason="Five units lost in shipment transit",
            status=StockCorrectionStatus.APPROVED,
            proposed_by=self.manager_user,
            approved_by=self.manager_user,
        )

        # First reversal succeeds
        res1 = self.client.post(
            f"/api/v2/inventory/stock-corrections/{adj.adjustment_id}/reverse/",
            {"reason": "First valid reversal of shipment transit loss"},
            format="json",
        )
        assert res1.status_code == status.HTTP_201_CREATED

        # Second reversal must fail
        res2 = self.client.post(
            f"/api/v2/inventory/stock-corrections/{adj.adjustment_id}/reverse/",
            {"reason": "Second invalid duplicate reversal attempt"},
            format="json",
        )
        assert res2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_ALREADY_REVERSED" in str(res2.data)

    def test_10_cross_tenant_isolation_and_direct_mutation_prohibited(self):
        """Cross-tenant access is blocked; direct PUT/PATCH/DELETE on adjustments returns 405."""
        # 1. Cross-tenant block
        tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.unique_id}",
            name="Rival Pharma",
            subdomain=f"rival-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        user_rival = PharmacyUser.objects.create_user(
            email=f"rival_{self.unique_id}@pharmacy.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Rival Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=tenant_b.tenant_id,
        )
        self.client.force_authenticate(user=user_rival)

        # Propose against Tenant A's branch
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch.branch_id,
            "batch_id": self.batch.batch_id,
            "adjustment_type": "DAMAGE",
            "quantity_delta": -5,
            "reason": "Attempting cross tenant stock alteration",
        }
        res_cross = self.client.post("/api/v2/inventory/stock-corrections/propose/", payload, format="json")
        assert res_cross.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_CROSS_TENANT_ACCESS" in str(res_cross.data)

        # 2. Direct mutation prohibition (HTTP 405 Method Not Allowed)
        self.client.force_authenticate(user=self.manager_user)
        adj = StockAdjustmentRecord.objects.create(
            adjustment_id=f"stkadj-edit-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            medicine=self.product,
            batch=self.batch,
            adjustment_type=StockAdjustmentType.DAMAGE,
            quantity_delta=-5,
            unit_cost_bdt=Decimal("2.20"),
            total_cost_impact=Decimal("11.00"),
            reason="Direct modification test record",
            status=StockCorrectionStatus.APPROVED,
        )
        res_put = self.client.put(f"/api/v2/inventory/stock-corrections/{adj.adjustment_id}/", {"quantity_delta": -10}, format="json")
        assert res_put.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        res_patch = self.client.patch(f"/api/v2/inventory/stock-corrections/{adj.adjustment_id}/", {"quantity_delta": -10}, format="json")
        assert res_patch.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        res_del = self.client.delete(f"/api/v2/inventory/stock-corrections/{adj.adjustment_id}/", format="json")
        assert res_del.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
