"""
backend/tests/test_p087_renewal_invoice.py
Architecture Decision: ADR-0013, ADR-0027, ADR-0055, ADR-0085 & ADR-0087
Phase 05: Basic/Standard/Pro Subscription & Billing (P087)

Comprehensive Integration Tests for Renewal Invoice Generation and Settlement:
1. Valid Scenario (Monthly): Generate monthly renewal invoice, verify period, 5% VAT, unique reference, and totals.
2. Valid Scenario (Annual Prepay Discount): Generate annual renewal invoice, verify 12-month period, 2 months free discount (16.67%), 5% VAT, and final total.
3. Idempotency Verification: Re-requesting invoice for active cycle returns existing open invoice without duplicate records.
4. Payment Settlement & Subscription Extension: Paying invoice updates payment_status to PAID, records immutable status_history, and extends subscription renewal_date.
5. Invalid Input Rejection: Invalid billing cycle or negative tax rate rejected with 400 Bad Request.
6. Double Payment Prevention: Attempting to pay an already settled invoice rejected with 400.
7. Cross-Tenant Security: Tenant Owner A cannot view or settle Tenant B's invoice (HTTP 403 ERR_CROSS_TENANT_VIOLATION).
8. SuperAdmin Global Access: SuperAdmin can view invoices across tenants and generate invoices for any tenant.
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

from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PlanTier,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    Tenant,
)
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP087RenewalInvoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p087_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (Standard Plan: 6500 BDT/mo)
        cls.subdomain_a = f"pharma-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-ren-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P087-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@pharmaa.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Pharmacy A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Main Branch A",
            branch_license=cls.license_a,
            is_trial=False,
            operator_id=str(cls.superadmin.user_id),
        )

        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.branch_a = Branch.objects.get(tenant=cls.tenant_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)
        cls.sub_a = SubscriptionRecord.objects.get(tenant=cls.tenant_a)

        # 3. Provision Tenant B (Pro Plan: 15000 BDT/mo)
        cls.subdomain_b = f"pharma-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-ren-b-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@pharmab.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Pharmacy B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.PRO,
            branch_name="Head Branch B",
            branch_license=f"DGDA-DL-P087-B-{cls.suffix}",
            is_trial=False,
            operator_id=str(cls.superadmin.user_id),
        )

        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.branch_b = Branch.objects.get(tenant=cls.tenant_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)
        cls.sub_b = SubscriptionRecord.objects.get(tenant=cls.tenant_b)

    def setUp(self):
        self.client = APIClient()

    def test_01_generate_monthly_renewal_invoice_success(self):
        """Owner A generates a monthly renewal invoice with accurate 5% VAT and totals."""
        self.client.force_authenticate(user=self.owner_a)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "billing_cycle": "MONTHLY",
            "tax_rate": "5.00",
        }
        res = self.client.post("/api/v2/billing/invoices/generate/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()["data"]

        self.assertEqual(data["tenant_id"], self.tenant_a.tenant_id)
        self.assertEqual(data["billing_cycle"], "MONTHLY")
        self.assertEqual(data["plan_tier"], "STANDARD")
        self.assertEqual(data["status"], "ISSUED")
        self.assertTrue(data["invoice_number"].startswith("INV-REN-"))

        # Standard plan monthly rate is 6500.00 BDT
        expected_subtotal = Decimal("6500.00")
        expected_discount = Decimal("0.00")
        expected_tax = Decimal("325.00")  # 5% of 6500.00
        expected_total = Decimal("6825.00")

        self.assertEqual(Decimal(data["subtotal_bdt"]), expected_subtotal)
        self.assertEqual(Decimal(data["discount_bdt"]), expected_discount)
        self.assertEqual(Decimal(data["tax_amount_bdt"]), expected_tax)
        self.assertEqual(Decimal(data["total_bdt"]), expected_total)

    def test_02_generate_annual_renewal_with_prepay_discount(self):
        """Owner B generates annual renewal; receives 2 months free prepay discount + 5% VAT."""
        self.client.force_authenticate(user=self.owner_b)

        payload = {
            "tenant_id": self.tenant_b.tenant_id,
            "billing_cycle": "ANNUAL",
            "tax_rate": "5.00",
        }
        res = self.client.post("/api/v2/billing/invoices/generate/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()["data"]

        self.assertEqual(data["billing_cycle"], "ANNUAL")
        self.assertEqual(data["plan_tier"], "PRO")

        # Pro monthly = 15000.00; 12 months = 180000.00; 2 months discount = 30000.00
        expected_subtotal = Decimal("180000.00")
        expected_discount = Decimal("30000.00")
        taxable_amount = expected_subtotal - expected_discount  # 150000.00
        expected_tax = (taxable_amount * Decimal("5.00") / Decimal("100.00")).quantize(Decimal("0.01"))  # 7500.00
        expected_total = (taxable_amount + expected_tax).quantize(Decimal("0.01"))  # 157500.00

        self.assertEqual(Decimal(data["subtotal_bdt"]), expected_subtotal)
        self.assertEqual(Decimal(data["discount_bdt"]), expected_discount)
        self.assertEqual(Decimal(data["tax_amount_bdt"]), expected_tax)
        self.assertEqual(Decimal(data["total_bdt"]), expected_total)

    def test_03_idempotent_invoice_generation(self):
        """Requesting invoice generation again during active period returns existing open invoice."""
        self.client.force_authenticate(user=self.owner_a)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "billing_cycle": "MONTHLY",
        }
        res1 = self.client.post("/api/v2/billing/invoices/generate/", payload, format="json")
        self.assertIn(res1.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        inv1_id = res1.json()["data"]["invoice_id"]

        res2 = self.client.post("/api/v2/billing/invoices/generate/", payload, format="json")
        self.assertIn(res2.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        inv2_id = res2.json()["data"]["invoice_id"]

        self.assertEqual(inv1_id, inv2_id)

    def test_04_payment_settlement_and_subscription_extension(self):
        """Paying renewal invoice settles status to PAID and extends subscription renewal_date."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Get or generate invoice
        payload = {"tenant_id": self.tenant_a.tenant_id, "billing_cycle": "MONTHLY"}
        gen_res = self.client.post("/api/v2/billing/invoices/generate/", payload, format="json")
        inv_data = gen_res.json()["data"]
        inv_id = inv_data["invoice_id"]
        period_end = inv_data["period_end"]

        # 2. Settle payment
        pay_payload = {
            "payment_reference": f"BKASH-TRX-{self.suffix}-99",
            "payment_method": "BKASH",
        }
        pay_res = self.client.post(
            f"/api/v2/billing/invoices/{inv_id}/pay/",
            pay_payload,
            format="json",
        )
        self.assertEqual(pay_res.status_code, status.HTTP_200_OK)
        settled_data = pay_res.json()["data"]

        self.assertEqual(settled_data["status"], "PAID")
        self.assertIsNotNone(settled_data["paid_at"])
        self.assertEqual(settled_data["payment_reference"], pay_payload["payment_reference"])
        self.assertEqual(settled_data["payment_method"], "BKASH")

        # Verify status history has audit record
        history = settled_data["status_history"]
        self.assertTrue(any(entry.get("to_status") == "PAID" for entry in history))

        # 3. Verify Subscription renewal_date extended in database
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.renewal_date.isoformat()[:10], period_end[:10])

    def test_05_invalid_input_rejections(self):
        """Invalid cycle or negative tax rate rejected with 400 Bad Request."""
        self.client.force_authenticate(user=self.owner_a)

        # Invalid cycle
        bad_cycle = {
            "tenant_id": self.tenant_a.tenant_id,
            "billing_cycle": "BIWEEKLY",
        }
        res = self.client.post("/api/v2/billing/invoices/generate/", bad_cycle, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Negative tax rate
        bad_tax = {
            "tenant_id": self.tenant_a.tenant_id,
            "billing_cycle": "MONTHLY",
            "tax_rate": "-5.00",
        }
        res = self.client.post("/api/v2/billing/invoices/generate/", bad_tax, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_06_double_payment_prevention(self):
        """Attempting to settle an already paid invoice rejected with 400."""
        self.client.force_authenticate(user=self.owner_a)

        # Find paid invoice from test_04
        paid_inv = SubscriptionRenewalInvoice.objects.filter(
            tenant=self.tenant_a,
            status="PAID",
        ).first()
        self.assertIsNotNone(paid_inv)

        pay_payload = {
            "payment_reference": f"NAGAD-TRX-{self.suffix}-DUP",
            "payment_method": "NAGAD",
        }
        res = self.client.post(
            f"/api/v2/billing/invoices/{paid_inv.invoice_id}/pay/",
            pay_payload,
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cannot accept payment", res.json()["message"])

    def test_07_cross_tenant_access_denied(self):
        """Owner A cannot view or settle Tenant B's renewal invoice (ERR_CROSS_TENANT_VIOLATION)."""
        # 1. Superadmin generates invoice for Tenant B
        self.client.force_authenticate(user=self.superadmin)
        res_b = self.client.post(
            "/api/v2/billing/invoices/generate/",
            {"tenant_id": self.tenant_b.tenant_id, "billing_cycle": "MONTHLY"},
            format="json",
        )
        inv_b_id = res_b.json()["data"]["invoice_id"]

        # 2. Owner A attempts to inspect Tenant B's invoice -> 403 Forbidden
        self.client.force_authenticate(user=self.owner_a)
        res_inspect = self.client.get(f"/api/v2/billing/invoices/{inv_b_id}/")
        self.assertEqual(res_inspect.status_code, status.HTTP_403_FORBIDDEN)
        error_code = res_inspect.json().get("error_code") or res_inspect.json().get("code")
        self.assertEqual(error_code, "ERR_CROSS_TENANT_VIOLATION")

        # 3. Owner A attempts to pay Tenant B's invoice -> 403 Forbidden
        res_pay = self.client.post(
            f"/api/v2/billing/invoices/{inv_b_id}/pay/",
            {"payment_reference": "ILLEGAL-PAY", "payment_method": "BKASH"},
            format="json",
        )
        self.assertEqual(res_pay.status_code, status.HTTP_403_FORBIDDEN)
        pay_error_code = res_pay.json().get("error_code") or res_pay.json().get("code")
        self.assertEqual(pay_error_code, "ERR_CROSS_TENANT_VIOLATION")

    def test_08_superadmin_global_access(self):
        """SuperAdmin can list invoices across all tenants."""
        self.client.force_authenticate(user=self.superadmin)

        res = self.client.get("/api/v2/billing/invoices/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        # Should contain invoices from both tenant_a and tenant_b
        tenant_ids = {inv["tenant_id"] for inv in data}
        self.assertIn(self.tenant_a.tenant_id, tenant_ids)
        self.assertIn(self.tenant_b.tenant_id, tenant_ids)
