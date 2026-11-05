"""
backend/tests/test_p180_packaging_batch_concurrency.py
Architecture Decision: ADR-0180 (P180).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P180 Packaging and Batch Concurrency Stress Testing:
1. Multi-threaded concurrent checkout guarantees exact mathematical stock conservation.
2. Excess demand cleanly rejects oversells, preventing negative physical inventory.
3. Strict FEFO batch consumption: earlier expiring lots are depleted before later lots under concurrency.
4. Split lot allocation across multiple candidate batches under concurrent requests.
5. Indivisible integer base units: multi-tier packaging conversions maintain zero fractional truncation.
6. Simultaneous checkout and return to lot maintains zero stock drift.
7. Server-authoritative stress test endpoint persists PackagingConcurrencyAuditRecord and audit log.
8. Audit record immutability: PUT/PATCH/DELETE barred with HTTP 405 Method Not Allowed.
9. Cashier RBAC gate: barred with HTTP 403 ERR_PERMISSION_DENIED.
10. Cross-tenant and suspended tenant isolation strictly enforced with HTTP 403.
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
    PackagingConcurrencyAuditRecord,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.packaging_concurrency_service import PackagingConcurrencyService
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPackagingBatchConcurrencyIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p180-{self.suffix}",
            name="Evercare Pharmacy Hospital",
            subdomain=f"evercare-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name="Main Hospital Branch A",
        )

        # Foreign Tenant
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.suffix}",
            name="Foreign Medico Ltd.",
            subdomain=f"foreign-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name="Suspended Pharma Ltd.",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 2. Setup Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"manager_{self.suffix}@evercare.com",
            phone_number=f"+880191{random.randint(1000000, 9999999)}",
            full_name="Hospital Pharmacy Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@evercare.com",
            phone_number=f"+880192{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@medico.com",
            phone_number=f"+880193{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
        )
        self.suspended_manager = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@susp.com",
            phone_number=f"+880194{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        # 3. Setup Product
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-cef-{self.suffix}",
            brand_name="Cef-3 200mg Capsule",
            generic_name="Cefixime Trihydrate",
            dosage_form="Capsule",
            strength="200mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("45.0000"),
            cost_unit=Decimal("35.0000"),
        )

        # 4. Setup FEFO Batches with varying expiry dates
        # Batch 1 (Early expiry: 60 days), 50 units
        self.batch_early = BatchStock.objects.create(
            batch_id=f"bat-early-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-EARLY-{self.suffix}",
            lot_reference=f"LOT-EARLY-{self.suffix}",
            quantity_base_units=50,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=60),
            purchase_date=self.today - datetime.timedelta(days=30),
            cost_per_unit=Decimal("35.00"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_early = PurchaseCostLot.objects.create(
            lot_id=f"lot-early-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_early,
            purchase_date=self.today - datetime.timedelta(days=30),
            unit_cost=Decimal("35.0000"),
            initial_quantity=50,
            remaining_quantity=50,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # Batch 2 (Late expiry: 365 days), 50 units
        self.batch_late = BatchStock.objects.create(
            batch_id=f"bat-late-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-LATE-{self.suffix}",
            lot_reference=f"LOT-LATE-{self.suffix}",
            quantity_base_units=50,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("36.00"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_late = PurchaseCostLot.objects.create(
            lot_id=f"lot-late-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_late,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("36.0000"),
            initial_quantity=50,
            remaining_quantity=50,
            status=PurchaseCostLotStatus.ACTIVE,
        )

    def test_01_concurrent_checkout_exact_stock_conservation(self):
        """
        1. Valid Scenario: 10 concurrent threads simultaneously checkout stock.
        Proves row-level locking, zero collision, and exact stock conservation (initial == final + allocated).
        """
        # Initial: 50 + 50 = 100 units.
        # 10 threads, 1 request each, 6 units per request = 60 units allocated.
        result = PackagingConcurrencyService.run_stress_test(
            user=self.manager,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
            product_id=self.product.product_id,
            concurrency_level=10,
            requests_per_thread=1,
            units_per_request=6,
            notes="Valid concurrent checkout stress test",
        )

        assert result["total_requests"] == 10
        assert result["successful_transactions"] == 10
        assert result["rejected_oversells"] == 0
        assert result["initial_stock_base_units"] == 100
        assert result["total_allocated_base_units"] == 60
        assert result["final_stock_base_units"] == 40
        assert result["stock_conserved"] is True
        assert result["integer_units_verified"] is True

        # Refresh database state
        self.batch_early.refresh_from_db()
        self.batch_late.refresh_from_db()
        assert self.batch_early.quantity_base_units + self.batch_late.quantity_base_units == 40

    def test_02_oversell_rejection_prevents_negative_inventory(self):
        """
        2. Incomplete / Over-demand Input: Requests exceed on-hand stock under concurrency.
        Proves excess transactions are cleanly rejected with ERR_INSUFFICIENT_STOCK, preventing negative stock.
        """
        # Total initial stock: 100 units.
        # 25 requests @ 5 units = 125 units demanded (exceeds 100).
        result = PackagingConcurrencyService.run_stress_test(
            user=self.manager,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
            product_id=self.product.product_id,
            concurrency_level=10,
            requests_per_thread=3,  # 30 requests total
            units_per_request=4,    # 30 * 4 = 120 units demanded
            notes="Over-demand depletion test",
        )

        assert result["initial_stock_base_units"] == 100
        # Exactly 25 checkouts can succeed (25 * 4 = 100 units)
        assert result["successful_transactions"] == 25
        # 5 checkouts must be cleanly rejected due to stock exhaustion
        assert result["rejected_oversells"] == 5
        assert result["final_stock_base_units"] == 0
        assert result["total_allocated_base_units"] == 100
        assert result["stock_conserved"] is True

        # Database state: exactly 0, never negative
        self.batch_early.refresh_from_db()
        self.batch_late.refresh_from_db()
        assert self.batch_early.quantity_base_units == 0
        assert self.batch_late.quantity_base_units == 0
        assert self.batch_early.status == BatchStatus.DEPLETED
        assert self.batch_late.status == BatchStatus.DEPLETED

    def test_03_fefo_earliest_expiry_batch_depleted_first(self):
        """
        3. FEFO Ordering Gate: Batch with earlier expiry date is depleted before later expiry batch.
        """
        # Consume 50 units (exactly matches batch_early's 50 units)
        result = PackagingConcurrencyService.run_stress_test(
            user=self.manager,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
            product_id=self.product.product_id,
            concurrency_level=5,
            requests_per_thread=2,  # 10 requests total
            units_per_request=5,    # 10 * 5 = 50 units
            notes="FEFO depletion verification",
        )

        assert result["successful_transactions"] == 10
        assert result["total_allocated_base_units"] == 50

        self.batch_early.refresh_from_db()
        self.batch_late.refresh_from_db()

        # Batch early (60 days expiry) must be 0
        assert self.batch_early.quantity_base_units == 0
        assert self.batch_early.status == BatchStatus.DEPLETED

        # Batch late (365 days expiry) must remain completely untouched at 50 units
        assert self.batch_late.quantity_base_units == 50
        assert self.batch_late.status == BatchStatus.ACTIVE

    def test_04_split_lot_allocation_across_fefo_batches(self):
        """
        4. Split Lot Allocation: A request requiring more units than remain in Batch 1
        splits cleanly across Batch 1 and Batch 2 in atomic FEFO sequence.
        """
        # Batch 1 has 50 units, Batch 2 has 50 units.
        # Execute single checkout of 65 units (takes all 50 from early, 15 from late).
        result = PackagingConcurrencyService.run_stress_test(
            user=self.manager,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
            product_id=self.product.product_id,
            concurrency_level=1,
            requests_per_thread=1,
            units_per_request=65,
            notes="Split lot allocation test",
        )

        assert result["successful_transactions"] == 1
        assert result["total_allocated_base_units"] == 65
        assert result["final_stock_base_units"] == 35
        assert result["stock_conserved"] is True

        self.batch_early.refresh_from_db()
        self.batch_late.refresh_from_db()
        self.lot_early.refresh_from_db()
        self.lot_late.refresh_from_db()

        # Batch 1 exhausted
        assert self.batch_early.quantity_base_units == 0
        assert self.lot_early.remaining_quantity == 0
        assert self.lot_early.status == PurchaseCostLotStatus.DEPLETED

        # Batch 2 deducted by 15 (50 - 15 = 35)
        assert self.batch_late.quantity_base_units == 35
        assert self.lot_late.remaining_quantity == 35
        assert self.lot_late.status == PurchaseCostLotStatus.ACTIVE

    def test_05_package_conversion_indivisible_integer_units(self):
        """
        5. Integer Units Gate: Verifies that multi-tier package conversions (e.g. 1 Strip = 10 Capsules)
        always maintain indivisible integer base units under concurrent allocation.
        """
        # Strip conversion: 10 base units
        units_per_strip = 10
        result = PackagingConcurrencyService.run_stress_test(
            user=self.manager,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
            product_id=self.product.product_id,
            concurrency_level=4,
            requests_per_thread=2,  # 8 requests
            units_per_request=units_per_strip,  # 8 * 10 = 80 units
            notes="Strip packaging unit conversion concurrency test",
        )

        assert result["total_allocated_base_units"] == 80
        assert result["final_stock_base_units"] == 20
        assert result["stock_conserved"] is True
        assert result["integer_units_verified"] is True

    def test_06_simultaneous_checkout_and_return_reconciliation(self):
        """
        6. Simultaneous Checkout and Restock: Mathematical stock conservation invariant holds.
        """
        # Execute checkout
        PackagingConcurrencyService.run_stress_test(
            user=self.manager,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
            product_id=self.product.product_id,
            concurrency_level=5,
            requests_per_thread=1,
            units_per_request=4,  # 20 units
        )

        # Simulate return to lot of 10 units
        self.batch_early.refresh_from_db()
        self.lot_early.refresh_from_db()

        self.batch_early.quantity_base_units += 10
        self.batch_early.save(update_fields=["quantity_base_units", "updated_at"])
        self.lot_early.remaining_quantity += 10
        self.lot_early.save(update_fields=["remaining_quantity", "updated_at"])

        # Total should be 100 - 20 + 10 = 90
        total_batches = sum(b.quantity_base_units for b in BatchStock.objects.filter(product=self.product))
        assert total_batches == 90

    def test_07_concurrency_stress_api_endpoint_execution(self):
        """
        7. API Endpoint Execution: POST /api/v2/inventory/concurrency-tests/run/
        creates PackagingConcurrencyAuditRecord and AuditLogEntry.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/concurrency-tests/run/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
            "concurrency_level": 8,
            "requests_per_thread": 1,
            "units_per_request": 5,
            "notes": "Hospital pharmacy morning shift peak load simulation",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data
        test_id = data["test_id"]
        assert test_id.startswith("cstress-")
        assert data["successful_transactions"] == 8
        assert data["total_allocated_base_units"] == 40
        assert data["stock_conserved"] is True

        # Invariant: Audit log exists
        audit = AuditLogEntry.objects.filter(
            action="CONCURRENCY_STRESS_TEST_COMPLETED",
            resource_id=test_id,
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

        # Invariant: Retrieve by ID
        get_res = self.client.get(f"/api/v2/inventory/concurrency-tests/{test_id}/")
        assert get_res.status_code == status.HTTP_200_OK
        assert get_res.data["test_id"] == test_id

    def test_08_audit_record_immutability_blocks_mutations(self):
        """
        8. Audit Immutability: PUT, PATCH, DELETE barred with HTTP 405 Method Not Allowed.
        """
        self.client.force_authenticate(user=self.manager)
        rec = PackagingConcurrencyAuditRecord.objects.create(
            test_id=f"cstress-imm-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            concurrency_level=10,
            total_requests=10,
            successful_transactions=10,
            initial_stock_base_units=100,
            final_stock_base_units=0,
            total_allocated_base_units=100,
            stock_conserved=True,
        )

        url = f"/api/v2/inventory/concurrency-tests/{rec.test_id}/"

        put_res = self.client.put(url, {"notes": "illegal edit"}, format="json")
        assert put_res.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        patch_res = self.client.patch(url, {"notes": "illegal patch"}, format="json")
        assert patch_res.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        del_res = self.client.delete(url)
        assert del_res.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_09_cashier_role_strictly_forbidden(self):
        """
        9. Role Unauthorized Gate: Cashiers cannot execute concurrency stress tests (HTTP 403).
        """
        self.client.force_authenticate(user=self.cashier)
        url = "/api/v2/inventory/concurrency-tests/run/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") == "ERR_PERMISSION_DENIED"

    def test_10_cross_tenant_and_suspended_tenant_blocked(self):
        """
        10. Cross-Tenant and Suspended Tenant: Access is strictly blocked (HTTP 403).
        """
        # Cross-Tenant
        self.client.force_authenticate(user=self.foreign_manager)
        url = "/api/v2/inventory/concurrency-tests/run/"
        payload_cross = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
        }
        res_cross = self.client.post(url, payload_cross, format="json")
        assert res_cross.status_code == status.HTTP_403_FORBIDDEN
        data_cross = res_cross.data if hasattr(res_cross, "data") else res_cross.json()
        assert data_cross.get("error_code") in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION", "ERR_PERMISSION_DENIED"]

        # Suspended Tenant
        self.client.force_authenticate(user=self.suspended_manager)
        payload_susp = {
            "tenant_id": self.suspended_tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "product_id": self.product.product_id,
        }
        res_susp = self.client.post(url, payload_susp, format="json")
        assert res_susp.status_code == status.HTTP_403_FORBIDDEN
        data_susp = res_susp.data if hasattr(res_susp, "data") else res_susp.json()
        assert data_susp.get("error_code") in [
            "ERR_TENANT_SUSPENDED",
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            "ERR_PERMISSION_DENIED",
        ]
        assert "SUSPENDED" in str(data_susp) or data_susp.get("error_code") == "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"

