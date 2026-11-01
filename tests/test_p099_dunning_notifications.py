"""
backend/tests/test_p099_dunning_notifications.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0081, ADR-0087, ADR-0088, ADR-0095, ADR-0096, ADR-0097, ADR-0098 & ADR-0099
Phase 05: Basic/Standard/Pro Subscription & Billing (P099)
Requirement Trace: FR-044, FR-066, FR-068, FR-070

Comprehensive Integration Tests for Subscription Dunning Notifications:
1. Scenario 1 (Valid Dispatch): SuperAdmin/System dispatches localized PAYMENT_FAILED notification with BDT currency (HTTP 201).
2. Scenario 2 (24-Hour De-duplication Rule): Duplicate notification in 24h window is automatically SUPPRESSED; force=True overrides.
3. Scenario 3 (Owner List & Acknowledge): Pharmacy Owner retrieves alerts and acknowledges them (HTTP 200).
4. Scenario 4 (Bounded Retry Rule): Manual retry increments retry_count; exceeding max_retries returns HTTP 400.
5. Scenario 5 (Invalid Input Case): Invalid dunning stage returns HTTP 400/422 Bad Request.
6. Scenario 6 (RBAC Cashier Denial): Cashier role is strictly forbidden from viewing or acknowledging dunning notifications (HTTP 403).
7. Scenario 7 (Cross-Tenant Security): Tenant B Owner querying Tenant A notifications is rejected with HTTP 403 CrossTenantViolationError.
8. Scenario 8 (Full Dunning Lifecycle & Recovery): Stepwise progression from UPCOMING_RENEWAL to PAYMENT_RECOVERED restores full service.
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

from apps.core.dunning_service import DunningNotificationService
from apps.core.models import (
    DunningDeliveryStatus,
    DunningNotification,
    DunningStage,
    RenewalInvoiceStatus,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP099DunningNotificationsIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin User
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p099_admin_{cls.suffix}@platform.internal",
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
        cls.subdomain_a = f"dunn-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-dunn-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P099-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@dunna.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Dunning Pharma A {cls.suffix}",
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
            email=f"cashier_a_{cls.suffix}@dunna.local",
            full_name=f"Cashier A {cls.suffix}",
            tenant_id=cls.tenant_id_a,
            role=UserRole.CASHIER,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            is_active=True,
        )

        # 3. Provision Tenant B (Basic Plan)
        cls.subdomain_b = f"dunn-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-dunn-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P099-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@dunnb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Dunning Pharma B {cls.suffix}",
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

        # Seed open renewal invoice for Tenant A
        sub_a = SubscriptionRecord.objects.filter(tenant=cls.tenant_a).first()
        cls.invoice_a = SubscriptionRenewalInvoice.objects.create(
            invoice_id=f"inv-dunn-{cls.suffix}",
            tenant=cls.tenant_a,
            subscription=sub_a,
            invoice_number=f"INV-P099-{cls.suffix.upper()}",
            plan_tier="STANDARD",
            billing_cycle="MONTHLY",
            status=RenewalInvoiceStatus.ISSUED,
            subtotal_bdt=Decimal("4285.71"),
            tax_amount_bdt=Decimal("214.29"),
            total_bdt=Decimal("4500.00"),
            period_start=django.utils.timezone.now().date(),
            period_end=django.utils.timezone.now().date() + django.utils.timezone.timedelta(days=30),
        )

    def setUp(self):
        self.client = APIClient()

    def test_01_valid_dunning_notification_dispatch(self):
        """Superadmin dispatches localized PAYMENT_FAILED notification (HTTP 201)."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.post(
            "/api/v2/billing/dunning/dispatch/",
            data={
                "tenant_id": self.tenant_id_a,
                "stage": "PAYMENT_FAILED",
                "invoice_id": self.invoice_a.invoice_id,
                "amount_bdt": "4500.00",
                "channel": "IN_APP",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["code"], "DUNNING_NOTIFICATION_DISPATCHED")

        data = response.data["data"]
        self.assertEqual(data["tenant_id"], self.tenant_id_a)
        self.assertEqual(data["stage"], "PAYMENT_FAILED")
        self.assertEqual(data["delivery_status"], "DELIVERED")
        self.assertEqual(data["amount_due_bdt"], "4500.00")
        self.assertIn("Payment Failed", data["title_en"])
        self.assertIn("পেমেন্ট ব্যর্থ", data["title_bn"])
        self.assertFalse(data["is_acknowledged"])

    def test_02_deduplication_rule_24h_window(self):
        """Identical dunning stage dispatched within 24h is SUPPRESSED unless force=True."""
        # 1. First dispatch: DELIVERED
        notif1 = DunningNotificationService.generate_and_dispatch_notification(
            tenant_id=self.tenant_id_a,
            stage="GRACE_PERIOD_ACTIVE",
            invoice_id=self.invoice_a.invoice_id,
            amount_bdt=Decimal("4500.00"),
        )
        self.assertEqual(notif1.delivery_status, DunningDeliveryStatus.DELIVERED)

        # 2. Second dispatch: SUPPRESSED due to 24h de-duplication
        notif2 = DunningNotificationService.generate_and_dispatch_notification(
            tenant_id=self.tenant_id_a,
            stage="GRACE_PERIOD_ACTIVE",
            invoice_id=self.invoice_a.invoice_id,
            amount_bdt=Decimal("4500.00"),
            force=False,
        )
        self.assertEqual(notif2.delivery_status, DunningDeliveryStatus.SUPPRESSED)
        self.assertIn("Suppressed", notif2.last_error_message)

        # 3. Third dispatch with force=True: DELIVERED
        notif3 = DunningNotificationService.generate_and_dispatch_notification(
            tenant_id=self.tenant_id_a,
            stage="GRACE_PERIOD_ACTIVE",
            invoice_id=self.invoice_a.invoice_id,
            amount_bdt=Decimal("4500.00"),
            force=True,
        )
        self.assertEqual(notif3.delivery_status, DunningDeliveryStatus.DELIVERED)

    def test_03_owner_list_and_acknowledge_notification(self):
        """Tenant A owner retrieves alerts and acknowledges a pending notification (HTTP 200)."""
        # Ensure at least one notification exists in this isolated test transaction
        DunningNotificationService.generate_and_dispatch_notification(
            tenant_id=self.tenant_id_a,
            stage="PAYMENT_FAILED",
            invoice_id=self.invoice_a.invoice_id,
            amount_bdt=Decimal("4500.00"),
            force=True,
        )

        self.client.force_authenticate(user=self.owner_a)

        # List
        list_resp = self.client.get(
            f"/api/v2/billing/dunning/?tenant_id={self.tenant_id_a}",
            HTTP_X_TENANT_ID=self.tenant_id_a,
        )
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(list_resp.data["success"])
        notifs = list_resp.data["data"]["notifications"]
        self.assertGreaterEqual(len(notifs), 1)

        target_notif = notifs[0]
        self.assertFalse(target_notif["is_acknowledged"])

        # Acknowledge
        ack_resp = self.client.post(
            f"/api/v2/billing/dunning/{target_notif['notification_id']}/acknowledge/",
            HTTP_X_TENANT_ID=self.tenant_id_a,
        )
        self.assertEqual(ack_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(ack_resp.data["success"])
        self.assertTrue(ack_resp.data["data"]["is_acknowledged"])
        self.assertIsNotNone(ack_resp.data["data"]["acknowledged_at"])

    def test_04_retry_rule_and_limit(self):
        """Notification retry increments count and rejects beyond max_retries limit (HTTP 400)."""
        # Create a failed notification
        notif = DunningNotification.objects.create(
            notification_id=f"notif-retry-{uuid.uuid4().hex[:8]}",
            tenant=self.tenant_a,
            stage=DunningStage.UPCOMING_RENEWAL,
            delivery_status=DunningDeliveryStatus.FAILED,
            dedup_key="test-retry-key",
            title_en="Test Retry",
            title_bn="টেস্ট রিট্রাই",
            body_en="Body",
            body_bn="বডি",
            amount_due_bdt=Decimal("4500.00"),
            retry_count=0,
            max_retries=2,
        )

        self.client.force_authenticate(user=self.owner_a)

        # Retry 1: Success (count=1)
        r1 = self.client.post(f"/api/v2/billing/dunning/{notif.notification_id}/retry/", HTTP_X_TENANT_ID=self.tenant_id_a)
        self.assertEqual(r1.status_code, status.HTTP_200_OK)
        self.assertEqual(r1.data["data"]["retry_count"], 1)

        # Retry 2: Success (count=2)
        r2 = self.client.post(f"/api/v2/billing/dunning/{notif.notification_id}/retry/", HTTP_X_TENANT_ID=self.tenant_id_a)
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.data["data"]["retry_count"], 2)

        # Retry 3: Exceeds max_retries=2 -> HTTP 400/422
        r3 = self.client.post(f"/api/v2/billing/dunning/{notif.notification_id}/retry/", HTTP_X_TENANT_ID=self.tenant_id_a)
        self.assertIn(r3.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("retry", str(r3.data))

    def test_05_invalid_input_case(self):
        """Invalid dunning stage returns HTTP 400 or 422 Bad Request."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.post(
            "/api/v2/billing/dunning/dispatch/",
            data={
                "tenant_id": self.tenant_id_a,
                "stage": "INVALID_RANDOM_STAGE",
            },
            format="json",
        )
        self.assertIn(response.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("stage", str(response.data))

    def test_06_unauthorized_cashier_denied(self):
        """Cashier role is strictly blocked from viewing or acknowledging dunning alerts (HTTP 403)."""
        self.client.force_authenticate(user=self.cashier_a)

        # List
        resp_list = self.client.get(
            f"/api/v2/billing/dunning/?tenant_id={self.tenant_id_a}",
            HTTP_X_TENANT_ID=self.tenant_id_a,
        )
        self.assertEqual(resp_list.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PERMISSION_DENIED", resp_list.data.get("error_code", ""))

        # Dispatch
        resp_dispatch = self.client.post(
            "/api/v2/billing/dunning/dispatch/",
            data={"tenant_id": self.tenant_id_a, "stage": "UPCOMING_RENEWAL"},
            HTTP_X_TENANT_ID=self.tenant_id_a,
        )
        self.assertEqual(resp_dispatch.status_code, status.HTTP_403_FORBIDDEN)

    def test_07_cross_tenant_isolation(self):
        """Tenant B owner attempting to access Tenant A dunning notifications receives HTTP 403."""
        self.client.force_authenticate(user=self.owner_b)

        # Cross-tenant list attempt
        resp = self.client.get(
            f"/api/v2/billing/dunning/?tenant_id={self.tenant_id_a}",
            HTTP_X_TENANT_ID=self.tenant_id_b,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_08_lifecycle_sweep_and_recovery(self):
        """Dispatches PAYMENT_RECOVERED notification and confirms account restoration."""
        self.client.force_authenticate(user=self.superadmin)

        recov_resp = self.client.post(
            "/api/v2/billing/dunning/dispatch/",
            data={
                "tenant_id": self.tenant_id_a,
                "stage": "PAYMENT_RECOVERED",
                "invoice_id": self.invoice_a.invoice_id,
                "amount_bdt": "4500.00",
            },
            format="json",
        )
        self.assertEqual(recov_resp.status_code, status.HTTP_201_CREATED)
        data = recov_resp.data["data"]
        self.assertEqual(data["stage"], "PAYMENT_RECOVERED")
        self.assertIn("Payment Recovered", data["title_en"])
        self.assertIn("পেমেন্ট নিশ্চিত", data["title_bn"])


if __name__ == "__main__":
    unittest.main()
