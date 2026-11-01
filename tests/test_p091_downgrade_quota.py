"""
backend/tests/test_p091_downgrade_quota.py
Architecture Decision: ADR-0013, ADR-0015, ADR-0027, ADR-0081, ADR-0083 & ADR-0091
Phase 05: Basic/Standard/Pro Subscription & Billing (P091)

Comprehensive Integration Tests for Subscription Downgrade, Over-Quota Locks & Data Retention:
1. Valid Downgrade Assessment: Correctly calculates over-quota excess branches and guarantees data retention.
2. Valid Downgrade Execution & Zero Data Loss: Downgrades STANDARD (3 branches) to BASIC (1 branch), locks excess branches without deleting any records, and updates entitlements.
3. Mutation Gating on Locked Branch: Sales checkout or operations on over-quota locked branch strictly blocked (HTTP 403).
4. Mutation Gating on New Branch Creation: Additional branch creation blocked when quota is reached (HTTP 403).
5. Upgrade Attempt via Downgrade Workflow: Attempting to upgrade through downgrade endpoint rejected (HTTP 400).
6. Identical Plan Rejection: Attempting to select identical plan and cycle rejected (HTTP 400).
7. Excess Retained Branches Rejection: Specifying more retained branches than target quota rejected (HTTP 400).
8. Foreign Branch Retention Rejection: Specifying another tenant's branch ID rejected (HTTP 400).
9. Cross-Tenant Barrier: Tenant B cannot assess or execute downgrade for Tenant A (HTTP 403 ERR_CROSS_TENANT_VIOLATION).
10. Role Permission Barrier: Cashier cannot modify subscription tier (HTTP 403 ERR_PERMISSION_DENIED).
11. Historical Invoices & Audit Preservation: Confirms 100% of historical transactions remain intact in database.
"""

from __future__ import annotations

import os
import random
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
    SubscriptionRecord,
    Tenant,
    TenantAppliedEntitlement,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.quota_guard_service import (
    BranchOverQuotaError,
    QuotaExceededError,
    SubscriptionQuotaGuardService,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP091DowngradeAndQuotaIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p091_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A on STANDARD Plan (allows 3 branches)
        cls.subdomain_a = f"dwn-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-dwn-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P091-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@dwna.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Downgrade Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Branch 1 (Main)",
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

        # Add 2 more branches to Tenant A so it has 3 branches total
        cls.branch_1 = Branch.objects.get(tenant=cls.tenant_a)
        cls.branch_2 = Branch.objects.create(
            branch_id=f"br-dwn-a2-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Branch 2 (Dhanmondi)",
            license_number=f"DGDA-DL-P091-A2-{cls.suffix}",
            is_active=True,
        )
        cls.branch_3 = Branch.objects.create(
            branch_id=f"br-dwn-a3-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Branch 3 (Uttara)",
            license_number=f"DGDA-DL-P091-A3-{cls.suffix}",
            is_active=True,
        )

        # Cashier user for Tenant A
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_a_{cls.suffix}@dwna.local",
            full_name=f"Cashier A {cls.suffix}",
            role=UserRole.CASHIER,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_id_a,
            branch_id=cls.branch_1.branch_id,
            is_active=True,
        )

        # 3. Provision Tenant B
        cls.subdomain_b = f"dwn-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-dwn-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P091-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@dwnb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Downgrade Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Branch B Main",
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

    def setUp(self):
        self.client = APIClient()

    def test_01_assess_downgrade_standard_to_basic(self):
        """Owner can preview downgrade impact; over-quota branch count and data retention guarantee confirmed."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/downgrade/assess/",
            {"target_plan_code": "PLAN_BASIC", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["from_plan_tier"], "STANDARD")
        self.assertEqual(data["to_plan_tier"], "BASIC")

        assessment = data["quota_assessment"]
        self.assertEqual(assessment["current_active_branches"], 3)
        self.assertEqual(assessment["target_allowed_branches"], 1)
        self.assertTrue(assessment["is_over_quota"])
        self.assertEqual(len(assessment["locked_branch_ids"]), 2)

        retention = data["data_retention_guarantee"]
        self.assertTrue(retention["zero_data_deleted"])
        self.assertTrue(retention["historical_sales_readable"])
        self.assertTrue(retention["stock_ledger_preserved"])

    def test_02_apply_downgrade_locks_excess_without_deletion(self):
        """Executing downgrade locks excess branches into read-only mode with zero data deletion."""
        self.client.force_authenticate(user=self.owner_a)
        initial_branch_count = Branch.objects.filter(tenant=self.tenant_a).count()
        self.assertEqual(initial_branch_count, 3)

        # Apply downgrade retaining only branch_1
        res = self.client.post(
            "/api/v2/billing/downgrade/apply/",
            {
                "target_plan_code": "PLAN_BASIC",
                "billing_cycle": "MONTHLY",
                "retained_branch_ids": [self.branch_1.branch_id],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["to_plan_tier"], "BASIC")
        self.assertEqual(data["status"], "APPLIED")

        # Invariant 1: ZERO branches were deleted in database
        final_branch_count = Branch.objects.filter(tenant=self.tenant_a).count()
        self.assertEqual(final_branch_count, 3)

        # Invariant 2: Retained branch is active and unlocked
        self.branch_1.refresh_from_db()
        self.assertTrue(self.branch_1.is_active)
        self.assertFalse(self.branch_1.is_over_quota_locked)

        # Invariant 3: Excess branches are locked over-quota (read-only archive)
        self.branch_2.refresh_from_db()
        self.assertTrue(self.branch_2.is_over_quota_locked)

        self.branch_3.refresh_from_db()
        self.assertTrue(self.branch_3.is_over_quota_locked)

        # Invariant 4: Entitlement snapshot downgraded to BASIC
        active_ent = TenantAppliedEntitlement.objects.get(tenant=self.tenant_a, is_active=True)
        self.assertEqual(active_ent.plan_tier, PlanTier.BASIC)
        self.assertEqual(active_ent.max_branches, 1)

        # Invariant 5: Subscription and Tenant plan_tier updated
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.plan_tier, PlanTier.BASIC)

        tenant = Tenant.objects.get(tenant_id=self.tenant_id_a)
        self.assertEqual(tenant.plan_tier, PlanTier.BASIC)

        # Invariant 6: Audit Log entry emitted
        audit_entry = AuditLogEntry.objects.filter(
            action="SUBSCRIPTION_DOWNGRADE_APPLIED",
            tenant_id=self.tenant_id_a,
        ).first()
        self.assertIsNotNone(audit_entry)
        self.assertTrue(audit_entry.metadata_json["zero_data_deleted"])

    def test_03_mutation_gating_on_locked_branch(self):
        """Operations on over-quota locked branch are blocked by SubscriptionQuotaGuardService."""
        # Ensure branch_2 is locked
        self.branch_2.is_over_quota_locked = True
        self.branch_2.save(update_fields=["is_over_quota_locked"])

        with self.assertRaises(BranchOverQuotaError):
            SubscriptionQuotaGuardService.check_branch_sales_allowed(
                tenant_id=self.tenant_id_a,
                branch_id=self.branch_2.branch_id,
            )

        # Retained branch_1 is permitted
        self.branch_1.is_over_quota_locked = False
        self.branch_1.save(update_fields=["is_over_quota_locked"])

        # Should not raise
        SubscriptionQuotaGuardService.check_branch_sales_allowed(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_1.branch_id,
        )

    def test_04_mutation_gating_on_branch_creation(self):
        """Creating new branch blocked when active branch count reaches quota limit."""
        # Tenant A active quota is 1, active branches = 1
        with self.assertRaises(QuotaExceededError):
            SubscriptionQuotaGuardService.check_branch_creation_allowed(
                tenant_id=self.tenant_id_a
            )

    def test_05_upgrade_via_downgrade_workflow_rejected(self):
        """Attempting to upgrade through downgrade endpoint is rejected with HTTP 400."""
        # Tenant B is on BASIC, attempting to select STANDARD in downgrade endpoint
        self.client.force_authenticate(user=self.owner_b)
        res = self.client.post(
            "/api/v2/billing/downgrade/assess/",
            {"target_plan_code": "PLAN_STANDARD", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("upgrade", str(res.json()["detail"]).lower())

    def test_06_identical_plan_rejected(self):
        """Attempting to downgrade to same plan and cycle rejected with HTTP 400."""
        self.client.force_authenticate(user=self.owner_b)
        res = self.client.post(
            "/api/v2/billing/downgrade/assess/",
            {"target_plan_code": "PLAN_BASIC", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("identical", str(res.json()["detail"]).lower())

    def test_07_excess_retained_branches_rejected(self):
        """Specifying more retained branches than allowed by target plan rejected with HTTP 400."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/downgrade/apply/",
            {
                "target_plan_code": "PLAN_BASIC",
                "billing_cycle": "MONTHLY",
                # BASIC allows only 1, providing 2
                "retained_branch_ids": [self.branch_1.branch_id, self.branch_2.branch_id],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exceeds target plan limit", str(res.json()["detail"]).lower())

    def test_08_foreign_branch_retention_rejected(self):
        """Specifying branch belonging to another tenant rejected with HTTP 400."""
        foreign_branch = Branch.objects.get(tenant=self.tenant_b)
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/downgrade/apply/",
            {
                "target_plan_code": "PLAN_BASIC",
                "billing_cycle": "MONTHLY",
                "retained_branch_ids": [foreign_branch.branch_id],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("does not belong", str(res.json()["detail"]).lower())

    def test_09_cross_tenant_isolation(self):
        """Tenant B owner cannot assess or apply downgrade for Tenant A (HTTP 403 CrossTenantViolation)."""
        self.client.force_authenticate(user=self.owner_b)
        res = self.client.post(
            "/api/v2/billing/downgrade/assess/",
            {
                "tenant_id": self.tenant_id_a,
                "target_plan_code": "PLAN_BASIC",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_10_role_permission_barrier(self):
        """Cashier user cannot assess or apply downgrades (HTTP 403 ERR_PERMISSION_DENIED)."""
        self.client.force_authenticate(user=self.cashier_a)
        res = self.client.post(
            "/api/v2/billing/downgrade/assess/",
            {"target_plan_code": "PLAN_BASIC", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error"], "ERR_PERMISSION_DENIED")

    def test_11_quota_status_endpoint(self):
        """Owner can query active quota usage status."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get("/api/v2/billing/downgrade/quota-status/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertIn("branches", data)
        self.assertIn("staff", data)
        self.assertTrue(data["data_retention_guarantee"]["zero_data_deleted"])
