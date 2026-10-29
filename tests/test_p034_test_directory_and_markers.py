"""
tests/test_p034_test_directory_and_markers.py
Architecture Decision: ADR-0012, ADR-0019, ADR-0026 & ADR-0034
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for Test Directory Organization, Markers & CI Discovery:
1. Valid Scenario: Verifies canonical 8-tier markers (unit, integration, contract, concurrency,
   security, ui, offline_fault, e2e) across all 34 test suites with full coverage and CI lane mapping.
2. Invalid / Incomplete Inputs: Rejects unregistered, rogue, or misspelled test markers and unknown CI lanes.
3. Security Boundary: Validates dedicated security and multi-tenant isolation suites under security marker.
4. Source-Specific Business Rule: Verifies strict marker registration in pyproject.toml (--strict-markers).
5. CLI Execution: shared/tools/test_runner_cli.py runs successfully with exit code 0.
6. ADR-0034 Decision Parity: Ensures existence and completeness of architectural decisions.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

# Add project root and backend to sys.path
_CANDIDATE = Path(__file__).resolve().parent.parent
_ROOT_DIR = _CANDIDATE.parent if (_CANDIDATE.parent / "backend").exists() else _CANDIDATE
_BACKEND_DIR = _ROOT_DIR / "backend"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR / "shared"))

from apps.core.test_taxonomy import (
    LANE_MARKER_MAP,
    CILane,
    TestMarker,
    TestMarkerError,
    TestTaxonomyRegistry,
)


class TestDirectoryAndMarkersTestSuite(unittest.TestCase):
    def setUp(self):
        self.registry = TestTaxonomyRegistry()

    def test_valid_scenario_taxonomy_coverage_and_ci_lanes(self):
        """Valid Scenario: Verifies test suites are registered with full 8-marker coverage."""
        suites = self.registry.list_suites()
        self.assertGreaterEqual(len(suites), 34)

        # Audit coverage across all 8 canonical markers
        coverage = self.registry.audit_coverage()
        self.assertTrue(coverage["has_full_marker_coverage"])
        self.assertEqual(len(coverage["missing_markers"]), 0)

        # Verify all 8 markers exist in distribution
        expected_markers = {m.value for m in TestMarker}
        self.assertEqual(set(coverage["marker_distribution"].keys()), expected_markers)
        for m_name, count in coverage["marker_distribution"].items():
            self.assertGreater(count, 0, f"Marker '{m_name}' has zero registered test suites.")

        # Verify CI Lanes Mapping
        fast_lane_suites = self.registry.get_suites_by_lane(CILane.FAST_LANE.value)
        integration_lane_suites = self.registry.get_suites_by_lane(CILane.INTEGRATION_LANE.value)
        ui_lane_suites = self.registry.get_suites_by_lane(CILane.UI_LANE.value)
        nightly_lane_suites = self.registry.get_suites_by_lane(CILane.NIGHTLY_LANE.value)

        self.assertGreater(len(fast_lane_suites), 0)
        self.assertGreater(len(integration_lane_suites), 0)
        self.assertGreater(len(ui_lane_suites), 0)
        self.assertGreaterEqual(len(nightly_lane_suites), 34)

        # Check lane marker definitions
        self.assertEqual(
            LANE_MARKER_MAP[CILane.FAST_LANE],
            [TestMarker.UNIT, TestMarker.CONTRACT, TestMarker.SECURITY],
        )

    def test_invalid_markers_and_unknown_lanes_rejected(self):
        """Invalid Inputs: Rejects unregistered markers and unknown CI discovery lanes."""
        # 1. Unregistered marker
        with self.assertRaises(TestMarkerError) as ctx:
            self.registry.assert_valid_marker("random_smoke_tag")
        self.assertIn("Unregistered test marker", str(ctx.exception))

        with self.assertRaises(TestMarkerError):
            self.registry.get_suites_by_marker("unregistered_marker")

        # 2. Unknown CI lane
        with self.assertRaises(TestMarkerError) as ctx:
            self.registry.get_suites_by_lane("invalid_ci_lane")
        self.assertIn("Unknown CI lane", str(ctx.exception))

        # 3. Non-existent prompt ID
        with self.assertRaises(TestMarkerError):
            self.registry.get_suite("P999_NON_EXISTENT")

    def test_security_marker_and_tenant_isolation_boundary(self):
        """Security: Validates that security marker isolates tenant and RBAC verification suites."""
        security_suites = self.registry.get_suites_by_marker(TestMarker.SECURITY.value)
        self.assertGreaterEqual(len(security_suites), 6)

        prompt_ids = {s.prompt_id for s in security_suites}
        self.assertIn("P005", prompt_ids)  # Permission Personas
        self.assertIn("P022", prompt_ids)  # Env Isolation
        self.assertIn("P027", prompt_ids)  # Pre-Commit Hooks
        self.assertIn("P031", prompt_ids)  # Redis Worker Boundary
        self.assertIn("P032", prompt_ids)  # Visual Regression Security
        self.assertIn("P033", prompt_ids)  # Mock Data Factory PHI & Tenant

    def test_strict_pytest_markers_configuration_parity(self):
        """Source-Specific Rule: Enforces that pyproject.toml registers all 8 markers with --strict-markers."""
        root_pyproject = _ROOT_DIR / "pyproject.toml"
        self.assertTrue(root_pyproject.exists())

        content = root_pyproject.read_text(encoding="utf-8")
        self.assertIn("[tool.pytest.ini_options]", content)
        self.assertIn("--strict-markers", content)

        for marker in TestMarker:
            self.assertIn(f'"{marker.value}:', content)

    def test_cli_runner_tool_execution(self):
        """CLI Toolchain: Verifies shared/tools/test_runner_cli.py executes with exit code 0."""
        cli_tool = _ROOT_DIR / "shared" / "tools" / "test_runner_cli.py"
        self.assertTrue(cli_tool.exists())

        # 1. Audit markers
        proc_audit = subprocess.run(
            [sys.executable, str(cli_tool), "--audit-markers"], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc_audit.returncode, 0, f"Audit failed:\n{proc_audit.stdout}\n{proc_audit.stderr}")
        self.assertIn("[AUDIT PASS]", proc_audit.stdout)
        self.assertIn("Full 8-Marker Coverage: PASS", proc_audit.stdout)

        # 2. List markers
        proc_list = subprocess.run(
            [sys.executable, str(cli_tool), "--list-markers"], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc_list.returncode, 0)
        self.assertIn("unit", proc_list.stdout)
        self.assertIn("offline_fault", proc_list.stdout)

    def test_adr_0034_documentation_parity(self):
        """Documentation: Ensures ADR-0034 decision record exists and contains required sections."""
        adr_file = (
            _ROOT_DIR
            / "shared"
            / "docs"
            / "architecture"
            / "decisions"
            / "ADR-0034-test-taxonomy-markers-and-ci-discovery.md"
        )
        self.assertTrue(adr_file.exists(), f"ADR-0034 not found at {adr_file}")

        content = adr_file.read_text(encoding="utf-8")
        self.assertIn("ADR-0034", content)
        self.assertIn("Multi-Tier Test Taxonomy", content)
        self.assertIn("fast_lane", content)
        self.assertIn("strict-markers", content)


if __name__ == "__main__":
    unittest.main()
