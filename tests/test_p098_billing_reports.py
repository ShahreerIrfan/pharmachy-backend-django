"""
backend/tests/test_p098_billing_reports.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0081, ADR-0087, ADR-0088, ADR-0095, ADR-0096, ADR-0097 & ADR-0098
Phase 05: Basic/Standard/Pro Subscription & Billing (P098)
Requirement Trace: FR-044, FR-064, FR-066, FR-067, FR-074

Integration Tests for Platform MRR, Churn, Collections, Outstanding Invoices,
and Subscription Audit Reports:
1. Scenario 1 (Valid Platform Reports & Snapshot): SuperAdmin fetches summary, MRR, churn, collections, and persists snapshot (HTTP 200/201).
2. Scenario 2 (Valid Tenant Billing Report): Pharmacy Owner retrieves own tenant billing statement and invoice history (HTTP 200).
3. Scenario 3 (Invalid Input / Date Range): Inverted dates (start_date > end_date) or malformed date strings return HTTP 400 Bad Request.
4. Scenario 4 (RBAC Cashier Denial): Cashier role receives HTTP 403 Forbidden for both tenant and platform billing endpoints.
5. Scenario 5 (Cross-Tenant Security): Tenant B Owner requesting Tenant A billing report is blocked with HTTP 403 CrossTenantViolationError.
6. Scenario 6 (Platform Route Isolation): Pharmacy Owner attempting to access platform-level MRR/summary is blocked with HTTP 403 PLATFORM_ACCESS_DENIED.
7. Scenario 7 (Subscription Audit Trail): Platform audit event stream correctly captures subscription state and payment events.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from datetime import date, timedelta
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    ManualPaymentClaim,
    ManualPaymentClaimStatus,
    ManualPaymentMethod,
    PaymentGatewayWebhookLog,
    RenewalInvoiceStatus,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    SubscriptionStatus,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP098BillingReportsIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin User
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p098_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (Standard Plan)
        cls.subdomain_a = f"bill-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-bill-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P098-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@billa.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Billing Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier="STANDARD",
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        # Cashier for Tenant A
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_a_{cls.suffix}@billa.local",
            full_name=f"Cashier A {cls.suffix}",
            tenant_id=cls.tenant_id_a,
            role=UserRole.CASHIER,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            is_active=True,
        )

        # 3. Provision Tenant B (Basic Plan)
        cls.subdomain_b = f"bill-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-bill-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P098-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@billb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Billing Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier="BASIC",
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

        # Seed billing data for Tenant A
        # SubscriptionRecord: Standard monthly at 4500.00 BDT
        sub_a = SubscriptionRecord.objects.filter(tenant=cls.tenant_a).first()
        if sub_a:
            sub_a.status = SubscriptionStatus.ACTIVE
            sub_a.amount_bdt = Decimal("4500.00")
            sub_a.billing_cycle = "MONTHLY"
            sub_a.save()

        # Seed billing data for Tenant B: Basic monthly at 1500.00 BDT
        sub_b = SubscriptionRecord.objects.filter(tenant=cls.tenant_b).first()
        if sub_b:
            sub_b.status = SubscriptionStatus.ACTIVE
            sub_b.amount_bdt = Decimal("1500.00")
            sub_b.billing_cycle = "MONTHLY"
            sub_b.save()

        # Invoices for Tenant A:
        # 1. Paid invoice
        today = date.today()
        SubscriptionRenewalInvoice.objects.create(
            invoice_id=f"inv-paid-{cls.suffix}",
            tenant=cls.tenant_a,
            subscription=sub_a,
            invoice_number=f"INV-P098-PAID-{cls.suffix}",
            plan_tier="STANDARD",
            billing_cycle="MONTHLY",
            status=RenewalInvoiceStatus.PAID,
            subtotal_bdt=Decimal("4285.71"),
            tax_amount_bdt=Decimal("214.29"),
            total_bdt=Decimal("4500.00"),
            period_start=today - timedelta(days=40),
            period_end=today - timedelta(days=10),
            paid_at=django.utils.timezone.now(),
        )

        # 2. Overdue invoice (35 days overdue -> bracket days_31_60)
        SubscriptionRenewalInvoice.objects.create(
            invoice_id=f"inv-overdue-{cls.suffix}",
            tenant=cls.tenant_a,
            subscription=sub_a,
            invoice_number=f"INV-P098-OVERDUE-{cls.suffix}",
            plan_tier="STANDARD",
            billing_cycle="MONTHLY",
            status=RenewalInvoiceStatus.PAST_DUE,
            subtotal_bdt=Decimal("4285.71"),
            tax_amount_bdt=Decimal("214.29"),
            total_bdt=Decimal("4500.00"),
            period_start=today - timedelta(days=65),
            period_end=today - timedelta(days=35),
        )

        # Manual approved payment claim for Tenant A
        paid_inv = SubscriptionRenewalInvoice.objects.get(invoice_id=f"inv-paid-{cls.suffix}")
        ManualPaymentClaim.objects.create(
            claim_id=f"clm-p098-{cls.suffix}",
            claim_number=f"CLM-98-{cls.suffix.upper()}",
            tenant=cls.tenant_a,
            invoice=paid_inv,
            claimed_amount_bdt=Decimal("4500.00"),
            payment_method=ManualPaymentMethod.BKASH,
            transaction_reference=f"TRX98BK{cls.suffix.upper()}",
            sender_account_number="+8801711223344",  # synthetic-fixture
            status=ManualPaymentClaimStatus.APPROVED,
            maker_user_id=cls.owner_a.user_id,
            maker_user_email=cls.owner_a.email,
            checker_user_id=cls.superadmin.user_id,
            checker_user_email=cls.superadmin.email,
            verified_at=django.utils.timezone.now(),
        )

        # Payment gateway webhook log
        PaymentGatewayWebhookLog.objects.create(
            event_id=f"evt-p098-{cls.suffix}",
            tenant_id=cls.tenant_id_a,
            gateway_name="sslcommerz",
            event_type="PAYMENT_CAPTURED",
            signature_verified=True,
            payload_json={"amount": "4500.00", "currency": "BDT"},
        )

        # Audit entry
        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_RENEWAL_COMPLETED",
            resource_type="SUBSCRIPTION",
            resource_id=sub_a.subscription_id if sub_a else "sub-test",
            tenant_id=cls.tenant_id_a,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"amount_bdt": "4500.00", "tier": "STANDARD"},
        )

    def setUp(self):
        self.client = APIClient()

    def test_01_platform_billing_summary_and_snapshots_success(self):
        """Superadmin retrieves full platform billing summary and creates an audited snapshot (HTTP 200/201)."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get("/api/v2/platform/billing-reports/summary/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["code"], "PLATFORM_BILLING_SUMMARY_RETRIEVED")

        data = response.data["data"]
        self.assertEqual(data["currency"], "BDT")
        self.assertIn("mrr_summary", data)
        self.assertIn("churn_summary", data)
        self.assertIn("collections_summary", data)
        self.assertIn("outstanding_summary", data)

        # Verify MRR arithmetic: Standard (4500) + Basic (1500) = 6000.00 BDT
        mrr_summary = data["mrr_summary"]
        self.assertGreaterEqual(Decimal(mrr_summary["total_mrr_bdt"]), Decimal("6000.00"))
        self.assertGreaterEqual(Decimal(mrr_summary["total_arr_bdt"]), Decimal("72000.00"))

        # Create persistent snapshot via POST
        post_resp = self.client.post(
            "/api/v2/platform/billing-reports/snapshots/",
            data={"period": "MONTHLY"},
            format="json",
        )
        self.assertEqual(post_resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(post_resp.data["success"])
        self.assertIn("report_id", post_resp.data["data"])

        # List snapshots via GET
        list_resp = self.client.get("/api/v2/platform/billing-reports/snapshots/")
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(list_resp.data["data"]["count"], 1)

    def test_02_platform_mrr_and_churn_endpoints(self):
        """Superadmin retrieves dedicated MRR and Churn breakdown endpoints (HTTP 200)."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. MRR
        mrr_resp = self.client.get("/api/v2/platform/billing-reports/mrr/")
        self.assertEqual(mrr_resp.status_code, status.HTTP_200_OK)
        mrr_data = mrr_resp.data["data"]
        self.assertEqual(mrr_data["currency"], "BDT")
        self.assertIn("BASIC", mrr_data["tier_breakdown"])
        self.assertIn("STANDARD", mrr_data["tier_breakdown"])
        self.assertIn("PRO", mrr_data["tier_breakdown"])
        self.assertGreaterEqual(mrr_data["paying_tenants_count"], 2)

        # 2. Churn
        churn_resp = self.client.get("/api/v2/platform/billing-reports/churn/")
        self.assertEqual(churn_resp.status_code, status.HTTP_200_OK)
        churn_data = churn_resp.data["data"]
        self.assertIn("churn_rate_pct", churn_data)
        self.assertIn("lost_mrr_bdt", churn_data)
        self.assertIn("reasons_breakdown", churn_data)

    def test_03_platform_collections_and_outstanding_endpoints(self):
        """Superadmin retrieves Collections and Outstanding aging brackets endpoints (HTTP 200)."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Collections
        coll_resp = self.client.get("/api/v2/platform/billing-reports/collections/")
        self.assertEqual(coll_resp.status_code, status.HTTP_200_OK)
        coll_data = coll_resp.data["data"]
        self.assertEqual(coll_data["currency"], "BDT")
        self.assertGreaterEqual(Decimal(coll_data["total_collected_bdt"]), Decimal("4500.00"))
        self.assertGreaterEqual(Decimal(coll_data["collections_by_method"]["BKASH"]), Decimal("4500.00"))
        self.assertGreaterEqual(coll_data["verified_claims_count"], 1)

        # 2. Outstanding Invoices and Aging Brackets
        out_resp = self.client.get("/api/v2/platform/billing-reports/outstanding/")
        self.assertEqual(out_resp.status_code, status.HTTP_200_OK)
        out_data = out_resp.data["data"]
        self.assertEqual(out_data["currency"], "BDT")
        self.assertGreaterEqual(Decimal(out_data["total_outstanding_bdt"]), Decimal("4500.00"))
        brackets = out_data["aging_brackets"]
        self.assertIn("current", brackets)
        self.assertIn("days_1_30", brackets)
        self.assertIn("days_31_60", brackets)
        # Our overdue invoice is 35 days overdue -> must be classified in days_31_60
        self.assertGreaterEqual(brackets["days_31_60"]["count"], 1)
        self.assertEqual(brackets["days_31_60"]["amount_bdt"], "4500.00")
        self.assertGreaterEqual(out_data["at_risk_tenants_count"], 1)

    def test_04_tenant_billing_report_success(self):
        """Tenant A owner retrieves own tenant billing report with statement totals (HTTP 200)."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/billing/reports/tenant/?tenant_id={self.tenant_id_a}",
            HTTP_X_TENANT_ID=self.tenant_id_a,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["code"], "TENANT_BILLING_REPORT_RETRIEVED")

        data = response.data["data"]
        self.assertEqual(data["tenant_id"], self.tenant_id_a)
        self.assertEqual(data["currency"], "BDT")
        self.assertEqual(data["current_subscription"]["plan_tier"], "STANDARD")
        self.assertEqual(data["lifetime_billed_bdt"], "9000.00")  # 4500 paid + 4500 overdue
        self.assertEqual(data["lifetime_paid_bdt"], "4500.00")
        self.assertEqual(data["outstanding_balance_bdt"], "4500.00")
        self.assertEqual(data["invoices_count"], 2)
        self.assertGreaterEqual(len(data["recent_invoices"]), 2)
        self.assertGreaterEqual(data["payment_claims_count"], 1)
        self.assertGreaterEqual(len(data["subscription_audit_events"]), 1)

    def test_05_invalid_date_range_rejected(self):
        """Inverted date range (start_date > end_date) returns HTTP 400 or 422 Unprocessable Entity."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get(
            "/api/v2/platform/billing-reports/summary/?start_date=2026-12-31&end_date=2026-01-01"
        )
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("date_range", str(response.data))

        # Malformed date string returns HTTP 400 or 422
        bad_date_resp = self.client.get(
            "/api/v2/platform/billing-reports/summary/?start_date=invalid-date"
        )
        self.assertIn(bad_date_resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("start_date", str(bad_date_resp.data))

    def test_06_rbac_cashier_denied(self):
        """Cashier role is strictly blocked from viewing billing reports (HTTP 403 Forbidden)."""
        self.client.force_authenticate(user=self.cashier_a)

        # Tenant route blocked
        resp_tenant = self.client.get(
            f"/api/v2/billing/reports/tenant/?tenant_id={self.tenant_id_a}",
            HTTP_X_TENANT_ID=self.tenant_id_a,
        )
        self.assertEqual(resp_tenant.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PERMISSION_DENIED", resp_tenant.data.get("error_code", ""))

        # Platform route blocked
        resp_platform = self.client.get("/api/v2/platform/billing-reports/summary/")
        self.assertEqual(resp_platform.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(resp_platform.data.get("error_code", ""), ["PLATFORM_ACCESS_DENIED", "ERR_PERMISSION_DENIED"])

    def test_07_cross_tenant_isolation_and_platform_boundary(self):
        """Tenant B owner cannot access Tenant A billing reports (HTTP 403 CrossTenantViolationError)."""
        self.client.force_authenticate(user=self.owner_b)

        # Cross-tenant query attempt
        resp = self.client.get(
            f"/api/v2/billing/reports/tenant/?tenant_id={self.tenant_id_a}",
            HTTP_X_TENANT_ID=self.tenant_id_b,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["code"], "ERR_CROSS_TENANT_VIOLATION")

        # Tenant Owner trying to access platform reports
        platform_resp = self.client.get("/api/v2/platform/billing-reports/mrr/")
        self.assertEqual(platform_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(platform_resp.data.get("error_code", ""), ["PLATFORM_ACCESS_DENIED", "ERR_PERMISSION_DENIED"])


if __name__ == "__main__":
    unittest.main()
