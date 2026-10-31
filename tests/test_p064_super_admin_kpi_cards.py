"""
backend/tests/test_p064_super_admin_kpi_cards.py
Architecture Decision: ADR-0005, ADR-0055 & ADR-0064
Comprehensive test suite for Platform Super Admin 7 KPI Cards:
- Valid scenario: Super Admin retrieves all 7 KPI cards with Decimal BDT revenue precision and audit verification.
- Invalid scenario: Unsupported currency (e.g. USD) or invalid period returns HTTP 400/422.
- Unauthorized scenario: Tenant Owner, Cashier, and anonymous callers receive HTTP 403/401 with PLATFORM_ACCESS_DENIED.
- Two-tenant isolation & privilege separation: Tenant users never access aggregate platform KPI telemetry.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import AsyncJobRecord, Branch, SubscriptionRecord, Tenant
from apps.identity.models import PharmacyUser, UserRole


class TestP064SuperAdminKpiCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p064_super_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "is_staff": True,
                "is_superuser": True,
                "phone_number": f"+88019{phone_suffix}",
            },
        )
        cls.superadmin.set_password("AdminSecurePass2026!")
        cls.superadmin.save()

        # 2. Tenant Alpha (ACTIVE with ACTIVE subscription)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"tenant-p064-a-{cls.suffix}",
            name=f"Alpha Care Pharma {cls.suffix}",
            subdomain=f"alpha-p064-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
            billing_email=f"alpha_{cls.suffix}@pharmacy.bd",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"{cls.tenant_a.tenant_id}-br-1",
            tenant=cls.tenant_a,
            name="Dhanmondi Branch",
            is_active=True,
        )
        phone_owner_a = random.randint(10000000, 99999999)
        cls.owner_a = PharmacyUser.objects.create(
            email=f"owner_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Dr. Owner Alpha {cls.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            phone_number=f"+88018{phone_owner_a}",
        )
        cls.owner_a.set_password("OwnerPassword2026!")
        cls.owner_a.save()
        cls.tenant_a.owner_user_id = str(cls.owner_a.user_id)
        cls.tenant_a.save()

        SubscriptionRecord.objects.create(
            subscription_id=f"sub-p064-a-{cls.suffix}",
            tenant=cls.tenant_a,
            plan_tier="STANDARD",
            status="ACTIVE",
            amount_bdt=Decimal("3500.00"),
            billing_cycle="MONTHLY",
            licensed_branches=5,
            licensed_devices=10,
            start_date=timezone.now().date(),
            renewal_date=(timezone.now() + timedelta(days=365)).date(),
        )

        # 3. Tenant Beta (TRIAL with TRIAL subscription)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"tenant-p064-b-{cls.suffix}",
            name=f"Beta Wellness Pharma {cls.suffix}",
            subdomain=f"beta-p064-{cls.suffix}",
            plan_tier="PRO",
            status="TRIAL",
            billing_email=f"beta_{cls.suffix}@pharmacy.bd",
        )
        phone_cashier_b = random.randint(10000000, 99999999)
        cls.cashier_b = PharmacyUser.objects.create(
            email=f"cashier_b_{cls.suffix}@pharmacy.bd",
            full_name=f"Cashier Beta {cls.suffix}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_b.tenant_id,
            phone_number=f"+88017{phone_cashier_b}",
        )
        cls.cashier_b.set_password("CashierPassword2026!")
        cls.cashier_b.save()

        SubscriptionRecord.objects.create(
            subscription_id=f"sub-p064-b-{cls.suffix}",
            tenant=cls.tenant_b,
            plan_tier="PRO",
            status="TRIAL",
            amount_bdt=Decimal("5000.00"),
            billing_cycle="MONTHLY",
            licensed_branches=10,
            licensed_devices=20,
            start_date=timezone.now().date(),
            renewal_date=(timezone.now() + timedelta(days=3)).date(),  # Expiring soon (<7 days)
        )

        # 4. Tenant Gamma (SUSPENDED)
        cls.tenant_c = Tenant.objects.create(
            tenant_id=f"tenant-p064-c-{cls.suffix}",
            name=f"Gamma Dormant Pharma {cls.suffix}",
            subdomain=f"gamma-p064-{cls.suffix}",
            plan_tier="BASIC",
            status="SUSPENDED",
            billing_email=f"gamma_{cls.suffix}@pharmacy.bd",
        )

        # 5. Async Jobs for Sync Backlog testing
        AsyncJobRecord.objects.create(
            job_id=f"job-p064-pending-{cls.suffix}",
            tenant=cls.tenant_a,
            task_name="tasks.sync_sale_invoice_to_cloud",
            status="PENDING",
        )
        AsyncJobRecord.objects.create(
            job_id=f"job-p064-dead-{cls.suffix}",
            tenant=cls.tenant_b,
            task_name="tasks.push_dgda_antibiotic_audit",
            status="DEAD_LETTERED",
        )

    def setUp(self):
        self.client = APIClient()

    def test_superadmin_kpi_cards_calculation_and_audit(self):
        """Valid scenario: Super Admin retrieves all 7 KPI cards with Decimal BDT revenue precision and audit trail."""
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.get("/api/v2/platform/console/kpis/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.json()
        self.assertTrue(data["success"])
        kpis = data["data"]

        # 1. Tenants KPI Card
        self.assertIn("tenants", kpis)
        self.assertGreaterEqual(kpis["tenants"]["total_tenants"], 3)
        self.assertGreaterEqual(kpis["tenants"]["active_tenants"], 1)
        self.assertTrue(isinstance(kpis["tenants"]["growth_rate_pct"], str))

        # 2. Trials KPI Card
        self.assertIn("trials", kpis)
        self.assertGreaterEqual(kpis["trials"]["active_trials"], 1)
        self.assertGreaterEqual(kpis["trials"]["expiring_soon_count"], 1)

        # 3. Active Subscriptions KPI Card
        self.assertIn("active_subscriptions", kpis)
        self.assertGreaterEqual(kpis["active_subscriptions"]["active_subscriptions_count"], 1)
        self.assertIn("plan_tier_breakdown", kpis["active_subscriptions"])

        # 4. Revenue KPI Card (Decimal BDT string format)
        self.assertIn("revenue", kpis)
        rev = kpis["revenue"]
        self.assertEqual(rev["currency"], "BDT")
        self.assertTrue(Decimal(rev["monthly_recurring_revenue"]) >= Decimal("3500.00"))
        self.assertTrue(Decimal(rev["annual_run_rate"]) >= Decimal("42000.00"))
        self.assertTrue(Decimal(rev["average_revenue_per_tenant"]) > Decimal("0.00"))

        # 5. Suspended Accounts KPI Card
        self.assertIn("suspended_accounts", kpis)
        self.assertGreaterEqual(kpis["suspended_accounts"]["suspended_tenants_count"], 1)

        # 6. System Health KPI Card
        self.assertIn("system_health", kpis)
        self.assertEqual(kpis["system_health"]["status"], "HEALTHY")
        self.assertTrue(kpis["system_health"]["database_connected"])
        self.assertTrue(kpis["system_health"]["latency_ms"] >= 0)

        # 7. Sync Backlog KPI Card
        self.assertIn("sync_backlog", kpis)
        self.assertGreaterEqual(kpis["sync_backlog"]["pending_jobs_count"], 1)
        self.assertGreaterEqual(kpis["sync_backlog"]["dead_letter_count"], 1)
        self.assertIn(kpis["sync_backlog"]["status"], ["OPTIMAL", "BACKLOG_HIGH", "CRITICAL"])

        # Verify Audit Log
        audit = AuditLogEntry.objects.filter(
            action="PLATFORM_KPIS_VIEWED",
            resource_id="kpi_cards",
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.status, "SUCCESS")

    def test_invalid_currency_or_period_rejected(self):
        """Invalid scenarios: Unsupported non-BDT currency or invalid period returns HTTP 400 or 422."""
        self.client.force_authenticate(user=self.superadmin)

        # Non-BDT currency attempt
        resp_usd = self.client.get("/api/v2/platform/console/kpis/?currency=USD")
        self.assertIn(resp_usd.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("currency", str(resp_usd.json()))

        # Invalid period attempt
        resp_period = self.client.get("/api/v2/platform/console/kpis/?period=unknown_range")
        self.assertIn(resp_period.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("period", str(resp_period.json()))

    def test_unauthorized_tenant_users_denied_kpis(self):
        """Unauthorized scenario: Tenant Owner, Cashier, and anonymous callers receive HTTP 403 Forbidden."""
        # Tenant Owner
        self.client.force_authenticate(user=self.owner_a)
        resp_owner = self.client.get("/api/v2/platform/console/kpis/")
        self.assertEqual(resp_owner.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_owner.json()))

        # Cashier
        self.client.force_authenticate(user=self.cashier_b)
        resp_cashier = self.client.get("/api/v2/platform/console/kpis/")
        self.assertEqual(resp_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(resp_cashier.json()))

        # Anonymous caller
        self.client.force_authenticate(user=None)
        resp_anon = self.client.get("/api/v2/platform/console/kpis/")
        self.assertIn(resp_anon.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


if __name__ == "__main__":
    unittest.main()
