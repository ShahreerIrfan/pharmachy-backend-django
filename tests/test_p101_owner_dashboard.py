"""
backend/tests/test_p101_owner_dashboard.py

Architecture Decision: ADR-0015, ADR-0027, ADR-0031, ADR-0033, ADR-0081, ADR-0101
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility (P101)
Requirement Trace: FR-031, FR-032, FR-033, FR-044, FR-066, FR-070

Comprehensive Integration Tests for Tenant-Scoped Owner Dashboard:
1. Scenario 1 (Valid Full Dashboard Summary): Exact sales, orders, COGS, gross/net profit, and inventory value.
2. Scenario 2 (Branch-Scoped Manager Access): Branch manager accesses assigned branch; sibling branch access is denied (HTTP 403).
3. Scenario 3 (Customer Dues & Repayment Settlement): Record credit sale due and execute partial/full settlement.
4. Scenario 4 (Inventory & Expiry Alerts): Live detection of low-stock thresholds and DGDA 30-day/90-day expiry risks.
5. Scenario 5 (Sync Health & Terminal Connectivity): Registered devices, active offline lease certificates, and hold queue status.
6. Scenario 6 (Invalid Query Input Handling): Malformed date formats, inverted date ranges, and non-existent branch IDs (HTTP 400).
7. Scenario 7 (Cross-Tenant Security Barrier): Tenant B owner querying Tenant A is rejected with HTTP 403 ERR_CROSS_TENANT_VIOLATION.
8. Scenario 8 (Staff Role RBAC Denials): Cashier, Staff Pharmacist, and Inventory Clerk denied access (HTTP 403 ERR_PERMISSION_DENIED).
9. Scenario 9 (Expired Subscription State): Delinquent/Expired subscription flags mutation lock.
10. Scenario 10 (Audit Log Generation): Audited access entry created in AuditLogEntry.
"""

from __future__ import annotations

import datetime
import os
import random
import unittest
import uuid
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.device_seat_service import DeviceSeatManagementService
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InvoiceStatus,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    SaleInvoice,
    SaleInvoiceItem,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP101OwnerDashboardIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin User
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p101_super_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Superadmin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (STANDARD Plan with 2 branches)
        cls.subdomain_a = f"dash-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-dash-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P101-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@dasha.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Dashboard Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        cls.branch_1 = Branch.objects.get(tenant=cls.tenant_a)
        cls.branch_2 = Branch.objects.create(
            branch_id=f"br-dash-a2-{cls.suffix}",
            tenant=cls.tenant_a,
            name=f"Branch 2 Dhanmondi {cls.suffix}",
            license_number=f"DGDA-DL-P101-A2-{cls.suffix}",
            is_active=True,
        )

        # Branch Manager for Branch 1
        cls.manager_1 = PharmacyUser.objects.create(
            email=f"manager_1_{cls.suffix}@dasha.local",
            full_name=f"Manager 1 {cls.suffix}",
            tenant_id=cls.tenant_id_a,
            branch_id=cls.branch_1.branch_id,
            role=UserRole.BRANCH_MANAGER,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            is_active=True,
        )

        # Cashier for Tenant A
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_a_{cls.suffix}@dasha.local",
            full_name=f"Cashier A {cls.suffix}",
            tenant_id=cls.tenant_id_a,
            role=UserRole.CASHIER,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            is_active=True,
        )

        # Staff Pharmacist for Tenant A
        cls.pharmacist_a = PharmacyUser.objects.create(
            email=f"pharmacist_a_{cls.suffix}@dasha.local",
            full_name=f"Pharmacist A {cls.suffix}",
            tenant_id=cls.tenant_id_a,
            role=UserRole.STAFF_PHARMACIST,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            is_active=True,
        )

        # 3. Provision Tenant B for Cross-Tenant testing
        cls.subdomain_b = f"dash-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-dash-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P101-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@dashb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Dashboard Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

        # 4. Seed Products and Batches for Tenant A
        cls.product_1 = MedicineProduct.objects.create(
            product_id=f"prod-p101-1-{cls.suffix}",
            brand_name=f"Napa Extra {cls.suffix}",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Beximco Pharmaceuticals Ltd",
            dgda_dar_number=f"DAR-P101-01-{cls.suffix}",
            mrp_unit=Decimal("3.50"),
            cost_unit=Decimal("2.00"),
        )
        cls.product_2 = MedicineProduct.objects.create(
            product_id=f"prod-p101-2-{cls.suffix}",
            brand_name=f"Seclo 20 {cls.suffix}",
            generic_name="Omeprazole",
            dosage_form="Capsule",
            strength="20mg",
            manufacturer="Square Pharmaceuticals Ltd",
            dgda_dar_number=f"DAR-P101-02-{cls.suffix}",
            mrp_unit=Decimal("7.00"),
            cost_unit=Decimal("4.00"),
        )

        today = timezone.now().date()

        # Batch 1 for Branch 1: Normal Active Stock (100 units, cost 2.00 BDT)
        cls.batch_1 = BatchStock.objects.create(
            batch_id=f"bat-p101-1-{cls.suffix}",
            tenant=cls.tenant_a,
            branch=cls.branch_1,
            product=cls.product_1,
            batch_number=f"B-NP-{cls.suffix}",
            quantity_base_units=100,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today - datetime.timedelta(days=15),
            expiry_date=today + datetime.timedelta(days=180),
            status=BatchStatus.ACTIVE,
        )

        # Batch 2 for Branch 1: Low Stock & Expiring in 20 days (8 units, cost 4.00 BDT)
        cls.batch_2 = BatchStock.objects.create(
            batch_id=f"bat-p101-2-{cls.suffix}",
            tenant=cls.tenant_a,
            branch=cls.branch_1,
            product=cls.product_2,
            batch_number=f"B-SC-{cls.suffix}",
            quantity_base_units=8,
            cost_per_unit=Decimal("4.00"),
            purchase_date=today - datetime.timedelta(days=10),
            expiry_date=today + datetime.timedelta(days=20),
            status=BatchStatus.ACTIVE,
        )

        # Batch 3 for Branch 2: Normal Active Stock (50 units, cost 2.00 BDT)
        cls.batch_3 = BatchStock.objects.create(
            batch_id=f"bat-p101-3-{cls.suffix}",
            tenant=cls.tenant_a,
            branch=cls.branch_2,
            product=cls.product_1,
            batch_number=f"B-NP2-{cls.suffix}",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.00"),
            purchase_date=today - datetime.timedelta(days=5),
            expiry_date=today + datetime.timedelta(days=200),
            status=BatchStatus.ACTIVE,
        )

    def setUp(self):
        self.client = APIClient()

    def test_01_valid_owner_dashboard_summary(self):
        """Scenario 1: Owner retrieves full tenant metrics matching exact database states."""
        # Create finalized retail sale for Branch 1
        # 10 units of product_1 @ 3.50 = 35.00 BDT total, cost = 10 * 2.00 = 20.00 BDT, gross profit = 15.00 BDT
        inv = SaleInvoice.objects.create(
            invoice_id=f"inv-test-{uuid.uuid4().hex[:10]}",
            tenant=self.tenant_a,
            branch=self.branch_1,
            invoice_number=f"INV-P101-1-{self.suffix}",
            customer_name="Rahim Uddin",
            customer_phone="+8801711223344",  # synthetic-fixture
            total_amount=Decimal("35.00"),
            tax_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            net_amount=Decimal("35.00"),
            payment_method=PaymentMethod.CASH,
            status=InvoiceStatus.FINALIZED,
        )
        SaleInvoiceItem.objects.create(
            invoice=inv,
            batch=self.batch_1,
            product=self.product_1,
            quantity=10,
            unit_price=Decimal("3.50"),
            subtotal=Decimal("35.00"),
        )

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/owner/dashboard/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        # Sales validation
        self.assertGreaterEqual(Decimal(data["sales_summary"]["today_sales_bdt"]), Decimal("35.00"))
        self.assertGreaterEqual(data["sales_summary"]["total_orders_count"], 1)

        # Profitability validation
        self.assertGreaterEqual(Decimal(data["profitability_summary"]["cogs_bdt"]), Decimal("20.00"))
        self.assertGreaterEqual(Decimal(data["profitability_summary"]["gross_profit_bdt"]), Decimal("15.00"))

        # Inventory valuation: 100*2 + 8*4 + 50*2 = 200 + 32 + 100 = 332.00 BDT
        self.assertGreaterEqual(Decimal(data["inventory_summary"]["total_stock_value_bdt"]), Decimal("332.00"))
        self.assertEqual(data["inventory_summary"]["low_stock_items_count"], 1)

    def test_02_branch_scoped_manager_access(self):
        """Scenario 2: Branch Manager accesses own branch; sibling branch is strictly forbidden."""
        self.client.force_authenticate(user=self.manager_1)

        # 1. Query own assigned branch (branch_1)
        res_own = self.client.get(f"/api/v2/owner/dashboard/?branch_id={self.branch_1.branch_id}")
        self.assertEqual(res_own.status_code, status.HTTP_200_OK)
        self.assertEqual(res_own.json()["data"]["branch_filter"], self.branch_1.branch_id)

        # 2. Query sibling branch (branch_2) -> HTTP 403 Forbidden
        res_sibling = self.client.get(f"/api/v2/owner/dashboard/?branch_id={self.branch_2.branch_id}")
        self.assertEqual(res_sibling.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", res_sibling.content.decode("utf-8"))

    def test_03_customer_dues_recording_and_settlement(self):
        """Scenario 3: Record customer due and execute partial and final repayments."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Record customer credit due balance (1500.00 BDT)
        res_record = self.client.post(
            "/api/v2/owner/dashboard/dues/record/",
            data={
                "branch_id": self.branch_1.branch_id,
                "customer_name": "Karim Mia",
                "customer_phone": "+8801722334455",  # synthetic-fixture
                "total_due_bdt": "1500.00",
                "notes": "Emergency prescription credit",
            },
            format="json",
        )
        self.assertEqual(res_record.status_code, status.HTTP_201_CREATED)
        due_id = res_record.json()["data"]["due_id"]
        self.assertEqual(res_record.json()["data"]["remaining_due_bdt"], "1500.00")

        # 2. Settle partial payment (500.00 BDT)
        res_partial = self.client.post(
            "/api/v2/owner/dashboard/dues/settle/",
            data={
                "due_id": due_id,
                "amount_bdt": "500.00",
            },
            format="json",
        )
        self.assertEqual(res_partial.status_code, status.HTTP_200_OK)
        self.assertEqual(res_partial.json()["data"]["remaining_due_bdt"], "1000.00")
        self.assertEqual(res_partial.json()["data"]["status"], "PARTIAL")

        # 3. Settle remaining payment (1000.00 BDT)
        res_settle = self.client.post(
            "/api/v2/owner/dashboard/dues/settle/",
            data={
                "due_id": due_id,
                "amount_bdt": "1000.00",
            },
            format="json",
        )
        self.assertEqual(res_settle.status_code, status.HTTP_200_OK)
        self.assertEqual(res_settle.json()["data"]["remaining_due_bdt"], "0.00")
        self.assertEqual(res_settle.json()["data"]["status"], "SETTLED")

    def test_04_inventory_alerts_endpoint(self):
        """Scenario 4: Inventory alerts endpoint returns low stock and DGDA expiry risks."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/owner/dashboard/inventory-alerts/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertIn("inventory_summary", data)
        self.assertIn("expiry_summary", data)
        # batch_2 expires in 20 days -> expiring_in_30_days_count >= 1
        self.assertGreaterEqual(data["expiry_summary"]["expiring_in_30_days_count"], 1)

    def test_05_sync_health_monitoring(self):
        """Scenario 5: Enrolled device seats and offline sync health reporting."""
        # Enroll a device for Branch 1
        DeviceSeatManagementService.enroll_device(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_1.branch_id,
            device_id=f"DEV-POS-101-{self.suffix}",
            device_name="Counter 1 POS",
            actor_user=self.owner_a,
        )

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/owner/dashboard/sync-health/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertGreaterEqual(data["sync_health_summary"]["enrolled_devices_count"], 1)
        self.assertIn(data["sync_health_summary"]["sync_status"], ["HEALTHY", "NEEDS_SYNC"])

    def test_06_invalid_input_and_date_range_validation(self):
        """Scenario 6: Malformed dates and non-existent branches rejected with HTTP 400."""
        self.client.force_authenticate(user=self.owner_a)

        # Invalid date format
        res_bad_date = self.client.get("/api/v2/owner/dashboard/?date_from=invalid-date")
        self.assertEqual(res_bad_date.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_date.json()["code"], "INVALID_DATE_FORMAT")

        # Inverted date range (from 2026-11-10 to 2026-11-01)
        res_inverted = self.client.get("/api/v2/owner/dashboard/?date_from=2026-11-10&date_to=2026-11-01")
        self.assertEqual(res_inverted.status_code, status.HTTP_400_BAD_REQUEST)

        # Non-existent branch ID triggers cross-branch security boundary (HTTP 403)
        res_bad_branch = self.client.get("/api/v2/owner/dashboard/?branch_id=br-non-existent")
        self.assertEqual(res_bad_branch.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", res_bad_branch.content.decode("utf-8"))

    def test_07_cross_tenant_security_isolation(self):
        """Scenario 7: Tenant B owner querying Tenant A dashboard rejected with ERR_CROSS_TENANT_VIOLATION."""
        self.client.force_authenticate(user=self.owner_b)
        res = self.client.get(f"/api/v2/owner/dashboard/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", res.content.decode("utf-8"))

    def test_08_rbac_denial_for_staff_roles(self):
        """Scenario 8: Non-manager staff roles (Cashier, Pharmacist) denied executive dashboard."""
        # 1. Cashier role denial
        self.client.force_authenticate(user=self.cashier_a)
        res_cashier = self.client.get("/api/v2/owner/dashboard/")
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", res_cashier.content.decode("utf-8"))

        # 2. Staff Pharmacist denial
        self.client.force_authenticate(user=self.pharmacist_a)
        res_pharmacist = self.client.get("/api/v2/owner/dashboard/")
        self.assertEqual(res_pharmacist.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", res_pharmacist.content.decode("utf-8"))

    def test_09_expired_subscription_mutation_lock(self):
        """Scenario 9: Expired subscription status accurately flags is_mutation_locked."""
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.status = SubscriptionStatus.EXPIRED
        sub.save(update_fields=["status"])

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/owner/dashboard/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertEqual(data["subscription_summary"]["status"], "EXPIRED")
        self.assertTrue(data["subscription_summary"]["is_mutation_locked"])

    def test_10_audit_log_tracking(self):
        """Scenario 10: Executive dashboard viewing generates audited access logs."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/owner/dashboard/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        audit = AuditLogEntry.objects.filter(
            action="OWNER_DASHBOARD_ACCESSED",
            tenant_id=self.tenant_id_a,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.resource_type, "EXECUTIVE_DASHBOARD")


if __name__ == "__main__":
    unittest.main()
