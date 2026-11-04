"""
backend/tests/test_p170_lot_allocation_records.py
Architecture Decision: ADR-0170 (P170).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Sale Item Lot Allocation Records, DGDA Recall Traceability,
Controlled Restocking Returns, and Unblended Gross Profit Analytics.
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
    SaleInvoice,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestSaleLotAllocationIntegration:
    """Integration test suite for P170 Lot Allocation Records in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        # 1. Tenant A & Branch A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p170-a-{self.suffix}",
            name=f"Square Pharma Care {self.suffix}",
            subdomain=f"square-p170-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p170-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Tenant B & Branch B (cross-tenant tests)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p170-b-{self.suffix}",
            name=f"Beximco Pharmacy {self.suffix}",
            subdomain=f"beximco-p170-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p170-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p170-susp-{self.suffix}",
            name="Suspended Tenant",
            subdomain=f"susp-p170-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p170-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Users
        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier POS",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist_user = PharmacyUser.objects.create_user(
            email=f"pharmacist-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Staff Pharmacist",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.auditor_user = PharmacyUser.objects.create_user(
            email=f"auditor-{self.suffix}@pharma.com",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Auditor User",
            password="Password123!",
            role=UserRole.AUDITOR,
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
            role=UserRole.CASHIER,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

        # 5. Product
        self.product = MedicineProduct.objects.create(
            product_id=f"med-p170-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg + 65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            cost_unit=Decimal("2.0000"),
            mrp_unit=Decimal("3.0000"),
        )

        # 6. Batches
        self.batch_1 = BatchStock.objects.create(
            batch_id=f"bat-p170-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BX-2026-A-{self.suffix}",
            quantity_base_units=200,
            cost_per_unit=Decimal("2.00"),
            purchase_date=self.today - datetime.timedelta(days=20),
            expiry_date=self.today + datetime.timedelta(days=365),
            status=BatchStatus.ACTIVE,
        )

        self.batch_2 = BatchStock.objects.create(
            batch_id=f"bat-p170-2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BX-2026-B-{self.suffix}",
            quantity_base_units=150,
            cost_per_unit=Decimal("2.20"),
            purchase_date=self.today - datetime.timedelta(days=10),
            expiry_date=self.today + datetime.timedelta(days=400),
            status=BatchStatus.ACTIVE,
        )

        # 7. Purchase Cost Lots
        self.cost_lot_1 = PurchaseCostLot.objects.create(
            lot_id=f"pcl-p170-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_1,
            purchase_date=self.today - datetime.timedelta(days=20),
            unit_cost=Decimal("2.0000"),
            initial_quantity=200,
            remaining_quantity=200,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        self.cost_lot_2 = PurchaseCostLot.objects.create(
            lot_id=f"pcl-p170-2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_2,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("2.2000"),
            initial_quantity=150,
            remaining_quantity=150,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # 8. Sale Invoice
        self.invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-p170-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice_number=f"INV-2026-{self.suffix.upper()}",
            customer_name="Rahim Ahmed",
            customer_phone="+8801711111111",
            total_amount=Decimal("210.00"),
            net_amount=Decimal("210.00"),
        )

    def test_01_record_sale_lot_allocations_success(self):
        """Records lot allocations across multiple batches and cost lots."""
        self.client.force_authenticate(user=self.cashier_user)

        res = self.client.post(
            "/api/v2/inventory/sale-lot-allocations/record-allocations/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "invoice_id": self.invoice.invoice_id,
                "items_allocations": [
                    {
                        "batch_id": self.batch_1.batch_id,
                        "cost_lot_id": self.cost_lot_1.lot_id,
                        "quantity_base_units": 50,
                        "unit_selling_price": "3.0000",
                    },
                    {
                        "batch_id": self.batch_2.batch_id,
                        "cost_lot_id": self.cost_lot_2.lot_id,
                        "quantity_base_units": 20,
                        "unit_selling_price": "3.0000",
                    },
                ],
            },
            format="json",
        )
        assert res.status_code == status.HTTP_201_CREATED, res.data
        allocations = res.data
        assert len(allocations) == 2

        # 1. Verify Allocation 1
        a1 = allocations[0]
        assert a1["batch_id"] == self.batch_1.batch_id
        assert a1["cost_lot_id"] == self.cost_lot_1.lot_id
        assert a1["quantity_base_units"] == 50
        assert Decimal(a1["total_selling_price"]) == Decimal("150.0000")
        assert Decimal(a1["total_cost"]) == Decimal("100.0000")  # 50 * 2.00
        assert Decimal(a1["gross_profit"]) == Decimal("50.0000")
        assert Decimal(a1["profit_margin_pct"]) == Decimal("33.33")

        # 2. Verify Allocation 2
        a2 = allocations[1]
        assert a2["batch_id"] == self.batch_2.batch_id
        assert a2["cost_lot_id"] == self.cost_lot_2.lot_id
        assert a2["quantity_base_units"] == 20
        assert Decimal(a2["total_selling_price"]) == Decimal("60.0000")
        assert Decimal(a2["total_cost"]) == Decimal("44.0000")  # 20 * 2.20
        assert Decimal(a2["gross_profit"]) == Decimal("16.0000")
        assert Decimal(a2["profit_margin_pct"]) == Decimal("26.67")

        # Verify Cost Lots decremented
        self.cost_lot_1.refresh_from_db()
        self.cost_lot_2.refresh_from_db()
        assert self.cost_lot_1.remaining_quantity == 150  # 200 - 50
        assert self.cost_lot_2.remaining_quantity == 130  # 150 - 20

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(
            tenant_id=self.tenant_a.tenant_id,
            action="SALE_LOT_ALLOCATIONS_RECORDED",
            resource_id=self.invoice.invoice_id,
        ).first()
        assert audit is not None

    def test_02_get_allocation_details_and_list(self):
        """Retrieves single allocation and filters list by invoice_id."""
        self.client.force_authenticate(user=self.cashier_user)

        alloc = SaleLotAllocation.objects.create(
            allocation_id=f"sall-get-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_1,
            cost_lot=self.cost_lot_1,
            quantity_base_units=40,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("120.0000"),
            total_cost=Decimal("80.0000"),
            gross_profit=Decimal("40.0000"),
            profit_margin_pct=Decimal("33.33"),
        )

        # Retrieve by pk
        res_get = self.client.get(
            f"/api/v2/inventory/sale-lot-allocations/{alloc.allocation_id}/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert res_get.status_code == status.HTTP_200_OK, res_get.data
        assert res_get.data["allocation_id"] == alloc.allocation_id

        # List by invoice_id
        res_list = self.client.get(
            f"/api/v2/inventory/sale-lot-allocations/?tenant_id={self.tenant_a.tenant_id}&invoice_id={self.invoice.invoice_id}"
        )
        assert res_list.status_code == status.HTTP_200_OK, res_list.data
        assert len(res_list.data) >= 1

    def test_03_process_return_partial_and_full(self):
        """Processes partial and full returns against exact lot allocation and restocks inventory."""
        self.client.force_authenticate(user=self.pharmacist_user)

        alloc = SaleLotAllocation.objects.create(
            allocation_id=f"sall-ret-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_1,
            cost_lot=self.cost_lot_1,
            quantity_base_units=50,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("150.0000"),
            total_cost=Decimal("100.0000"),
            gross_profit=Decimal("50.0000"),
            profit_margin_pct=Decimal("33.33"),
            status=SaleLotAllocationStatus.COMMITTED,
        )
        self.batch_1.quantity_base_units = 150
        self.batch_1.save()
        self.cost_lot_1.remaining_quantity = 150
        self.cost_lot_1.save()

        # 1. Partial return: 20 units
        res_part = self.client.post(
            "/api/v2/inventory/sale-lot-allocations/process-return/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "allocation_id": alloc.allocation_id,
                "return_quantity_base_units": 20,
                "restock_inventory": True,
                "reason": "Customer over-purchased",
            },
            format="json",
        )
        assert res_part.status_code == status.HTTP_200_OK, res_part.data
        assert res_part.data["returned_quantity_base_units"] == 20
        assert Decimal(res_part.data["refund_amount"]) == Decimal("60.0000")  # 20 * 3.00
        assert Decimal(res_part.data["refunded_cogs"]) == Decimal("40.0000")  # 20 * 2.00

        # Verify batch and cost lot restocked
        self.batch_1.refresh_from_db()
        self.cost_lot_1.refresh_from_db()
        assert self.batch_1.quantity_base_units == 170  # 150 + 20
        assert self.cost_lot_1.remaining_quantity == 170
        alloc.refresh_from_db()
        assert alloc.status == SaleLotAllocationStatus.PARTIALLY_RETURNED

        # 2. Complete remainder return: 30 units
        res_full = self.client.post(
            "/api/v2/inventory/sale-lot-allocations/process-return/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "allocation_id": alloc.allocation_id,
                "return_quantity_base_units": 30,
                "restock_inventory": True,
            },
            format="json",
        )
        assert res_full.status_code == status.HTTP_200_OK, res_full.data
        alloc.refresh_from_db()
        assert alloc.status == SaleLotAllocationStatus.FULLY_RETURNED
        assert alloc.returned_quantity_base_units == 50

    def test_04_excess_return_rejected(self):
        """Rejects return quantity exceeding allocated base units."""
        self.client.force_authenticate(user=self.pharmacist_user)

        alloc = SaleLotAllocation.objects.create(
            allocation_id=f"sall-exc-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_1,
            quantity_base_units=30,
            returned_quantity_base_units=20,  # 10 units remaining
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("90.0000"),
            total_cost=Decimal("60.0000"),
            gross_profit=Decimal("30.0000"),
            status=SaleLotAllocationStatus.PARTIALLY_RETURNED,
        )

        res = self.client.post(
            "/api/v2/inventory/sale-lot-allocations/process-return/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "allocation_id": alloc.allocation_id,
                "return_quantity_base_units": 15,  # 15 > 10
            },
            format="json",
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_RETURN_EXCEEDS_DISPENSED"
        assert int(res.data.get("max_returnable_units")) == 10

    def test_05_invalid_input_zero_or_negative_quantity(self):
        """Rejects zero or negative return quantity."""
        self.client.force_authenticate(user=self.cashier_user)

        alloc = SaleLotAllocation.objects.create(
            allocation_id=f"sall-invq-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_1,
            quantity_base_units=30,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("90.0000"),
            total_cost=Decimal("60.0000"),
            gross_profit=Decimal("30.0000"),
        )

        res = self.client.post(
            "/api/v2/inventory/sale-lot-allocations/process-return/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "allocation_id": alloc.allocation_id,
                "return_quantity_base_units": 0,
            },
            format="json",
        )
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert res.data.get("error_code") == "ERR_INVALID_QUANTITY"

    def test_06_statutory_recall_traceability(self):
        """Traces all customer contacts and invoices who received a recalled batch."""
        self.client.force_authenticate(user=self.pharmacist_user)

        # Invoice 1 to Rahim Ahmed
        SaleLotAllocation.objects.create(
            allocation_id=f"sall-rec1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_1,
            quantity_base_units=50,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("150.0000"),
            total_cost=Decimal("100.0000"),
            gross_profit=Decimal("50.0000"),
        )

        # Invoice 2 to Nasima Begum
        inv2 = SaleInvoice.objects.create(
            invoice_id=f"inv-rec2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice_number=f"INV-REC2-{self.suffix.upper()}",
            customer_name="Nasima Begum",
            customer_phone="+8801733333333",
            total_amount=Decimal("90.00"),
            net_amount=Decimal("90.00"),
        )
        SaleLotAllocation.objects.create(
            allocation_id=f"sall-rec2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=inv2,
            product=self.product,
            batch=self.batch_1,
            quantity_base_units=30,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("90.0000"),
            total_cost=Decimal("60.0000"),
            gross_profit=Decimal("30.0000"),
        )

        res = self.client.get(
            f"/api/v2/inventory/sale-lot-allocations/recall-traceability/?tenant_id={self.tenant_a.tenant_id}&batch_id={self.batch_1.batch_id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        report = res.data
        assert report["total_dispensed_units"] == 80  # 50 + 30
        assert report["affected_invoices_count"] == 2
        assert len(report["affected_customers"]) == 2
        customer_names = [c["customer_name"] for c in report["affected_customers"]]
        assert "Rahim Ahmed" in customer_names
        assert "Nasima Begum" in customer_names

    def test_07_profit_analytics_unblended_cogs(self):
        """Calculates exact aggregate revenue, unblended COGS, and profit margin."""
        self.client.force_authenticate(user=self.pharmacist_user)

        SaleLotAllocation.objects.create(
            allocation_id=f"sall-ana1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_1,
            cost_lot=self.cost_lot_1,
            quantity_base_units=50,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("150.0000"),
            total_cost=Decimal("100.0000"),
            gross_profit=Decimal("50.0000"),
            profit_margin_pct=Decimal("33.33"),
        )
        SaleLotAllocation.objects.create(
            allocation_id=f"sall-ana2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_2,
            cost_lot=self.cost_lot_2,
            quantity_base_units=20,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.2000"),
            total_selling_price=Decimal("60.0000"),
            total_cost=Decimal("44.0000"),
            gross_profit=Decimal("16.0000"),
            profit_margin_pct=Decimal("26.67"),
        )

        res = self.client.get(
            f"/api/v2/inventory/sale-lot-allocations/profit-analytics/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.data
        data = res.data
        assert Decimal(data["total_revenue"]) == Decimal("210.0000")  # 150 + 60
        assert Decimal(data["total_cogs"]) == Decimal("144.0000")  # 100 + 44
        assert Decimal(data["gross_profit"]) == Decimal("66.0000")  # 210 - 144
        assert Decimal(data["profit_margin_pct"]) == Decimal("31.43")  # 66 / 210 * 100
        assert data["total_units_sold"] == 70

    def test_08_rbac_unauthorized_role_blocked(self):
        """Auditor cannot record allocations or process returns."""
        self.client.force_authenticate(user=self.auditor_user)

        res = self.client.post(
            "/api/v2/inventory/sale-lot-allocations/record-allocations/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "invoice_id": self.invoice.invoice_id,
                "items_allocations": [{"batch_id": self.batch_1.batch_id, "quantity_base_units": 10}],
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert res.data.get("error_code") == "ERR_ROLE_UNAUTHORIZED"

    def test_09_cross_tenant_isolation_blocked(self):
        """Tenant B user cannot access or modify Tenant A allocations."""
        self.client.force_authenticate(user=self.user_b)

        alloc = SaleLotAllocation.objects.create(
            allocation_id=f"sall-cross-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_1,
            quantity_base_units=10,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("30.0000"),
            total_cost=Decimal("20.0000"),
            gross_profit=Decimal("10.0000"),
        )

        res = self.client.get(
            f"/api/v2/inventory/sale-lot-allocations/{alloc.allocation_id}/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        payload = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert payload.get("error_code") in ("ERR_TENANT_MISMATCH", "ERR_CROSS_TENANT_VIOLATION")

    def test_10_suspended_tenant_blocked(self):
        """Suspended tenant is blocked from sale lot operations."""
        self.client.force_authenticate(user=self.user_susp)

        res = self.client.post(
            "/api/v2/inventory/sale-lot-allocations/record-allocations/",
            {
                "tenant_id": self.tenant_suspended.tenant_id,
                "invoice_id": "inv-any",
                "items_allocations": [{"batch_id": "bat-any", "quantity_base_units": 10}],
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        payload = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert payload.get("error_code") in (
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_CROSS_TENANT_VIOLATION",
        )

