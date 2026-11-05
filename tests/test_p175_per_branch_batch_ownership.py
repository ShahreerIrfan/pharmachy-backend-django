"""
backend/tests/test_p175_per_branch_batch_ownership.py
Architecture Decision: ADR-0175 (P175).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P175 Per-Branch Batch Ownership & Isolation:
1. Cashier queries are strictly scoped to their assigned physical branch.
2. Cross-branch query attempt by cashier/pharmacist triggers HTTP 403 ERR_CROSS_BRANCH_ACCESS.
3. Independent per-branch identity: identical manufacturer batch numbers in Branch A and B hold separate balances.
4. POS checkout pre-flight validation succeeds for local branch items.
5. Cross-branch dispense attempt is barred with HTTP 422 ERR_CROSS_BRANCH_DISPENSE_BLOCKED.
6. Model-level constraint: PurchaseCostLot branch must match BatchStock branch.
7. Multi-branch owner view returns segregated branch breakdowns without stock mixing.
8. Audited inter-branch stock transfer atomically moves inventory and updates dual-branch availability.
9. Transfer exceeding unreserved branch stock is rejected with ERR_INSUFFICIENT_UNRESERVED_STOCK.
10. Cross-tenant isolation boundaries and suspended tenant lockouts.
"""

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
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
    PurchaseCostLotStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestPerBranchBatchOwnershipIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.unique_id = uuid.uuid4().hex[:6]

        # 1. Setup Tenant and Two Distinct Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p175-{self.unique_id}",
            name="Apex Healthcare Ltd.",
            subdomain=f"apex-p175-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.unique_id}",
            tenant=self.tenant,
            name="Banani Branch A",
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.unique_id}",
            tenant=self.tenant,
            name="Dhanmondi Branch B",
        )

        # 2. Setup Users
        # Cashier assigned to Branch A
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.unique_id}@pharmacy.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Cashier Banani",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        # Cashier assigned to Branch B
        self.cashier_b = PharmacyUser.objects.create_user(
            email=f"cashier_b_{self.unique_id}@pharmacy.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier Dhanmondi",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        # Pharmacy Owner (Multi-Branch access)
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.unique_id}@pharmacy.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 3. Setup Medicine Product & Packaging Base Unit Strategy
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-ace-{self.unique_id}",
            brand_name="Ace Plus 500mg/65mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("3.5000"),
            cost_unit=Decimal("2.4000"),
        )
        # Packaging Strategy in Branch A & B
        for br in (self.branch_a, self.branch_b):
            InventoryBaseUnitStrategy.objects.create(
                strategy_id=f"strat-{br.branch_id}",
                tenant=self.tenant,
                branch=br,
                product=self.product,
                base_unit="TABLET",
                pack_unit_label="BOX",
                intermediate_unit_label="STRIP",
                pack_to_intermediate_multiplier=10,
                intermediate_to_base_multiplier=10,
                total_base_units_per_pack=100,
                is_active=True,
            )

        # 4. Setup Batches
        # Batch A: In Branch A with 100 physical, 20 reserved -> 80 available
        self.batch_a = BatchStock.objects.create(
            batch_id=f"bat-a-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number="BAT-COMMON-2026",
            quantity_base_units=100,
            reserved_quantity_base_units=20,
            expiry_date=self.today + datetime.timedelta(days=180),
            purchase_date=self.today - datetime.timedelta(days=20),
            cost_per_unit=Decimal("2.40"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_a = PurchaseCostLot.objects.create(
            lot_id=f"lot-a-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch=self.batch_a,
            purchase_date=self.today - datetime.timedelta(days=20),
            unit_cost=Decimal("2.4000"),
            initial_quantity=100,
            remaining_quantity=100,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # Batch B: In Branch B with identical batch_number "BAT-COMMON-2026", 150 physical, 0 reserved
        self.batch_b = BatchStock.objects.create(
            batch_id=f"bat-b-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_b,
            product=self.product,
            batch_number="BAT-COMMON-2026",
            quantity_base_units=150,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=180),
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("2.40"),
            status=BatchStatus.ACTIVE,
        )
        self.lot_b = PurchaseCostLot.objects.create(
            lot_id=f"lot-b-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_b,
            product=self.product,
            batch=self.batch_b,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("2.4000"),
            initial_quantity=150,
            remaining_quantity=150,
            status=PurchaseCostLotStatus.ACTIVE,
        )

    def test_01_cashier_scoped_to_assigned_branch_and_batches(self):
        """Cashier A queries branch-batches without query param; receives ONLY Branch A batches."""
        self.client.force_authenticate(user=self.cashier_a)

        res = self.client.get(
            f"/api/v2/inventory/branch-batches/?tenant_id={self.tenant.tenant_id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.data

        data = res.data
        assert len(data) == 1
        assert data[0]["batch_id"] == self.batch_a.batch_id
        assert data[0]["branch_id"] == self.branch_a.branch_id
        assert data[0]["quantity_base_units"] == 100
        assert data[0]["available_quantity_base_units"] == 80

    def test_02_cashier_cross_branch_query_forbidden(self):
        """Cashier A explicitly requesting Branch B batches gets HTTP 403 ERR_CROSS_BRANCH_ACCESS."""
        self.client.force_authenticate(user=self.cashier_a)

        res = self.client.get(
            f"/api/v2/inventory/branch-batches/?tenant_id={self.tenant.tenant_id}&branch_id={self.branch_b.branch_id}"
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_CROSS_BRANCH_ACCESS" in str(res.data)

        # Verify audit log entry
        audit_entry = AuditLogEntry.objects.filter(
            action="CROSS_BRANCH_ACCESS_BLOCKED",
            tenant_id=self.tenant.tenant_id,
        ).first()
        assert audit_entry is not None

    def test_03_same_manufacturer_batch_in_different_branches_independent_identity(self):
        """Identical manufacturer batch number exists in Branch A and B as distinct DB records."""
        assert self.batch_a.batch_number == self.batch_b.batch_number
        assert self.batch_a.batch_id != self.batch_b.batch_id
        assert self.batch_a.branch_id == self.branch_a.branch_id
        assert self.batch_b.branch_id == self.branch_b.branch_id

        # Verify unique_together enforcement in same branch
        with pytest.raises(Exception):
            BatchStock.objects.create(
                batch_id=f"bat-dup-{self.unique_id}",
                tenant=self.tenant,
                branch=self.branch_a,
                product=self.product,
                batch_number="BAT-COMMON-2026",  # Duplicate in Branch A
                quantity_base_units=10,
                expiry_date=self.today + datetime.timedelta(days=100),
                purchase_date=self.today,
                cost_per_unit=Decimal("2.40"),
            )

    def test_04_validate_dispense_succeeds_for_local_branch(self):
        """POS checkout pre-flight check validates local branch batches successfully."""
        self.client.force_authenticate(user=self.cashier_a)

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "items": [{"batch_id": self.batch_a.batch_id, "quantity": 10}],
        }
        res = self.client.post("/api/v2/inventory/branch-batches/validate-dispense/", payload, format="json")
        assert res.status_code == status.HTTP_200_OK, res.data
        assert res.data["valid"] is True
        assert res.data["items"][0]["branch_id"] == self.branch_a.branch_id

    def test_05_cross_branch_dispense_blocked(self):
        """POS checkout in Branch A including Branch B batch is blocked (422 Unprocessable)."""
        self.client.force_authenticate(user=self.cashier_a)

        # Attempt to checkout Branch B's batch from Branch A counter
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "items": [{"batch_id": self.batch_b.batch_id, "quantity": 10}],
        }
        res = self.client.post("/api/v2/inventory/branch-batches/validate-dispense/", payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_CROSS_BRANCH_DISPENSE_BLOCKED" in str(res.data)

        # Audit entry
        audit_entry = AuditLogEntry.objects.filter(
            action="CROSS_BRANCH_DISPENSE_BLOCKED",
            tenant_id=self.tenant.tenant_id,
        ).first()
        assert audit_entry is not None

    def test_06_model_level_cost_lot_and_batch_branch_alignment(self):
        """Attempting to assign a cost lot to a mismatched branch batch fails model validation."""
        # Cost lot in Branch A attached to Batch B (in Branch B)
        lot_mismatched = PurchaseCostLot(
            lot_id=f"lot-mis-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,  # Branch A
            product=self.product,
            batch=self.batch_b,   # Batch B is in Branch B!
            purchase_date=self.today,
            unit_cost=Decimal("2.4000"),
            initial_quantity=50,
            remaining_quantity=50,
        )
        with pytest.raises(DjangoValidationError) as exc_info:
            lot_mismatched.clean()
        assert "branch" in exc_info.value.message_dict

    def test_07_owner_segregated_multi_branch_summary(self):
        """Owner views medicine summary across branches; stock is segregated per branch, never blended."""
        self.client.force_authenticate(user=self.owner)

        res = self.client.get(
            f"/api/v2/inventory/branch-batches/branch-summary/?tenant_id={self.tenant.tenant_id}&product_id={self.product.product_id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.data

        data = res.data
        assert data["branch_count"] == 2
        breakdowns = {b["branch_id"]: b for b in data["branch_breakdowns"]}

        # Branch A: on_hand=100, reserved=20, available=80
        assert breakdowns[self.branch_a.branch_id]["quantity_on_hand"] == 100
        assert breakdowns[self.branch_a.branch_id]["quantity_reserved"] == 20
        assert breakdowns[self.branch_a.branch_id]["quantity_available"] == 80

        # Branch B: on_hand=150, reserved=0, available=150
        assert breakdowns[self.branch_b.branch_id]["quantity_on_hand"] == 150
        assert breakdowns[self.branch_b.branch_id]["quantity_reserved"] == 0
        assert breakdowns[self.branch_b.branch_id]["quantity_available"] == 150

    def test_08_audited_inter_branch_stock_transfer(self):
        """Inter-branch stock transfer atomically moves 30 units from Branch A to Branch B."""
        self.client.force_authenticate(user=self.owner)

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "source_branch_id": self.branch_a.branch_id,
            "target_branch_id": self.branch_b.branch_id,
            "batch_id": self.batch_a.batch_id,
            "quantity": 30,
            "reason": "Replenishment dispatch: Branch B low on stock for morning peak",
        }
        res = self.client.post("/api/v2/inventory/branch-batches/transfer/", payload, format="json")
        assert res.status_code == status.HTTP_200_OK, res.data

        # Source branch A decreased from 100 to 70
        self.batch_a.refresh_from_db()
        assert self.batch_a.quantity_base_units == 70

        # Target branch B increased from 150 to 180
        self.batch_b.refresh_from_db()
        assert self.batch_b.quantity_base_units == 180

        # Dual availability updated
        avail_a = BranchStockAvailability.objects.get(
            tenant=self.tenant, branch=self.branch_a, product=self.product
        )
        avail_b = BranchStockAvailability.objects.get(
            tenant=self.tenant, branch=self.branch_b, product=self.product
        )
        assert avail_a.quantity_on_hand == 70
        assert avail_b.quantity_on_hand == 180

        # Audit log entry created
        audit_entry = AuditLogEntry.objects.filter(
            action="INTER_BRANCH_STOCK_TRANSFER",
            tenant_id=self.tenant.tenant_id,
        ).first()
        assert audit_entry is not None

    def test_09_transfer_exceeding_source_unreserved_stock_rejected(self):
        """Transfer attempting to move more than available unreserved stock in source branch is blocked."""
        self.client.force_authenticate(user=self.owner)

        # Batch A: 100 physical, 20 reserved -> unreserved is 80
        # Transfer of 85 must fail
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "source_branch_id": self.branch_a.branch_id,
            "target_branch_id": self.branch_b.branch_id,
            "batch_id": self.batch_a.batch_id,
            "quantity": 85,
            "reason": "Attempting to transfer reserved stock to another branch",
        }
        res = self.client.post("/api/v2/inventory/branch-batches/transfer/", payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_INSUFFICIENT_UNRESERVED_STOCK" in str(res.data)

    def test_10_cross_tenant_isolation_and_suspended_tenant_blocked(self):
        """Cross-tenant access blocked with 403; suspended tenant operations barred with 403."""
        # 1. Cross-tenant block
        tenant_other = Tenant.objects.create(
            tenant_id=f"ten-oth-{self.unique_id}",
            name="Other Pharma Ltd.",
            subdomain=f"other-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        user_other = PharmacyUser.objects.create_user(
            email=f"user_other_{self.unique_id}@pharmacy.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Other Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=tenant_other.tenant_id,
        )
        self.client.force_authenticate(user=user_other)

        res_cross = self.client.get(
            f"/api/v2/inventory/branch-batches/?tenant_id={self.tenant.tenant_id}"
        )
        assert res_cross.status_code == status.HTTP_403_FORBIDDEN
        data_cross = res_cross.json() if hasattr(res_cross, "json") else res_cross.data
        assert "ERR_CROSS_TENANT" in str(data_cross)

        # 2. Suspended tenant block
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        self.client.force_authenticate(user=self.owner)
        res_susp = self.client.get(
            f"/api/v2/inventory/branch-batches/?tenant_id={self.tenant.tenant_id}"
        )
        assert res_susp.status_code == status.HTTP_403_FORBIDDEN
        data_susp = res_susp.json() if hasattr(res_susp, "json") else res_susp.data
        assert "ERR_TENANT_SUSPENDED" in str(data_susp)
