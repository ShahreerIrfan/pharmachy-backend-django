"""
Unit tests for P015 - Feature Package Taxonomy and Clinical Safety Guarantees
Validates:
1. One valid scenario: Clinical safety controls enabled on ALL plans; commercial differentiation and safe downgrade.
2. One invalid/incomplete input case: PlanQuota validation rejection, device quota exceeded, and clinical gating error.
3. One unauthorized/cross-tenant case: Cross-tenant subscription mutation blocked with PermissionError.
4. Source-specific business rule: Clinical safety is never placed behind a paid feature gate.
"""

import unittest
from decimal import Decimal

from apps.core.plans import (
    CLINICALLY_PROTECTED_FEATURES,
    ClinicalFeatureGatingViolationError,
    PlanQuota,
    PlanQuotaExceededError,
    PlanTaxonomyManager,
    PlanTier,
    SubscriptionStatus,
    TenantSubscriptionState,
)


class TestFeaturePackageTaxonomy(unittest.TestCase):
    """Verifies plan quotas, feature entitlement, downgrade policies, and clinical safety invariants."""

    def setUp(self):
        self.mgr = PlanTaxonomyManager()

    def test_valid_scenario_clinical_safety_never_gated(self):
        """Source-specific invariant: Clinical safety features must be enabled across ALL plans, even Basic."""
        for tier in (PlanTier.BASIC, PlanTier.STANDARD, PlanTier.PRO):
            for feature in CLINICALLY_PROTECTED_FEATURES:
                self.assertTrue(
                    self.mgr.verify_entitlement(tier, feature),
                    f"Clinical safety feature '{feature}' MUST be available in {tier.value} plan!",
                )

    def test_valid_scenario_commercial_tiering_and_downgrade(self):
        """Acceptance Criteria: One valid scenario. Verifies feature gating and safe downgrade soft-lock."""
        # 1. Commercial feature gating
        self.assertFalse(self.mgr.verify_entitlement(PlanTier.BASIC, "CONSOLIDATED_REPORTING"))
        self.assertFalse(self.mgr.verify_entitlement(PlanTier.STANDARD, "CONSOLIDATED_REPORTING"))
        self.assertTrue(self.mgr.verify_entitlement(PlanTier.PRO, "CONSOLIDATED_REPORTING"))

        # 2. Branch provisioning within quota
        sub_standard = TenantSubscriptionState(
            tenant_id="tenant-dhaka-001",
            plan_tier=PlanTier.STANDARD,
            active_branch_count=2,  # Standard allows up to 3
            active_device_count=4,
        )
        self.assertTrue(self.mgr.can_provision_branch(sub_standard))

        # 3. Device provisioning within quota (Standard allows up to 5 devices per branch)
        self.assertTrue(self.mgr.can_provision_device(sub_standard, branch_devices_count=3))

        # 4. Safe downgrade: Pro tenant with 5 branches downgrades to Standard (max 3)
        sub_pro = TenantSubscriptionState(
            tenant_id="tenant-ctg-002",
            plan_tier=PlanTier.PRO,
            active_branch_count=5,
            active_device_count=10,
        )
        downgraded = self.mgr.execute_downgrade(
            subscription=sub_pro,
            target_tier=PlanTier.STANDARD,
            actor_role="TENANT_OWNER",
            actor_tenant_id="tenant-ctg-002",
        )
        self.assertEqual(downgraded.plan_tier, PlanTier.STANDARD)
        self.assertEqual(downgraded.status, SubscriptionStatus.OVER_QUOTA)
        self.assertEqual(downgraded.grace_period_days_remaining, 7)
        self.assertEqual(downgraded.active_branch_count, 5, "Historical branches must NEVER be silently deleted!")

    def test_invalid_input_and_quota_exceeded_rejection(self):
        """Acceptance Criteria: One invalid or incomplete input case."""
        # 1. Invalid PlanQuota with negative pricing
        with self.assertRaises(ValueError) as ctx:
            bad_quota = PlanQuota(
                max_branches=1,
                max_devices_per_branch=1,
                max_staff_users=3,
                monthly_price_bdt=Decimal("-500.00"),
            )
            bad_quota.validate()
        self.assertIn("cannot be negative", str(ctx.exception))

        # 2. Invalid PlanQuota with zero branches
        with self.assertRaises(ValueError) as ctx:
            bad_quota = PlanQuota(
                max_branches=0,
                max_devices_per_branch=1,
                max_staff_users=3,
                monthly_price_bdt=Decimal("1500.00"),
            )
            bad_quota.validate()
        self.assertIn("max_branches must be positive", str(ctx.exception))

        # 3. Branch Quota exceeded: Basic tenant with 1 branch tries to provision another
        sub_basic = TenantSubscriptionState(
            tenant_id="tenant-rural-003",
            plan_tier=PlanTier.BASIC,
            active_branch_count=1,
            active_device_count=1,
        )
        with self.assertRaises(PlanQuotaExceededError) as ctx:
            self.mgr.can_provision_branch(sub_basic)
        self.assertIn("Branch limit reached", str(ctx.exception))

        # 4. Device Quota exceeded: Basic tenant with 1 device tries to provision second device for branch
        with self.assertRaises(PlanQuotaExceededError) as ctx:
            self.mgr.can_provision_device(sub_basic, branch_devices_count=1)
        self.assertIn("Device limit reached", str(ctx.exception))

        # 5. Attempting to gate a clinically protected safety feature raises ClinicalFeatureGatingViolationError
        with self.assertRaises(ClinicalFeatureGatingViolationError) as ctx:
            self.mgr.assert_feature_not_clinically_gated("ANTIBIOTIC_REGISTER")
        self.assertIn("is a clinically protected safety control", str(ctx.exception))

    def test_unauthorized_and_cross_tenant_subscription_mutation(self):
        """Acceptance Criteria: One unauthorized or cross-tenant case."""
        sub_dhaka = TenantSubscriptionState(
            tenant_id="tenant-dhaka-001",
            plan_tier=PlanTier.STANDARD,
            active_branch_count=2,
            active_device_count=4,
        )

        # Cross-tenant downgrade attempt: Chittagong owner attempts to alter Dhaka's plan -> Blocked!
        with self.assertRaises(PermissionError) as ctx:
            self.mgr.execute_downgrade(
                subscription=sub_dhaka,
                target_tier=PlanTier.BASIC,
                actor_role="TENANT_OWNER",
                actor_tenant_id="tenant-ctg-002",  # Different tenant!
            )
        self.assertIn("cannot alter subscription for tenant 'tenant-dhaka-001'", str(ctx.exception))

        # Super Admin can legitimately downgrade any tenant
        admin_downgraded = self.mgr.execute_downgrade(
            subscription=sub_dhaka,
            target_tier=PlanTier.BASIC,
            actor_role="PLATFORM_SUPER_ADMIN",
            actor_tenant_id="platform",
        )
        self.assertEqual(admin_downgraded.plan_tier, PlanTier.BASIC)


if __name__ == "__main__":
    unittest.main()
