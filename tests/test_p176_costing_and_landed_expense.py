"""
backend/tests/test_p176_costing_and_landed_expense.py
Architecture Decision: ADR-0176 (P176).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P176 Costing & Landed Expense Allocation:
1. Pure pro-rata by value allocation math and strict penny conservation.
2. Pure pro-rata by quantity allocation math across base units.
3. Preview allocation endpoint calculates distribution without database mutation.
4. Create and post landed cost capitalizes PurchaseCostLot and BatchStock valuations.
5. Reversal restores original base purchase unit costs in lots and batches.
6. Double reversal attempt is rejected with ERR_ALREADY_REVERSED.
7. Cashier role access is barred with HTTP 403 ERR_PERMISSION_DENIED.
8. Cross-branch cost lot allocation attempt is rejected with ERR_CROSS_BRANCH_LOT.
9. Cross-tenant access is barred with HTTP 403 ERR_CROSS_TENANT_ACCESS.
10. Operations on suspended tenant are blocked with ERR_TENANT_SUSPENDED.
"""

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.landed_cost_service import LandedCostService
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    LandedCostAllocationMethod,
    LandedCostExpense,
    LandedCostStatus,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestLandedCostExpenseIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.unique_id = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Two Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p176-{self.unique_id}",
            name="Square Health Retail",
            subdomain=f"square-p176-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.unique_id}",
            tenant=self.tenant,
            name="Banani Main Branch",
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.unique_id}",
            tenant=self.tenant,
            name="Dhanmondi Branch",
        )

        # 2. Setup Foreign Tenant for cross-tenant tests
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.unique_id}",
            name="Foreign Pharma Ltd.",
            subdomain=f"foreign-p176-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # 3. Setup Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"manager_{self.unique_id}@pharmacy.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.unique_id}@pharmacy.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. Setup Products
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.unique_id}",
            brand_name="Napa 500mg",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="500mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("2.0000"),
            cost_unit=Decimal("1.2000"),
        )
        self.product_seclo = MedicineProduct.objects.create(
            product_id=f"prod-seclo-{self.unique_id}",
            brand_name="Seclo 20mg",
            generic_name="Omeprazole",
            dosage_form="Capsule",
            strength="20mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("6.0000"),
            cost_unit=Decimal("4.5000"),
        )

        # 5. Setup Batches & PurchaseCostLots in Branch A
        self.batch_napa = BatchStock.objects.create(
            batch_id=f"bat-napa-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"BN-NAPA-{self.unique_id}",
            quantity_base_units=100,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=5),
            cost_per_unit=Decimal("1.2000"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_napa = PurchaseCostLot.objects.create(
            lot_id=f"lot-napa-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_napa,
            batch=self.batch_napa,
            purchase_date=self.today - datetime.timedelta(days=5),
            unit_cost=Decimal("1.2000"),
            initial_quantity=100,
            remaining_quantity=100,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        self.batch_seclo = BatchStock.objects.create(
            batch_id=f"bat-seclo-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_seclo,
            batch_number=f"BN-SECLO-{self.unique_id}",
            quantity_base_units=200,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=5),
            cost_per_unit=Decimal("4.5000"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_seclo = PurchaseCostLot.objects.create(
            lot_id=f"lot-seclo-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_seclo,
            batch=self.batch_seclo,
            purchase_date=self.today - datetime.timedelta(days=5),
            unit_cost=Decimal("4.5000"),
            initial_quantity=200,
            remaining_quantity=200,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # 6. Setup Cost Lot in Branch B (to test cross-branch block)
        self.batch_branch_b = BatchStock.objects.create(
            batch_id=f"bat-b-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_b,
            product=self.product_napa,
            batch_number=f"BN-NAPA-B-{self.unique_id}",
            quantity_base_units=50,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=5),
            cost_per_unit=Decimal("1.2000"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_branch_b = PurchaseCostLot.objects.create(
            lot_id=f"lot-b-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_b,
            product=self.product_napa,
            batch=self.batch_branch_b,
            purchase_date=self.today - datetime.timedelta(days=5),
            unit_cost=Decimal("1.2000"),
            initial_quantity=50,
            remaining_quantity=50,
            status=PurchaseCostLotStatus.ACTIVE,
        )

    def test_01_pro_rata_by_value_allocation_and_penny_conservation(self):
        """
        Verify pure allocation math by purchase value and exact penny conservation.
        Total expense = 750.35 BDT (odd amount with fractional remainder).
        """
        total_expense = Decimal("750.3500")
        cost_lots = [self.lot_napa, self.lot_seclo]

        allocations = LandedCostService.calculate_allocations(
            total_expense=total_expense,
            allocation_method=LandedCostAllocationMethod.BY_VALUE,
            cost_lots=cost_lots,
        )

        assert len(allocations) == 2
        sum_allocated = sum(item["allocated_expense_amount"] for item in allocations)
        # Strict penny conservation: sum must EQUAL total expense down to 0.0001 BDT
        assert sum_allocated == total_expense

        # Verify lot_seclo got more allocation because its purchase value is 200 * 4.5 = 900 vs 100 * 1.2 = 120
        napa_alloc = next(a for a in allocations if a["cost_lot_id"] == self.lot_napa.lot_id)
        seclo_alloc = next(a for a in allocations if a["cost_lot_id"] == self.lot_seclo.lot_id)

        assert seclo_alloc["allocated_expense_amount"] > napa_alloc["allocated_expense_amount"]
        assert napa_alloc["lot_landed_unit_cost_after"] > napa_alloc["lot_base_unit_cost_before"]
        assert seclo_alloc["lot_landed_unit_cost_after"] > seclo_alloc["lot_base_unit_cost_before"]

    def test_02_pro_rata_by_quantity_allocation(self):
        """
        Verify pro-rata by quantity allocation distributes based on base units.
        Napa: 100 units, Seclo: 200 units -> Total = 300 units.
        Total expense = 300.00 BDT -> Exactly 1.00 BDT per unit.
        """
        total_expense = Decimal("300.0000")
        cost_lots = [self.lot_napa, self.lot_seclo]

        allocations = LandedCostService.calculate_allocations(
            total_expense=total_expense,
            allocation_method=LandedCostAllocationMethod.BY_QUANTITY,
            cost_lots=cost_lots,
        )

        napa_alloc = next(a for a in allocations if a["cost_lot_id"] == self.lot_napa.lot_id)
        seclo_alloc = next(a for a in allocations if a["cost_lot_id"] == self.lot_seclo.lot_id)

        assert napa_alloc["allocated_expense_amount"] == Decimal("100.0000")
        assert seclo_alloc["allocated_expense_amount"] == Decimal("200.0000")
        assert napa_alloc["allocated_cost_per_unit"] == Decimal("1.0000")
        assert seclo_alloc["allocated_cost_per_unit"] == Decimal("1.0000")

    def test_03_preview_allocation_endpoint(self):
        """
        Verify the preview allocation endpoint calculates distribution without database mutation.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/landed-costs/preview/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "total_expense_bdt": "500.0000",
            "allocation_method": "BY_VALUE",
            "cost_lot_ids": [self.lot_napa.lot_id, self.lot_seclo.lot_id],
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        data = response.data
        assert Decimal(str(data["total_expense_bdt"])) == Decimal("500.0000")
        assert len(data["allocations"]) == 2

        # Invariant: No LandedCostExpense record created in database during preview
        assert LandedCostExpense.objects.count() == 0

    def test_04_create_and_post_landed_cost_updates_lot_and_batch_valuation(self):
        """
        Verify creating and posting a landed cost expense capitalizes lot and batch unit costs.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/landed-costs/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "expense_type": "TRANSPORT_FREIGHT",
            "allocation_method": "BY_VALUE",
            "total_expense_bdt": "400.0000",
            "invoice_ref": "TRUCK-INV-101",
            "reason": "Freight from central warehouse to Banani branch",
            "cost_lot_ids": [self.lot_napa.lot_id, self.lot_seclo.lot_id],
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        expense_id = response.data["expense_id"]
        expense = LandedCostExpense.objects.get(expense_id=expense_id)
        assert expense.status == LandedCostStatus.POSTED
        assert expense.lines.count() == 2

        # Refresh lots and batches
        self.lot_napa.refresh_from_db()
        self.lot_seclo.refresh_from_db()
        self.batch_napa.refresh_from_db()
        self.batch_seclo.refresh_from_db()

        # Valuation must be capitalized above base cost
        assert self.lot_napa.unit_cost > Decimal("1.2000")
        assert self.lot_seclo.unit_cost > Decimal("4.5000")
        assert self.batch_napa.cost_per_unit > Decimal("1.20")
        assert self.batch_seclo.cost_per_unit > Decimal("4.50")

        # Audit log entry must be present
        audit_entry = AuditLogEntry.objects.filter(
            action="LANDED_COST_POSTED",
            resource_id=expense_id,
        ).first()
        assert audit_entry is not None
        assert audit_entry.status == AuditActionStatus.SUCCESS

    def test_05_reversal_restores_original_base_unit_cost(self):
        """
        Verify that reversing a posted landed cost expense restores the original acquisition unit costs.
        """
        self.client.force_authenticate(user=self.manager)

        # 1. Post a landed cost
        post_url = "/api/v2/inventory/landed-costs/"
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "expense_type": "COLD_CHAIN_LOGISTICS",
            "allocation_method": "BY_QUANTITY",
            "total_expense_bdt": "300.0000",
            "reason": "Dry ice and refrigerated container logistics",
            "cost_lot_ids": [self.lot_napa.lot_id, self.lot_seclo.lot_id],
        }
        res = self.client.post(post_url, payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        expense_id = res.data["expense_id"]

        # Unit costs should be increased
        self.lot_napa.refresh_from_db()
        assert self.lot_napa.unit_cost == Decimal("2.2000")  # 1.20 + 1.00

        # 2. Reverse the landed cost
        rev_url = f"/api/v2/inventory/landed-costs/{expense_id}/reverse/"
        rev_payload = {"reason": "Logistics invoice was double-billed and credited back"}
        rev_res = self.client.post(rev_url, rev_payload, format="json")
        assert rev_res.status_code == status.HTTP_200_OK
        assert rev_res.data["status"] == LandedCostStatus.REVERSED

        # 3. Check lot and batch unit costs restored
        self.lot_napa.refresh_from_db()
        self.lot_seclo.refresh_from_db()
        self.batch_napa.refresh_from_db()
        self.batch_seclo.refresh_from_db()

        assert self.lot_napa.unit_cost == Decimal("1.2000")
        assert self.lot_seclo.unit_cost == Decimal("4.5000")
        assert self.batch_napa.cost_per_unit == Decimal("1.20")
        assert self.batch_seclo.cost_per_unit == Decimal("4.50")

        # Audit log for reversal
        rev_audit = AuditLogEntry.objects.filter(
            action="LANDED_COST_REVERSED",
            resource_id=expense_id,
        ).first()
        assert rev_audit is not None

    def test_06_double_reversal_prohibited(self):
        """
        Verify that a second reversal attempt on an already reversed expense is blocked.
        """
        self.client.force_authenticate(user=self.manager)
        post_url = "/api/v2/inventory/landed-costs/"
        res = self.client.post(
            post_url,
            {
                "tenant_id": self.tenant.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "expense_type": "REGULATORY_FEE",
                "allocation_method": "BY_VALUE",
                "total_expense_bdt": "150.0000",
                "reason": "Port clearance regulatory fee for inspection",
                "cost_lot_ids": [self.lot_napa.lot_id],
            },
            format="json",
        )
        expense_id = res.data["expense_id"]

        # First reversal succeeds
        rev_url = f"/api/v2/inventory/landed-costs/{expense_id}/reverse/"
        rev_res1 = self.client.post(rev_url, {"reason": "Initial correct reversal"}, format="json")
        assert rev_res1.status_code == status.HTTP_200_OK

        # Second reversal fails with ERR_ALREADY_REVERSED
        rev_res2 = self.client.post(rev_url, {"reason": "Second reversal attempt"}, format="json")
        assert rev_res2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert rev_res2.data.get("error_code") in ["ERR_ALREADY_REVERSED", "ERR_VALIDATION_FAILED"]
        assert "ERR_ALREADY_REVERSED" in str(rev_res2.data)

    def test_07_cashier_cannot_post_landed_cost_permission_denied(self):
        """
        Verify that cashiers and non-managers cannot post landed costs (HTTP 403).
        """
        self.client.force_authenticate(user=self.cashier)
        url = "/api/v2/inventory/landed-costs/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "expense_type": "HANDLING_LABOR",
            "allocation_method": "BY_VALUE",
            "total_expense_bdt": "200.0000",
            "reason": "Unloading labor expenses for incoming cargo",
            "cost_lot_ids": [self.lot_napa.lot_id],
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_PERMISSION_DENIED"

    def test_08_cross_branch_cost_lot_allocation_blocked(self):
        """
        Verify that attempting to allocate expenses to cost lots belonging to a different branch is barred.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/landed-costs/"

        # lot_branch_b belongs to branch_b, but request branch is branch_a
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "expense_type": "TRANSPORT_FREIGHT",
            "allocation_method": "BY_VALUE",
            "total_expense_bdt": "250.0000",
            "reason": "Transport charges across locations",
            "cost_lot_ids": [self.lot_napa.lot_id, self.lot_branch_b.lot_id],
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data.get("error_code") in ["ERR_CROSS_BRANCH_LOT", "ERR_VALIDATION_FAILED"]
        assert "ERR_CROSS_BRANCH_LOT" in str(response.data)

    def test_09_cross_tenant_isolation_blocked(self):
        """
        Verify that cross-tenant access attempts are strictly blocked with HTTP 403.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/landed-costs/"

        payload = {
            "tenant_id": self.foreign_tenant.tenant_id,  # Foreign tenant
            "branch_id": self.branch_a.branch_id,
            "expense_type": "TRANSPORT_FREIGHT",
            "allocation_method": "BY_VALUE",
            "total_expense_bdt": "100.0000",
            "reason": "Cross tenant illicit posting attempt",
            "cost_lot_ids": [self.lot_napa.lot_id],
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION", "ERR_PERMISSION_DENIED"]

    def test_10_suspended_tenant_blocked(self):
        """
        Verify that operations on a suspended tenant are locked with HTTP 403.
        """
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/landed-costs/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "expense_type": "TRANSPORT_FREIGHT",
            "allocation_method": "BY_VALUE",
            "total_expense_bdt": "100.0000",
            "reason": "Attempting to post on suspended tenant account",
            "cost_lot_ids": [self.lot_napa.lot_id],
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") in [
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_TENANT_SUSPENDED",
            "ERR_TENANT_INACTIVE",
            "ERR_PERMISSION_DENIED",
        ]
