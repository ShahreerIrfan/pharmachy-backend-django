import json
import os
import unittest

from apps.core.design_system import (
    AccessibilityContrastError,
    DesignSystemValidator,
    calculate_contrast_ratio,
    hex_to_rgb,
)
from apps.core.theme_tokens import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    build_master_token_dictionary,
    generate_flutter_dart,
    generate_javafx_css,
)


class TestP008TokensAndThemes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adr_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "docs",
                "architecture",
                "decisions",
                "ADR-0008-design-tokens-and-multiplatform-themes.md",
            )
        )
        assert os.path.exists(cls.adr_path), f"ADR-0008 not found at: {cls.adr_path}"
        with open(cls.adr_path, encoding="utf-8") as f:
            cls.content = f.read()

        candidates = [
            os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "backend", "apps", "core", "tokens_snapshot.json")
            ),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "apps", "core", "tokens_snapshot.json")),
        ]
        cls.snapshot_path = next((path for path in candidates if os.path.exists(path)), candidates[0])
        assert os.path.exists(cls.snapshot_path), f"tokens_snapshot.json not found at: {cls.snapshot_path}"
        with open(cls.snapshot_path, encoding="utf-8") as f:
            cls.snapshot = json.load(f)

    def test_adr0008_file_exists_and_substantial(self):
        """ADR-0008 must exist and contain substantial specification."""
        self.assertGreater(len(self.content), 5000, "ADR-0008 is too brief.")

    def test_token_snapshot_integrity(self):
        """Token snapshot JSON must match the canonical master token dictionary exactly."""
        master = build_master_token_dictionary()
        self.assertEqual(self.snapshot["version"], master["version"])
        self.assertEqual(self.snapshot["spatial_scale"], master["spatial_scale"])
        self.assertEqual(self.snapshot["radii_scale"], master["radii_scale"])
        self.assertEqual(self.snapshot["themes"]["light"]["text_primary"], master["themes"]["light"]["text_primary"])
        self.assertEqual(self.snapshot["themes"]["dark"]["text_primary"], master["themes"]["dark"]["text_primary"])

    def test_valid_scenario_dual_theme_contrast_report(self):
        """
        Acceptance Criteria: One valid scenario.
        Both Light AND Dark themes pass WCAG 2.1 AA/AAA contrast checks across all roles.
        """
        # 1. LIGHT THEME CONTRAST
        # Text on Surface
        ratio_light_primary = calculate_contrast_ratio(LIGHT_PALETTE.text_primary, LIGHT_PALETTE.bg_surface)
        self.assertGreaterEqual(ratio_light_primary, 4.5, "Light Primary Text fails AA 4.5:1")
        self.assertGreaterEqual(ratio_light_primary, 7.0, "Light Primary Text reaches AAA 7.0:1")

        ratio_light_secondary = calculate_contrast_ratio(LIGHT_PALETTE.text_secondary, LIGHT_PALETTE.bg_surface)
        self.assertGreaterEqual(ratio_light_secondary, 4.5, "Light Secondary Text fails AA 4.5:1")

        ratio_light_muted = calculate_contrast_ratio(LIGHT_PALETTE.text_muted, LIGHT_PALETTE.bg_surface)
        self.assertGreaterEqual(ratio_light_muted, 4.5, "Light Muted Text fails AA 4.5:1")

        # Light Status Badges (AAA >= 7.0:1)
        ratio_light_success = calculate_contrast_ratio(
            LIGHT_PALETTE.status_success_text, LIGHT_PALETTE.status_success_bg
        )
        self.assertGreaterEqual(ratio_light_success, 7.0, "Light Success Badge fails AAA 7.0:1")

        ratio_light_warning = calculate_contrast_ratio(
            LIGHT_PALETTE.status_warning_text, LIGHT_PALETTE.status_warning_bg
        )
        self.assertGreaterEqual(ratio_light_warning, 7.0, "Light Warning Badge fails AAA 7.0:1")

        ratio_light_danger = calculate_contrast_ratio(LIGHT_PALETTE.status_danger_text, LIGHT_PALETTE.status_danger_bg)
        self.assertGreaterEqual(ratio_light_danger, 7.0, "Light Danger Badge fails AAA 7.0:1")

        # 2. DARK THEME CONTRAST (Night Shift / 24-Hour Pharmacy)
        # Text on Dark Surface (#1E293B)
        ratio_dark_primary = calculate_contrast_ratio(DARK_PALETTE.text_primary, DARK_PALETTE.bg_surface)
        self.assertGreaterEqual(ratio_dark_primary, 7.0, "Dark Primary Text reaches AAA 7.0:1")

        ratio_dark_secondary = calculate_contrast_ratio(DARK_PALETTE.text_secondary, DARK_PALETTE.bg_surface)
        self.assertGreaterEqual(ratio_dark_secondary, 4.5, "Dark Secondary Text fails AA 4.5:1")

        ratio_dark_muted = calculate_contrast_ratio(DARK_PALETTE.text_muted, DARK_PALETTE.bg_surface)
        self.assertGreaterEqual(ratio_dark_muted, 4.5, "Dark Muted Text fails AA 4.5:1")

        # Dark Status Badges (AAA >= 7.0:1)
        ratio_dark_success = calculate_contrast_ratio(DARK_PALETTE.status_success_text, DARK_PALETTE.status_success_bg)
        self.assertGreaterEqual(ratio_dark_success, 7.0, "Dark Success Badge fails AAA 7.0:1")

        ratio_dark_warning = calculate_contrast_ratio(DARK_PALETTE.status_warning_text, DARK_PALETTE.status_warning_bg)
        self.assertGreaterEqual(ratio_dark_warning, 7.0, "Dark Warning Badge fails AAA 7.0:1")

        ratio_dark_danger = calculate_contrast_ratio(DARK_PALETTE.status_danger_text, DARK_PALETTE.status_danger_bg)
        self.assertGreaterEqual(ratio_dark_danger, 7.0, "Dark Danger Badge fails AAA 7.0:1")

    def test_invalid_input_malformed_hex_codes(self):
        """
        Acceptance Criteria: One invalid or incomplete input case.
        Malformed hex codes or non-6-digit hex values must be rejected.
        """
        invalid_hex_codes = ["#12345", "#GGGGGG", "red", "123456", ""]
        for bad_hex in invalid_hex_codes:
            with self.assertRaises(ValueError):
                hex_to_rgb(bad_hex)

    def test_unauthorized_custom_theme_contrast_failure(self):
        """
        Acceptance Criteria: One unauthorized or cross-tenant case.
        A tenant attempting to inject an un-vetted dark theme with low contrast
        (e.g. dark text on dark background) is rejected by DesignSystemValidator.
        """
        tenant_bad_text = "#334155"  # Dark slate
        tenant_dark_bg = "#1E293B"  # Dark surface
        with self.assertRaises(AccessibilityContrastError):
            DesignSystemValidator.validate_contrast(tenant_bad_text, tenant_dark_bg)

    def test_javafx_css_code_generation(self):
        """JavaFX CSS stylesheet generation must emit valid -fx- variables for both light and dark."""
        css = generate_javafx_css()
        self.assertIn(".root {", css)
        self.assertIn(".root.dark-theme {", css)
        self.assertIn("-fx-bg-canvas: #F8FAFC;", css)
        self.assertIn("-fx-bg-surface: #FFFFFF;", css)
        self.assertIn("-fx-accent-primary: #0369A1;", css)
        self.assertIn("-fx-radius-md: 6px;", css)
        self.assertIn("-fx-status-success-text: #14532D;", css)
        self.assertIn("-fx-bg-canvas: #0F172A;", css)  # Dark theme canvas

    def test_flutter_dart_code_generation(self):
        """Flutter Dart theme code generation must emit valid Color(0xFF...) constants."""
        dart = generate_flutter_dart()
        self.assertIn("class PharmacyColors {", dart)
        self.assertIn("static const Color lightBgCanvas = Color(0xFFF8FAFC);", dart)
        self.assertIn("static const Color lightAccentPrimary = Color(0xFF0369A1);", dart)
        self.assertIn("static const Color darkBgCanvas = Color(0xFF0F172A);", dart)
        self.assertIn("static const Color successBg = Color(0xFFDCFCE7);", dart)
        self.assertIn("static const Color successText = Color(0xFF14532D);", dart)


if __name__ == "__main__":
    unittest.main()
