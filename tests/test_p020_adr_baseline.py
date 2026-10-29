"""
Unit and Contract Tests for ADR-0020: Baseline Architecture Decision Records & Sign-Off
Verifies:
1. Complete registration of all 20 foundational ADRs (ADR-0001 to ADR-0020).
2. Physical existence of all 20 ADR markdown documents on disk.
3. Integrity validation (trade-offs, technologies, reversibility ratings).
4. Formal baseline architecture sign-off by LEAD_ARCHITECT or PLATFORM_SUPER_ADMIN.
5. Rejection of corrupted/incomplete ADR entries (FieldValidationError).
6. Unauthorized sign-off rejection (PermissionError for Cashier/other non-architect roles).
"""

import unittest

from apps.core.adr_registry import (
    ADRRecord,
    ADRStatus,
    ArchitectureDecisionRegistry,
    ReversibilityRating,
)
from apps.core.errors import FieldValidationError


class TestBaselineArchitectureDecisions(unittest.TestCase):
    def setUp(self):
        self.registry = ArchitectureDecisionRegistry()

    def test_all_20_adrs_registered(self):
        adrs = self.registry.list_adrs()
        self.assertEqual(len(adrs), 20)

        for i in range(1, 21):
            adr_num = f"ADR-{i:04d}"
            record = self.registry.get_adr(adr_num)
            self.assertEqual(record.adr_number, adr_num)
            self.assertTrue(record.title)
            self.assertEqual(record.status, ADRStatus.ACCEPTED)
            self.assertTrue(record.decision_area)
            self.assertTrue(record.primary_technology)
            self.assertTrue(record.trade_offs)
            self.assertIsInstance(record.reversibility, ReversibilityRating)
            self.assertTrue(record.relative_doc_path.endswith(".md"))

    def test_physical_existence_of_all_20_markdown_files(self):
        # Validates that every ADR listed in the registry actually exists on disk!
        res = self.registry.validate_integrity(check_files_on_disk=True)
        self.assertEqual(res["status"], "VALID")
        self.assertEqual(res["adr_count"], 20)
        self.assertEqual(res["verified_files_count"], 20)

    def test_formal_baseline_sign_off_by_lead_architect(self):
        cert = self.registry.sign_off_baseline(
            actor_role="LEAD_ARCHITECT",
            lead_architect_name="Dr. Tariq Hasan (Lead Enterprise Architect)",
        )
        self.assertEqual(cert["status"], "APPROVED_FOR_PHASE_02_IMPLEMENTATION")
        self.assertEqual(cert["total_adrs_verified"], 20)
        self.assertEqual(cert["role"], "LEAD_ARCHITECT")
        self.assertTrue(cert["sign_off_id"].startswith("CERT-PHASE01-SIGNOFF-"))

    def test_missing_files_or_tradeoffs_rejection(self):
        # 1. Invalid docs directory where files do not exist
        fake_registry = ArchitectureDecisionRegistry(docs_base_path="non/existent/path")
        with self.assertRaises(FieldValidationError) as ctx:
            fake_registry.validate_integrity(check_files_on_disk=True)
        self.assertIn("missing_markdown_files", ctx.exception.field_errors)

        # 2. Corrupted record with missing trade-offs
        corrupted_registry = ArchitectureDecisionRegistry()
        corrupted_record = ADRRecord(
            adr_number="ADR-0001",
            title="Scope",
            status=ADRStatus.ACCEPTED,
            decision_area="SCOPE",
            primary_technology="Django",
            trade_offs="",  # Blank trade-offs!
            reversibility=ReversibilityRating.HIGH,
            relative_doc_path="ADR-0001-project-scope-and-non-goals.md",
        )
        corrupted_registry._adrs["ADR-0001"] = corrupted_record
        with self.assertRaises(FieldValidationError) as ctx:
            corrupted_registry.validate_integrity(check_files_on_disk=False)
        self.assertIn("ADR-0001", ctx.exception.field_errors)

    def test_unauthorized_sign_off_blocked(self):
        with self.assertRaises(PermissionError) as ctx:
            self.registry.sign_off_baseline(
                actor_role="CASHIER",
                lead_architect_name="Kamrul Cashier",
            )
        self.assertIn("not authorized to sign off baseline architecture", str(ctx.exception))

    def test_export_adr_baseline_catalog_and_phase_01_readiness(self):
        # 1. Before sign-off readiness check
        initial_readiness = self.registry.verify_phase_01_readiness()
        self.assertEqual(initial_readiness["adrs_verified"], 20)
        self.assertEqual(initial_readiness["files_on_disk"], 20)
        self.assertFalse(initial_readiness["signed_off"])
        self.assertEqual(initial_readiness["readiness"], "AWAITING_ARCHITECT_SIGN_OFF")

        # 2. Execute sign-off
        self.registry.sign_off_baseline(
            actor_role="LEAD_ARCHITECT",
            lead_architect_name="Dr. Tariq Hasan (Lead Enterprise Architect)",
        )

        # 3. After sign-off readiness check
        final_readiness = self.registry.verify_phase_01_readiness()
        self.assertTrue(final_readiness["signed_off"])
        self.assertEqual(final_readiness["readiness"], "READY_FOR_PHASE_02")

        # 4. Export baseline catalog
        catalog = self.registry.export_adr_baseline_catalog()
        self.assertEqual(catalog["total_adrs"], 20)
        self.assertEqual(catalog["sign_off_status"], "APPROVED_FOR_PHASE_02_IMPLEMENTATION")
        self.assertEqual(len(catalog["records"]), 20)


if __name__ == "__main__":
    unittest.main()
