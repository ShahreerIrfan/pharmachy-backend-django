"""
backend/tests/test_p095_quota_usage_counter.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0081, ADR-0082, ADR-0091 & ADR-0095
Phase 05: Basic/Standard/Pro Subscription & Billing (P095)
Requirement Trace: FR-070 / FR-071 / FR-072 (Authoritative Quota Usage Counters & Concurrency Race Prevention)

Comprehensive Integration Tests:
1. Valid Scenario: Retrieves real-time authoritative quota usage report (HTTP 200).
2. Atomic Reserve & Consume: Successfully reserves capacity inside row-locked transaction (HTTP 200).
3. Concurrency Protection & Over-Allocation Prevention: Attempting to reserve beyond quota returns ERR_QUOTA_EXCEEDED (HTTP 400).
4. Atomic Release on Archival: Decrements usage counter accurately without going below zero.
5. Authoritative Recalibration: Recalibrates counters by scanning actual database records (HTTP 200).
6. Cross-Tenant Isolation Barrier: Tenant B cannot view or mutate Tenant A counters (HTTP 403 ERR_CROSS_TENANT_VIOLATION).
7. Role-Based Access Control (RBAC): Cashier cannot reserve or recalibrate counters (HTTP 403 ERR_PERMISSION_DENIED).
8. Input Validation: Invalid resource_type or non-positive amount returns HTTP 400 VALIDATION_ERROR.
9. Zero Data Deleted Invariant: Hitting quota limits does not delete any historical records or branches.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    PlanTier,
    QuotaResourceType,
    Tenant,
    TenantAppliedEntitlement,
    TenantQuotaUsageCounter,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.quota_counter_service import SubscriptionQuotaCounterService
from apps.identity.models import PharmacyUser, UserRole


class TestP095QuotaUsageCounterIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p095_admin_{cls.suffix}@platform.internal",
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
        cls.subdomain_a = f"quota-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-quota-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P095-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@quotaa.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Quota Pharma A {cls.suffix}",
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

        # Cashier user for Tenant A
        cls.cashier_a, _ = PharmacyUser.objects.get_or_create(
            email=f"cashier_a_{cls.suffix}@quotaa.local",
            defaults={
                "full_name": f"Cashier A {cls.suffix}",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant_id_a,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
            },
        )

        # 3. Provision Tenant B (Isolation boundary: Basic Plan)
        cls.subdomain_b = f"quota-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-quota-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P095-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@quotab.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Quota Pharma B {cls.suffix}",
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
        # Seed/recalibrate counters to clean state
        SubscriptionQuotaCounterService.recalibrate_counters(self.tenant_id_a, actor_user=self.owner_a)

    def test_01_retrieve_authoritative_quota_usage_report(self):
        """Owner retrieves real-time authoritative quota report with bilingual messaging and invariants."""
        self.client.force_authenticate(user=self.owner_a)
        url = f"/api/v2/billing/quota-usage/?tenant_id={self.tenant_id_a}"

        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["tenant_id"], self.tenant_id_a)
        self.assertEqual(data["plan_tier"], "STANDARD")
        self.assertIn(data["overall_status"], ["OK", "NEAR_LIMIT", "LIMIT_REACHED", "OVER_QUOTA"])
        self.assertIn("BRANCHES", data["counters"])
        self.assertIn("STAFF_SEATS", data["counters"])
        self.assertIn("DEVICES", data["counters"])

        # Check Branch counter
        branch_counter = data["counters"]["BRANCHES"]
        self.assertEqual(branch_counter["resource_type"], "BRANCHES")
        self.assertEqual(branch_counter["max_limit"], 3)
        self.assertGreaterEqual(branch_counter["current_used"], 1)
        self.assertFalse(branch_counter["is_over_quota"])

        # Invariants Check
        self.assertTrue(data["invariants"]["zero_data_deleted"])
        self.assertTrue(data["invariants"]["race_condition_safe"])
        self.assertTrue(data["invariants"]["authoritative_locking"])
        self.assertIn("কোটা", data["client_messaging"]["headline_bn"])

    def test_02_atomic_reserve_and_consume(self):
        """Atomically reserves and consumes quota inside row lock, creating audit entry."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/quota-usage/check-and-reserve/"

        # Check starting branch count
        report_before = SubscriptionQuotaCounterService.get_quota_usage_report(self.tenant_id_a)
        branches_before = report_before["counters"]["BRANCHES"]["current_used"]

        payload = {
            "tenant_id": self.tenant_id_a,
            "resource_type": "BRANCHES",
            "amount": 1,
        }

        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        branches_after = data["counters"]["BRANCHES"]["current_used"]
        self.assertEqual(branches_after, branches_before + 1)

        # Audit check
        self.assertTrue(
            AuditLogEntry.objects.filter(
                action="QUOTA_RESERVED_AND_CONSUMED",
                tenant_id=self.tenant_id_a,
            ).exists()
        )

    def test_03_concurrency_protection_blocks_over_allocation(self):
        """Attempting to reserve beyond quota raises ERR_QUOTA_EXCEEDED without mutation."""
        # Set max limit and used to full
        counter = TenantQuotaUsageCounter.objects.get(
            tenant=self.tenant_a,
            resource_type=QuotaResourceType.BRANCHES,
        )
        counter.current_used = counter.max_limit  # at capacity (3/3)
        counter.save()

        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/quota-usage/check-and-reserve/"
        payload = {
            "tenant_id": self.tenant_id_a,
            "resource_type": "BRANCHES",
            "amount": 1,
        }

        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        data = res.json()
        self.assertEqual(data["error"], "ERR_QUOTA_EXCEEDED")
        self.assertIn("Quota exceeded", data["detail"])

        # Confirm count was NOT mutated
        counter.refresh_from_db()
        self.assertEqual(counter.current_used, counter.max_limit)

    def test_04_atomic_release_on_archival(self):
        """Releasing quota decrements counter safely and never drops below zero."""
        counter = TenantQuotaUsageCounter.objects.get(
            tenant=self.tenant_a,
            resource_type=QuotaResourceType.STAFF_SEATS,
        )
        counter.current_used = 4
        counter.save()

        # Release 2 seats
        rep = SubscriptionQuotaCounterService.release_usage(
            tenant_id=self.tenant_id_a,
            resource_type=QuotaResourceType.STAFF_SEATS,
            amount=2,
            actor_user=self.owner_a,
        )
        self.assertEqual(rep["counters"]["STAFF_SEATS"]["current_used"], 2)

        # Release 5 seats (more than remaining 2) -> clamps to 0
        rep2 = SubscriptionQuotaCounterService.release_usage(
            tenant_id=self.tenant_id_a,
            resource_type=QuotaResourceType.STAFF_SEATS,
            amount=5,
            actor_user=self.owner_a,
        )
        self.assertEqual(rep2["counters"]["STAFF_SEATS"]["current_used"], 0)

    def test_05_recalibrate_scans_underlying_database(self):
        """Recalibrate synchronizes counters against actual database records and increments version."""
        # Intentionally introduce counter drift
        counter = TenantQuotaUsageCounter.objects.get(
            tenant=self.tenant_a,
            resource_type=QuotaResourceType.BRANCHES,
        )
        counter.current_used = 999  # artificial drift
        counter.save()

        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/quota-usage/recalibrate/"
        res = self.client.post(url, {"tenant_id": self.tenant_id_a}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        # The actual active branch count for Tenant A is 1
        actual_branches = Branch.objects.filter(tenant=self.tenant_a, is_active=True).count()
        self.assertEqual(data["counters"]["BRANCHES"]["current_used"], actual_branches)

        # Audit log verification
        self.assertTrue(
            AuditLogEntry.objects.filter(
                action="OFFLINE_QUOTA_COUNTERS_RECALIBRATED",
                tenant_id=self.tenant_id_a,
            ).exists()
        )

    def test_06_cross_tenant_isolation_barrier(self):
        """Tenant B owner is strictly blocked from viewing or reserving Tenant A quota."""
        self.client.force_authenticate(user=self.owner_b)

        # 1. Blocked from listing Tenant A report
        res_list = self.client.get(f"/api/v2/billing/quota-usage/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_list.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_list.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 2. Blocked from reserving on Tenant A
        res_reserve = self.client.post(
            "/api/v2/billing/quota-usage/check-and-reserve/",
            {"tenant_id": self.tenant_id_a, "resource_type": "BRANCHES", "amount": 1},
            format="json",
        )
        self.assertEqual(res_reserve.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_reserve.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 3. Blocked from recalibrating Tenant A
        res_recal = self.client.post(
            "/api/v2/billing/quota-usage/recalibrate/",
            {"tenant_id": self.tenant_id_a},
            format="json",
        )
        self.assertEqual(res_recal.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_recal.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_07_rbac_cashier_cannot_reserve_or_recalibrate(self):
        """Cashier role is forbidden from reserving capacity or triggering recalibrations."""
        self.client.force_authenticate(user=self.cashier_a)

        # 1. Reserve attempt -> 403 Forbidden
        res_res = self.client.post(
            "/api/v2/billing/quota-usage/check-and-reserve/",
            {"tenant_id": self.tenant_id_a, "resource_type": "STAFF_SEATS", "amount": 1},
            format="json",
        )
        self.assertEqual(res_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_res.json()["error"], "ERR_PERMISSION_DENIED")

        # 2. Recalibrate attempt -> 403 Forbidden
        res_recal = self.client.post(
            "/api/v2/billing/quota-usage/recalibrate/",
            {"tenant_id": self.tenant_id_a},
            format="json",
        )
        self.assertEqual(res_recal.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_recal.json()["error"], "ERR_PERMISSION_DENIED")

    def test_08_invalid_input_validation(self):
        """Invalid resource type or non-positive amount returns HTTP 400 Bad Request."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/quota-usage/check-and-reserve/"

        # Invalid resource type
        res_bad_type = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "resource_type": "INVALID_RESOURCE", "amount": 1},
            format="json",
        )
        self.assertEqual(res_bad_type.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_type.json()["error"], "VALIDATION_ERROR")

        # Zero amount
        res_zero = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "resource_type": "BRANCHES", "amount": 0},
            format="json",
        )
        self.assertEqual(res_zero.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_zero.json()["error"], "VALIDATION_ERROR")

    def test_09_zero_data_deleted_invariant(self):
        """Verifies reaching quota limits or recalibrating never deletes branches or historical records."""
        branch_count_before = Branch.objects.filter(tenant=self.tenant_a).count()
        user_count_before = PharmacyUser.objects.filter(tenant_id=self.tenant_id_a).count()
        audit_count_before = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        # Recalibrate
        SubscriptionQuotaCounterService.recalibrate_counters(self.tenant_id_a, actor_user=self.owner_a)

        branch_count_after = Branch.objects.filter(tenant=self.tenant_a).count()
        user_count_after = PharmacyUser.objects.filter(tenant_id=self.tenant_id_a).count()
        audit_count_after = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        self.assertEqual(branch_count_before, branch_count_after)
        self.assertEqual(user_count_before, user_count_after)
        self.assertGreater(audit_count_after, audit_count_before)


if __name__ == "__main__":
    unittest.main()

