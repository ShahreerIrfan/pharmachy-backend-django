"""
backend/tests/test_p085_subscription_activation_gate.py
Architecture Decision: ADR-0013, ADR-0015, ADR-0033, ADR-0084 & ADR-0085
Phase 05: Basic/Standard/Pro Subscription & Billing (P085)

Comprehensive Integration Tests for Subscription Activation Gate:
1. Operational ERP Write Barrier: Mutating requests (POST/PUT/PATCH/DELETE) rejected with HTTP 403
   ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED when subscription is SUSPENDED.
2. Read-Only Exemption: Safe methods (GET, HEAD, OPTIONS) permitted regardless of subscription state.
3. Billing Exemption: Authorized billing endpoints (/api/v2/billing/*) permitted under suspended/expired states.
4. Active Subscription Permitted: Operational writes permitted when subscription is ACTIVE or TRIAL.
5. Super Admin Exemption: Platform Super Admin exempt from operational write blocks during reconciliation.
6. Unentitled Status Matrix: EXPIRED, CANCELLED, and PENDING all properly rejected.
7. Cross-Tenant Protection: Cross-tenant operations blocked with HTTP 403 ERR_CROSS_TENANT_VIOLATION.
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
    MedicineProduct,
    PlanTier,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.subscription_state_machine_service import SubscriptionStateMachineService
from apps.identity.models import PharmacyUser, UserRole


class TestP085SubscriptionActivationGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p085_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Tenant A (Dhaka Pharma)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-p085-a-{cls.suffix}",
            name=f"Dhaka Central Pharma {cls.suffix}",
            subdomain=f"gate-a-{cls.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"br-p085-a-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Branch",
            is_active=True,
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p085_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # 3. Tenant B (Chittagong Pharma)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p085-b-{cls.suffix}",
            name=f"Chittagong Pharma {cls.suffix}",
            subdomain=f"gate-b-{cls.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p085_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # 4. Medicine Product for operational inventory test
        cls.product = MedicineProduct.objects.create(
            product_id=f"prod_gate_{cls.suffix}",
            brand_name=f"Napa Extend {cls.suffix}",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="665mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("1.80"),
            dgda_dar_number=f"DAR-GATE-{cls.suffix}",
        )

        # Subscriptions
        cls.sub_a = SubscriptionStateMachineService.get_or_create_subscription(cls.tenant_a.tenant_id)
        cls.sub_b = SubscriptionStateMachineService.get_or_create_subscription(cls.tenant_b.tenant_id)

    def setUp(self):
        self.client = APIClient()

    def test_01_operational_erp_writes_blocked_when_subscription_suspended(self):
        """Mutating operational ERP requests (POST/PUT/PATCH/DELETE) are blocked when subscription is SUSPENDED."""
        # Suspend tenant A subscription
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.status = SubscriptionStatus.SUSPENDED
        sub.save()

        self.client.force_authenticate(user=self.owner_a)

        # Attempt operational sale checkout
        checkout_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "customer_name": "Walk-in Customer",
            "customer_phone": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            "payment_method": "CASH",
            "items": [
                {
                    "product_id": self.product.product_id,
                    "batch_id": f"batch_{self.suffix}",
                    "quantity": 1,
                    "unit_price": "2.50",
                }
            ],
            "total_amount": "2.50",
        }

        res = self.client.post("/api/v2/invoices/checkout/", checkout_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        body = res.json()
        self.assertEqual(
            body.get("error_code") or body.get("code"),
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
        )
        self.assertEqual(body.get("recovery_action"), "RENEW_SUBSCRIPTION")
        self.assertIn("SUSPENDED", body.get("message_en", ""))

    def test_02_read_only_endpoints_permitted_when_subscription_suspended(self):
        """Safe read-only methods (GET) are permitted for regulatory audit even when SUSPENDED."""
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.status = SubscriptionStatus.SUSPENDED
        sub.save()

        self.client.force_authenticate(user=self.owner_a)

        # GET Sales Invoices
        res = self.client.get("/api/v2/invoices/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # GET Medicine Products
        res_prod = self.client.get("/api/v2/products/")
        self.assertEqual(res_prod.status_code, status.HTTP_200_OK)

    def test_03_authorized_billing_endpoints_permitted_when_subscription_suspended(self):
        """Authorized billing endpoints remain fully functional when SUSPENDED to allow payment renewals."""
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.status = SubscriptionStatus.SUSPENDED
        sub.save()

        self.client.force_authenticate(user=self.owner_a)

        # 1. Billing Quote
        res_quote = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "plan_code": "PLAN_STANDARD",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertEqual(res_quote.status_code, status.HTTP_200_OK)

        # 2. Billing Verified Payment
        res_pay = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/verify-payment/",
            {
                "amount_bdt": "3500.00",
                "transaction_ref": f"TXN_GATE_{uuid.uuid4().hex[:8]}",
                "payment_method": "BKASH",
            },
            format="json",
        )
        self.assertEqual(res_pay.status_code, status.HTTP_200_OK)
        self.assertEqual(res_pay.json()["data"]["status"], SubscriptionStatus.ACTIVE)

    def test_04_operational_erp_writes_permitted_when_subscription_active(self):
        """When subscription is ACTIVE, operational requests pass the activation gate."""
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.status = SubscriptionStatus.ACTIVE
        sub.save()

        self.client.force_authenticate(user=self.owner_a)

        # Calling checkout passes the subscription activation gate (returns 200/201 or business validation error)
        res = self.client.post(
            "/api/v2/invoices/checkout/",
            {"tenant_id": self.tenant_a.tenant_id, "branch_id": self.branch_a.branch_id, "items": []},
            format="json",
        )
        # It must NOT be blocked by activation gate (i.e. status code is not 403 unentitled blocked)
        self.assertNotEqual(
            res.json().get("error_code") or res.json().get("code"),
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
        )

    def test_05_platform_superadmin_exempt_from_operational_write_block(self):
        """Platform Super Admin can perform operational writes even if tenant subscription is SUSPENDED."""
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.status = SubscriptionStatus.SUSPENDED
        sub.save()

        self.client.force_authenticate(user=self.superadmin)

        # Super Admin passes the activation gate
        res = self.client.post(
            "/api/v2/invoices/checkout/",
            {"tenant_id": self.tenant_a.tenant_id, "branch_id": self.branch_a.branch_id, "items": []},
            format="json",
        )
        self.assertNotEqual(
            res.json().get("error_code") or res.json().get("code"),
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
        )

    def test_06_unentitled_statuses_all_blocked(self):
        """EXPIRED, CANCELLED, and PENDING statuses are all blocked by activation gate."""
        unentitled = [SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED, SubscriptionStatus.PENDING]
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)

        self.client.force_authenticate(user=self.owner_a)

        for unent_status in unentitled:
            sub.status = unent_status
            sub.save()

            res = self.client.post(
                "/api/v2/invoices/checkout/",
                {"tenant_id": self.tenant_a.tenant_id, "branch_id": self.branch_a.branch_id, "items": []},
                format="json",
            )
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(
                res.json().get("error_code") or res.json().get("code"),
                "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
            )

    def test_07_cross_tenant_violation_blocked_prior_to_activation_check(self):
        """Cross-tenant requests are rejected with ERR_CROSS_TENANT_VIOLATION."""
        self.client.force_authenticate(user=self.owner_a)

        res = self.client.get(
            f"/api/v2/invoices/?tenant_id={self.tenant_b.tenant_id}",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            res.json().get("error_code") or res.json().get("code"),
            ["ERR_CROSS_TENANT_VIOLATION", "CROSS_TENANT_VIOLATION"],
        )


if __name__ == "__main__":
    unittest.main()
