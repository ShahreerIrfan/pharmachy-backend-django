"""
backend/tests/test_p179_stock_valuation_service.py
Architecture Decision: ADR-0179 (P179).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P179 Stock Valuation Service:
1. Valid scenario: FIFO actual lot costing, retail valuation, potential gross margin.
2. 5-tier inventory aging buckets (0-30, 31-60, 61-90, 91-180, 181+ days).
3. Weighted average costing method derived from batch inventory.
4. Realized margin derived from committed immutable SaleLotAllocation entries.
5. Valuation snapshot creation and audit log entry persistence.
6. Snapshot immutability: PUT/PATCH/DELETE barred with HTTP 405.
7. Invalid inputs: invalid costing method, reversed date range, non-existent branch.
8. Cashier RBAC gate: barred with HTTP 403 ERR_PERMISSION_DENIED.
9. Cross-tenant access: strictly blocked with HTTP 403 ERR_CROSS_TENANT_ACCESS.
10. Suspended tenant: blocked with HTTP 403 ERR_TENANT_SUSPENDED.
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

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    SaleInvoice,
    SaleInvoiceItem,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    StockValuationSnapshot,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestStockValuationServiceIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant and Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p179-{self.suffix}",
            name="Popular Diagnostic Pharmacy",
            subdomain=f"popular-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Dhanmondi Branch A",
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant,
            name="Mirpur Branch B",
        )

        # Foreign Tenant
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.suffix}",
            name="Foreign Apex Pharma",
            subdomain=f"apex-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name="Suspended Retailer Ltd.",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 2. Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"manager_{self.suffix}@popular.com",
            phone_number=f"+880181{random.randint(1000000, 9999999)}",
            full_name="Branch Manager Popular",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@popular.com",
            phone_number=f"+880182{random.randint(1000000, 9999999)}",
            full_name="Cashier Popular",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@apex.com",
            phone_number=f"+880183{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
        )
        self.suspended_manager = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@susp.com",
            phone_number=f"+880184{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 3. Products
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("3.0000"),
            cost_unit=Decimal("2.0000"),
        )
        self.product_seclo = MedicineProduct.objects.create(
            product_id=f"prod-seclo-{self.suffix}",
            brand_name="Seclo 20mg",
            generic_name="Omeprazole",
            dosage_form="Capsule",
            strength="20mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("7.0000"),
            cost_unit=Decimal("5.0000"),
        )

        # 4. Batches in Branch A
        self.batch_napa = BatchStock.objects.create(
            batch_id=f"bat-napa-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"BN-NAPA-{self.suffix}",
            lot_reference=f"LOT-NAPA-{self.suffix}",
            quantity_base_units=150,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=15),
            cost_per_unit=Decimal("2.0667"),  # Weighted avg of lots
            status=BatchStatus.ACTIVE,
        )
        self.batch_seclo = BatchStock.objects.create(
            batch_id=f"bat-seclo-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_seclo,
            batch_number=f"BN-SECLO-{self.suffix}",
            lot_reference=f"LOT-SECLO-{self.suffix}",
            quantity_base_units=40,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=400),
            purchase_date=self.today - datetime.timedelta(days=100),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.ACTIVE,
        )

        # 5. Purchase Cost Lots (FIFO Accounting)
        # Lot 1: Napa, 10 days old (Bucket 0-30), 100 units @ 2.0000 BDT
        self.lot_napa_fresh = PurchaseCostLot.objects.create(
            lot_id=f"lot-fresh-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_napa,
            batch=self.batch_napa,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("2.0000"),
            initial_quantity=100,
            remaining_quantity=100,
            status=PurchaseCostLotStatus.ACTIVE,
        )
        # Lot 2: Napa, 45 days old (Bucket 31-60), 50 units @ 2.2000 BDT
        self.lot_napa_normal = PurchaseCostLot.objects.create(
            lot_id=f"lot-norm-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_napa,
            batch=self.batch_napa,
            purchase_date=self.today - datetime.timedelta(days=45),
            unit_cost=Decimal("2.2000"),
            initial_quantity=50,
            remaining_quantity=50,
            status=PurchaseCostLotStatus.ACTIVE,
        )
        # Lot 3: Seclo, 100 days old (Bucket 91-180), 40 units @ 5.0000 BDT
        self.lot_seclo_slow = PurchaseCostLot.objects.create(
            lot_id=f"lot-slow-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product_seclo,
            batch=self.batch_seclo,
            purchase_date=self.today - datetime.timedelta(days=100),
            unit_cost=Decimal("5.0000"),
            initial_quantity=40,
            remaining_quantity=40,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # 6. Committed Sale Invoice with Immutable SaleLotAllocation
        self.invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            invoice_number=f"INV-VAL-{self.suffix}",
            customer_name="Rahim Uddin",
            customer_phone=f"+88017{random.randint(10000000, 99999999)}",
            total_amount=Decimal("60.00"),
            net_amount=Decimal("60.00"),
            status="FINALIZED",
        )
        self.invoice_item = SaleInvoiceItem.objects.create(
            invoice=self.invoice,
            batch=self.batch_napa,
            product=self.product_napa,
            quantity=20,
            unit_price=Decimal("3.00"),
            subtotal=Decimal("60.00"),
        )
        self.allocation = SaleLotAllocation.objects.create(
            allocation_id=f"sall-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            invoice=self.invoice,
            invoice_item=self.invoice_item,
            product=self.product_napa,
            batch=self.batch_napa,
            cost_lot=self.lot_napa_fresh,
            quantity_base_units=20,
            unit_selling_price=Decimal("3.0000"),
            unit_cost=Decimal("2.0000"),
            total_selling_price=Decimal("60.0000"),
            total_cost=Decimal("40.0000"),
            gross_profit=Decimal("20.0000"),
            profit_margin_pct=Decimal("33.33"),
            status=SaleLotAllocationStatus.COMMITTED,
        )

    def test_01_valid_fifo_actual_stock_valuation_and_margins(self):
        """
        Verify live stock valuation calculates exact FIFO acquisition cost,
        retail valuation, and potential gross margin.
        """
        self.client.force_authenticate(user=self.manager)
        url = f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}&branch_id={self.branch_a.branch_id}&costing_method=FIFO_ACTUAL"

        response = self.client.get(url)
        assert response.status_code == status.HTTP_200_OK

        data = response.data
        assert data["tenant_id"] == self.tenant.tenant_id
        assert data["branch_id"] == self.branch_a.branch_id
        assert data["costing_method"] == "FIFO_ACTUAL"
        assert data["total_products_count"] == 2
        # Total units = 100 + 50 + 40 = 190
        assert data["total_quantity_base_units"] == 190

        # Total Cost = (100*2.0000) + (50*2.2000) + (40*5.0000) = 200 + 110 + 200 = 510.0000 BDT
        assert Decimal(data["total_cost_valuation_bdt"]) == Decimal("510.0000")

        # Total Retail = (150*3.0000) + (40*7.0000) = 450 + 280 = 730.0000 BDT
        assert Decimal(data["total_retail_valuation_bdt"]) == Decimal("730.0000")

        # Potential Gross Profit = 730 - 510 = 220.0000 BDT
        assert Decimal(data["potential_gross_margin_bdt"]) == Decimal("220.0000")

        # Potential Margin % = (220 / 730) * 100 = 30.14%
        assert Decimal(data["potential_margin_pct"]) == Decimal("30.14")

    def test_02_five_tier_aging_analysis_breakdown(self):
        """
        Verify that inventory aging is correctly bucketed into standard 5 tiers:
        0-30, 31-60, 61-90, 91-180, 181+ days.
        """
        self.client.force_authenticate(user=self.manager)
        url = f"/api/v2/inventory/valuation/aging/?tenant_id={self.tenant.tenant_id}&branch_id={self.branch_a.branch_id}"

        response = self.client.get(url)
        assert response.status_code == status.HTTP_200_OK

        data = response.data
        aging = data["aging_summary"]

        # 0-30 Days: Lot fresh (100 units @ 2.0000 = 200.0000 BDT)
        assert aging["days_0_to_30"]["quantity_base_units"] == 100
        assert Decimal(aging["days_0_to_30"]["cost_valuation_bdt"]) == Decimal("200.0000")

        # 31-60 Days: Lot normal (50 units @ 2.2000 = 110.0000 BDT)
        assert aging["days_31_to_60"]["quantity_base_units"] == 50
        assert Decimal(aging["days_31_to_60"]["cost_valuation_bdt"]) == Decimal("110.0000")

        # 61-90 Days: 0 units
        assert aging["days_61_to_90"]["quantity_base_units"] == 0
        assert Decimal(aging["days_61_to_90"]["cost_valuation_bdt"]) == Decimal("0.0000")

        # 91-180 Days: Lot slow (40 units @ 5.0000 = 200.0000 BDT)
        assert aging["days_91_to_180"]["quantity_base_units"] == 40
        assert Decimal(aging["days_91_to_180"]["cost_valuation_bdt"]) == Decimal("200.0000")

        # 181+ Days: 0 units
        assert aging["days_181_plus"]["quantity_base_units"] == 0

    def test_03_weighted_average_costing_method(self):
        """
        Verify that requesting WEIGHTED_AVERAGE calculates inventory value
        based on active batch records.
        """
        self.client.force_authenticate(user=self.manager)
        url = f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}&branch_id={self.branch_a.branch_id}&costing_method=WEIGHTED_AVERAGE"

        response = self.client.get(url)
        assert response.status_code == status.HTTP_200_OK

        data = response.data
        assert data["costing_method"] == "WEIGHTED_AVERAGE"
        # Total units = 150 + 40 = 190
        assert data["total_quantity_base_units"] == 190
        # Napa cost = 150 * 2.07 = 310.5000 BDT, Seclo = 40 * 5.0000 = 200.0000 BDT -> Total = 510.5000
        assert Decimal(data["total_cost_valuation_bdt"]) == Decimal("510.5000")

    def test_04_realized_margin_from_immutable_sale_lot_allocations(self):
        """
        Verify realized revenue, COGS, and gross profit derived from SaleLotAllocations.
        """
        self.client.force_authenticate(user=self.manager)
        url = f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}&branch_id={self.branch_a.branch_id}"

        response = self.client.get(url)
        assert response.status_code == status.HTTP_200_OK

        realized = response.data["realized_margin"]
        assert realized["allocations_count"] == 1
        assert Decimal(realized["total_revenue_bdt"]) == Decimal("60.0000")
        assert Decimal(realized["total_cogs_bdt"]) == Decimal("40.0000")
        assert Decimal(realized["realized_gross_profit_bdt"]) == Decimal("20.0000")
        assert Decimal(realized["realized_margin_pct"]) == Decimal("33.33")

    def test_05_snapshot_creation_and_audit_log_persistence(self):
        """
        Verify creating an immutable point-in-time valuation snapshot persists
        to database and writes an audit log entry.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/valuation/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "costing_method": "FIFO_ACTUAL",
            "notes": "Quarterly balance sheet closure audit",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data
        snapshot_id = data["snapshot_id"]
        assert snapshot_id.startswith("valsnap-")
        assert Decimal(data["total_cost_valuation_bdt"]) == Decimal("510.0000")
        assert Decimal(data["total_retail_valuation_bdt"]) == Decimal("730.0000")

        # Verify database record
        snap = StockValuationSnapshot.objects.filter(snapshot_id=snapshot_id).first()
        assert snap is not None
        assert snap.total_quantity_base_units == 190

        # Verify audit log
        audit = AuditLogEntry.objects.filter(
            action="STOCK_VALUATION_SNAPSHOT_CREATED",
            resource_id=snapshot_id,
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

        # Retrieve by ID
        get_res = self.client.get(f"/api/v2/inventory/valuation/{snapshot_id}/")
        assert get_res.status_code == status.HTTP_200_OK
        assert get_res.data["snapshot_id"] == snapshot_id

    def test_06_snapshot_immutability_blocks_mutations(self):
        """
        Verify that snapshots cannot be modified or deleted (HTTP 405 Method Not Allowed).
        """
        self.client.force_authenticate(user=self.manager)
        snap = StockValuationSnapshot.objects.create(
            snapshot_id=f"valsnap-imm-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            valuation_date=self.today,
            total_cost_valuation_bdt=Decimal("510.0000"),
            total_retail_valuation_bdt=Decimal("730.0000"),
            potential_gross_margin_bdt=Decimal("220.0000"),
            potential_margin_pct=Decimal("30.14"),
        )

        url = f"/api/v2/inventory/valuation/{snap.snapshot_id}/"

        put_res = self.client.put(url, {"notes": "illegal edit"}, format="json")
        assert put_res.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        patch_res = self.client.patch(url, {"notes": "illegal patch"}, format="json")
        assert patch_res.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        del_res = self.client.delete(url)
        assert del_res.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_07_invalid_input_and_date_handling(self):
        """
        Verify that invalid costing method, reversed date ranges, and non-existent branch return errors.
        """
        self.client.force_authenticate(user=self.manager)

        # 1. Invalid Costing Method
        res1 = self.client.get(f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}&costing_method=LIFO_INVALID")
        assert res1.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        assert "ERR_INVALID_VALUATION_INPUT" in str(res1.data)

        # 2. Reversed Date Range (start > end)
        res2 = self.client.get(f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}&start_date=2026-12-31&end_date=2026-01-01")
        assert res2.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        assert "ERR_INVALID_VALUATION_INPUT" in str(res2.data)

        # 3. Non-existent branch
        res3 = self.client.get(f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}&branch_id=nonexistent-branch")
        assert res3.status_code in [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN]

    def test_08_cashier_role_strictly_forbidden(self):
        """
        Verify cashiers are barred from valuation calculations and snapshots (HTTP 403).
        """
        self.client.force_authenticate(user=self.cashier)

        # GET valuation
        res_get = self.client.get(f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}")
        assert res_get.status_code == status.HTTP_403_FORBIDDEN
        data = res_get.data if hasattr(res_get, "data") else res_get.json()
        assert data.get("error_code") == "ERR_PERMISSION_DENIED"

        # POST snapshot
        res_post = self.client.post("/api/v2/inventory/valuation/", {"tenant_id": self.tenant.tenant_id, "branch_id": self.branch_a.branch_id}, format="json")
        assert res_post.status_code == status.HTTP_403_FORBIDDEN
        data_post = res_post.data if hasattr(res_post, "data") else res_post.json()
        assert data_post.get("error_code") == "ERR_PERMISSION_DENIED"

    def test_09_cross_tenant_access_barred(self):
        """
        Verify manager from foreign tenant cannot inspect valuation metrics of another tenant.
        """
        self.client.force_authenticate(user=self.foreign_manager)
        url = f"/api/v2/inventory/valuation/?tenant_id={self.tenant.tenant_id}"

        response = self.client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION", "ERR_PERMISSION_DENIED"]

    def test_10_suspended_tenant_subscription_locked(self):
        """
        Verify valuation operations on suspended tenant are blocked (HTTP 403 ERR_TENANT_SUSPENDED).
        """
        self.client.force_authenticate(user=self.suspended_manager)
        url = f"/api/v2/inventory/valuation/?tenant_id={self.suspended_tenant.tenant_id}"

        response = self.client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_SUSPENDED", "ERR_PERMISSION_DENIED"]
        assert "ERR_TENANT_SUSPENDED" in str(data)
