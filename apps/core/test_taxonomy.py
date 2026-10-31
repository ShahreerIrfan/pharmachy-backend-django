"""
apps/core/test_taxonomy.py
Architecture Decision: ADR-0012, ADR-0019, ADR-0026 & ADR-0034
Phase 02: Repository, Toolchain & Engineering Automation

Canonical Test Taxonomy, Execution Markers & CI Discovery Engine:
- Organizes unit, integration, contract, concurrency, security, ui, offline_fault, and e2e test markers.
- Defines CI discovery lanes (fast_lane, integration_lane, ui_lane, nightly_lane).
- Validates marker registration and rejects unregistered/rogue markers.
- Synchronizes cross-platform marker semantics across Python pytest, JavaFX JUnit 5, and Flutter tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Set


class TestMarkerError(Exception):
    """Raised when an unknown, unregistered, or disallowed test marker is used."""

    pass


class TestMarker(str, Enum):
    """Canonical 8-tier test markers registered in pyproject.toml and pytest (ADR-0034)."""

    UNIT = "unit"
    INTEGRATION = "integration"
    CONTRACT = "contract"
    CONCURRENCY = "concurrency"
    SECURITY = "security"
    UI = "ui"
    OFFLINE_FAULT = "offline_fault"
    E2E = "e2e"

    @classmethod
    def all_markers(cls) -> Set[str]:
        return {m.value for m in cls}


class CILane(str, Enum):
    """Standardized CI discovery execution lanes (ADR-0034)."""

    FAST_LANE = "fast_lane"
    INTEGRATION_LANE = "integration_lane"
    UI_LANE = "ui_lane"
    NIGHTLY_LANE = "nightly_lane"


LANE_MARKER_MAP: Dict[CILane, List[TestMarker]] = {
    CILane.FAST_LANE: [TestMarker.UNIT, TestMarker.CONTRACT, TestMarker.SECURITY],
    CILane.INTEGRATION_LANE: [TestMarker.INTEGRATION, TestMarker.CONCURRENCY],
    CILane.UI_LANE: [TestMarker.UI, TestMarker.OFFLINE_FAULT],
    CILane.NIGHTLY_LANE: list(TestMarker),
}


@dataclass(frozen=True)
class TestMetadata:
    """Metadata specification for an automated test suite file."""

    prompt_id: str
    name: str
    file_path: str
    primary_marker: TestMarker
    secondary_markers: List[TestMarker] = field(default_factory=list)
    timeout_seconds: int = 15

    @property
    def all_markers(self) -> Set[TestMarker]:
        return {self.primary_marker} | set(self.secondary_markers)


class TestTaxonomyRegistry:
    """Central registry and validator for all architectural test suites in the monorepo."""

    def __init__(self):
        self._suites: Dict[str, TestMetadata] = {}
        self._register_default_suites()

    def _register_default_suites(self) -> None:
        canonical_map = [
            (
                "P001",
                "Scope Specification",
                "shared/tests/test_p001_scope_specification.py",
                TestMarker.CONTRACT,
                [TestMarker.UNIT],
            ),
            (
                "P002",
                "Bounded Contexts",
                "shared/tests/test_p002_bounded_contexts.py",
                TestMarker.CONTRACT,
                [TestMarker.UNIT],
            ),
            (
                "P003",
                "Cloud Authority",
                "shared/tests/test_p003_cloud_authority.py",
                TestMarker.INTEGRATION,
                [TestMarker.CONTRACT, TestMarker.OFFLINE_FAULT],
            ),
            ("P004", "DB Policy", "shared/tests/test_p004_db_policy.py", TestMarker.CONTRACT, [TestMarker.UNIT]),
            (
                "P005",
                "Permission Personas",
                "shared/tests/test_p005_permission_personas.py",
                TestMarker.SECURITY,
                [TestMarker.UNIT],
            ),
            ("P006", "Navigation IA", "shared/tests/test_p006_navigation_ia.py", TestMarker.UNIT, []),
            (
                "P007",
                "Design Principles",
                "shared/tests/test_p007_design_principles.py",
                TestMarker.UI,
                [TestMarker.UNIT],
            ),
            (
                "P008",
                "Tokens & Themes",
                "shared/tests/test_p008_tokens_and_themes.py",
                TestMarker.UI,
                [TestMarker.UNIT],
            ),
            (
                "P009",
                "Icons & Assets",
                "shared/tests/test_p009_icons_and_assets.py",
                TestMarker.UI,
                [TestMarker.SECURITY],
            ),
            ("P010", "Localization", "shared/tests/test_p010_localization.py", TestMarker.UNIT, [TestMarker.UI]),
            (
                "P011",
                "Traceability RTM",
                "shared/tests/test_p011_traceability.py",
                TestMarker.CONTRACT,
                [TestMarker.UNIT],
            ),
            ("P012", "SLO Benchmarks", "shared/tests/test_p012_slo.py", TestMarker.INTEGRATION, []),
            (
                "P013",
                "Lifecycle Vocabulary",
                "shared/tests/test_p013_lifecycle.py",
                TestMarker.UNIT,
                [TestMarker.CONTRACT],
            ),
            ("P014", "Error Handling", "shared/tests/test_p014_errors.py", TestMarker.UNIT, [TestMarker.CONTRACT]),
            ("P015", "Feature Plans", "shared/tests/test_p015_plans.py", TestMarker.CONTRACT, [TestMarker.SECURITY]),
            (
                "P016",
                "Operational Documents",
                "shared/tests/test_p016_documents.py",
                TestMarker.UNIT,
                [TestMarker.CONTRACT],
            ),
            ("P017", "Domain Fixtures", "shared/tests/test_p017_fixtures.py", TestMarker.UNIT, [TestMarker.CONTRACT]),
            ("P018", "User Journeys", "shared/tests/test_p018_journeys.py", TestMarker.E2E, [TestMarker.INTEGRATION]),
            ("P019", "Release Milestones", "shared/tests/test_p019_milestones.py", TestMarker.CONTRACT, []),
            ("P020", "ADR Baseline", "shared/tests/test_p020_adr_baseline.py", TestMarker.CONTRACT, []),
            ("P021", "Monorepo Pillars", "shared/tests/test_p021_monorepo.py", TestMarker.UNIT, []),
            (
                "P022",
                "Env Isolation",
                "shared/tests/test_p022_env_isolation.py",
                TestMarker.SECURITY,
                [TestMarker.UNIT],
            ),
            ("P023", "JavaFX Gradle", "shared/tests/test_p023_javafx_gradle.py", TestMarker.UI, [TestMarker.CONTRACT]),
            (
                "P024",
                "Flutter Workspace",
                "shared/tests/test_p024_flutter_workspace.py",
                TestMarker.UI,
                [TestMarker.CONTRACT],
            ),
            (
                "P025",
                "Contract Repository",
                "shared/tests/test_p025_contract_repository.py",
                TestMarker.CONTRACT,
                [TestMarker.UNIT],
            ),
            (
                "P026",
                "Formatting Standards",
                "shared/tests/test_p026_formatting_standards.py",
                TestMarker.UNIT,
                [TestMarker.CONTRACT],
            ),
            (
                "P027",
                "Pre-Commit Hooks",
                "shared/tests/test_p027_precommit_hooks.py",
                TestMarker.SECURITY,
                [TestMarker.UNIT],
            ),
            (
                "P028",
                "Git Strategy",
                "shared/tests/test_p028_git_strategy.py",
                TestMarker.SECURITY,
                [TestMarker.CONTRACT],
            ),
            (
                "P029",
                "SQLite Bootstrap",
                "shared/tests/test_p029_sqlite_bootstrap.py",
                TestMarker.INTEGRATION,
                [TestMarker.UNIT],
            ),
            (
                "P030",
                "PostgreSQL Service",
                "shared/tests/test_p030_postgres_service.py",
                TestMarker.CONCURRENCY,
                [TestMarker.INTEGRATION],
            ),
            (
                "P031",
                "Redis Async Boundary",
                "shared/tests/test_p031_redis_async_boundary.py",
                TestMarker.INTEGRATION,
                [TestMarker.SECURITY],
            ),
            (
                "P032",
                "Visual Regression",
                "shared/tests/test_p032_visual_regression.py",
                TestMarker.UI,
                [TestMarker.SECURITY],
            ),
            (
                "P033",
                "Mock Data Factory",
                "shared/tests/test_p033_mock_data_factory.py",
                TestMarker.UNIT,
                [TestMarker.SECURITY, TestMarker.INTEGRATION],
            ),
            (
                "P034",
                "Test Directory & Markers",
                "shared/tests/test_p034_test_directory_and_markers.py",
                TestMarker.UNIT,
                [TestMarker.CONTRACT],
            ),
            (
                "P035",
                "Structured Logging",
                "shared/tests/test_p035_structured_logging.py",
                TestMarker.INTEGRATION,
                [TestMarker.SECURITY, TestMarker.UNIT, TestMarker.CONTRACT],
            ),
            (
                "P036",
                "Developer Quickstart",
                "shared/tests/test_p036_developer_quickstart.py",
                TestMarker.INTEGRATION,
                [TestMarker.UNIT, TestMarker.CONTRACT, TestMarker.SECURITY],
            ),
            (
                "P037",
                "Environment Secrets",
                "shared/tests/test_p037_environment_secrets.py",
                TestMarker.SECURITY,
                [TestMarker.INTEGRATION, TestMarker.UNIT, TestMarker.CONTRACT],
            ),
            (
                "P038",
                "Release Version and Changelog",
                "shared/tests/test_p038_release_versioning.py",
                TestMarker.CONTRACT,
                [TestMarker.INTEGRATION, TestMarker.UNIT, TestMarker.SECURITY],
            ),
        ]

        for pid, name, path, primary, secondaries in canonical_map:
            self._suites[pid] = TestMetadata(
                prompt_id=pid,
                name=name,
                file_path=path,
                primary_marker=primary,
                secondary_markers=secondaries,
            )

    def get_suite(self, prompt_id: str) -> TestMetadata:
        if prompt_id not in self._suites:
            raise TestMarkerError(f"Test suite '{prompt_id}' not found in taxonomy registry.")
        return self._suites[prompt_id]

    def list_suites(self) -> List[TestMetadata]:
        return list(self._suites.values())

    def get_suites_by_marker(self, marker: str) -> List[TestMetadata]:
        self.assert_valid_marker(marker)
        target = TestMarker(marker)
        return [s for s in self._suites.values() if target in s.all_markers]

    def get_suites_by_lane(self, lane: str) -> List[TestMetadata]:
        try:
            target_lane = CILane(lane)
        except ValueError:
            raise TestMarkerError(f"Unknown CI lane '{lane}'. Allowed lanes: {[l.value for l in CILane]}")

        allowed_markers = set(LANE_MARKER_MAP[target_lane])
        return [s for s in self._suites.values() if any(m in allowed_markers for m in s.all_markers)]

    @classmethod
    def assert_valid_marker(cls, marker: str) -> None:
        """Validates that a marker string is an approved canonical test marker."""
        if marker not in TestMarker.all_markers():
            raise TestMarkerError(
                f"Unregistered test marker '{marker}'. Must match approved canonical taxonomy: "
                f"{sorted(TestMarker.all_markers())} (ADR-0034)."
            )

    def audit_coverage(self) -> Dict[str, Any]:
        """Audits taxonomy coverage and distribution across all 8 markers."""
        distribution: Dict[str, int] = {m.value: 0 for m in TestMarker}
        for s in self._suites.values():
            for m in s.all_markers:
                distribution[m.value] += 1

        missing_markers = [m for m, count in distribution.items() if count == 0]
        return {
            "total_suites": len(self._suites),
            "marker_distribution": distribution,
            "has_full_marker_coverage": len(missing_markers) == 0,
            "missing_markers": missing_markers,
        }
