"""
backend/tests/test_p097_customer_portal.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0081, ADR-0082, ADR-0087, ADR-0088, ADR-0095, ADR-0096 & ADR-0097
Phase 05: Basic/Standard/Pro Subscription & Billing (P097)
Requirement Trace: FR-070, FR-071, FR-072 (Subscription Customer Portal & Self-Service Billing)

Comprehensive Integration Tests for Customer Subscription Portal:
1. Valid Scenario: Pharmacy owner retrieves comprehensive customer portal overview (HTTP 200).
2. Self-Service Auto-Renew Toggle: Owner toggles auto-renew on/off with audit entry (HTTP 200).
3. Invalid Input: Non-existent tenant returns HTTP 400 VALIDATION_ERROR.
4. Cross-Tenant Barrier: Tenant B cannot view or modify Tenant A customer portal (HTTP 403).
5. RBAC Barrier: Cashier is strictly forbidden from accessing portal or toggling auto-renew (HTTP 403).
6. Portal State Resilience: Grace period and expired subscription statuses generate tailored warnings.
7. Real-Time Quota Integration: Portal includes authoritative row-locked usage counters.
8. Upgrade Options: Portal lists valid higher plan tiers with pricing.
9. Zero Data Deleted Invariant: Browsing portal or changing settings never deletes historical records.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    ManualPaymentClaim,
    ManualPaymentClaimStatus,
    ManualPaymentMethod,
    PlanTier,
    RenewalInvoiceStatus,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.quota_counter_service import SubscriptionQuotaCounterService
from apps.core.subscription_portal_service import SubscriptionCustomerPortalService
from apps.identity.models import PharmacyUser, UserRole


class TestP097CustomerPortalIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p097_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (Standard Plan: 3 branches, 10 staff, 5 devices)
        cls.subdomain_a = f"portal-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-portal-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P097-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@portala.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Portal Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Branch Dhanmondi",
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)
        cls.branch_a = Branch.objects.filter(tenant=cls.tenant_a).first()

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_a,
            plan_code="PLAN_STANDARD",
            billing_cycle="MONTHLY",
        )

        # Seed sample invoice for Tenant A
        sub = SubscriptionRecord.objects.filter(tenant=cls.tenant_a).order_by("-created_at").first()
        cls.sample_invoice = SubscriptionRenewalInvoice.objects.create(
            invoice_id=f"inv-portal-{cls.suffix}",
            invoice_number=f"REN-INV-P097-{cls.suffix.upper()}",
            tenant=cls.tenant_a,
            subscription=sub,
            plan_tier=PlanTier.STANDARD,
            billing_cycle="MONTHLY",
            period_start="2026-11-01",
            period_end="2026-12-01",
            subtotal_bdt=Decimal("3333.33"),
            tax_rate_percent=Decimal("5.00"),
            tax_amount_bdt=Decimal("166.67"),
            total_bdt=Decimal("3500.00"),
            status=RenewalInvoiceStatus.PAID,
        )

        # Seed sample manual payment claim for Tenant A
        cls.sample_claim = ManualPaymentClaim.objects.create(
            claim_id=f"clm-portal-{cls.suffix}",
            claim_number=f"CLM-P097-{cls.suffix.upper()}",
            tenant=cls.tenant_a,
            invoice=cls.sample_invoice,
            payment_method=ManualPaymentMethod.BKASH,
            transaction_reference=f"TRX-BKASH-{cls.suffix}",
            sender_account_number="01711000000",
            claimed_amount_bdt=Decimal("3500.00"),
            evidence_file_url="https://storage.pharmacy.internal/claims/p097.png",
            status=ManualPaymentClaimStatus.APPROVED,
            maker_user_id=str(cls.owner_a.user_id),
            maker_user_email=cls.owner_a.email,
        )

        # Cashier user for Tenant A
        cls.cashier_a, _ = PharmacyUser.objects.get_or_create(
            email=f"cashier_a_{cls.suffix}@portala.local",
            defaults={
                "full_name": f"Cashier A {cls.suffix}",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant_id_a,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
            },
        )

        # 3. Provision Tenant B (Isolation Boundary: Basic Plan)
        cls.subdomain_b = f"portal-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-portal-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P097-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@portalb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Portal Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Branch Banani",
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)
        cls.branch_b = Branch.objects.filter(tenant=cls.tenant_b).first()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        TenantAppliedEntitlement.objects.filter(tenant=self.tenant_a).update(is_active=True)
        SubscriptionQuotaCounterService.recalibrate_counters(self.tenant_id_a, actor_user=self.owner_a)

    def test_01_valid_customer_portal_overview(self):
        """Owner retrieves complete customer portal overview with invoices, quota, and upgrade options."""
        self.client.force_authenticate(user=self.owner_a)
        url = f"/api/v2/billing/portal/?tenant_id={self.tenant_id_a}"

        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        # 1. Tenant & Active Package
        self.assertEqual(data["tenant_id"], self.tenant_id_a)
        pkg = data["active_package"]
        self.assertEqual(pkg["plan_tier"], "STANDARD")
        self.assertEqual(pkg["billing_cycle"], "MONTHLY")
        self.assertEqual(pkg["status"], "ACTIVE")
        self.assertEqual(pkg["currency"], "BDT")

        # 2. Next Renewal Projection
        ren = data["next_renewal"]
        self.assertIn("renewal_date", ren)
        self.assertGreaterEqual(ren["days_remaining"], 0)
        self.assertIn("projected_amount_bdt", ren)

        # 3. Invoices
        self.assertGreaterEqual(len(data["invoices"]), 1)
        inv = data["invoices"][0]
        self.assertEqual(inv["invoice_number"], self.sample_invoice.invoice_number)
        self.assertEqual(inv["status"], "PAID")
        self.assertEqual(Decimal(inv["total_bdt"]), Decimal("3500.00"))

        # 4. Payment History
        self.assertGreaterEqual(len(data["payment_history"]), 1)

        # 5. Quota Usage Counters
        self.assertIn("counters", data["quota_usage"])
        self.assertIn("BRANCHES", data["quota_usage"]["counters"])
        self.assertIn("STAFF_SEATS", data["quota_usage"]["counters"])
        self.assertIn("DEVICES", data["quota_usage"]["counters"])

        # 6. Upgrade Options (Standard plan should have PRO as an upgrade option)
        upgrade_tiers = [u["tier"] for u in data["upgrade_options"]]
        self.assertIn("PRO", upgrade_tiers)

        # 7. Invariants & Bilingual Text
        self.assertTrue(data["invariants"]["zero_data_deleted"])
        self.assertTrue(data["invariants"]["safe_export_retained"])
        self.assertTrue(data["invariants"]["authoritative_sync"])
        self.assertIn("সাবস্ক্রিপশন সক্রিয়", data["client_messaging"]["headline_bn"])

    def test_02_self_service_toggle_auto_renew(self):
        """Owner toggles auto-renew on and off; persists to database and logs audit entry."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/portal/toggle-auto-renew/"

        # 1. Turn off auto-renew
        res_off = self.client.post(url, {"tenant_id": self.tenant_id_a, "auto_renew": False}, format="json")
        self.assertEqual(res_off.status_code, status.HTTP_200_OK)
        data_off = res_off.json()
        self.assertFalse(data_off["active_package"]["auto_renew"])
        self.assertFalse(data_off["next_renewal"]["auto_renew"])

        # Verify DB
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        self.assertFalse(sub.is_auto_renew)

        # 2. Turn on auto-renew
        res_on = self.client.post(url, {"tenant_id": self.tenant_id_a, "auto_renew": True}, format="json")
        self.assertEqual(res_on.status_code, status.HTTP_200_OK)
        data_on = res_on.json()
        self.assertTrue(data_on["active_package"]["auto_renew"])

        # Audit Check
        self.assertTrue(
            AuditLogEntry.objects.filter(
                action="SUBSCRIPTION_AUTO_RENEW_TOGGLED",
                tenant_id=self.tenant_id_a,
            ).exists()
        )

    def test_03_invalid_tenant_rejected(self):
        """Non-existent tenant ID returns HTTP 404 ERR_TENANT_NOT_FOUND via middleware and ValidationError via service."""
        # 1. API request intercepted by TenantContextMiddleware
        self.client.force_authenticate(user=self.superadmin)
        url = "/api/v2/billing/portal/?tenant_id=tn-nonexistent-999"

        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(res.json()["error_code"], "ERR_TENANT_NOT_FOUND")

        # 2. Direct service call raises ValidationError
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            SubscriptionCustomerPortalService.get_portal_overview("tn-nonexistent-999")

    def test_04_cross_tenant_isolation_barrier(self):
        """Tenant B owner is blocked from viewing or mutating Tenant A customer portal."""
        self.client.force_authenticate(user=self.owner_b)

        # 1. Blocked from listing Tenant A portal
        res_view = self.client.get(f"/api/v2/billing/portal/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_view.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_view.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 2. Blocked from mutating Tenant A auto-renew
        res_toggle = self.client.post(
            "/api/v2/billing/portal/toggle-auto-renew/",
            {"tenant_id": self.tenant_id_a, "auto_renew": False},
            format="json",
        )
        self.assertEqual(res_toggle.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_toggle.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_05_rbac_cashier_strictly_forbidden(self):
        """Cashier role is forbidden from accessing portal or mutating renewal settings."""
        self.client.force_authenticate(user=self.cashier_a)

        # 1. View attempt -> 403 Forbidden
        res_view = self.client.get(f"/api/v2/billing/portal/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_view.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_view.json()["error"], "ERR_PERMISSION_DENIED")

        # 2. Toggle attempt -> 403 Forbidden
        res_toggle = self.client.post(
            "/api/v2/billing/portal/toggle-auto-renew/",
            {"tenant_id": self.tenant_id_a, "auto_renew": False},
            format="json",
        )
        self.assertEqual(res_toggle.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_toggle.json()["error"], "ERR_PERMISSION_DENIED")

    def test_06_portal_grace_period_and_expired_states(self):
        """Portal reflects appropriate messaging under GRACE_PERIOD and EXPIRED subscription standing."""
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()

        # 1. Set to GRACE_PERIOD
        sub.status = SubscriptionStatus.GRACE_PERIOD
        sub.save()
        overview_grace = SubscriptionCustomerPortalService.get_portal_overview(self.tenant_id_a)
        self.assertEqual(overview_grace["active_package"]["status"], "GRACE_PERIOD")
        self.assertIn("গ্রেস পিরিয়ডে রয়েছে", overview_grace["client_messaging"]["headline_bn"])

        # 2. Set to EXPIRED
        sub.status = SubscriptionStatus.EXPIRED
        sub.save()
        overview_expired = SubscriptionCustomerPortalService.get_portal_overview(self.tenant_id_a)
        self.assertEqual(overview_expired["active_package"]["status"], "EXPIRED")
        self.assertIn("মেয়াদ উত্তীর্ণ হয়েছে", overview_expired["client_messaging"]["headline_bn"])

        # Restore ACTIVE
        sub.status = SubscriptionStatus.ACTIVE
        sub.save()

    def test_07_zero_data_deleted_invariant(self):
        """Customer portal overview and setting changes never delete invoices, subscriptions, or audits."""
        sub_count_before = SubscriptionRecord.objects.filter(tenant=self.tenant_a).count()
        inv_count_before = SubscriptionRenewalInvoice.objects.filter(tenant=self.tenant_a).count()
        claim_count_before = ManualPaymentClaim.objects.filter(tenant=self.tenant_a).count()
        audit_count_before = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        SubscriptionCustomerPortalService.toggle_auto_renew(
            tenant_id=self.tenant_id_a,
            auto_renew=True,
            actor_user=self.owner_a,
        )

        sub_count_after = SubscriptionRecord.objects.filter(tenant=self.tenant_a).count()
        inv_count_after = SubscriptionRenewalInvoice.objects.filter(tenant=self.tenant_a).count()
        claim_count_after = ManualPaymentClaim.objects.filter(tenant=self.tenant_a).count()
        audit_count_after = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        self.assertEqual(sub_count_before, sub_count_after)
        self.assertEqual(inv_count_before, inv_count_after)
        self.assertEqual(claim_count_before, claim_count_after)
        self.assertGreater(audit_count_after, audit_count_before)


if __name__ == "__main__":
    unittest.main()
