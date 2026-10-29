"""
tests/test_p024_flutter_workspace.py
Architecture Decision: ADR-0007, ADR-0009, ADR-0021 & ADR-0024
Phase 02: Repository, Toolchain & Engineering Automation

Verification test suite for Flutter mobile workspace setup, stable SDK pinning,
analysis options, platform flavors, zero-emoji policy, and automated test execution.
"""

import subprocess
import unittest
from pathlib import Path

from apps.core.flutter_toolchain import (
    FlutterEmojiValidator,
    FlutterFinancialPrecisionValidator,
    FlutterSdkValidator,
    FlutterToolchainError,
    FlutterWorkspaceValidator,
)


class TestP024FlutterWorkspaceSetup(unittest.TestCase):
    """Verifies all acceptance criteria for P024 and ADR-0024."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        if (self.root_dir / "mobile").exists():
            self.mobile_dir = self.root_dir / "mobile"
        elif (self.root_dir.parent / "mobile").exists():
            self.mobile_dir = self.root_dir.parent / "mobile"
        elif (self.root_dir / "mobile-flutter").exists():
            self.mobile_dir = self.root_dir / "mobile-flutter"
        elif (self.root_dir.parent / "mobile-flutter").exists():
            self.mobile_dir = self.root_dir.parent / "mobile-flutter"
        else:
            self.mobile_dir = self.root_dir / "mobile"

    # 1. Flutter SDK Stable Pinning
    def test_flutter_sdk_stable_installed(self):
        """Installed Flutter SDK must be >= 3.19.0 and Dart >= 3.3.0."""
        info = FlutterSdkValidator.validate_installed_sdk()
        self.assertTrue(info.is_supported)
        self.assertEqual(info.channel, "stable")

    def test_unsupported_flutter_version_rejected(self):
        """Unsupported legacy versions (Flutter 2.x, Dart 2.x) must be rejected."""
        legacy_output = "Flutter 2.10.5 • channel stable\nTools • Dart 2.16.2"
        info = FlutterSdkValidator.parse_version_output(legacy_output)
        self.assertFalse(info.is_supported)

    # 2. Workspace Structure & Layered Architecture
    def test_flutter_workspace_structure_and_mandatory_files(self):
        """All mandatory layered architecture files must exist and be valid."""
        result = FlutterWorkspaceValidator.validate_workspace(self.mobile_dir)
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["app_name"], "pharmacy_mobile_manager")
        self.assertEqual(result["flavors"], ["dev", "staging", "prod"])
        self.assertGreaterEqual(result["files_checked"], 15)

    def test_missing_mandatory_file_rejected(self):
        """Missing mandatory workspace file raises FlutterToolchainError."""
        with self.assertRaises(FlutterToolchainError) as ctx:
            FlutterWorkspaceValidator.validate_workspace(self.mobile_dir / "nonexistent_dir")
        self.assertIn("Incomplete Flutter workspace", str(ctx.exception))

    # 3. Linter Rules & Strict Type Analyzer
    def test_analysis_options_strict_rules(self):
        """analysis_options.yaml must enforce strict casts, inference, and linter rules."""
        analysis_opts = self.mobile_dir / "analysis_options.yaml"
        self.assertTrue(analysis_opts.exists())
        content = analysis_opts.read_text(encoding="utf-8")
        self.assertIn("strict-casts: true", content)
        self.assertIn("strict-inference: true", content)
        self.assertIn("avoid_print", content)
        self.assertIn("prefer_const_constructors", content)

    # 4. Platform Flavors
    def test_flavors_defined_correctly(self):
        """flavors.dart must configure dev, staging, and prod base URLs."""
        flavors_file = self.mobile_dir / "lib" / "core" / "constants" / "flavors.dart"
        content = flavors_file.read_text(encoding="utf-8")
        self.assertIn("AppFlavor.dev", content)
        self.assertIn("http://localhost:8000/api/v2", content)
        self.assertIn("AppFlavor.staging", content)
        self.assertIn("https://staging-api.pharmacy.com.bd/api/v2", content)
        self.assertIn("AppFlavor.prod", content)
        self.assertIn("https://api.pharmacy.com.bd/api/v2", content)

    # 5. Cupertino-Inspired Design Tokens
    def test_cupertino_theme_tokens_present(self):
        """tokens.dart must declare 8pt grid, neutral surfaces, and minimum touch target 44pt."""
        tokens_file = self.mobile_dir / "lib" / "core" / "theme" / "tokens.dart"
        content = tokens_file.read_text(encoding="utf-8")
        self.assertIn("surfaceBackground = Color(0xFFF8F9FA)", content)
        self.assertIn("primaryTeal = Color(0xFF0D9488)", content)
        self.assertIn("minTouchTarget = 44.0", content)
        self.assertIn("space8 = 8.0", content)
        self.assertIn("space16 = 16.0", content)

    # 6. Zero-Emoji Icon Policy
    def test_zero_emojis_in_flutter_codebase(self):
        """All Dart files must contain ZERO emojis for UI controls (ADR-0009)."""
        violations = FlutterEmojiValidator.scan_directory(self.mobile_dir / "lib")
        self.assertEqual(violations, [], "Found forbidden emoji icons in mobile/lib!")

    def test_emoji_detector_flags_forbidden_symbols(self):
        """Emoji detector catches forbidden pictograms in Dart strings."""
        sample_with_emoji = "final title = 'Sales 📈';"
        self.assertTrue(bool(FlutterEmojiValidator.EMOJI_PATTERN.search(sample_with_emoji)))

    # 7. Financial Accuracy & Monetary Decimal Strings
    def test_financial_precision_avoids_float_drift(self):
        """Dashboard KPI DTO must use exact decimal strings, forbidding double/num for sales."""
        kpi_file = self.mobile_dir / "lib" / "models" / "dashboard_kpi.dart"
        FlutterFinancialPrecisionValidator.validate_dart_monetary_types(kpi_file)
        content = kpi_file.read_text(encoding="utf-8")
        self.assertIn("final String todaySalesBdt;", content)
        self.assertIn("formattedSalesText", content)

    # 8. Real Flutter Test Execution (Phase-Specific Gate)
    def test_real_flutter_test_suite_passes(self):
        """Execute real flutter test in mobile and assert 100% pass."""
        import shutil

        cmd = shutil.which("flutter") or shutil.which("flutter.bat") or "flutter"
        proc = subprocess.run(
            [cmd, "test"], cwd=str(self.mobile_dir), capture_output=True, text=True, shell=True, check=False
        )
        self.assertEqual(proc.returncode, 0, f"flutter test failed with output:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("All tests passed!", proc.stdout)


if __name__ == "__main__":
    unittest.main()
