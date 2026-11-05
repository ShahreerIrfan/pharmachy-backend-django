"""
backend/tests/test_p178_batch_merge_guard.py
Architecture Decision: ADR-0178 (P178).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P178 Batch Merge Guard:
1. Safe batch merge succeeds for identical manufacturer lot & expiry, recalculating weighted average unit cost.
2. Pre-flight validation endpoint checks compatibility without database mutation.
3. Mismatched manufacturer batch reference is strictly blocked with ERR_MANUFACTURER_BATCH_MISMATCH.
4. Mismatched expiry date corrupts FEFO and is strictly blocked with ERR_EXPIRY_DATE_MISMATCH.
5. Recalled batch merge is blocked with ERR_BATCH_RECALLED.
6. Quarantined batch merge is blocked with ERR_BATCH_QUARANTINED.
7. Expired batch merge is blocked with ERR_SOURCE_BATCH_EXPIRED.
8. Manager reversal safely restores original batch quantities, costs, and cost lots.
9. Cashier role attempting to merge is barred with HTTP 403 ERR_PERMISSION_DENIED.
10. Cross-branch and cross-tenant merges are strictly blocked.
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
    BatchMergeRecord,
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestBatchMergeGuardIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.unique_id = uuid.uuid4().hex[:6]
        self.common_expiry = self.today + datetime.timedelta(days=365)

        # 1. Setup Tenant and Two Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p178-{self.unique_id}",
            name="Labaid Pharma Retail",
            subdomain=f"labaid-p178-{self.unique_id}",
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
            name="Uttara Branch B",
        )

        # Foreign Tenant
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-for-{self.unique_id}",
            name="Foreign Healthcare Ltd.",
            subdomain=f"foreign-p178-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )

        # 2. Setup Users
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

        # 3. Setup Product
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

        # 4. Setup Compatible Twin Batches in Branch A
        # Source Batch: 40 units @ 2.0000 BDT, Lot BN-2026-X
        self.source_batch = BatchStock.objects.create(
            batch_id=f"bat-src-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-SRC-{self.unique_id}",
            lot_reference="BN-2026-X",
            manufacturer_name="Square Pharmaceuticals Ltd.",
            quantity_base_units=40,
            reserved_quantity_base_units=0,
            expiry_date=self.common_expiry,
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("2.0000"),
            status=BatchStatus.ACTIVE,
        )
        self.source_lot = PurchaseCostLot.objects.create(
            lot_id=f"lot-src-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch=self.source_batch,
            purchase_date=self.today - datetime.timedelta(days=10),
            unit_cost=Decimal("2.0000"),
            initial_quantity=40,
            remaining_quantity=40,
            status=PurchaseCostLotStatus.ACTIVE,
        )

        # Target Batch: 60 units @ 3.0000 BDT, Lot BN-2026-X
        # Expected weighted cost = (40*2.00 + 60*3.00) / 100 = (80 + 180) / 100 = 2.6000 BDT
        self.target_batch = BatchStock.objects.create(
            batch_id=f"bat-tgt-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-TGT-{self.unique_id}",
            lot_reference="BN-2026-X",
            manufacturer_name="Square Pharmaceuticals Ltd.",
            quantity_base_units=60,
            reserved_quantity_base_units=0,
            expiry_date=self.common_expiry,
            purchase_date=self.today - datetime.timedelta(days=5),
            cost_per_unit=Decimal("3.0000"),
            status=BatchStatus.ACTIVE,
        )
        self.target_lot = PurchaseCostLot.objects.create(
            lot_id=f"lot-tgt-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch=self.target_batch,
            purchase_date=self.today - datetime.timedelta(days=5),
            unit_cost=Decimal("3.0000"),
            initial_quantity=60,
            remaining_quantity=60,
            status=PurchaseCostLotStatus.ACTIVE,
        )

    def test_01_safe_batch_merge_success_and_weighted_cost_recalculation(self):
        """
        Verify that compatible batches merge successfully, consolidating quantities,
        calculating weighted average cost, and transferring purchase cost lots.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/batch-merges/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": self.source_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Consolidating split delivery invoice manifests",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data
        assert data["merged_quantity_base_units"] == 40
        assert Decimal(str(data["target_cost_per_unit_after"])) == Decimal("2.6000")

        # Refresh database state
        self.source_batch.refresh_from_db()
        self.target_batch.refresh_from_db()
        self.source_lot.refresh_from_db()

        # Invariant: Target batch has 100 units @ 2.6000 BDT
        assert self.target_batch.quantity_base_units == 100
        assert self.target_batch.cost_per_unit == Decimal("2.60")

        # Invariant: Source batch is depleted
        assert self.source_batch.quantity_base_units == 0
        assert self.source_batch.status == BatchStatus.DEPLETED

        # Invariant: Source cost lot was relinked to target batch
        assert self.source_lot.batch_id == self.target_batch.batch_id

        # Invariant: Audit log exists
        audit = AuditLogEntry.objects.filter(
            action="BATCH_MERGED",
            resource_id=data["merge_id"],
        ).first()
        assert audit is not None
        assert audit.status == AuditActionStatus.SUCCESS

    def test_02_preflight_validation_endpoint(self):
        """
        Verify that the pre-flight validate endpoint returns compatibility details without mutating DB.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/batch-merges/validate/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": self.source_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        data = response.data
        assert data["can_merge"] is True
        assert len(data["blockers"]) == 0
        assert data["projected_target_quantity"] == 100
        assert Decimal(str(data["projected_target_cost_per_unit"])) == Decimal("2.6000")

        # Invariant: Database remains completely un-mutated
        assert BatchMergeRecord.objects.count() == 0
        self.source_batch.refresh_from_db()
        assert self.source_batch.quantity_base_units == 40

    def test_03_mismatched_manufacturer_batch_number_blocked(self):
        """
        Verify that batches with differing manufacturer lot numbers are barred with ERR_MANUFACTURER_BATCH_MISMATCH.
        """
        self.client.force_authenticate(user=self.manager)

        # Batch with different manufacturer number
        diff_lot_batch = BatchStock.objects.create(
            batch_id=f"bat-diff-lot-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-DIFF-{self.unique_id}",
            lot_reference="BN-2026-DIFFERENT",
            quantity_base_units=30,
            reserved_quantity_base_units=0,
            expiry_date=self.common_expiry,
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("2.0000"),
            status=BatchStatus.ACTIVE,
        )

        url = "/api/v2/inventory/batch-merges/"
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": diff_lot_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Attempting to merge differing manufacturer lots",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_MANUFACTURER_BATCH_MISMATCH" in str(response.data)

    def test_04_mismatched_expiry_date_blocked(self):
        """
        Verify that batches with differing expiry dates are barred with ERR_EXPIRY_DATE_MISMATCH.
        """
        self.client.force_authenticate(user=self.manager)

        # Batch with different expiry date
        diff_exp_batch = BatchStock.objects.create(
            batch_id=f"bat-diff-exp-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_a,
            product=self.product,
            batch_number=f"BN-DIFF-EXP-{self.unique_id}",
            lot_reference="BN-2026-X",
            quantity_base_units=30,
            reserved_quantity_base_units=0,
            expiry_date=self.common_expiry - datetime.timedelta(days=90),  # 3 months earlier!
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("2.0000"),
            status=BatchStatus.ACTIVE,
        )

        url = "/api/v2/inventory/batch-merges/"
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": diff_exp_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Attempting differing expiry date merge",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_EXPIRY_DATE_MISMATCH" in str(response.data)

    def test_05_recalled_batch_merge_blocked(self):
        """
        Verify that attempting to merge a recalled batch into active inventory is barred.
        """
        self.client.force_authenticate(user=self.manager)
        self.source_batch.status = BatchStatus.RECALLED
        self.source_batch.save(update_fields=["status"])

        url = "/api/v2/inventory/batch-merges/"
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": self.source_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Attempting to merge recalled medicine",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_BATCH_RECALLED" in str(response.data)

    def test_06_quarantined_batch_merge_blocked(self):
        """
        Verify that quarantined stock cannot be merged into active inventory.
        """
        self.client.force_authenticate(user=self.manager)
        self.source_batch.is_quarantined = True
        self.source_batch.status = BatchStatus.QUARANTINED
        self.source_batch.save(update_fields=["is_quarantined", "status"])

        url = "/api/v2/inventory/batch-merges/"
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": self.source_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Attempting to merge quarantined stock",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_BATCH_QUARANTINED" in str(response.data)

    def test_07_expired_batch_merge_blocked(self):
        """
        Verify that merging expired batches is rejected.
        """
        self.client.force_authenticate(user=self.manager)
        self.source_batch.expiry_date = self.today - datetime.timedelta(days=1)
        self.source_batch.save(update_fields=["expiry_date"])

        url = "/api/v2/inventory/batch-merges/"
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": self.source_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Attempting to merge expired batch",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_SOURCE_BATCH_EXPIRED" in str(response.data)

    def test_08_reversal_restores_original_quantities_and_costs(self):
        """
        Verify that an authorized manager can reverse an executed merge,
        restoring source batch quantity and reverting target batch unit cost.
        """
        self.client.force_authenticate(user=self.manager)
        url = "/api/v2/inventory/batch-merges/"

        # 1. Execute merge
        res = self.client.post(
            url,
            {
                "tenant_id": self.tenant.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "source_batch_id": self.source_batch.batch_id,
                "target_batch_id": self.target_batch.batch_id,
                "reason": "Valid merge before accidental reversal",
            },
            format="json",
        )
        assert res.status_code == status.HTTP_201_CREATED
        merge_id = res.data["merge_id"]

        # 2. Reverse merge
        rev_url = f"/api/v2/inventory/batch-merges/{merge_id}/reverse/"
        rev_res = self.client.post(
            rev_url,
            {"reason": "Merged by mistake; reverting to separate physical lots"},
            format="json",
        )
        assert rev_res.status_code == status.HTTP_200_OK
        assert rev_res.data["is_reversed"] is True

        # 3. Verify stock restoration
        self.source_batch.refresh_from_db()
        self.target_batch.refresh_from_db()

        assert self.source_batch.quantity_base_units == 40
        assert self.source_batch.status == BatchStatus.ACTIVE
        assert self.source_batch.cost_per_unit == Decimal("2.00")

        assert self.target_batch.quantity_base_units == 60
        assert self.target_batch.cost_per_unit == Decimal("3.00")

        # Double reversal blocked
        rev_res2 = self.client.post(rev_url, {"reason": "Second reversal attempt"}, format="json")
        assert rev_res2.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_ALREADY_REVERSED" in str(rev_res2.data)

    def test_09_cashier_cannot_execute_batch_merge(self):
        """
        Verify that cashiers are barred from merging batches (HTTP 403 ERR_PERMISSION_DENIED).
        """
        self.client.force_authenticate(user=self.cashier)
        url = "/api/v2/inventory/batch-merges/"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": self.source_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Cashier unauthorized consolidation attempt",
        }

        response = self.client.post(url, payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.data if hasattr(response, "data") else response.json()
        assert data.get("error_code") == "ERR_PERMISSION_DENIED"

    def test_10_cross_branch_and_cross_tenant_merge_blocked(self):
        """
        Verify that cross-branch and cross-tenant batch merging is strictly barred.
        """
        self.client.force_authenticate(user=self.manager)

        # Batch in Branch B
        branch_b_batch = BatchStock.objects.create(
            batch_id=f"bat-br-b-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch_b,  # Branch B!
            product=self.product,
            batch_number=f"BN-BR-B-{self.unique_id}",
            lot_reference="BN-2026-X",
            quantity_base_units=50,
            reserved_quantity_base_units=0,
            expiry_date=self.common_expiry,
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("2.0000"),
            status=BatchStatus.ACTIVE,
        )

        url = "/api/v2/inventory/batch-merges/"

        # 1. Cross-branch merge attempt
        payload_cross_branch = {
            "tenant_id": self.tenant.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": branch_b_batch.batch_id,  # From Branch B
            "target_batch_id": self.target_batch.batch_id,  # In Branch A
            "reason": "Attempting cross-branch merge consolidation",
        }
        res_branch = self.client.post(url, payload_cross_branch, format="json")
        assert res_branch.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_BRANCH_MISMATCH" in str(res_branch.data)

        # 2. Cross-tenant merge attempt
        payload_cross_tenant = {
            "tenant_id": self.foreign_tenant.tenant_id,  # Foreign tenant
            "branch_id": self.branch_a.branch_id,
            "source_batch_id": self.source_batch.batch_id,
            "target_batch_id": self.target_batch.batch_id,
            "reason": "Attempting illicit cross-tenant merge",
        }
        res_tenant = self.client.post(url, payload_cross_tenant, format="json")
        assert res_tenant.status_code == status.HTTP_403_FORBIDDEN
        data_tenant = res_tenant.data if hasattr(res_tenant, "data") else res_tenant.json()
        assert data_tenant.get("error_code") in ["ERR_CROSS_TENANT_ACCESS", "ERR_CROSS_TENANT_VIOLATION", "ERR_PERMISSION_DENIED"]

