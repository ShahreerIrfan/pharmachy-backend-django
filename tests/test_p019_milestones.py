"""
Unit and Contract Tests for ADR-0019: Release and Migration Milestones Architecture
Verifies:
1. Registration of all 6 canonical milestones (M01 to M06) with bilingual labels and exit gates.
2. Sequential progression and evaluation of milestones (DAG order).
3. Dependency enforcement (MilestoneDependencyError raised if upstream is unsatisfied).
4. Mandatory gate completeness checks (FieldValidationError raised if gates are incomplete).
5. Evaluation states (NOT_STARTED, IN_PROGRESS, BLOCKED, SATISFIED).
6. Non-existent gate update rejection.
"""

import unittest

from apps.core.errors import FieldValidationError
from apps.core.milestones import (
    GateStatus,
    MilestoneDependencyError,
    MilestoneId,
    MilestoneRegistry,
)


class TestReleaseAndMigrationMilestones(unittest.TestCase):
    def setUp(self):
        self.registry = MilestoneRegistry()

    def test_all_six_milestones_registered(self):
        milestones = self.registry.list_milestones()
        self.assertEqual(len(milestones), 6)

        expected_ids = {
            MilestoneId.M01_FOUNDATION,
            MilestoneId.M02_CLOUD_CORE,
            MilestoneId.M03_OFFLINE_POS,
            MilestoneId.M04_CLINICAL_COMPLIANCE,
            MilestoneId.M05_FLUTTER_MOBILE,
            MilestoneId.M06_PILOT_AND_GA,
        }
        registered_ids = {m.milestone_id for m in milestones}
        self.assertEqual(expected_ids, registered_ids)

        for m in milestones:
            self.assertTrue(m.name_en)
            self.assertTrue(m.name_bn)
            self.assertTrue(m.phase_range)
            self.assertGreater(len(m.gates), 0)
            for g in m.gates.values():
                self.assertTrue(g.gate_id)
                self.assertTrue(g.name_en)
                self.assertTrue(g.name_bn)
                self.assertTrue(g.is_mandatory)

    def test_sequential_milestone_progression_happy_path(self):
        # 1. Satisfy M01
        m01 = self.registry.get_milestone(MilestoneId.M01_FOUNDATION)
        for gate_id in m01.gates.keys():
            self.registry.update_gate_status(
                MilestoneId.M01_FOUNDATION, gate_id, GateStatus.SATISFIED, "Verified by tests"
            )
        self.assertEqual(self.registry.evaluate_milestone(MilestoneId.M01_FOUNDATION), GateStatus.SATISFIED)
        self.registry.mark_milestone_satisfied(MilestoneId.M01_FOUNDATION)
        self.assertEqual(m01.status, GateStatus.SATISFIED)

        # 2. Satisfy M02 (Depends on M01)
        m02 = self.registry.get_milestone(MilestoneId.M02_CLOUD_CORE)
        for gate_id in m02.gates.keys():
            self.registry.update_gate_status(
                MilestoneId.M02_CLOUD_CORE, gate_id, GateStatus.SATISFIED, "Verified by DRF test suite"
            )
        self.assertEqual(self.registry.evaluate_milestone(MilestoneId.M02_CLOUD_CORE), GateStatus.SATISFIED)
        self.registry.mark_milestone_satisfied(MilestoneId.M02_CLOUD_CORE)
        self.assertEqual(m02.status, GateStatus.SATISFIED)

        # 3. Satisfy M03 & M04 (Both depend on M01 and M02)
        for mid in [MilestoneId.M03_OFFLINE_POS, MilestoneId.M04_CLINICAL_COMPLIANCE]:
            m = self.registry.get_milestone(mid)
            for gate_id in m.gates.keys():
                self.registry.update_gate_status(mid, gate_id, GateStatus.SATISFIED, "Verified by automated suite")
            self.assertEqual(self.registry.evaluate_milestone(mid), GateStatus.SATISFIED)
            self.registry.mark_milestone_satisfied(mid)
            self.assertEqual(m.status, GateStatus.SATISFIED)

        # 4. Satisfy M05 (Depends on M03 and M04)
        m05 = self.registry.get_milestone(MilestoneId.M05_FLUTTER_MOBILE)
        for gate_id in m05.gates.keys():
            self.registry.update_gate_status(
                MilestoneId.M05_FLUTTER_MOBILE, gate_id, GateStatus.SATISFIED, "Verified by Flutter tests"
            )
        self.assertEqual(self.registry.evaluate_milestone(MilestoneId.M05_FLUTTER_MOBILE), GateStatus.SATISFIED)
        self.registry.mark_milestone_satisfied(MilestoneId.M05_FLUTTER_MOBILE)
        self.assertEqual(m05.status, GateStatus.SATISFIED)

        # 5. Satisfy M06 (Depends on M03, M04, M05)
        m06 = self.registry.get_milestone(MilestoneId.M06_PILOT_AND_GA)
        for gate_id in m06.gates.keys():
            self.registry.update_gate_status(
                MilestoneId.M06_PILOT_AND_GA, gate_id, GateStatus.SATISFIED, "Verified by 30-day pilot"
            )
        self.assertEqual(self.registry.evaluate_milestone(MilestoneId.M06_PILOT_AND_GA), GateStatus.SATISFIED)
        self.registry.mark_milestone_satisfied(MilestoneId.M06_PILOT_AND_GA)
        self.assertEqual(m06.status, GateStatus.SATISFIED)

    def test_upstream_dependency_blocks_progression(self):
        # M03 depends on M01 and M02. Currently M01 and M02 are NOT_STARTED.
        # Even if all M03 gates are marked satisfied:
        m03 = self.registry.get_milestone(MilestoneId.M03_OFFLINE_POS)
        for gate_id in m03.gates.keys():
            self.registry.update_gate_status(MilestoneId.M03_OFFLINE_POS, gate_id, GateStatus.SATISFIED)

        # evaluate_milestone must return BLOCKED
        self.assertEqual(self.registry.evaluate_milestone(MilestoneId.M03_OFFLINE_POS), GateStatus.BLOCKED)

        # mark_milestone_satisfied must raise MilestoneDependencyError
        with self.assertRaises(MilestoneDependencyError) as ctx:
            self.registry.mark_milestone_satisfied(MilestoneId.M03_OFFLINE_POS)
        self.assertIn("Upstream dependency", str(ctx.exception))

    def test_incomplete_mandatory_gates_rejection(self):
        # M01 has 4 mandatory gates. Satisfy only 3 of them.
        self.registry.update_gate_status(MilestoneId.M01_FOUNDATION, "GATE-M01-01", GateStatus.SATISFIED)
        self.registry.update_gate_status(MilestoneId.M01_FOUNDATION, "GATE-M01-02", GateStatus.SATISFIED)
        self.registry.update_gate_status(MilestoneId.M01_FOUNDATION, "GATE-M01-03", GateStatus.SATISFIED)
        # GATE-M01-04 left as NOT_STARTED

        self.assertEqual(self.registry.evaluate_milestone(MilestoneId.M01_FOUNDATION), GateStatus.NOT_STARTED)

        with self.assertRaises(FieldValidationError) as ctx:
            self.registry.mark_milestone_satisfied(MilestoneId.M01_FOUNDATION)
        self.assertIn("GATE-M01-04", ctx.exception.field_errors["gates"])

    def test_update_non_existent_gate_rejection(self):
        with self.assertRaises(FieldValidationError) as ctx:
            self.registry.update_gate_status(
                MilestoneId.M01_FOUNDATION,
                "GATE-NON-EXISTENT",
                GateStatus.SATISFIED,
            )
        self.assertIn("gate_id", ctx.exception.field_errors)

    def test_sign_off_milestone_and_export_matrix(self):
        # Satisfy M01 gates
        m01 = self.registry.get_milestone(MilestoneId.M01_FOUNDATION)
        for gate_id in m01.gates.keys():
            self.registry.update_gate_status(MilestoneId.M01_FOUNDATION, gate_id, GateStatus.SATISFIED)

        # Sign-off by RELEASE_ENGINEER
        receipt = self.registry.sign_off_milestone(
            milestone_id=MilestoneId.M01_FOUNDATION,
            actor_role="RELEASE_ENGINEER",
            signed_by="Dr. Tariq Hasan (Lead Enterprise Architect)",
        )
        self.assertEqual(receipt["status"], "SATISFIED")
        self.assertEqual(receipt["role"], "RELEASE_ENGINEER")
        self.assertEqual(receipt["sign_off_certificate"], "CERT-M01-FOUNDATION-SATISFIED")

        # Export telemetry matrix
        matrix = self.registry.export_milestone_matrix()
        self.assertEqual(len(matrix), 6)
        self.assertEqual(matrix["M01-FOUNDATION"]["status"], "SATISFIED")
        self.assertEqual(matrix["M01-FOUNDATION"]["percent_complete"], 100)
        self.assertEqual(matrix["M02-CLOUD-CORE"]["percent_complete"], 0)

    def test_unauthorized_and_cross_tenant_milestone_mutation(self):
        # Satisfy M01 gates
        m01 = self.registry.get_milestone(MilestoneId.M01_FOUNDATION)
        for gate_id in m01.gates.keys():
            self.registry.update_gate_status(MilestoneId.M01_FOUNDATION, gate_id, GateStatus.SATISFIED)

        # 1. Cashier role blocked
        with self.assertRaises(PermissionError) as ctx:
            self.registry.sign_off_milestone(
                milestone_id=MilestoneId.M01_FOUNDATION,
                actor_role="CASHIER",
                signed_by="Kamrul Cashier",
            )
        self.assertIn("unauthorized to sign off milestone", str(ctx.exception))

        # 2. Tenant user blocked from cross-tenant platform milestone mutation
        with self.assertRaises(PermissionError) as ctx:
            self.registry.sign_off_milestone(
                milestone_id=MilestoneId.M01_FOUNDATION,
                actor_role="RELEASE_ENGINEER",
                signed_by="Tenant Staff",
                actor_tenant_id="tenant-apollo-dhaka",
            )
        self.assertIn("cannot mutate cross-tenant release milestones", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
