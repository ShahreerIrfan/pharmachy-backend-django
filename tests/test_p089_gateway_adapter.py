"""
backend/tests/test_p089_gateway_adapter.py
Architecture Decision: ADR-0013, ADR-0027, ADR-0055, ADR-0087 & ADR-0089
Phase 05: Basic/Standard/Pro Subscription & Billing (P089)

Comprehensive Integration Tests for Payment Gateway Integration, Signed Webhooks & Idempotency:
1. Valid Checkout Initiation: Initiates session via BKASH adapter with accurate BDT totals and session URL.
2. Valid Signed Webhook & Automatic Activation: External webhook with valid HMAC-SHA256 signature settles invoice to PAID and extends subscription.
3. Idempotent Replay Protection: Replaying the identical webhook returns 200 DUPLICATE without double settlement or subscription mutation.
4. Forged Signature Rejection: Webhook with tampered body or invalid signature rejected with HTTP 403 ERR_INVALID_WEBHOOK_SIGNATURE.
5. Amount Mismatch Rejection: Webhook reporting incorrect amount rejected with HTTP 400 Bad Request.
6. Automated Reconciliation: Querying reconciliation endpoint returns verified gateway settlement history.
7. Cross-Tenant Barrier: Tenant B cannot initiate checkout or reconcile for Tenant A (HTTP 403 ERR_CROSS_TENANT_VIOLATION).
8. Zero Stored Secrets Invariant: Database audit verifies zero sensitive gateway API keys or secrets in webhook logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import unittest
import uuid
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.conf import settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PaymentGatewayWebhookLog,
    PlanTier,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    Tenant,
)
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.renewal_invoice_service import RenewalInvoiceService
from apps.identity.models import PharmacyUser, UserRole


class TestP089GatewayAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p089_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (Standard Plan: 6500 BDT + 5% VAT = 6825.00 BDT)
        cls.subdomain_a = f"gw-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-gw-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P089-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@gwa.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Gateway Pharma A {cls.suffix}",
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

        # 3. Provision Tenant B
        cls.subdomain_b = f"gw-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-gw-b-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@gwb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Gateway Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Head Branch B",
            branch_license=f"DGDA-DL-P089-B-{cls.suffix}",
            is_trial=False,
            operator_id=str(cls.superadmin.user_id),
        )

        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

        # Gateway Webhook Secret
        cls.webhook_secret = getattr(settings, "GATEWAY_WEBHOOK_SECRET", "pharma-saas-webhook-secret-key-2026")

    def setUp(self):
        self.client = APIClient()

    def _sign_payload(self, body_bytes: bytes) -> str:
        return hmac.new(self.webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

    def test_01_initiate_gateway_checkout_session(self):
        """Authenticated tenant owner can initiate gateway checkout session."""
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_a.email,
        )

        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "invoice_id": inv.invoice_id,
            "gateway_name": "BKASH",
            "customer_phone": self.phone_a,
            "customer_email": self.email_a,
        }
        res = self.client.post("/api/v2/billing/gateway/checkout/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertEqual(data["gateway_name"], "BKASH")
        self.assertTrue(data["checkout_url"].startswith("https://"))
        self.assertEqual(data["invoice_number"], inv.invoice_number)
        self.assertEqual(Decimal(data["amount_bdt"]), inv.total_bdt)

    def test_02_signed_webhook_success_and_subscription_activation(self):
        """Signed webhook with valid HMAC signature settles invoice and activates subscription."""
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_a.email,
        )

        payment_id = f"TRX-BKA-{self.suffix}-99"
        idemp_key = f"idemp-bka-{self.suffix}-01"
        webhook_body = {
            "event_type": "PAYMENT_COMPLETED",
            "payment_id": payment_id,
            "idempotency_key": idemp_key,
            "invoice_number": inv.invoice_number,
            "amount": "6825.00",
            "currency": "BDT",
        }
        raw_bytes = json.dumps(webhook_body).encode("utf-8")
        signature = self._sign_payload(raw_bytes)

        # External webhook callback (Unauthenticated client with cryptographic signature)
        res = self.client.post(
            "/api/v2/billing/gateway/webhook/BKASH/",
            data=raw_bytes,
            content_type="application/json",
            HTTP_X_GATEWAY_SIGNATURE=signature,
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        res_data = res.json()["data"]

        self.assertEqual(res_data["status"], "PROCESSED")
        self.assertTrue(res_data["signature_verified"])
        self.assertEqual(res_data["gateway_payment_id"], payment_id)

        # Database assertions: Invoice is PAID, Subscription renewal date extended
        inv.refresh_from_db()
        self.assertEqual(inv.status, "PAID")
        self.assertEqual(inv.payment_reference, payment_id)

        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.renewal_date, inv.period_end)

    def test_03_idempotent_duplicate_webhook_delivery(self):
        """Replaying identical webhook with same idempotency key returns 200 DUPLICATE without re-settlement."""
        # Retrieve paid invoice from test_02
        paid_inv = SubscriptionRenewalInvoice.objects.filter(
            tenant=self.tenant_a,
            status="PAID",
        ).first()
        self.assertIsNotNone(paid_inv)

        old_renewal_date = SubscriptionRecord.objects.get(tenant=self.tenant_a).renewal_date

        webhook_body = {
            "event_type": "PAYMENT_COMPLETED",
            "payment_id": paid_inv.payment_reference,
            "idempotency_key": f"idemp-bka-{self.suffix}-01",  # Same idempotency key from test_02
            "invoice_number": paid_inv.invoice_number,
            "amount": str(paid_inv.total_bdt),
            "currency": "BDT",
        }
        raw_bytes = json.dumps(webhook_body).encode("utf-8")
        signature = self._sign_payload(raw_bytes)

        res = self.client.post(
            "/api/v2/billing/gateway/webhook/BKASH/",
            data=raw_bytes,
            content_type="application/json",
            HTTP_X_GATEWAY_SIGNATURE=signature,
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        res_data = res.json()["data"]

        self.assertEqual(res_data["status"], "DUPLICATE")
        self.assertIn("Duplicate event ignored", res_data["error_message"])

        # Subscription renewal date must NOT change
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.renewal_date, old_renewal_date)

    def test_04_forged_webhook_signature_rejection(self):
        """Webhook with invalid or forged HMAC signature rejected with HTTP 403."""
        webhook_body = {
            "event_type": "PAYMENT_COMPLETED",
            "payment_id": "TRX-FORGED-01",
            "idempotency_key": "idemp-forged-01",
            "invoice_number": "INV-REN-202611-FAKE-0000",
            "amount": "6825.00",
            "currency": "BDT",
        }
        raw_bytes = json.dumps(webhook_body).encode("utf-8")

        res = self.client.post(
            "/api/v2/billing/gateway/webhook/BKASH/",
            data=raw_bytes,
            content_type="application/json",
            HTTP_X_GATEWAY_SIGNATURE="forged-invalid-signature-hex-1234",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_WEBHOOK_SIGNATURE")

        # Verify recorded log shows SIGNATURE_INVALID
        failed_log = PaymentGatewayWebhookLog.objects.filter(idempotency_key="idemp-forged-01").first()
        self.assertIsNotNone(failed_log)
        self.assertEqual(failed_log.status, "SIGNATURE_INVALID")
        self.assertFalse(failed_log.signature_verified)

    def test_05_amount_mismatch_rejection(self):
        """Webhook reporting amount different from invoice total rejected with 400."""
        inv = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.owner_a.email,
        )

        webhook_body = {
            "event_type": "PAYMENT_COMPLETED",
            "payment_id": f"TRX-MISMATCH-{self.suffix}",
            "idempotency_key": f"idemp-mismatch-{self.suffix}",
            "invoice_number": inv.invoice_number,
            "amount": "100.00",  # Mismatch (invoice is 6825.00)
            "currency": "BDT",
        }
        raw_bytes = json.dumps(webhook_body).encode("utf-8")
        signature = self._sign_payload(raw_bytes)

        res = self.client.post(
            "/api/v2/billing/gateway/webhook/BKASH/",
            data=raw_bytes,
            content_type="application/json",
            HTTP_X_GATEWAY_SIGNATURE=signature,
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not match invoice total", res.json()["message"])

    def test_06_automated_reconciliation_endpoint(self):
        """Querying reconciliation endpoint returns verified gateway settlement history."""
        # Find paid invoice from test_02
        paid_inv = SubscriptionRenewalInvoice.objects.filter(
            tenant=self.tenant_a,
            status="PAID",
        ).first()
        self.assertIsNotNone(paid_inv)

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(f"/api/v2/billing/gateway/{paid_inv.invoice_id}/reconcile/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertTrue(data["is_reconciled"])
        self.assertEqual(data["status"], "PAID")
        self.assertEqual(data["gateway_name"], "BKASH")
        self.assertIsNotNone(data["reconciled_at"])

    def test_07_cross_tenant_access_denied(self):
        """Owner B cannot initiate checkout or reconcile for Tenant A (ERR_CROSS_TENANT_VIOLATION)."""
        inv_a = RenewalInvoiceService.generate_renewal_invoice(
            tenant_id=self.tenant_a.tenant_id,
            billing_cycle="MONTHLY",
            tax_rate_percent=Decimal("5.00"),
            operator=self.superadmin.email,
        )

        # Owner B tries to initiate checkout for Tenant A
        self.client.force_authenticate(user=self.owner_b)
        res_checkout = self.client.post(
            "/api/v2/billing/gateway/checkout/",
            {"tenant_id": self.tenant_a.tenant_id, "invoice_id": inv_a.invoice_id},
            format="json",
        )
        self.assertEqual(res_checkout.status_code, status.HTTP_403_FORBIDDEN)
        err_code = res_checkout.json().get("error_code") or res_checkout.json().get("code")
        self.assertEqual(err_code, "ERR_CROSS_TENANT_VIOLATION")

        # Owner B tries to reconcile Tenant A's invoice
        res_recon = self.client.post(f"/api/v2/billing/gateway/{inv_a.invoice_id}/reconcile/")
        self.assertEqual(res_recon.status_code, status.HTTP_403_FORBIDDEN)

    def test_08_zero_stored_secrets_in_database(self):
        """Zero payment gateway private keys, passwords, or webhook secrets in database logs."""
        logs = PaymentGatewayWebhookLog.objects.all()
        for log in logs:
            payload_str = json.dumps(log.payload_json).lower()
            self.assertNotIn("secret", payload_str)
            self.assertNotIn("private_key", payload_str)
            self.assertNotIn("password", payload_str)
            # Log fields do not store credentials
            self.assertEqual(len(log.payload_hash), 64)  # Hex digest only
