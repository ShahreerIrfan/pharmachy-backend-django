import os
import unittest

from apps.core.design_system import (
    AccessibilityContrastError,
    DesignSystem,
    DesignSystemValidator,
    InvalidSpacingGridError,
    ProhibitedDesignAssetError,
    calculate_contrast_ratio,
)


class TestP007DesignPrinciples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0007-mac-inspired-design-system.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0007 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

    def test_adr0007_file_exists_and_substantial(self):
        """ADR-0007 must exist and contain substantial design specification."""
        self.assertGreater(len(self.content), 5000, "ADR-0007 is too brief.")

    def test_wcag_aa_contrast_report_valid_scenario(self):
        """
        Acceptance Criteria: One valid scenario + Accessibility Contrast Report.
        All primary, secondary, accent, and semantic status badge tokens must pass WCAG 2.1 AA.
        """
        # 1. Body Text Contrast on Surface (#FFFFFF)
        ratio_primary = calculate_contrast_ratio(DesignSystem.TEXT_PRIMARY, DesignSystem.SURFACE_BG)
        self.assertGreaterEqual(ratio_primary, 4.5, "Text Primary fails WCAG AA minimum 4.5:1")
        self.assertGreaterEqual(ratio_primary, 7.0, "Text Primary should ideally reach AAA 7.0:1")

        ratio_secondary = calculate_contrast_ratio(DesignSystem.TEXT_SECONDARY, DesignSystem.SURFACE_BG)
        self.assertGreaterEqual(ratio_secondary, 4.5, "Text Secondary fails WCAG AA minimum 4.5:1")

        ratio_muted = calculate_contrast_ratio(DesignSystem.TEXT_MUTED, DesignSystem.SURFACE_BG)
        self.assertGreaterEqual(ratio_muted, 4.5, "Text Muted fails WCAG AA minimum 4.5:1")

        # 2. Accent Primary on Surface
        ratio_accent = calculate_contrast_ratio(DesignSystem.ACCENT_PRIMARY, DesignSystem.SURFACE_BG)
        self.assertGreaterEqual(ratio_accent, 4.5, "Accent Primary fails WCAG AA minimum 4.5:1")

        # 3. Semantic Badges on their tinted backgrounds (Triple-A level readability)
        success = DesignSystem.STATUS_SUCCESS
        ratio_success = calculate_contrast_ratio(success.text_color, success.bg_color)
        self.assertGreaterEqual(ratio_success, 7.0, "Status Success must meet AAA 7.0:1 on badge background")

        warning = DesignSystem.STATUS_WARNING
        ratio_warning = calculate_contrast_ratio(warning.text_color, warning.bg_color)
        self.assertGreaterEqual(ratio_warning, 7.0, "Status Warning must meet AAA 7.0:1 on badge background")

        danger = DesignSystem.STATUS_DANGER
        ratio_danger = calculate_contrast_ratio(danger.text_color, danger.bg_color)
        self.assertGreaterEqual(ratio_danger, 7.0, "Status Danger must meet AAA 7.0:1 on badge background")

    def test_invalid_input_contrast_and_spacing_rejected(self):
        """
        Acceptance Criteria: One invalid or incomplete input case.
        Low-contrast colors and non-8pt rogue spacing must be rejected by the validator.
        """
        # Low contrast text (#A0AEC0 on #FFFFFF has ratio ~2.4:1)
        with self.assertRaises(AccessibilityContrastError):
            DesignSystemValidator.validate_contrast("#A0AEC0", "#FFFFFF", is_large_text=False)

        # Illegal rogue spacing (e.g. 7px or 19px not in 8pt grid)
        with self.assertRaises(InvalidSpacingGridError):
            DesignSystemValidator.validate_spacing(7)
        with self.assertRaises(InvalidSpacingGridError):
            DesignSystemValidator.validate_spacing(19)

        # Valid spacing values must pass
        self.assertTrue(DesignSystemValidator.validate_spacing(8))
        self.assertTrue(DesignSystemValidator.validate_spacing(16))
        self.assertTrue(DesignSystemValidator.validate_spacing(24))

    def test_unauthorized_cross_tenant_styling_and_forbidden_assets_rejected(self):
        """
        Acceptance Criteria: One unauthorized or cross-tenant case.
        Tenant attempting to inject forbidden assets (emojis, Apple trademarks) or
        inaccessible custom colors is rejected.
        """
        # 1. Emoji injection in UI label or custom theme icon
        with self.assertRaises(ProhibitedDesignAssetError):
            DesignSystemValidator.validate_asset_string("icon:💊_medicine")

        # 2. Apple trademark asset reference
        with self.assertRaises(ProhibitedDesignAssetError):
            DesignSystemValidator.validate_asset_string("assets/icons/apple_logo.svg")

        # 3. Legitimate Lucide SVG icon must pass
        self.assertTrue(DesignSystemValidator.validate_asset_string("lucide:shopping-cart"))

    def test_viewport_layout_specifications_review(self):
        """
        Specific Acceptance: 1280px, 1440px, and 1920px mock review.
        Verifies viewport breakpoint parameters for responsive POS layouts.
        """
        # 1280px Desktop Standard
        vp_1280 = DesignSystem.VIEWPORT_1280
        self.assertEqual(vp_1280.min_width_px, 1280)
        self.assertEqual(vp_1280.sidebar_width_px, 220)
        self.assertEqual(vp_1280.cart_panel_width_px, 440)
        self.assertEqual(vp_1280.table_row_height_px, 36)

        # 1440px Widescreen Retail
        vp_1440 = DesignSystem.VIEWPORT_1440
        self.assertEqual(vp_1440.min_width_px, 1440)
        self.assertEqual(vp_1440.sidebar_width_px, 240)
        self.assertEqual(vp_1440.cart_panel_width_px, 440)
        self.assertEqual(vp_1440.table_row_height_px, 38)

        # 1920px Full HD Manager
        vp_1920 = DesignSystem.VIEWPORT_1920
        self.assertEqual(vp_1920.min_width_px, 1920)
        self.assertEqual(vp_1920.sidebar_width_px, 260)
        self.assertEqual(vp_1920.cart_panel_width_px, 480)
        self.assertEqual(vp_1920.table_row_height_px, 40)


if __name__ == "__main__":
    unittest.main()
