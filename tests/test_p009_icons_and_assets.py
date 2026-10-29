import os
import unittest

from apps.core.asset_catalog import (
    AssetCatalog,
    AssetPolicyViolationError,
    IconCategory,
    IconDefinition,
)


class TestP009IconsAndStaticImagesPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0009-icons-and-static-images-policy.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0009 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

        cls.catalog = AssetCatalog()

    def test_adr0009_file_exists_and_substantial(self):
        """ADR-0009 must exist and contain substantial specification."""
        self.assertGreater(len(self.content), 5000, "ADR-0009 is too brief.")

    def test_complete_icon_catalog_audit_valid_scenario(self):
        """
        Acceptance Criteria: One valid scenario + Specific Acceptance Audit.
        100% of core icons must pass license verification, alt label verification (EN/BN),
        and strict zero-emoji audit.
        """
        audit = self.catalog.audit_all_icons()
        total = audit["total_icons"]

        self.assertGreaterEqual(total, 20, "Core catalog must have at least 20 verified icons.")
        self.assertEqual(audit["no_emoji_verified"], total, f"All {total} icons must pass zero-emoji audit.")
        self.assertEqual(
            audit["license_verified"], total, f"All {total} icons must have approved open-source licenses."
        )
        self.assertEqual(
            audit["accessible_labels_verified"],
            total,
            f"All {total} icons must have English and Bengali accessibility labels.",
        )

    def test_invalid_input_emoji_and_license_rejected(self):
        """
        Acceptance Criteria: One invalid or incomplete input case.
        Attempting to register icons with emojis, invalid licenses, or raster formats is rejected.
        """
        # 1. Emoji in icon key or label
        bad_icon_emoji = IconDefinition(
            key="bad-icon-emoji",
            package="lucide",
            license="ISC License",
            svg_identifier="lucide:pill",
            alt_en="Medicine Pill 💊",
            alt_bn="ওষুধ",
            category=IconCategory.CLINICAL,
        )
        with self.assertRaises(AssetPolicyViolationError):
            self.catalog.register_icon(bad_icon_emoji)

        # 2. Unauthorized license (e.g. unknown proprietary license)
        bad_icon_license = IconDefinition(
            key="bad-icon-license",
            package="custom",
            license="Unlicensed Commercial",
            svg_identifier="custom:icon",
            alt_en="Custom Icon",
            alt_bn="কাস্টম আইকন",
            category=IconCategory.ACTION,
        )
        with self.assertRaises(AssetPolicyViolationError):
            self.catalog.register_icon(bad_icon_license)

        # 3. Raster format (.png) attempted as functional icon
        bad_icon_raster = IconDefinition(
            key="bad-icon-raster",
            package="lucide",
            license="ISC License",
            svg_identifier="assets/icons/shopping_cart.png",
            alt_en="Cart",
            alt_bn="কার্ট",
            category=IconCategory.NAVIGATION,
        )
        with self.assertRaises(AssetPolicyViolationError):
            self.catalog.register_icon(bad_icon_raster)

    def test_unauthorized_cross_tenant_asset_injection_blocked(self):
        """
        Acceptance Criteria: One unauthorized or cross-tenant case.
        Tenant attempting to register rogue icons or bypass no-emoji rules is blocked.
        """
        tenant_rogue_icon = IconDefinition(
            key="tenant-custom-discount",
            package="tenant_pack",
            license="GPL-Copyleft-Unvetted",
            svg_identifier="tenant:discount_badge",
            alt_en="Special Discount 🏷️",
            alt_bn="বিশেষ ছাড়",
            category=IconCategory.ACTION,
        )
        with self.assertRaises(AssetPolicyViolationError):
            self.catalog.register_icon(tenant_rogue_icon)

    def test_static_artwork_nano_banana_guardrails(self):
        """
        Verifies that all Nano Banana static artwork requirements adhere strictly
        to the prompt formula: no text, no logos, no medicine dosage instructions.
        """
        hero_req = self.catalog.get_artwork_requirement("img-onboarding-hero")
        self.assertIsNotNone(hero_req, "Onboarding hero requirement must be defined.")
        self.assertIn("no text", hero_req.prompt_recipe.lower())
        self.assertIn("no logos", hero_req.prompt_recipe.lower())
        self.assertIn("no medicine dosage instructions", hero_req.prompt_recipe.lower())
        self.assertTrue(hero_req.fallback_vector_svg.endswith(".svg"), "Fallback must be a durable vector SVG.")

        empty_req = self.catalog.get_artwork_requirement("img-empty-shelf")
        self.assertIsNotNone(empty_req, "Empty shelf requirement must be defined.")
        self.assertIn("no text", empty_req.prompt_recipe.lower())
        self.assertIn("no logos", empty_req.prompt_recipe.lower())
        self.assertTrue(empty_req.fallback_vector_svg.endswith(".svg"))


if __name__ == "__main__":
    unittest.main()
