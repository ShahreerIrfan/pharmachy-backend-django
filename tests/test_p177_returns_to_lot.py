"""
backend/tests/test_p177_returns_to_lot.py
Architecture Decision: ADR-0177 (P177).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P177 Returns to Lot with Safety Inspections & Quarantine Routing:
1. Pharmacist safety inspection and restock restores exact batch and cost lot balances.
2. Quarantine hold for uncertain restock does NOT increment dispensable physical availability.
3. Damaged/destroyed disposition writes off inventory without stock increment.
4. Attempting to return more than dispensed base units is rejected with ERR_RETURN_EXCEEDS_DISPENSED.
5. Failed checklist items block restock disposition with ERR_FAILED_SAFETY_INSPECTION_CANNOT_RESTOCK.
6. Attempting to restock into an expired batch is barred with ERR_BATCH_EXPIRED_NO_RESTOCK.
7. Reversal safely undoes stock restoration and audit metadata.
8. Double reversal attempt is rejected with ERR_ALREADY_REVERSED.
9. Cashier role attempting to approve restock is barred with HTTP 403 ERR_PERMISSION_DENIED.
10. Cross-branch and cross-tenant returns are strictly blocked.
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
    BranchStockAvailability,
    MedicineProduct,
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
class TestReturnsToLotIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.unique_id = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Two Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p177-{self.unique_id}",
            name="Popular Health Pharmacy",
            subdomain=f"popular-p177-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.unique_id}",
            tenant=self.tenant,
            name="Dhanmondi Branch A",
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.unique_id}",
            tenant=self.tenant,
            name="Gulshan Branch B",
        )

        # Foreign Tenant
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.unique_id}",
            name="Foreign Healthcare Ltd.",
            subdomain=f"foreign-p177-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # 2. Setup Users
        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.unique_id}@pharmacy.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Registered Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.manager = PharmacyUser.objects.create_user(
            email=f"manager_{self.unique_id}@pharmacy.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.unique_id}@pharmacy.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Cashier Staff",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 3. Setup Product
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-seclo-{self.unique_id}",
            brand_name="Seclo 20mg Capsule",
            generic_name="Omeprazole",
            dosage_form="Capsule",
            strength="20mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("7.0000"),
            cost_unit=Decimal("5.0000"),
        )

        # 4. Setup Batches & Cost Lots
        # Batch 1: Valid active batch
        self.batch_valid = BatchStock.objects.create(
            batch_id=f"bat-val-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-2026-VAL-{self.unique_id}",
            quantity_base_units=90,  # 10 was sold
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=180),
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_valid = PurchaseCostLot.objects.create(
            lot_id=f"lot-val-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_valid,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("5.0000"),
            initial_quantity=100,
            remaining_quantity=90,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # Batch 2: Expired batch
        self.batch_expired = BatchStock.objects.create(
            batch_id=f"bat-exp-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-2026-EXP-{self.unique_id}",
            quantity_base_units=40,
            reserved_quantity_base_units=0,
            expiry_date=self.today - datetime.timedelta(days=1),  # Expired yesterday
            purchase_date=self.today - datetime.timedelta(days=120),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.EXPIRED,
        )
        self.lot_expired = PurchaseCostLot.objects.create(
            lot_id=f"lot-exp-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_expired,
            purchase_date=self.today - datetime.timedelta(days=120),
            unit_cost=Decimal("5.0000"),
            initial_quantity=50,
            remaining_quantity=40,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # 5. Setup BranchStockAvailability
        self.availability = BranchStockAvailability.objects.create(
            availability_id=f"avail-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            quantity_on_hand=90,
            quantity_reserved=0,
            quantity_quarantined=0,
            quantity_pending_inward=0,
            quantity_physically_available=90,
            quantity_server_confirmed_available=90,
        )

        # 6. Setup Sale Invoices & SaleLotAllocations
        self.invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            invoice_number=f"INV-2026-{self.unique_id}",
            customer_name="Rahim Uddin",
            customer_phone="+8801711223344",
            total_amount=Decimal("70.00"),
            tax_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            net_amount=Decimal("70.00"),
        )
        self.alloc_valid = SaleLotAllocation.objects.create(
            allocation_id=f"sall-val-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_valid,
            cost_lot=self.lot_valid,
            quantity_base_units=10,
            returned_quantity_base_units=0,
            unit_selling_price=Decimal("7.0000"),
            unit_cost=Decimal("5.0000"),
            total_selling_price=Decimal("70.0000"),
            total_cost=Decimal("50.0000"),
            gross_profit=Decimal("20.0000"),
            profit_margin_pct=Decimal("28.57"),
            status=SaleLotAllocationStatus.COMMITTED,
        )

        self.alloc_expired = SaleLotAllocation.objects.create(
            allocation_id=f"sall-exp-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            invoice=self.invoice,
            product=self.product,
            batch=self.batch_expired,
            cost_lot=self.lot_expired,
            quantity_base_units=10,
            returned_quantity_base_units=0,
            unit_selling_price=Decimal("7.0000"),
            unit_cost=Decimal("5.0000"),
            total_selling_price=Decimal("70.0000"),
            total_cost=Decimal("50.0000"),
            gross_profit=Decimal("20.0000"),
            profit_margin_pct=Decimal("28.57"),
            status=SaleLotAllocationStatus.COMMITTED,
        )

    def test_01_pharmacist_inspection_and_restock_success(self):
        """
        Verify that a registered pharmacist successfully inspects returned medicines,
        confirms safety checklist, approves restock, and restores exact batch and lot balances.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 4,
            "disposition": "RESTOCK_APPROVED",
            "is_seal_intact": True,
            "is_packaging_undamaged": True,
            "is_storage_condition_verified": True,
            "is_unexpired": True,
            "customer_reason": "Purchased excess strip",
            "inspection_notes": "Blister packaging pristine and seals intact",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data
        assert data["disposition"] == "RESTOCK_APPROVED"
        assert data["returned_quantity_base_units"] == 4
        assert Decimal(str(data["refund_amount"])) == Decimal("28.0000")  # 4 * 7.00
        assert Decimal(str(data["restocked_cogs"])) == Decimal("20.0000")  # 4 * 5.00

        # Refresh database state
        self.batch_valid.refresh_from_db()
        self.lot_valid.refresh_from_db()
        self.availability.refresh_from_db()
        self.alloc_valid.refresh_from_db()

        # Stock balances restored by exactly 4 units
        assert self.batch_valid.quantity_base_units == 94  # 90 + 4
        assert self.lot_valid.remaining_quantity == 94  # 90 + 4
        assert self.availability.quantity_on_hand == 94
        assert self.availability.quantity_physically_available == 94
        assert self.availability.quantity_server_confirmed_available == 94

        # Allocation updated
        assert self.alloc_valid.returned_quantity_base_units == 4
        assert self.alloc_valid.status == SaleLotAllocationStatus.PARTIALLY_RETURNED

        # Audit log entry exists
        audit = AuditLogEntry.objects.filter(
            action="RETURN_SAFETY_INSPECTED",
            resource_id=data["inspection_id"],
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

    def test_02_quarantine_hold_does_not_increase_physically_available_stock(self):
        """
        Verify that uncertain restock status placed on QUARANTINE_HOLD isolates stock
        in quarantine and does NOT increment dispensable physical availability.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 3,
            "disposition": "QUARANTINE_HOLD",
            "is_seal_intact": False,  # Customer opened box
            "is_packaging_undamaged": True,
            "is_storage_condition_verified": False,  # Ambient storage unknown
            "is_unexpired": True,
            "customer_reason": "Box was unsealed at home",
            "inspection_notes": "Foil appears intact but outer seal broken. Hold for QA review.",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        # Refresh database state
        self.batch_valid.refresh_from_db()
        self.lot_valid.refresh_from_db()
        self.availability.refresh_from_db()

        # Invariant: Batch and Lot active dispensable stock are NOT incremented!
        assert self.batch_valid.quantity_base_units == 90
        assert self.lot_valid.remaining_quantity == 90

        # Availability: Quarantined stock increases, physically available remains strictly 90!
        assert self.availability.quantity_on_hand == 93  # 90 + 3
        assert self.availability.quantity_quarantined == 3
        assert self.availability.quantity_physically_available == 90  # 93 - 3 = 90!

    def test_03_damaged_destroy_disposition_discards_inventory(self):
        """
        Verify that damaged/spoiled returns write off the item without restocking.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 2,
            "disposition": "DAMAGED_DESTROY",
            "is_seal_intact": False,
            "is_packaging_undamaged": False,
            "customer_reason": "Fell in water and ruined",
            "inspection_notes": "Blister crushed and wet. Disposed.",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        self.batch_valid.refresh_from_db()
        self.availability.refresh_from_db()

        assert self.batch_valid.quantity_base_units == 90
        assert self.availability.quantity_physically_available == 90

    def test_04_cannot_return_more_quantity_than_dispensed(self):
        """
        Verify that attempting to return more than dispensed units is rejected with HTTP 422.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 15,  # Only 10 were sold!
            "disposition": "RESTOCK_APPROVED",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_RETURN_EXCEEDS_DISPENSED" in str(response.data)

    def test_05_failed_checklist_blocks_restock_disposition(self):
        """
        Verify that attempting RESTOCK_APPROVED with a failed safety checklist item is barred.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 2,
            "disposition": "RESTOCK_APPROVED",
            "is_seal_intact": False,  # Broken seal!
            "is_packaging_undamaged": True,
            "is_storage_condition_verified": True,
            "is_unexpired": True,
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_FAILED_SAFETY_INSPECTION_CANNOT_RESTOCK" in str(response.data)

    def test_06_expired_batch_restock_blocked(self):
        """
        Verify that returning an item whose batch expired is hard-blocked from active shelf restock.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_expired.allocation_id,
            "returned_quantity_base_units": 2,
            "disposition": "RESTOCK_APPROVED",
            "is_seal_intact": True,
            "is_packaging_undamaged": True,
            "is_storage_condition_verified": True,
            "is_unexpired": True,
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_BATCH_EXPIRED_NO_RESTOCK" in str(response.data)

    def test_07_reversal_restores_original_stock_balances(self):
        """
        Verify that manager reversal safely undoes stock restoration and updates audit state.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        # 1. Inspect and restock 4 units
        res = self.client.post(
            url,
            {
                "tenant_id": self.tenant.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "allocation_id": self.alloc_valid.allocation_id,
                "returned_quantity_base_units": 4,
                "disposition": "RESTOCK_APPROVED",
            },
            format="json",
        )
        assert res.status_code == status.HTTP_201_CREATED
        insp_id = res.data["inspection_id"]

        self.batch_valid.refresh_from_db()
        assert self.batch_valid.quantity_base_units == 94

        # 2. Manager reverses the return inspection
        self.client.force_authenticate(user=self.manager)
        rev_url = f"/api/v2/inventory/return-inspections/{insp_id}/reverse/"
        rev_res = self.client.post(
            rev_url,
            {"reason": "Secondary QA audit revealed foil micro-perforations"},
            format="json",
        )
        assert rev_res.status_code == status.HTTP_200_OK
        assert rev_res.data["is_reversed"] is True

        # 3. Check stock decremented back
        self.batch_valid.refresh_from_db()
        self.lot_valid.refresh_from_db()
        self.availability.refresh_from_db()
        self.alloc_valid.refresh_from_db()

        assert self.batch_valid.quantity_base_units == 90
        assert self.lot_valid.remaining_quantity == 90
        assert self.availability.quantity_physically_available == 90
        assert self.alloc_valid.returned_quantity_base_units == 0
        assert self.alloc_valid.status == SaleLotAllocationStatus.COMMITTED

    def test_08_double_reversal_prohibited(self):
        """
        Verify that a second reversal attempt on an already reversed return is blocked.
        """
        self.client.force_authenticate(user=self.pharmacist)
        res = self.client.post(
            "/api/v2/inventory/return-inspections/",
            {
                "tenant_id": self.tenant.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "allocation_id": self.alloc_valid.allocation_id,
                "returned_quantity_base_units": 2,
                "disposition": "RESTOCK_APPROVED",
            },
            format="json",
        )
        insp_id = res.data["inspection_id"]

        self.client.force_authenticate(user=self.manager)
        rev_url = f"/api/v2/inventory/return-inspections/{insp_id}/reverse/"
        rev1 = self.client.post(rev_url, {"reason": "Initial correct reversal"}, format="json")
        assert rev1.status_code == status.HTTP_200_OK

        rev2 = self.client.post(rev_url, {"reason": "Second reversal attempt"}, format="json")
        assert rev2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_ALREADY_REVERSED" in str(rev2.data)

    def test_09_cashier_cannot_perform_safety_inspection(self):
        """
        Verify that cashiers cannot approve safety inspections without a registered pharmacist (HTTP 403).
        """
        self.client.force_authenticate(user=self.cashier)
        url = "/api/v2/inventory/return-inspections/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 2,
            "disposition": "RESTOCK_APPROVED",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") == "ERR_PERMISSION_DENIED"

    def test_10_cross_branch_and_cross_tenant_access_blocked(self):
        """
        Verify that cross-branch returns to wrong batches and cross-tenant actions are blocked.
        """
        self.client.force_authenticate(user=self.pharmacist)
        url = "/api/v2/inventory/return-inspections/"

        # 1. Cross-branch return: alloc was in branch_a, but request specifies branch_b
        payload_cross_branch = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_b.branch_id,  # Wrong branch!
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 2,
            "disposition": "RESTOCK_APPROVED",
        }
        res_branch = self.client.post(url, payload_cross_branch, format="json")
        assert res_branch.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_CROSS_BRANCH_RETURN" in str(res_branch.data)

        # 2. Cross-tenant return
        payload_cross_tenant = {
            "tenant_id": self.foreign_tenant.tenant_id,  # Foreign tenant!
            "branch_id": self.branch_a.branch_id,
            "allocation_id": self.alloc_valid.allocation_id,
            "returned_quantity_base_units": 2,
            "disposition": "RESTOCK_APPROVED",
        }
        res_tenant = self.client.post(url, payload_cross_tenant, format="json")
        assert res_tenant.status_code == status.HTTP_403_FORBIDDEN
        data_tenant = res_tenant.data if hasattr(res_tenant, "data") else res_tenant.json()
        assert data_tenant.get("error_code") in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION", "ERR_PERMISSION_DENIED"]

