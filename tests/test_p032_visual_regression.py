"""
tests/test_p032_visual_regression.py
Architecture Decision: ADR-0007, ADR-0009, ADR-0024 & ADR-0032
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for Frontend Visual Regression Harness & Golden Testing:
1. Valid Scenario: Baseline visual specs loaded from VisualRegressionRegistry for JavaFX Desktop POS
   and Flutter Mobile Manager across 7 canonical states with fixed 1280x800 / 390x844 dimensions,
   frozen 1.0 font scale, deterministic synthetic DGDA fixtures, and zero floating point drift (exact Decimal BDT).
2. Invalid / Incomplete Inputs: Non-standard dimensions, non-1.0 font scaling, corrupted SHA-256 checksums,
   out-of-bounds diff tolerance, zero/negative medicine prices, and forbidden Unicode emojis raise VisualBaselineError.
3. Security & Cross-Tenant Isolation: Unauthorized roles (CASHIER, BRANCH_MANAGER) attempting baseline mutations
   and cross-tenant fixture access raise VisualBaselineSecurityError.
4. CLI Execution: shared/tools/visual_regression_check.py runs successfully with exit code 0.
5. ADR-0032 Documentation Parity: Verifies existence and completeness of architectural decisions.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

# Ensure root, backend, and shared are on sys.path
_CANDIDATE = Path(__file__).resolve().parent.parent
_ROOT_DIR = _CANDIDATE.parent if (_CANDIDATE.parent / "backend").exists() else _CANDIDATE
_BACKEND_DIR = _ROOT_DIR / "backend"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR / "shared"))

from apps.core.visual_regression import (
    PlatformClient,
    SyntheticMedicineItem,
    SyntheticPharmacyFixture,
    VisualBaselineError,
    VisualBaselineSecurityError,
    VisualDiffEngine,
    VisualDimensions,
    VisualGoldenBaseline,
    VisualRegressionRegistry,
    VisualRegressionSecurityBoundary,
    VisualState,
)


class FrontendVisualRegressionTestSuite(unittest.TestCase):
    def setUp(self):
        self.registry = VisualRegressionRegistry()
        self.canonical_fixture = SyntheticPharmacyFixture.create_canonical_sale_fixture()

    def test_valid_scenario_canonical_baselines_and_fixtures(self):
        """Valid Scenario: Verifies all 14 canonical baselines, dimensions, and exact Decimal BDT math."""
        baselines = self.registry.list_baselines()
        self.assertEqual(len(baselines), 14)

        desktop_baselines = self.registry.list_baselines(PlatformClient.DESKTOP_JAVAFX)
        mobile_baselines = self.registry.list_baselines(PlatformClient.MOBILE_FLUTTER)

        self.assertEqual(len(desktop_baselines), 7)
        self.assertEqual(len(mobile_baselines), 7)

        # Verify all 7 states are represented for both platforms
        expected_states = {s for s in VisualState}
        self.assertEqual({b.state for b in desktop_baselines}, expected_states)
        self.assertEqual({b.state for b in mobile_baselines}, expected_states)

        # Verify Desktop dimensions and fixed font scale
        for b in desktop_baselines:
            self.assertEqual(b.dimensions.width, 1280)
            self.assertEqual(b.dimensions.height, 800)
            self.assertEqual(b.dimensions.pixel_ratio, 1.0)
            self.assertEqual(b.dimensions.font_scale, 1.0)
            is_valid, issues = self.registry.validate_baseline(b)
            self.assertTrue(is_valid, f"Desktop baseline {b.baseline_id} failed validation: {issues}")

        # Verify Mobile dimensions and fixed font scale
        for b in mobile_baselines:
            self.assertEqual(b.dimensions.width, 390)
            self.assertEqual(b.dimensions.height, 844)
            self.assertEqual(b.dimensions.pixel_ratio, 2.0)
            self.assertEqual(b.dimensions.font_scale, 1.0)
            is_valid, issues = self.registry.validate_baseline(b)
            self.assertTrue(is_valid, f"Mobile baseline {b.baseline_id} failed validation: {issues}")

        # Verify Canonical Synthetic Fixture Decimal Math
        # Items:
        # Napa Extra: 2 * 35.00 = 70.00 BDT
        # Seclo: 1 * 70.00 = 70.00 BDT
        # Azithrocin: 1 * 140.00 = 140.00 BDT
        # Subtotal: 280.00 BDT
        # 5% Retail VAT: 280.00 * 0.05 = 14.00 BDT
        # Grand Total: 294.00 BDT
        self.assertEqual(len(self.canonical_fixture.items), 3)
        self.assertEqual(self.canonical_fixture.subtotal_bdt, Decimal("280.00"))
        self.assertEqual(self.canonical_fixture.vat_bdt, Decimal("14.00"))
        self.assertEqual(self.canonical_fixture.grand_total_bdt, Decimal("294.00"))
        self.assertIsInstance(self.canonical_fixture.grand_total_bdt, Decimal)

        # Verify VisualDiffEngine exact match behavior
        checksum = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        diff_res = VisualDiffEngine.compare_renders(checksum, checksum, simulated_pixel_mismatches=0)
        self.assertEqual(diff_res["status"], "PASS")
        self.assertEqual(diff_res["diff_percent"], 0.0)
        self.assertTrue(diff_res["is_match"])

    def test_invalid_dimensions_and_scaling_inputs(self):
        """Invalid Inputs: Rejects negative dimensions, non-standard resolutions, and unfrozen font scales."""
        # 1. Non-positive dimensions
        with self.assertRaises(VisualBaselineError) as ctx:
            VisualDimensions(width=0, height=800).validate(PlatformClient.DESKTOP_JAVAFX)
        self.assertIn("Invalid dimensions", str(ctx.exception))

        with self.assertRaises(VisualBaselineError) as ctx:
            VisualDimensions(width=1280, height=-10).validate(PlatformClient.DESKTOP_JAVAFX)
        self.assertIn("Invalid dimensions", str(ctx.exception))

        # 2. Unfrozen font scaling (flakiness hazard)
        with self.assertRaises(VisualBaselineError) as ctx:
            VisualDimensions(width=1280, height=800, font_scale=1.2).validate(PlatformClient.DESKTOP_JAVAFX)
        self.assertIn("Font scale must be frozen at 1.0", str(ctx.exception))

        # 3. Non-standard desktop aspect ratio
        with self.assertRaises(VisualBaselineError) as ctx:
            VisualDimensions(width=800, height=600).validate(PlatformClient.DESKTOP_JAVAFX)
        self.assertIn("Non-standard JavaFX POS dimensions", str(ctx.exception))

        # 4. Desktop invalid pixel ratio
        with self.assertRaises(VisualBaselineError) as ctx:
            VisualDimensions(width=1280, height=800, pixel_ratio=1.5).validate(PlatformClient.DESKTOP_JAVAFX)
        self.assertIn("Desktop JavaFX pixel ratio must be fixed at 1.0", str(ctx.exception))

        # 5. Non-standard mobile viewport
        with self.assertRaises(VisualBaselineError) as ctx:
            VisualDimensions(width=1280, height=800).validate(PlatformClient.MOBILE_FLUTTER)
        self.assertIn("Non-standard Flutter Mobile viewport", str(ctx.exception))

        # 6. Mobile invalid pixel ratio
        with self.assertRaises(VisualBaselineError) as ctx:
            VisualDimensions(width=390, height=844, pixel_ratio=4.0).validate(PlatformClient.MOBILE_FLUTTER)
        self.assertIn("Flutter device pixel ratio must be 1.0, 2.0, or 3.0", str(ctx.exception))

    def test_invalid_baselines_checksums_and_emoji_rejection(self):
        """Invalid Inputs: Baseline auditor detects corrupted checksum, excessive tolerance, emoji, and invalid prices."""
        dim = VisualDimensions(width=1280, height=800)

        # 1. Corrupted checksum
        bad_checksum_baseline = VisualGoldenBaseline(
            baseline_id="invalid_checksum_test",
            client=PlatformClient.DESKTOP_JAVAFX,
            screen_name="PosMainView",
            state=VisualState.LOADED,
            dimensions=dim,
            checksum_sha256="corrupted-checksum-not-64-hex",
        )
        is_valid, issues = self.registry.validate_baseline(bad_checksum_baseline)
        self.assertFalse(is_valid)
        self.assertTrue(any("Invalid SHA-256 checksum" in i for i in issues))

        # 2. Out of threshold tolerance
        bad_tolerance_baseline = VisualGoldenBaseline(
            baseline_id="invalid_tolerance_test",
            client=PlatformClient.DESKTOP_JAVAFX,
            screen_name="PosMainView",
            state=VisualState.LOADED,
            dimensions=dim,
            checksum_sha256="a" * 64,
            tolerated_diff_percent=12.5,
        )
        is_valid, issues = self.registry.validate_baseline(bad_tolerance_baseline)
        self.assertFalse(is_valid)
        self.assertTrue(any("exceeds allowed threshold" in i for i in issues))

        # 3. Forbidden Unicode emoji in synthetic fixture (ADR-0009 violation)
        emoji_item = SyntheticMedicineItem(
            medicine_id="med-emoji",
            brand_name="Napa Extra 💊",
            generic_name="Paracetamol",
            manufacturer="Beximco",
            packaging_unit="Strip",
            unit_price_bdt=Decimal("35.00"),
            batch_number="B1",
            quantity=1,
        )
        emoji_fixture = SyntheticPharmacyFixture(items=[emoji_item])
        emoji_baseline = VisualGoldenBaseline(
            baseline_id="emoji_test",
            client=PlatformClient.DESKTOP_JAVAFX,
            screen_name="PosMainView",
            state=VisualState.LOADED,
            dimensions=dim,
            checksum_sha256="b" * 64,
            fixture=emoji_fixture,
        )
        is_valid, issues = self.registry.validate_baseline(emoji_baseline)
        self.assertFalse(is_valid)
        self.assertTrue(any("Forbidden emoji found" in i for i in issues))

        # 4. Non-positive unit price in fixture
        free_item = SyntheticMedicineItem(
            medicine_id="med-free",
            brand_name="Free Sample",
            generic_name="Paracetamol",
            manufacturer="Beximco",
            packaging_unit="Strip",
            unit_price_bdt=Decimal("0.00"),
            batch_number="B2",
            quantity=1,
        )
        free_fixture = SyntheticPharmacyFixture(items=[free_item])
        free_baseline = VisualGoldenBaseline(
            baseline_id="free_price_test",
            client=PlatformClient.DESKTOP_JAVAFX,
            screen_name="PosMainView",
            state=VisualState.LOADED,
            dimensions=dim,
            checksum_sha256="c" * 64,
            fixture=free_fixture,
        )
        is_valid, issues = self.registry.validate_baseline(free_baseline)
        self.assertFalse(is_valid)
        self.assertTrue(any("invalid non-positive price" in i for i in issues))

    def test_diff_engine_pixel_mismatch_and_threshold_enforcement(self):
        """Diff Engine: Enforces 0.50% threshold for visual discrepancies."""
        total_pixels = 1280 * 800  # 1,024,000 pixels

        # 1. Invalid total pixels
        with self.assertRaises(VisualBaselineError):
            VisualDiffEngine.compare_renders("a" * 64, "b" * 64, total_pixels=0)

        # 2. Tolerated minor diff: 2,000 pixels (~0.1953%) -> PASS
        pass_res = VisualDiffEngine.compare_renders(
            "a" * 64, "b" * 64, simulated_pixel_mismatches=2000, total_pixels=total_pixels
        )
        self.assertEqual(pass_res["status"], "PASS")
        self.assertTrue(pass_res["is_match"])
        self.assertLessEqual(pass_res["diff_percent"], 0.50)

        # 3. Exceeded diff: 10,000 pixels (~0.9766%) -> FAIL
        fail_res = VisualDiffEngine.compare_renders(
            "a" * 64, "b" * 64, simulated_pixel_mismatches=10000, total_pixels=total_pixels
        )
        self.assertEqual(fail_res["status"], "FAIL")
        self.assertFalse(fail_res["is_match"])
        self.assertGreater(fail_res["diff_percent"], 0.50)

    def test_security_rbac_and_cross_tenant_isolation(self):
        """Security: Rejects unauthorized baseline mutations and cross-tenant fixture leaks."""
        # 1. Cashier cannot mutate visual baselines
        with self.assertRaises(VisualBaselineSecurityError) as ctx:
            VisualRegressionSecurityBoundary.assert_can_mutate_baselines("CASHIER")
        self.assertIn("is not authorized to update or approve visual regression goldens", str(ctx.exception))

        # 2. Branch Manager cannot mutate visual baselines
        with self.assertRaises(VisualBaselineSecurityError):
            VisualRegressionSecurityBoundary.assert_can_mutate_baselines("BRANCH_MANAGER")

        # 3. Authorized roles pass
        VisualRegressionSecurityBoundary.assert_can_mutate_baselines("PLATFORM_SUPER_ADMIN")
        VisualRegressionSecurityBoundary.assert_can_mutate_baselines("LEAD_ARCHITECT")

        # 4. Attempting to register baseline with unauthorized role fails
        valid_custom_baseline = VisualGoldenBaseline(
            baseline_id="custom_snapshot",
            client=PlatformClient.DESKTOP_JAVAFX,
            screen_name="PosMainView",
            state=VisualState.LOADED,
            dimensions=VisualDimensions(width=1280, height=800),
            checksum_sha256="d" * 64,
        )
        with self.assertRaises(VisualBaselineSecurityError):
            self.registry.register_baseline(valid_custom_baseline, actor_role="CASHIER")

        # 5. Cross-tenant fixture access blocked
        with self.assertRaises(VisualBaselineSecurityError) as ctx:
            VisualRegressionSecurityBoundary.assert_tenant_boundary(
                session_tenant_id="tenant-dhk-01",
                baseline_tenant_id="tenant-ctg-99",
            )
        self.assertIn("Cross-tenant visual fixture leak", str(ctx.exception))

        # 6. Matching tenant passes
        VisualRegressionSecurityBoundary.assert_tenant_boundary(
            session_tenant_id="tenant-dhk-01",
            baseline_tenant_id="tenant-dhk-01",
        )

    def test_cli_tool_execution(self):
        """Toolchain: Runs shared/tools/visual_regression_check.py --audit via subprocess."""
        tool_script = _ROOT_DIR / "shared" / "tools" / "visual_regression_check.py"
        self.assertTrue(tool_script.exists(), f"CLI tool not found at {tool_script}")

        cmd = [sys.executable, str(tool_script), "--audit"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, f"CLI tool failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[AUDIT PASS]", proc.stdout)
        self.assertIn("All 14 visual baselines passed validation", proc.stdout)

    def test_adr_0032_documentation_and_spec_parity(self):
        """Documentation: Ensures ADR-0032 decision record exists and contains required sections."""
        adr_file = (
            _ROOT_DIR
            / "shared"
            / "docs"
            / "architecture"
            / "decisions"
            / "ADR-0032-frontend-visual-regression-harness-and-golden-testing.md"
        )
        self.assertTrue(adr_file.exists(), f"ADR-0032 documentation not found at {adr_file}")

        content = adr_file.read_text(encoding="utf-8")
        self.assertIn("ADR-0032", content)
        self.assertIn("Frontend Visual Regression Harness", content)
        self.assertIn("JavaFX Snapshot Testing", content)
        self.assertIn("Flutter Golden Tests", content)
        self.assertIn("1280 x 800", content)
        self.assertIn("390 x 844", content)
        self.assertIn("LOADED", content)
        self.assertIn("OFFLINE", content)
        self.assertIn("PERMISSION_DENIED", content)


if __name__ == "__main__":
    unittest.main()
