import os
import re
import unittest

from apps.core.navigation import (
    NavGroup,
    NavigationRouter,
    NavigationStatus,
    PlanTier,
)


class TestP006MainNavigationIA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0006-main-navigation-information-architecture.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0006 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

        cls.router = NavigationRouter()

    def test_adr0006_file_exists_and_substantial(self):
        """ADR-0006 must exist and contain substantial specification."""
        self.assertGreater(len(self.content), 5000, "ADR-0006 is too brief.")

    def test_no_emojis_in_navigation_registry_or_adr(self):
        """
        Global UX Rule: Strict ban on emojis in navigation icons, labels, or status badges.
        All icons must use verified SVG identifiers like 'lucide:...'.
        """
        # Emoji regex range
        emoji_pattern = re.compile(r"[\U00010000-\U0010ffff]|[\u2600-\u27ff]|[\u2300-\u23ff]|[\u2b50-\u2b55]")
        for dest in self.router.registry.get_all():
            self.assertFalse(emoji_pattern.search(dest.label_en), f"Emoji found in English label: {dest.label_en}")
            self.assertFalse(emoji_pattern.search(dest.label_bn), f"Emoji found in Bengali label: {dest.label_bn}")
            self.assertTrue(dest.icon.startswith("lucide:"), f"Icon must be a verified Lucide SVG, found: {dest.icon}")
            if dest.empty_state:
                self.assertTrue(
                    dest.empty_state.icon.startswith("lucide:"),
                    f"Empty state icon must be Lucide SVG, found: {dest.empty_state.icon}",
                )

    def test_navigation_groups_coverage(self):
        """Registry must contain routes for OPERATIONS, BUSINESS, ADMIN, and PLATFORM."""
        destinations = self.router.registry.get_all()
        groups = {d.group for d in destinations}
        self.assertIn(NavGroup.OPERATIONS, groups)
        self.assertIn(NavGroup.BUSINESS, groups)
        self.assertIn(NavGroup.ADMIN, groups)
        self.assertIn(NavGroup.PLATFORM, groups)

    def test_valid_scenario_pharmacist_operations(self):
        """
        Acceptance Criteria: One valid scenario.
        Registered Pharmacist on Standard Plan accesses POS, Prescriptions, and Antibiotic Register.
        """
        # 1. POS access
        res_pos = self.router.resolve_route(
            route="/app/pos", user_role="PHARMACIST", user_plan=PlanTier.STANDARD, is_online=True, has_data=True
        )
        self.assertEqual(res_pos.status, NavigationStatus.RENDER_VIEW)
        self.assertEqual(res_pos.destination.id, "pos")

        # 2. Prescriptions access
        res_rx = self.router.resolve_route(
            route="/app/prescriptions",
            user_role="PHARMACIST",
            user_plan=PlanTier.STANDARD,
            is_online=True,
            has_data=True,
        )
        self.assertEqual(res_rx.status, NavigationStatus.RENDER_VIEW)
        self.assertEqual(res_rx.destination.id, "prescriptions")

        # 3. Antibiotic register access
        res_ab = self.router.resolve_route(
            route="/app/antibiotics", user_role="PHARMACIST", user_plan=PlanTier.STANDARD, is_online=True, has_data=True
        )
        self.assertEqual(res_ab.status, NavigationStatus.RENDER_VIEW)
        self.assertEqual(res_ab.destination.id, "antibiotics")

    def test_invalid_or_incomplete_input_scenario(self):
        """
        Acceptance Criteria: One invalid or incomplete input case.
        User enters a non-existent or malformed route.
        """
        res_invalid = self.router.resolve_route(
            route="/app/non_existent_module", user_role="TENANT_OWNER", user_plan=PlanTier.PRO, is_online=True
        )
        self.assertEqual(res_invalid.status, NavigationStatus.NOT_FOUND)
        self.assertIn("does not exist", res_invalid.message)

    def test_unauthorized_and_cross_role_scenario(self):
        """
        Acceptance Criteria: One unauthorized or cross-tenant case.
        Cashier attempting to navigate to General Ledger or Subscription Billing.
        """
        # Cashier trying to access /app/accounting
        res_acct = self.router.resolve_route(
            route="/app/accounting", user_role="CASHIER", user_plan=PlanTier.STANDARD, is_online=True
        )
        self.assertEqual(res_acct.status, NavigationStatus.PERMISSION_DENIED)
        self.assertIn("not authorized", res_acct.message)

        # Cashier trying to access /app/billing
        res_bill = self.router.resolve_route(
            route="/app/billing", user_role="CASHIER", user_plan=PlanTier.STANDARD, is_online=True
        )
        self.assertEqual(res_bill.status, NavigationStatus.PERMISSION_DENIED)

        # Staff trying to access Platform Console
        res_platform = self.router.resolve_route(
            route="/platform/metrics", user_role="TENANT_OWNER", user_plan=PlanTier.PRO, is_online=True
        )
        self.assertEqual(res_platform.status, NavigationStatus.PERMISSION_DENIED)

    def test_subscription_plan_tier_gating(self):
        """Routes requiring Standard or Pro tier block Basic tier tenants with PLAN_UPGRADE_REQUIRED."""
        # Multi-branch requires Standard plan
        res_branch = self.router.resolve_route(
            route="/app/branches", user_role="TENANT_OWNER", user_plan=PlanTier.BASIC, is_online=True
        )
        self.assertEqual(res_branch.status, NavigationStatus.PLAN_UPGRADE_REQUIRED)
        self.assertIn("requires 'STANDARD' plan tier", res_branch.message)

    def test_offline_connectivity_gating(self):
        """When offline, cloud-only routes return OFFLINE_UNAVAILABLE; offline-capable routes succeed."""
        # 1. Billing is Cloud-Only -> Blocks offline
        res_billing_offline = self.router.resolve_route(
            route="/app/billing", user_role="TENANT_OWNER", user_plan=PlanTier.STANDARD, is_online=False
        )
        self.assertEqual(res_billing_offline.status, NavigationStatus.OFFLINE_UNAVAILABLE)

        # 2. POS is Offline-Capable -> Allowed offline
        res_pos_offline = self.router.resolve_route(
            route="/app/pos", user_role="CASHIER", user_plan=PlanTier.BASIC, is_online=False, has_data=True
        )
        self.assertEqual(res_pos_offline.status, NavigationStatus.RENDER_VIEW)

    def test_empty_state_resolution(self):
        """When route has zero data, returns EMPTY_STATE with localized BN/EN copy and action CTA."""
        res_empty_cart = self.router.resolve_route(
            route="/app/pos", user_role="CASHIER", user_plan=PlanTier.BASIC, is_online=True, has_data=False
        )
        self.assertEqual(res_empty_cart.status, NavigationStatus.EMPTY_STATE)
        self.assertIsNotNone(res_empty_cart.empty_state)
        self.assertEqual(res_empty_cart.empty_state.heading_bn, "কার্ট খালি আছে")
        self.assertEqual(res_empty_cart.empty_state.heading_en, "Cart is Empty")
        self.assertEqual(res_empty_cart.empty_state.cta_action, "FOCUS_SEARCH")


if __name__ == "__main__":
    unittest.main()
