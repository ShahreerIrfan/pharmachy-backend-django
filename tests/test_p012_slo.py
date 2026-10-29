"""
Unit tests for P012 - Quality Attributes and Service Level Objectives (SLOs)
Validates:
1. One valid scenario: Real SLO evaluation (HEALTHY, DEGRADED, BREACHED) and provisional flag confirmation.
2. One invalid/incomplete input case: Negative latency, percentage > 100%, and invalid thresholds rejection.
3. One unauthorized/cross-tenant case: Cross-tenant telemetry access blocked and role-restricted global SLOs.
4. Source-specific business rule: All initial performance targets marked as provisional.
"""

import unittest
from decimal import Decimal

from apps.core.slo import (
    MetricObservation,
    SLIDefinition,
    SLOEvaluationEngine,
    SLOStatus,
)


class TestQualityAttributesAndSLOs(unittest.TestCase):
    """Verifies Quality Attributes, Service Level Objectives, and multi-tenant security boundaries."""

    def setUp(self):
        self.engine = SLOEvaluationEngine()

    def test_canonical_slis_registered_and_provisional(self):
        """Source-specific rule: All initial performance targets must be provisional."""
        slis = self.engine.list_slis()
        self.assertGreaterEqual(len(slis), 8, "Must register at least 8 canonical SLIs")

        for sli in slis:
            self.assertTrue(
                sli.is_provisional, f"SLI {sli.sli_id} must be marked as provisional per SRS Section 8 mandate"
            )

    def test_valid_scenario_slo_status_evaluation(self):
        """Acceptance Criteria: One valid scenario. Demonstrates HEALTHY, DEGRADED, and BREACHED transitions."""
        # 1. Cloud API Availability (Target >= 99.5%, Degraded >= 99.0%, Breach < 99.0%)
        healthy_avail = self.engine.evaluate_metric("SLO-AVAIL-01", Decimal("99.8"))
        self.assertEqual(healthy_avail.status, SLOStatus.HEALTHY)
        self.assertTrue(healthy_avail.is_provisional)
        self.assertIn("HEALTHY", healthy_avail.summary)

        degraded_avail = self.engine.evaluate_metric("SLO-AVAIL-01", Decimal("99.2"))
        self.assertEqual(degraded_avail.status, SLOStatus.DEGRADED)

        breached_avail = self.engine.evaluate_metric("SLO-AVAIL-01", Decimal("97.5"))
        self.assertEqual(breached_avail.status, SLOStatus.BREACHED)

        # 2. Local POS Search Latency (Target <= 350ms, Degraded <= 500ms, Breach > 500ms)
        fast_pos = self.engine.evaluate_metric("SLO-LAT-POS-01", Decimal("180"))
        self.assertEqual(fast_pos.status, SLOStatus.HEALTHY)

        slow_pos = self.engine.evaluate_metric("SLO-LAT-POS-01", Decimal("420"))
        self.assertEqual(slow_pos.status, SLOStatus.DEGRADED)

        lagging_pos = self.engine.evaluate_metric("SLO-LAT-POS-01", Decimal("850"))
        self.assertEqual(lagging_pos.status, SLOStatus.BREACHED)

        # 3. Sync Push Start Latency (Target <= 30s)
        fast_sync = self.engine.evaluate_metric("SLO-SYNC-LAT-01", Decimal("12"))
        self.assertEqual(fast_sync.status, SLOStatus.HEALTHY)

    def test_invalid_input_rejection(self):
        """Acceptance Criteria: One invalid or incomplete input case."""
        # 1. Negative latency value rejected
        with self.assertRaises(ValueError) as ctx:
            self.engine.evaluate_metric("SLO-LAT-POS-01", Decimal("-15"))
        self.assertIn("cannot be negative", str(ctx.exception))

        # 2. Percentage > 100% rejected
        with self.assertRaises(ValueError) as ctx:
            self.engine.evaluate_metric("SLO-AVAIL-01", Decimal("105.0"))
        self.assertIn("cannot exceed 100%", str(ctx.exception))

        # 3. Unknown SLI ID rejected
        with self.assertRaises(ValueError) as ctx:
            self.engine.evaluate_metric("SLO-NONEXISTENT-99", Decimal("100"))
        self.assertIn("Unknown SLI ID", str(ctx.exception))

        # 4. Invalid SLIDefinition thresholds rejected
        invalid_sli = SLIDefinition(
            sli_id="SLO-BAD-01",
            nfr_id="NFR-99",
            name="Bad Thresholds",
            description="Testing invalid thresholds",
            target_value=Decimal("100"),
            comparison="<=",
            degradation_threshold=Decimal("200"),
            breach_threshold=Decimal("150"),  # Breach is less than degradation for '<='!
            unit="ms",
        )
        with self.assertRaises(ValueError) as ctx:
            invalid_sli.validate()
        self.assertIn("target <= degradation_threshold <= breach_threshold", str(ctx.exception))

    def test_unauthorized_and_cross_tenant_telemetry_isolation(self):
        """Acceptance Criteria: One unauthorized or cross-tenant case."""
        # Record observations for two different tenants
        self.engine.record_observation(
            MetricObservation(
                sli_id="SLO-LAT-POS-01",
                value=Decimal("220"),
                tenant_id="tenant-dhaka-001",
            )
        )
        self.engine.record_observation(
            MetricObservation(
                sli_id="SLO-LAT-POS-01",
                value=Decimal("310"),
                tenant_id="tenant-ctg-002",
            )
        )

        # 1. Tenant Owner of Dhaka legitimately queries own telemetry
        dhaka_obs = self.engine.query_tenant_telemetry(
            requesting_role="TENANT_OWNER",
            requesting_tenant_id="tenant-dhaka-001",
            target_tenant_id="tenant-dhaka-001",
        )
        self.assertEqual(len(dhaka_obs), 1)
        self.assertEqual(dhaka_obs[0].tenant_id, "tenant-dhaka-001")

        # 2. Cross-tenant attempt: Dhaka attempts to inspect Chittagong telemetry -> Blocked!
        with self.assertRaises(PermissionError) as ctx:
            self.engine.query_tenant_telemetry(
                requesting_role="TENANT_OWNER",
                requesting_tenant_id="tenant-dhaka-001",
                target_tenant_id="tenant-ctg-002",
            )
        self.assertIn("Access Denied", str(ctx.exception))
        self.assertIn("cannot access telemetry for tenant 'tenant-ctg-002'", str(ctx.exception))

        # 3. Platform Super Admin can query any tenant telemetry
        super_admin_obs = self.engine.query_tenant_telemetry(
            requesting_role="PLATFORM_SUPER_ADMIN",
            requesting_tenant_id="platform",
            target_tenant_id="tenant-ctg-002",
        )
        self.assertEqual(len(super_admin_obs), 1)
        self.assertEqual(super_admin_obs[0].tenant_id, "tenant-ctg-002")

        # 4. Cashier attempting to query global infrastructure SLOs -> Blocked!
        with self.assertRaises(PermissionError) as ctx:
            self.engine.query_global_telemetry(requesting_role="CASHIER")
        self.assertIn("Only platform administrators can view global infrastructure SLOs", str(ctx.exception))

        # 5. Super Admin querying global infrastructure SLOs -> Allowed
        global_slis = self.engine.query_global_telemetry(requesting_role="PLATFORM_SUPER_ADMIN")
        self.assertGreater(len(global_slis), 0)


if __name__ == "__main__":
    unittest.main()
