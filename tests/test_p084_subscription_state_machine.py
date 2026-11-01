"""
backend/tests/test_p084_subscription_state_machine.py
Architecture Decision: ADR-0013, ADR-0015, ADR-0033 & ADR-0084
Phase 05: Basic/Standard/Pro Subscription & Billing (P084)

Comprehensive Integration Tests for Subscription State Machine:
1. Valid Scenario: Subscription details retrieval and full guarded lifecycle progression.
2. Verified Payment Settlement: Payment verification transitions delinquent/suspended subscriptions back to ACTIVE and extends renewal date.
3. Invariant INV-SAFETY-01: Clinical safety controls remain entitled even when subscription is SUSPENDED.
4. Invalid/Incomplete Inputs: Invalid transitions or missing reasons for adverse states rejected with 400/422.
5. Cross-Tenant Security: Cross-tenant transition attempts blocked with HTTP 403 ERR_CROSS_TENANT_VIOLATION.
6. Role-Based Access Control: Non-owner staff member blocked with HTTP 403 ROLE_PERMISSION_DENIED.
7. Audited History Timeline: Transition events recorded in immutable audit log.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.core.feature_entitlement_service import FeatureEntitlementEngine
from apps.core.models import (
    Branch,
    PlanTier,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.subscription_state_machine_service import SubscriptionStateMachineService
from apps.identity.models import PharmacyUser, UserRole


class TestP084SubscriptionStateMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p084_admin_{cls.suffix}@platform.internal",
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
            tenant_id=f"t-p084-a-{cls.suffix}",
            name=f"Dhaka Central Pharma {cls.suffix}",
            subdomain=f"sub-a-{cls.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"br-a-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Branch",
            is_active=True,
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p084_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )
        cls.staff_a = PharmacyUser.objects.create(
            email=f"p084_staff_a_{cls.suffix}@pharmacy.com",
            full_name=f"Staff A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.CASHIER,
            is_active=True,
        )

        # 3. Tenant B (Chittagong Pharma)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p084-b-{cls.suffix}",
            name=f"Chittagong Pharma {cls.suffix}",
            subdomain=f"sub-b-{cls.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p084_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # Bootstrap subscriptions
        cls.sub_a = SubscriptionStateMachineService.get_or_create_subscription(cls.tenant_a.tenant_id)
        cls.sub_b = SubscriptionStateMachineService.get_or_create_subscription(cls.tenant_b.tenant_id)

    def setUp(self):
        self.client = APIClient()

    def test_01_retrieve_subscription_details(self):
        """Authenticated tenant owner can retrieve subscription details and permitted transitions."""
        self.client.force_authenticate(user=self.owner_a)

        res = self.client.get(f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertEqual(data["tenant_id"], self.tenant_a.tenant_id)
        self.assertEqual(data["status"], SubscriptionStatus.ACTIVE)
        self.assertTrue(data["is_checkout_allowed"])
        self.assertFalse(data["is_delinquent"])
        self.assertIn("PAST_DUE", data["allowed_next_transitions"])
        self.assertIn("GRACE_PERIOD", data["allowed_next_transitions"])
        self.assertIn("SUSPENDED", data["allowed_next_transitions"])
        self.assertIn("CANCELLED", data["allowed_next_transitions"])

    def test_02_valid_lifecycle_progression_and_safety_invariant(self):
        """Super Admin can transition subscription through PAST_DUE -> GRACE_PERIOD -> SUSPENDED."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Transition ACTIVE -> PAST_DUE
        res1 = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/transition/",
            {"target_status": SubscriptionStatus.PAST_DUE, "reason": "Monthly renewal charge failed"},
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(res1.json()["data"]["status"], SubscriptionStatus.PAST_DUE)
        self.assertTrue(res1.json()["data"]["is_delinquent"])
        self.assertTrue(res1.json()["data"]["is_checkout_allowed"])

        # 2. Transition PAST_DUE -> GRACE_PERIOD
        res2 = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/transition/",
            {"target_status": SubscriptionStatus.GRACE_PERIOD, "reason": "Enters 15-day grace period"},
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.json()["data"]["status"], SubscriptionStatus.GRACE_PERIOD)
        self.assertIsNotNone(res2.json()["data"]["grace_period_end_date"])
        self.assertTrue(res2.json()["data"]["is_checkout_allowed"])

        # 3. Transition GRACE_PERIOD -> SUSPENDED
        res3 = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/transition/",
            {"target_status": SubscriptionStatus.SUSPENDED, "reason": "Grace period elapsed without settlement"},
            format="json",
        )
        self.assertEqual(res3.status_code, status.HTTP_200_OK)
        self.assertEqual(res3.json()["data"]["status"], SubscriptionStatus.SUSPENDED)
        self.assertFalse(res3.json()["data"]["is_checkout_allowed"])

        # 4. Invariant INV-SAFETY-01: Verify feature entitlement engine under SUSPENDED state
        entitlements = FeatureEntitlementEngine.evaluate_effective_entitlements(self.tenant_a.tenant_id)
        self.assertFalse(entitlements["subscription"]["is_checkout_allowed"])
        self.assertFalse(entitlements["capabilities"]["CHECKOUT_DISPENSING"])
        self.assertEqual(entitlements["offline_lease"]["lease_status"], "SUSPENDED")

        # Crucial check: Clinical safety features are STILL TRUE (INV-SAFETY-01)
        self.assertTrue(entitlements["capabilities"]["ANTIBIOTIC_REGISTER"])
        self.assertTrue(entitlements["capabilities"]["BATCH_EXPIRY_BLOCK"])
        self.assertTrue(entitlements["capabilities"]["RECALL_QUARANTINE"])
        self.assertTrue(entitlements["capabilities"]["BANNED_DRUG_RESTRICTION"])
        self.assertTrue(entitlements["capabilities"]["RX_VERIFICATION"])

    def test_03_verified_payment_restores_delinquent_subscription(self):
        """Verified payment automatically reactivates delinquent/suspended subscription and extends renewal date."""
        self.client.force_authenticate(user=self.superadmin)

        # Ensure tenant A subscription is SUSPENDED first
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.status = SubscriptionStatus.SUSPENDED
        sub.save()

        # Process verified payment
        payment_payload = {
            "amount_bdt": "3500.00",
            "transaction_ref": f"TXN_BKASH_{uuid.uuid4().hex[:8]}",
            "payment_method": "BKASH",
            "notes": "Verified bKash merchant gateway subscription renewal",
        }

        res = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/verify-payment/",
            payment_payload,
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertEqual(data["status"], SubscriptionStatus.ACTIVE)
        self.assertIsNotNone(data["renewal_date"])

        # Verify DB records
        sub.refresh_from_db()
        self.assertEqual(sub.status, SubscriptionStatus.ACTIVE)
        self.assertIsNone(sub.grace_period_end_date)

        # Duplicate payment reference rejected
        res_dup = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/verify-payment/",
            payment_payload,
            format="json",
        )
        self.assertIn(res_dup.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_04_invalid_state_transitions_and_missing_reasons_rejected(self):
        """Invalid state transitions or missing reasons for adverse states are rejected."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Invalid transition from ACTIVE directly to TRIAL
        res1 = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/transition/",
            {"target_status": SubscriptionStatus.TRIAL, "reason": "Not permitted from active"},
            format="json",
        )
        self.assertIn(res1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 2. Missing reason when transitioning to SUSPENDED
        res2 = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/transition/",
            {"target_status": SubscriptionStatus.SUSPENDED, "reason": ""},
            format="json",
        )
        self.assertIn(res2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_05_cross_tenant_isolation_blocked(self):
        """Owner of Tenant A cannot mutate or access Tenant B's subscription."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Read
        res_read = self.client.get(f"/api/v2/billing/subscriptions/{self.tenant_b.tenant_id}/")
        self.assertEqual(res_read.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            res_read.json().get("error_code") or res_read.json().get("code"),
            ["ERR_CROSS_TENANT_VIOLATION", "CROSS_TENANT_VIOLATION"],
        )

        # 2. Transition
        res_trans = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_b.tenant_id}/transition/",
            {"target_status": SubscriptionStatus.CANCELLED, "reason": "Malicious cancellation attempt"},
            format="json",
        )
        self.assertEqual(res_trans.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            res_trans.json().get("error_code") or res_trans.json().get("code"),
            ["ERR_CROSS_TENANT_VIOLATION", "CROSS_TENANT_VIOLATION"],
        )

    def test_06_non_owner_role_permission_denied(self):
        """Cashier staff member is forbidden from managing subscription transitions."""
        self.client.force_authenticate(user=self.staff_a)

        res = self.client.post(
            f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/transition/",
            {"target_status": SubscriptionStatus.CANCELLED, "reason": "Cashier unauthorized attempt"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            res.json().get("error_code") or res.json().get("code"),
            ["ROLE_PERMISSION_DENIED", "ERR_PERMISSION_DENIED"],
        )

    def test_07_transition_history_audit_trail(self):
        """Audited timeline of subscription transitions can be retrieved."""
        self.client.force_authenticate(user=self.owner_a)

        res = self.client.get(f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/history/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)


if __name__ == "__main__":
    unittest.main()
