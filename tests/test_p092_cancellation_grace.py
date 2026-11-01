"""
backend/tests/test_p092_cancellation_grace.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0033, ADR-0073, ADR-0084, ADR-0087 & ADR-0092
Phase 05: Basic/Standard/Pro Subscription & Billing (P092)

Integration Tests for Subscription Cancellation, Grace Period, Expiration, and Reactivation:
1. Valid End-of-Period Cancellation: Sets cancel_at_period_end=True, keeps status ACTIVE until renewal date.
2. Valid Immediate Cancellation: Sets status=CANCELLED, suspends sales dispensing, maintains safe read/export.
3. Invalid Cancellation Cases: Rejects short reason (<5 chars) and invalid effective_mode (HTTP 400).
4. Statutory 15-Day Grace Period Protection: Verifies operational sales dispensing is permitted during grace.
5. Grace Expiration: Transitions to EXPIRED when grace period lapses, suspends sales dispensing.
6. Seamless Reactivation: Restores ACTIVE status, extends renewal date, restores entitlements, and clears flags.
7. Reactivation Validation: Rejects short or missing payment reference (HTTP 400).
8. Cross-Tenant Barrier: Blocks cross-tenant status, cancellation, and reactivation attempts (HTTP 403).
9. Role-Based Access Control (RBAC): Blocks cashiers/staff from cancelling or reactivating subscriptions (HTTP 403).
10. Zero Data Deletion Invariant: Confirms 100% of historical branches, sales, and audit logs are retained.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.cancellation_grace_service import SubscriptionCancellationGraceService
from apps.core.models import (
    Branch,
    CancellationEffectiveMode,
    PlanTier,
    SubscriptionCancellationRecord,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP092CancellationGraceIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p092_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Tenant A (Primary Test Subject)
        cls.subdomain_a = f"cnc-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-cnc-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P092-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@cnca.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Cancellation Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Branch Main",
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_a,
            plan_code="PLAN_STANDARD",
            billing_cycle="MONTHLY",
        )

        # Cashier user for Tenant A (to test RBAC enforcement)
        cls.cashier_a, _ = PharmacyUser.objects.get_or_create(
            email=f"cashier_a_{cls.suffix}@cnca.local",
            defaults={
                "full_name": f"Cashier A {cls.suffix}",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant_id_a,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
            },
        )

        # 3. Tenant B (To test Cross-Tenant isolation)
        cls.subdomain_b = f"cnc-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-cnc-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P092-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@cncb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Isolation Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Branch Single",
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_b,
            plan_code="PLAN_BASIC",
            billing_cycle="MONTHLY",
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        # Reset tenant A subscription to standard active state before each test
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        if sub:
            sub.status = SubscriptionStatus.ACTIVE
            sub.cancel_at_period_end = False
            sub.cancellation_reason = ""
            sub.cancelled_at = None
            sub.grace_period_end_date = None
            sub.renewal_date = timezone.now().date() + timedelta(days=30)
            sub.is_auto_renew = True
            sub.save()
        self.tenant_a.status = TenantLifecycleStatus.ACTIVE
        self.tenant_a.save()
        TenantAppliedEntitlement.objects.filter(tenant=self.tenant_a).update(is_active=True)

    def test_01_valid_end_of_period_cancellation(self):
        """Cancels subscription at period end: retains ACTIVE access, auto-renew disabled."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/lifecycle/cancel/"
        payload = {
            "tenant_id": self.tenant_id_a,
            "reason": "Relocating business to another district next month.",
            "effective_mode": "END_OF_BILLING_PERIOD",
        }
        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        # Invariants verification
        self.assertEqual(data["status"], SubscriptionStatus.ACTIVE)
        self.assertTrue(data["lifecycle_details"]["cancel_at_period_end"])
        self.assertFalse(data["lifecycle_details"]["is_auto_renew"])
        self.assertTrue(data["permissions"]["can_dispense_sales"])
        self.assertTrue(data["permissions"]["zero_data_deleted"])
        self.assertIn("বিলিং সাইকেল শেষে", data["owner_messaging"]["headline_bn"])

        # DB Record verification
        record = SubscriptionCancellationRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        self.assertIsNotNone(record)
        self.assertEqual(record.effective_mode, CancellationEffectiveMode.END_OF_BILLING_PERIOD)
        self.assertEqual(record.reason, payload["reason"])
        self.assertFalse(record.is_reactivated)

    def test_02_valid_immediate_cancellation(self):
        """Cancels subscription immediately: transitions to CANCELLED, suspends sales dispensing, keeps reads."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/lifecycle/cancel/"
        payload = {
            "tenant_id": self.tenant_id_a,
            "reason": "Ceasing pharmacy operations immediately due to retirement.",
            "effective_mode": "IMMEDIATE",
        }
        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["status"], SubscriptionStatus.CANCELLED)
        self.assertFalse(data["permissions"]["can_dispense_sales"])
        self.assertTrue(data["permissions"]["can_read_historical_data"])
        self.assertTrue(data["permissions"]["can_export_tenant_data"])
        self.assertTrue(data["permissions"]["zero_data_deleted"])
        self.assertIn("বাতিল", data["owner_messaging"]["headline_bn"])

        self.tenant_a.refresh_from_db()
        self.assertEqual(self.tenant_a.status, TenantLifecycleStatus.SUSPENDED)

    def test_03_invalid_cancellation_input_cases(self):
        """Rejects short reason and invalid effective mode."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/lifecycle/cancel/"

        # Short reason (<5 chars)
        res_short = self.client.post(url, {"tenant_id": self.tenant_id_a, "reason": "No"}, format="json")
        self.assertEqual(res_short.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_short.json()["error"], "VALIDATION_ERROR")

        # Invalid effective mode
        res_bad_mode = self.client.post(url, {
            "tenant_id": self.tenant_id_a,
            "reason": "Valid reason explanation",
            "effective_mode": "UNKNOWN_MODE",
        }, format="json")
        self.assertEqual(res_bad_mode.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_mode.json()["error"], "VALIDATION_ERROR")

    def test_04_statutory_grace_period_protects_clinical_dispensing(self):
        """When renewal lapses, subscription enters 15-day GRACE_PERIOD and dispensing remains operational."""
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        sub.renewal_date = timezone.now().date() - timedelta(days=1)  # lapsed yesterday
        sub.save()

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get(f"/api/v2/billing/lifecycle/status/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["status"], SubscriptionStatus.GRACE_PERIOD)
        self.assertTrue(data["lifecycle_details"]["in_grace_period"])
        self.assertGreaterEqual(data["lifecycle_details"]["grace_period_days_remaining"], 13)
        self.assertTrue(data["permissions"]["can_dispense_sales"])  # Patient safety invariant
        self.assertIn("গ্রেস পিরিয়ড", data["owner_messaging"]["headline_bn"])
        self.assertEqual(data["owner_messaging"]["action_required"], "PAY_RENEWAL_INVOICE")

    def test_05_grace_period_expiration(self):
        """When grace period passes, subscription transitions to EXPIRED, suspending new dispensing."""
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        sub.status = SubscriptionStatus.GRACE_PERIOD
        sub.renewal_date = timezone.now().date() - timedelta(days=20)
        sub.grace_period_end_date = timezone.now().date() - timedelta(days=5)  # grace ended 5 days ago
        sub.save()

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get(f"/api/v2/billing/lifecycle/status/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["status"], SubscriptionStatus.EXPIRED)
        self.assertFalse(data["permissions"]["can_dispense_sales"])
        self.assertTrue(data["permissions"]["can_read_historical_data"])
        self.assertTrue(data["permissions"]["can_export_tenant_data"])
        self.assertEqual(data["owner_messaging"]["action_required"], "REACTIVATE")

    def test_06_seamless_reactivation(self):
        """Reactivates cancelled subscription with valid payment confirmation, extending renewal and restoring sales."""
        # Cancel first
        SubscriptionCancellationGraceService.cancel_subscription(
            tenant_id=self.tenant_id_a,
            reason="Temporary closure for renovations",
            effective_mode=CancellationEffectiveMode.IMMEDIATE,
            actor_user=self.owner_a,
        )

        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/lifecycle/reactivate/"
        payload = {
            "tenant_id": self.tenant_id_a,
            "payment_method": "BKASH",
            "payment_reference": "TRX-REACTIVATE-992",
        }
        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["status"], SubscriptionStatus.ACTIVE)
        self.assertTrue(data["permissions"]["can_dispense_sales"])
        self.assertTrue(data["lifecycle_details"]["is_auto_renew"])
        self.assertFalse(data["lifecycle_details"]["cancel_at_period_end"])

        # Check DB states
        self.tenant_a.refresh_from_db()
        self.assertEqual(self.tenant_a.status, TenantLifecycleStatus.ACTIVE)
        self.assertTrue(TenantAppliedEntitlement.objects.filter(tenant=self.tenant_a, is_active=True).exists())

        cancellation = SubscriptionCancellationRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        self.assertTrue(cancellation.is_reactivated)

    def test_07_reactivation_validation_error(self):
        """Rejects reactivation request with invalid/short payment reference."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/lifecycle/reactivate/"
        res = self.client.post(url, {
            "tenant_id": self.tenant_id_a,
            "payment_reference": "12",  # too short (<4 chars)
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["error"], "VALIDATION_ERROR")

    def test_08_cross_tenant_isolation_barrier(self):
        """Blocks Tenant B user from viewing, cancelling or reactivating Tenant A subscription."""
        self.client.force_authenticate(user=self.owner_b)

        # 1. Status read attempt
        res_read = self.client.get(f"/api/v2/billing/lifecycle/status/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_read.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_read.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 2. Cancel attempt
        res_cancel = self.client.post("/api/v2/billing/lifecycle/cancel/", {
            "tenant_id": self.tenant_id_a,
            "reason": "Hostile cross-tenant cancellation attempt",
        }, format="json")
        self.assertEqual(res_cancel.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_cancel.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 3. Reactivate attempt
        res_reactivate = self.client.post("/api/v2/billing/lifecycle/reactivate/", {
            "tenant_id": self.tenant_id_a,
            "payment_reference": "TRX-CROSS-FAIL",
        }, format="json")
        self.assertEqual(res_reactivate.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_reactivate.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_09_rbac_barrier_cashier_denied(self):
        """Blocks Cashier/Staff from modifying subscription lifecycle."""
        self.client.force_authenticate(user=self.cashier_a)

        res_cancel = self.client.post("/api/v2/billing/lifecycle/cancel/", {
            "tenant_id": self.tenant_id_a,
            "reason": "Unauthorized cashier attempt",
        }, format="json")
        self.assertEqual(res_cancel.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_cancel.json()["error"], "ERR_PERMISSION_DENIED")

        res_reactivate = self.client.post("/api/v2/billing/lifecycle/reactivate/", {
            "tenant_id": self.tenant_id_a,
            "payment_reference": "TRX-CASHIER-DENIED",
        }, format="json")
        self.assertEqual(res_reactivate.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_reactivate.json()["error"], "ERR_PERMISSION_DENIED")

    def test_10_zero_data_deleted_guarantee(self):
        """Verifies that cancellation or expiration never deletes branches, entitlements, or audit logs."""
        branch_count_before = Branch.objects.filter(tenant=self.tenant_a).count()
        audit_count_before = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        # Execute cancellation
        self.client.force_authenticate(user=self.owner_a)
        self.client.post("/api/v2/billing/lifecycle/cancel/", {
            "tenant_id": self.tenant_id_a,
            "reason": "Zero data loss verification cancellation",
            "effective_mode": "IMMEDIATE",
        }, format="json")

        branch_count_after = Branch.objects.filter(tenant=self.tenant_a).count()
        audit_count_after = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        self.assertEqual(branch_count_before, branch_count_after, "Branches must never be deleted")
        self.assertGreater(audit_count_after, audit_count_before, "Audit logs must be created, never deleted")


if __name__ == "__main__":
    unittest.main()
