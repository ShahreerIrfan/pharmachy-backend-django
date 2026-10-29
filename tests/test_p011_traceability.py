"""
Unit tests for P011 - Functional Requirement Traceability Matrix (RTM v2.0)
Validates:
1. Zero orphan requirements.
2. Zero unmapped REST endpoints.
3. Exact counts: 82 Functional Requirements (FR-001 to FR-082) and 20 Non-Functional Requirements (NFR-01 to NFR-20).
4. Validation rejection for invalid/incomplete input and unmapped endpoints.
5. Role and tenant security coverage across all operational layers.
"""

import json
import os
import unittest

from apps.core.traceability import (
    CANONICAL_REST_ENDPOINTS,
    RequirementsTraceabilityMatrix,
    RequirementTrace,
)


class TestRequirementsTraceabilityMatrix(unittest.TestCase):
    """Verifies RTM v2.0 structural completeness and invariant guarantees."""

    def setUp(self):
        self.rtm = RequirementsTraceabilityMatrix()

    def test_canonical_rtm_counts(self):
        """Matrix must have exactly 82 FRs and 20 NFRs, totaling 102 items."""
        self.assertEqual(self.rtm.fr_count(), 82, "Must contain exactly 82 Functional Requirements")
        self.assertEqual(self.rtm.nfr_count(), 20, "Must contain exactly 20 Non-Functional Requirements")
        self.assertEqual(self.rtm.total_count(), 102, "Total requirements must be 102")

    def test_all_fr_ids_present_sequentially(self):
        """FR-001 through FR-082 must exist with no missing numbers or duplicates."""
        for i in range(1, 83):
            req_id = f"FR-{i:03d}"
            item = self.rtm.get(req_id)
            self.assertIsNotNone(item, f"Missing functional requirement {req_id}")
            self.assertEqual(item.category, "FR")

    def test_all_nfr_ids_present_sequentially(self):
        """NFR-01 through NFR-20 must exist with no missing numbers or duplicates."""
        for i in range(1, 21):
            req_id = f"NFR-{i:02d}"
            item = self.rtm.get(req_id)
            self.assertIsNotNone(item, f"Missing non-functional requirement {req_id}")
            self.assertEqual(item.category, "NFR")

    def test_zero_orphan_requirements_invariant(self):
        """Every requirement must trace to Models, Endpoints, Screens, Permissions, and Tests."""
        for req in self.rtm.requirements:
            self.assertTrue(len(req.models) > 0, f"Orphan requirement {req.req_id}: models cannot be empty")
            self.assertTrue(len(req.endpoints) > 0, f"Orphan requirement {req.req_id}: endpoints cannot be empty")
            self.assertTrue(len(req.screens) > 0, f"Orphan requirement {req.req_id}: screens cannot be empty")
            self.assertTrue(len(req.permissions) > 0, f"Orphan requirement {req.req_id}: permissions cannot be empty")
            self.assertTrue(len(req.test_targets) > 0, f"Orphan requirement {req.req_id}: test_targets cannot be empty")
            self.assertIn(req.priority, ("M", "S", "P"), f"Invalid priority for {req.req_id}")

    def test_zero_unmapped_rest_endpoints_invariant(self):
        """Every canonical REST endpoint must map to at least one requirement."""
        unmapped = self.rtm.get_unmapped_endpoints()
        self.assertEqual(
            len(unmapped), 0, f"Zero unmapped endpoints invariant violated! Unmapped: {sorted(list(unmapped))}"
        )

    def test_validate_integrity_passes_canonical(self):
        """Full integrity validation passes cleanly for canonical matrix."""
        # Should execute without throwing any exception
        self.rtm.validate_integrity()

    def test_validation_rejects_missing_title(self):
        """Validator rejects requirement with empty title."""
        invalid_req = RequirementTrace(
            req_id="FR-001",
            title="",
            section="5.1 Tenant",
            priority="M",
            category="FR",
            bounded_context="apps.identity",
            models=["Tenant"],
            endpoints=["/api/v1/tenants/me/"],
            screens=["/app/staff"],
            permissions=["TENANT_OWNER"],
            test_targets=["tests.test_p001"],
        )
        with self.assertRaises(ValueError) as ctx:
            invalid_req.validate()
        self.assertIn("must have a non-empty title", str(ctx.exception))

    def test_validation_rejects_invalid_id_format(self):
        """Validator rejects malformed requirement IDs."""
        invalid_req = RequirementTrace(
            req_id="INVALID-123",
            title="Valid title",
            section="5.1 Tenant",
            priority="M",
            category="FR",
            bounded_context="apps.identity",
            models=["Tenant"],
            endpoints=["/api/v1/tenants/me/"],
            screens=["/app/staff"],
            permissions=["TENANT_OWNER"],
            test_targets=["tests.test_p001"],
        )
        with self.assertRaises(ValueError) as ctx:
            invalid_req.validate()
        self.assertIn("Invalid requirement ID format", str(ctx.exception))

    def test_validation_rejects_orphan_requirement(self):
        """Validator rejects requirement with empty test targets or endpoints."""
        orphan_req = RequirementTrace(
            req_id="FR-001",
            title="Valid title",
            section="5.1 Tenant",
            priority="M",
            category="FR",
            bounded_context="apps.identity",
            models=["Tenant"],
            endpoints=["/api/v1/tenants/me/"],
            screens=["/app/staff"],
            permissions=["TENANT_OWNER"],
            test_targets=[],  # Empty test targets!
        )
        with self.assertRaises(ValueError) as ctx:
            orphan_req.validate()
        self.assertIn("orphan requirement", str(ctx.exception))

    def test_validation_rejects_unmapped_endpoint_in_catalog(self):
        """Matrix integrity check fails if an unmapped endpoint is added to catalog."""
        modified_catalog = set(CANONICAL_REST_ENDPOINTS)
        modified_catalog.add("/api/v1/unmapped/rogue-endpoint/")

        with self.assertRaises(ValueError) as ctx:
            self.rtm.validate_integrity(canonical_catalog=modified_catalog)
        self.assertIn("Unmapped REST endpoints found", str(ctx.exception))
        self.assertIn("/api/v1/unmapped/rogue-endpoint/", str(ctx.exception))

    def test_filter_by_context_and_section(self):
        """Filtering by bounded context and section returns expected slices."""
        sync_reqs = self.rtm.filter_by_context("apps.sync")
        self.assertTrue(len(sync_reqs) >= 15, "apps.sync should contain at least 15 requirements")

        section_5_5 = self.rtm.filter_by_section("5.5")
        self.assertEqual(len(section_5_5), 8, "Section 5.5 has 8 requirements (FR-034 through FR-041)")

    def test_exported_json_artifact_integrity(self):
        """docs/traceability/RTM_v2.0.json must exist and match canonical export."""
        json_path = "docs/traceability/RTM_v2.0.json"
        self.assertTrue(os.path.exists(json_path), f"Artifact file {json_path} must exist")

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["matrix_version"], "2.0.0")
        self.assertEqual(data["total_functional_requirements"], 82)
        self.assertEqual(data["total_non_functional_requirements"], 20)
        self.assertTrue(data["zero_orphan_guarantee"])
        self.assertTrue(data["zero_unmapped_endpoints_guarantee"])
        self.assertEqual(len(data["requirements"]), 102)

    def test_sensitive_compliance_and_backup_rbac(self):
        """Critical endpoints must be guarded by authorized roles, never unauthenticated."""
        antibiotic_req = self.rtm.get("FR-036")
        self.assertIn("PHARMACIST", antibiotic_req.permissions)
        self.assertNotIn("CASHIER", antibiotic_req.permissions)

        backup_req = self.rtm.get("FR-077")
        self.assertIn("PLATFORM_SUPER_ADMIN", backup_req.permissions)
        self.assertNotIn("TENANT_OWNER", backup_req.permissions)

        sync_req = self.rtm.get("FR-050")
        self.assertIn("SIGNED_DEVICE", sync_req.permissions)


if __name__ == "__main__":
    unittest.main()
