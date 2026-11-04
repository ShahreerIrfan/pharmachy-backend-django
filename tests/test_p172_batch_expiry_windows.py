"""
backend/tests/test_p172_batch_expiry_windows.py
Architecture Decision: ADR-0172 (P172).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration tests for P172 Batch Expiry Windows & Sale Cutoff Guards:
- Multi-tier alert windows (Warning, Critical, Quarantine, Cutoff)
- Configurable tenant & branch policies and constraint validation
- Unblended GAAP cost valuation at risk dashboard
- Hard block on actual expired stock (FR-019 - no admin bypass)
- Sale cutoff guard with authorized manager override
- Automated quarantine sweeps and P171 stock availability synchronization
- Role-based authorization, cross-tenant isolation, and suspended subscription blocks
"""

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchExpiryPolicy,
    BatchStatus,
    BatchStock,
    Branch,
    BranchStockAvailability,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestBatchExpiryWindowsIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.today = timezone.now().date()
        self.unique_id = uuid.uuid4().hex[:6]

        # 1. Setup Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-exp-{self.unique_id}",
            name="Gulshan Healthcare Pharmacy",
            subdomain=f"gulshan-exp-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-exp-{self.unique_id}",
            tenant=self.tenant,
            name="Gulshan Branch",
        )

        # 2. Setup Users
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

        # 3. Setup Product
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-amox-{self.unique_id}",
            brand_name="Moxacil 500mg Capsule",
            generic_name="Amoxicillin",
            dosage_form="Capsule",
            strength="500mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("7.0000"),
            cost_unit=Decimal("5.0000"),
        )

        # 4. Setup Batches Across 5 Expiry Tiers:
        # Batch 1: Expired (-5 days, 10 units @ 5.00 = 50.00)
        self.batch_expired = BatchStock.objects.create(
            batch_id=f"bat-exp-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-EXPIRED",
            quantity_base_units=10,
            reserved_quantity_base_units=0,
            expiry_date=self.today - datetime.timedelta(days=5),
            purchase_date=self.today - datetime.timedelta(days=120),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

        # Batch 2: Cutoff Blocked (3 days, 20 units @ 5.00 = 100.00)
        self.batch_cutoff = BatchStock.objects.create(
            batch_id=f"bat-cut-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-CUTOFF",
            quantity_base_units=20,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=3),
            purchase_date=self.today - datetime.timedelta(days=90),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

        # Batch 3: Near Expiry Critical (25 days, 30 units @ 5.00 = 150.00)
        self.batch_critical = BatchStock.objects.create(
            batch_id=f"bat-crit-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-CRITICAL",
            quantity_base_units=30,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=25),
            purchase_date=self.today - datetime.timedelta(days=60),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

        # Batch 4: Early Warning (60 days, 40 units @ 5.00 = 200.00)
        self.batch_warning = BatchStock.objects.create(
            batch_id=f"bat-warn-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-WARNING",
            quantity_base_units=40,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=60),
            purchase_date=self.today - datetime.timedelta(days=30),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

        # Batch 5: Safe (150 days, 50 units @ 5.00 = 250.00)
        self.batch_safe = BatchStock.objects.create(
            batch_id=f"bat-safe-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number="BAT-SAFE",
            quantity_base_units=50,
            reserved_quantity_base_units=0,
            expiry_date=self.today + datetime.timedelta(days=150),
            purchase_date=self.today - datetime.timedelta(days=10),
            cost_per_unit=Decimal("5.0000"),
            status=BatchStatus.ACTIVE,
            is_quarantined=False,
            is_active=True,
        )

    def test_01_get_and_configure_expiry_policy(self):
        """
        Validates retrieval and configuration of branch-specific expiry policy.
        """
        self.client.force_authenticate(user=self.manager_user)

        # 1. GET effective policy (creates default)
        get_res = self.client.get(
            f"/api/v2/inventory/expiry-windows/policy/?branch_id={self.branch.branch_id}"
        )
        assert get_res.status_code == status.HTTP_200_OK
        data = get_res.json() if callable(getattr(get_res, "json", None)) else getattr(get_res, "data", {})
        assert data["warning_lead_days"] == 90
        assert data["critical_lead_days"] == 30
        assert data["sale_cutoff_lead_days"] == 7

        # 2. POST update policy
        update_payload = {
            "branch_id": self.branch.branch_id,
            "warning_lead_days": 120,
            "critical_lead_days": 45,
            "quarantine_lead_days": 20,
            "sale_cutoff_lead_days": 10,
            "auto_quarantine_on_cutoff": True,
            "allow_manager_override_near_expiry": True,
        }
        post_res = self.client.post("/api/v2/inventory/expiry-windows/policy/", update_payload, format="json")
        assert post_res.status_code == status.HTTP_200_OK
        updated_data = post_res.json() if callable(getattr(post_res, "json", None)) else getattr(post_res, "data", {})
        assert updated_data["warning_lead_days"] == 120
        assert updated_data["critical_lead_days"] == 45
        assert updated_data["sale_cutoff_lead_days"] == 10
        assert updated_data["allow_manager_override_near_expiry"] is True

    def test_02_policy_validation_constraints(self):
        """
        Validates constraint checks: critical cannot exceed warning, cutoff cannot exceed quarantine.
        """
        self.client.force_authenticate(user=self.manager_user)
        invalid_payload = {
            "branch_id": self.branch.branch_id,
            "warning_lead_days": 30,
            "critical_lead_days": 60,  # Invalid: 60 > 30
            "quarantine_lead_days": 15,
            "sale_cutoff_lead_days": 5,
        }
        res = self.client.post("/api/v2/inventory/expiry-windows/policy/", invalid_payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_03_expiry_dashboard_aggregation_and_valuation(self):
        """
        Validates multi-tier grouping and unblended GAAP cost valuation at risk across all 5 tiers.
        """
        self.client.force_authenticate(user=self.cashier_user)
        url = f"/api/v2/inventory/expiry-windows/dashboard/?branch_id={self.branch.branch_id}"
        res = self.client.get(url)
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        summary = data["summary"]

        # Expired: 1 batch, 10 units, 50.00 BDT
        assert summary["EXPIRED"]["batch_count"] == 1
        assert summary["EXPIRED"]["total_units"] == 10
        assert Decimal(summary["EXPIRED"]["valuation_cost"]) == Decimal("50.00")

        # Cutoff: 1 batch, 20 units, 100.00 BDT
        assert summary["CUTOFF_BLOCKED"]["batch_count"] == 1
        assert summary["CUTOFF_BLOCKED"]["total_units"] == 20
        assert Decimal(summary["CUTOFF_BLOCKED"]["valuation_cost"]) == Decimal("100.00")

        # Critical: 1 batch, 30 units, 150.00 BDT
        assert summary["NEAR_EXPIRY_CRITICAL"]["batch_count"] == 1
        assert summary["NEAR_EXPIRY_CRITICAL"]["total_units"] == 30
        assert Decimal(summary["NEAR_EXPIRY_CRITICAL"]["valuation_cost"]) == Decimal("150.00")

        # Warning: 1 batch, 40 units, 200.00 BDT
        assert summary["EARLY_WARNING"]["batch_count"] == 1
        assert summary["EARLY_WARNING"]["total_units"] == 40
        assert Decimal(summary["EARLY_WARNING"]["valuation_cost"]) == Decimal("200.00")

        # Safe: 1 batch, 50 units, 250.00 BDT
        assert summary["NORMAL_SAFE"]["batch_count"] == 1
        assert summary["NORMAL_SAFE"]["total_units"] == 50
        assert Decimal(summary["NORMAL_SAFE"]["valuation_cost"]) == Decimal("250.00")

    def test_04_hard_block_expired_medicine_at_pos(self):
        """
        DGDA statutory hard block: actual expired medicine (expiry <= today) can NEVER be sold.
        """
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "batch_id": self.batch_expired.batch_id,
        }
        res = self.client.post("/api/v2/inventory/expiry-windows/verify-sale-cutoff/", payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert "ERR_BATCH_EXPIRED" in str(data)

        # Even with manager override, expired stock CANNOT be sold (FR-019)
        self.client.force_authenticate(user=self.manager_user)
        payload_override = {
            "batch_id": self.batch_expired.batch_id,
            "manager_override": True,
        }
        res_override = self.client.post("/api/v2/inventory/expiry-windows/verify-sale-cutoff/", payload_override, format="json")
        assert res_override.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        data_override = res_override.json() if callable(getattr(res_override, "json", None)) else getattr(res_override, "data", {})
        assert "ERR_BATCH_EXPIRED" in str(data_override)

    def test_05_sale_cutoff_blocked_without_override(self):
        """
        Batch within sale cutoff lead days (3 days remaining <= 7 cutoff days) is blocked.
        """
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "batch_id": self.batch_cutoff.batch_id,
        }
        res = self.client.post("/api/v2/inventory/expiry-windows/verify-sale-cutoff/", payload, format="json")
        assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert "ERR_SALE_CUTOFF_BREACHED" in str(data)

    def test_06_sale_cutoff_approved_with_manager_override(self):
        """
        When policy enables manager override, an authorized manager can approve cutoff dispensing.
        """
        # Enable override in policy
        BatchExpiryPolicy.objects.create(
            policy_id=f"expol-ov-{self.unique_id}",
            tenant=self.tenant,
            branch=self.branch,
            warning_lead_days=90,
            critical_lead_days=30,
            quarantine_lead_days=15,
            sale_cutoff_lead_days=7,
            allow_manager_override_near_expiry=True,
        )

        # 1. Cashier attempts override -> PermissionDenied
        self.client.force_authenticate(user=self.cashier_user)
        res_cashier = self.client.post(
            "/api/v2/inventory/expiry-windows/verify-sale-cutoff/",
            {"batch_id": self.batch_cutoff.batch_id, "manager_override": True},
            format="json",
        )
        assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

        # 2. Manager attempts override -> Success
        self.client.force_authenticate(user=self.manager_user)
        res_mgr = self.client.post(
            "/api/v2/inventory/expiry-windows/verify-sale-cutoff/",
            {"batch_id": self.batch_cutoff.batch_id, "manager_override": True},
            format="json",
        )
        assert res_mgr.status_code == status.HTTP_200_OK
        data = res_mgr.json() if callable(getattr(res_mgr, "json", None)) else getattr(res_mgr, "data", {})
        assert data["status"] == "APPROVED"
        assert data["manager_override_applied"] is True

    def test_07_auto_quarantine_sweep_and_availability_sync(self):
        """
        Validates sweep: transitions expired batches to EXPIRED, cutoff batches to quarantined,
        and synchronizes P171 BranchStockAvailability atomically.
        """
        self.client.force_authenticate(user=self.manager_user)
        res = self.client.post(
            "/api/v2/inventory/expiry-windows/auto-quarantine-scan/",
            {"branch_id": self.branch.branch_id},
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK

        data = res.json() if callable(getattr(res, "json", None)) else getattr(res, "data", {})
        assert data["status"] == "SUCCESS"
        assert data["expired_batches_count"] >= 1
        assert data["cutoff_quarantined_count"] >= 1

        # Verify DB batch states
        self.batch_expired.refresh_from_db()
        assert self.batch_expired.status == BatchStatus.EXPIRED
        assert self.batch_expired.is_quarantined is True

        self.batch_cutoff.refresh_from_db()
        assert self.batch_cutoff.is_quarantined is True

        # Verify P171 stock availability projection updated
        avail = BranchStockAvailability.objects.filter(
            tenant=self.tenant, branch=self.branch, product=self.product
        ).first()
        assert avail is not None
        # Quarantined stock includes expired (10) + cutoff (20) = 30
        assert avail.quantity_quarantined == 30
        # Total on hand was 10 + 20 + 30 + 40 + 50 = 150
        assert avail.quantity_on_hand == 150
        # Server confirmed = 150 - 30 = 120
        assert avail.quantity_server_confirmed_available == 120

    def test_08_rbac_unauthorized_role_blocked(self):
        """
        Cashiers cannot modify expiry policy or trigger quarantine sweeps.
        """
        self.client.force_authenticate(user=self.cashier_user)
        res = self.client.post(
            "/api/v2/inventory/expiry-windows/policy/",
            {"branch_id": self.branch.branch_id, "warning_lead_days": 100},
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_09_cross_tenant_isolation_blocked(self):
        """
        Users from Tenant B cannot access or modify Tenant A's expiry policy.
        """
        tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-exp-{self.unique_id}",
            name="Other Tenant",
            subdomain=f"other-exp-{self.unique_id}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        user_b = PharmacyUser.objects.create_user(
            email=f"userb_{self.unique_id}@pharmacy.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Tenant B Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=tenant_b.tenant_id,
        )
        self.client.force_authenticate(user=user_b)

        res = self.client.get(
            f"/api/v2/inventory/expiry-windows/policy/?branch_id={self.branch.branch_id}&tenant_id={self.tenant.tenant_id}"
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_10_suspended_tenant_blocked(self):
        """
        Suspended tenants cannot access batch expiry endpoints.
        """
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        self.client.force_authenticate(user=self.cashier_user)
        res = self.client.get(
            f"/api/v2/inventory/expiry-windows/dashboard/?branch_id={self.branch.branch_id}"
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN

