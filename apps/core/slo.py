"""
Service Level Objectives (SLOs) & Quality Attributes Framework
Architecture Decision: ADR-0012
Implements NFR measurement, provisional status tagging, and tenant-safe telemetry boundaries.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class SLOStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BREACHED = "BREACHED"


class MetricScope(str, Enum):
    GLOBAL_INFRASTRUCTURE = "GLOBAL_INFRASTRUCTURE"
    TENANT_SCOPED = "TENANT_SCOPED"


@dataclass(frozen=True)
class SLIDefinition:
    sli_id: str
    nfr_id: str
    name: str
    description: str
    target_value: Decimal
    comparison: str  # '>=' or '<='
    degradation_threshold: Decimal
    breach_threshold: Decimal
    unit: str  # '%', 'ms', 's', 'min', 'hours'
    scope: MetricScope = MetricScope.TENANT_SCOPED
    is_provisional: bool = True  # SRS mandate: provisional engineering baseline

    def validate(self) -> None:
        if not self.sli_id.strip():
            raise ValueError("SLI ID cannot be empty")
        if self.comparison not in (">=", "<="):
            raise ValueError(f"Comparison must be '>=' or '<=', got {self.comparison}")
        if self.unit == "%":
            if not (Decimal("0") <= self.target_value <= Decimal("100")):
                raise ValueError(f"Percentage target must be between 0 and 100, got {self.target_value}")
        if self.comparison == ">=":
            if not (self.target_value >= self.degradation_threshold >= self.breach_threshold):
                raise ValueError("For '>=' SLIs: target >= degradation_threshold >= breach_threshold")
        elif self.comparison == "<=":
            if not (self.target_value <= self.degradation_threshold <= self.breach_threshold):
                raise ValueError("For '<=' SLIs: target <= degradation_threshold <= breach_threshold")


@dataclass(frozen=True)
class MetricObservation:
    sli_id: str
    value: Decimal
    tenant_id: str | None = None
    branch_id: str | None = None
    timestamp: datetime.datetime | None = None

    def validate(self) -> None:
        if not self.sli_id.strip():
            raise ValueError("Observation must declare an SLI ID")
        if self.value < Decimal("0"):
            raise ValueError(f"Metric value cannot be negative: {self.value}")


@dataclass(frozen=True)
class SLOEvaluationResult:
    sli_id: str
    nfr_id: str
    name: str
    observed_value: Decimal
    target_value: Decimal
    unit: str
    status: SLOStatus
    is_provisional: bool
    summary: str
    tenant_id: str | None = None


class SLOEvaluationEngine:
    """Evaluates runtime quality metrics against approved NFR SLOs."""

    def __init__(self):
        self._definitions: dict[str, SLIDefinition] = {}
        self._observations: list[MetricObservation] = []
        self._register_canonical_slis()

    def _register_canonical_slis(self) -> None:
        """Initializes canonical SLIs from SRS Section 8 (NFR-01 to NFR-20)."""
        canonical_slis = [
            SLIDefinition(
                sli_id="SLO-AVAIL-01",
                nfr_id="NFR-03",
                name="Monthly Cloud API Availability",
                description="Percentage of successful HTTP requests against Cloud PostgreSQL DRF API",
                target_value=Decimal("99.5"),
                comparison=">=",
                degradation_threshold=Decimal("99.0"),
                breach_threshold=Decimal("98.0"),
                unit="%",
                scope=MetricScope.GLOBAL_INFRASTRUCTURE,
                is_provisional=True,
            ),
            SLIDefinition(
                sli_id="SLO-LAT-POS-01",
                nfr_id="NFR-04",
                name="Local Medicine Search Latency (P95)",
                description="P95 latency of local SQLite brand/generic/barcode searches on JavaFX POS",
                target_value=Decimal("350"),
                comparison="<=",
                degradation_threshold=Decimal("500"),
                breach_threshold=Decimal("800"),
                unit="ms",
                scope=MetricScope.TENANT_SCOPED,
                is_provisional=True,
            ),
            SLIDefinition(
                sli_id="SLO-LAT-API-01",
                nfr_id="NFR-04",
                name="Online Read API Latency (P95)",
                description="P95 response time for online cloud read endpoints under agreed load",
                target_value=Decimal("800"),
                comparison="<=",
                degradation_threshold=Decimal("1200"),
                breach_threshold=Decimal("2000"),
                unit="ms",
                scope=MetricScope.GLOBAL_INFRASTRUCTURE,
                is_provisional=True,
            ),
            SLIDefinition(
                sli_id="SLO-SYNC-LAT-01",
                nfr_id="NFR-05",
                name="Sync Push Start Latency",
                description="Elapsed time from verified network reconnect to first outbox batch push",
                target_value=Decimal("30"),
                comparison="<=",
                degradation_threshold=Decimal("60"),
                breach_threshold=Decimal("120"),
                unit="s",
                scope=MetricScope.TENANT_SCOPED,
                is_provisional=True,
            ),
            SLIDefinition(
                sli_id="SLO-SYNC-ACK-01",
                nfr_id="NFR-05",
                name="Batch Sync ACK Latency (P95)",
                description="P95 latency from sync push dispatch to server ACK receipt",
                target_value=Decimal("60"),
                comparison="<=",
                degradation_threshold=Decimal("120"),
                breach_threshold=Decimal("240"),
                unit="s",
                scope=MetricScope.TENANT_SCOPED,
                is_provisional=True,
            ),
            SLIDefinition(
                sli_id="SLO-DUR-RPO-01",
                nfr_id="NFR-01",
                name="Cloud Recovery Point Objective (RPO)",
                description="Maximum allowable data loss window in transactional PostgreSQL backups",
                target_value=Decimal("15"),
                comparison="<=",
                degradation_threshold=Decimal("30"),
                breach_threshold=Decimal("60"),
                unit="min",
                scope=MetricScope.GLOBAL_INFRASTRUCTURE,
                is_provisional=True,
            ),
            SLIDefinition(
                sli_id="SLO-DUR-RTO-01",
                nfr_id="NFR-01",
                name="Cloud Recovery Time Objective (RTO)",
                description="Maximum allowable wall-clock duration to restore cloud operational services",
                target_value=Decimal("4"),
                comparison="<=",
                degradation_threshold=Decimal("8"),
                breach_threshold=Decimal("12"),
                unit="hours",
                scope=MetricScope.GLOBAL_INFRASTRUCTURE,
                is_provisional=True,
            ),
            SLIDefinition(
                sli_id="SLO-OFF-LEASE-01",
                nfr_id="NFR-19",
                name="Offline Authorization Lease Expiry",
                description="Initial proposal validity duration for signed offline JavaFX device lease",
                target_value=Decimal("72"),
                comparison="<=",
                degradation_threshold=Decimal("72"),
                breach_threshold=Decimal("96"),
                unit="hours",
                scope=MetricScope.TENANT_SCOPED,
                is_provisional=True,
            ),
        ]
        for sli in canonical_slis:
            sli.validate()
            self._definitions[sli.sli_id] = sli

    def register_sli(self, sli: SLIDefinition) -> None:
        sli.validate()
        self._definitions[sli.sli_id] = sli

    def get_sli(self, sli_id: str) -> SLIDefinition | None:
        return self._definitions.get(sli_id)

    def list_slis(self) -> list[SLIDefinition]:
        return list(self._definitions.values())

    def record_observation(self, observation: MetricObservation) -> None:
        observation.validate()
        if observation.sli_id not in self._definitions:
            raise ValueError(f"Unknown SLI ID: {observation.sli_id}")
        self._observations.append(observation)

    def evaluate_metric(
        self, sli_id: str, observed_value: Decimal, tenant_id: str | None = None
    ) -> SLOEvaluationResult:
        """Evaluates an observed value against target, degradation, and breach thresholds."""
        if observed_value < Decimal("0"):
            raise ValueError(f"Observed value cannot be negative: {observed_value}")
        if sli_id not in self._definitions:
            raise ValueError(f"Unknown SLI ID: {sli_id}")

        sli = self._definitions[sli_id]

        if sli.unit == "%" and observed_value > Decimal("100"):
            raise ValueError(f"Percentage metric cannot exceed 100%: {observed_value}")

        if sli.comparison == ">=":
            if observed_value >= sli.target_value:
                status = SLOStatus.HEALTHY
                summary = f"{sli.name} is HEALTHY ({observed_value}{sli.unit} >= target {sli.target_value}{sli.unit})"
            elif observed_value >= sli.degradation_threshold:
                status = SLOStatus.DEGRADED
                summary = f"{sli.name} is DEGRADED ({observed_value}{sli.unit} < target {sli.target_value}{sli.unit}, but >= {sli.degradation_threshold}{sli.unit})"
            else:
                status = SLOStatus.BREACHED
                summary = f"{sli.name} is BREACHED ({observed_value}{sli.unit} < critical threshold {sli.breach_threshold}{sli.unit})"
        else:  # "<="
            if observed_value <= sli.target_value:
                status = SLOStatus.HEALTHY
                summary = f"{sli.name} is HEALTHY ({observed_value}{sli.unit} <= target {sli.target_value}{sli.unit})"
            elif observed_value <= sli.degradation_threshold:
                status = SLOStatus.DEGRADED
                summary = f"{sli.name} is DEGRADED ({observed_value}{sli.unit} > target {sli.target_value}{sli.unit}, but <= {sli.degradation_threshold}{sli.unit})"
            else:
                status = SLOStatus.BREACHED
                summary = f"{sli.name} is BREACHED ({observed_value}{sli.unit} > critical threshold {sli.breach_threshold}{sli.unit})"

        return SLOEvaluationResult(
            sli_id=sli.sli_id,
            nfr_id=sli.nfr_id,
            name=sli.name,
            observed_value=observed_value,
            target_value=sli.target_value,
            unit=sli.unit,
            status=status,
            is_provisional=sli.is_provisional,
            summary=summary,
            tenant_id=tenant_id,
        )

    def query_tenant_telemetry(
        self,
        requesting_role: str,
        requesting_tenant_id: str,
        target_tenant_id: str,
        sli_id: str | None = None,
    ) -> list[MetricObservation]:
        """
        Retrieves tenant-scoped telemetry with strict tenant boundary enforcement.
        Raises PermissionError if cross-tenant read is attempted without Super Admin role.
        """
        # Super Admin can view all; tenant roles can only view their own
        if requesting_role != "PLATFORM_SUPER_ADMIN":
            if requesting_tenant_id != target_tenant_id:
                raise PermissionError(
                    f"Access Denied: Tenant '{requesting_tenant_id}' cannot access telemetry for tenant '{target_tenant_id}'"
                )

        results = [obs for obs in self._observations if obs.tenant_id == target_tenant_id]
        if sli_id:
            results = [obs for obs in results if obs.sli_id == sli_id]
        return results

    def query_global_telemetry(self, requesting_role: str) -> list[SLIDefinition]:
        """
        Retrieves global infrastructure SLO definitions.
        Restricted to platform operations and super admin personas.
        """
        if requesting_role not in ("PLATFORM_SUPER_ADMIN", "PLATFORM_SUPPORT"):
            raise PermissionError("Access Denied: Only platform administrators can view global infrastructure SLOs")
        return [sli for sli in self._definitions.values() if sli.scope == MetricScope.GLOBAL_INFRASTRUCTURE]
