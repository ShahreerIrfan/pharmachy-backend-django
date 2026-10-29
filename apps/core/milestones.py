"""
Release & Migration Milestones Engine
Architecture Decision: ADR-0019
Specifies the 6 canonical release milestones across the 23 development phases,
their DAG dependencies, and automated exit gate verification.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from apps.core.errors import FieldValidationError


class MilestoneId(str, Enum):
    M01_FOUNDATION = "M01-FOUNDATION"
    M02_CLOUD_CORE = "M02-CLOUD-CORE"
    M03_OFFLINE_POS = "M03-OFFLINE-POS"
    M04_CLINICAL_COMPLIANCE = "M04-CLINICAL-COMPLIANCE"
    M05_FLUTTER_MOBILE = "M05-FLUTTER-MOBILE"
    M06_PILOT_AND_GA = "M06-PILOT-AND-GA"


class GateStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    SATISFIED = "SATISFIED"
    BLOCKED = "BLOCKED"


class MilestoneDependencyError(Exception):
    """Raised when attempting to satisfy a milestone whose upstream dependencies are incomplete."""

    pass


@dataclass
class ExitGate:
    gate_id: str
    name_en: str
    name_bn: str
    description: str
    is_mandatory: bool
    status: GateStatus = GateStatus.NOT_STARTED
    evidence: str | None = None


@dataclass
class ReleaseMilestone:
    milestone_id: MilestoneId
    name_en: str
    name_bn: str
    phase_range: str
    dependencies: list[MilestoneId]
    gates: dict[str, ExitGate]
    status: GateStatus = GateStatus.NOT_STARTED


class MilestoneRegistry:
    """Canonical engine and gatekeeper for SaaS release milestones and exit criteria."""

    def __init__(self):
        self._milestones: dict[MilestoneId, ReleaseMilestone] = {}
        self._register_canonical_milestones()

    def _register_canonical_milestones(self) -> None:
        # M01: Architectural Foundation (Phase 01)
        self._milestones[MilestoneId.M01_FOUNDATION] = ReleaseMilestone(
            milestone_id=MilestoneId.M01_FOUNDATION,
            name_en="Architectural Foundation & Shared Primitives",
            name_bn="স্থাপত্য ভিত্তি ও শেয়ার্ড প্রিমিতিভস",
            phase_range="Phase 01 (P001 - P020)",
            dependencies=[],
            gates={
                "GATE-M01-01": ExitGate(
                    "GATE-M01-01",
                    "Kahn's DAG Cycle Check",
                    "কাহন'স ড্যাগ চক্রহীনতা যাচাই",
                    "Zero cycles across 11 bounded contexts",
                    True,
                ),
                "GATE-M01-02": ExitGate(
                    "GATE-M01-02",
                    "RTM Completeness",
                    "আরটিএম ট্রেসেবিলিটি পূর্ণতা",
                    "Zero unmapped REST endpoints in RTM v2.0",
                    True,
                ),
                "GATE-M01-03": ExitGate(
                    "GATE-M01-03",
                    "Strict Decimal Enforcement",
                    "কঠোর ডেসিমাল বাধ্যবাধকতা",
                    "No floats in currency or temperature",
                    True,
                ),
                "GATE-M01-04": ExitGate(
                    "GATE-M01-04",
                    "100% Phase 01 Unit Pass",
                    "ফেজ ০১ টেস্ট শতভাগ উত্তীর্ণ",
                    "All architectural tests pass in CI",
                    True,
                ),
            },
        )

        # M02: Cloud Core & Multi-Tenant MVP (Phases 02-05)
        self._milestones[MilestoneId.M02_CLOUD_CORE] = ReleaseMilestone(
            milestone_id=MilestoneId.M02_CLOUD_CORE,
            name_en="Cloud Core & Multi-Tenant MVP",
            name_bn="ক্লাউড কোর ও মাল্টি-টেন্যান্ট এমভিপি",
            phase_range="Phases 02 - 05 (P021 - P100)",
            dependencies=[MilestoneId.M01_FOUNDATION],
            gates={
                "GATE-M02-01": ExitGate(
                    "GATE-M02-01",
                    "Multi-Tenant Isolation",
                    "মাল্টি-টেন্যান্ট আইসোলেশন",
                    "Cross-tenant read/write blocked",
                    True,
                ),
                "GATE-M02-02": ExitGate(
                    "GATE-M02-02",
                    "DRF RBAC & OpenAPI Spec",
                    "আরবিএসি ও ওপেন-এপিআই স্পেক",
                    "34 endpoints authenticated and documented",
                    True,
                ),
                "GATE-M02-03": ExitGate(
                    "GATE-M02-03",
                    "Safe Schema Migrations",
                    "নিরাপদ ডাটাবেস মাইগ্রেশন",
                    "Forward and rollback migrations verified",
                    True,
                ),
            },
        )

        # M03: Offline-First Desktop POS (Phases 06-09)
        self._milestones[MilestoneId.M03_OFFLINE_POS] = ReleaseMilestone(
            milestone_id=MilestoneId.M03_OFFLINE_POS,
            name_en="Offline-First JavaFX Desktop POS",
            name_bn="অফলাইন-ফার্স্ট জাভাএফএক্স ডেস্কটপ পিওএস",
            phase_range="Phases 06 - 09 (P101 - P180)",
            dependencies=[MilestoneId.M01_FOUNDATION, MilestoneId.M02_CLOUD_CORE],
            gates={
                "GATE-M03-01": ExitGate(
                    "GATE-M03-01",
                    "Durable Outbox Replay",
                    "আউটবক্স রিপ্লে পরীক্ষা",
                    "100 offline sales synced with zero loss",
                    True,
                ),
                "GATE-M03-02": ExitGate(
                    "GATE-M03-02",
                    "Idempotency Deduplication",
                    "আইডেমপোটেন্সি রিট্রাই যাচাই",
                    "Duplicate UUID payloads return cached result",
                    True,
                ),
                "GATE-M03-03": ExitGate(
                    "GATE-M03-03",
                    "Offline Receipt Watermark",
                    "অফলাইন রসিদ ওয়াটারমার্ক",
                    "Pending sync receipts print watermark banner",
                    True,
                ),
            },
        )

        # M04: Specialized Clinical Compliance (Phases 10-14)
        self._milestones[MilestoneId.M04_CLINICAL_COMPLIANCE] = ReleaseMilestone(
            milestone_id=MilestoneId.M04_CLINICAL_COMPLIANCE,
            name_en="Specialized Clinical Compliance & Inventory",
            name_bn="ক্লিনিক্যাল কমপ্লায়েন্স ও ইনভেন্টরি",
            phase_range="Phases 10 - 14 (P181 - P280)",
            dependencies=[MilestoneId.M01_FOUNDATION, MilestoneId.M02_CLOUD_CORE],
            gates={
                "GATE-M04-01": ExitGate(
                    "GATE-M04-01",
                    "Expired Batch Quarantine Gate",
                    "মেয়াদোত্তীর্ণ ব্যাচ লক",
                    "Sales blocked on expired or quarantined stock",
                    True,
                ),
                "GATE-M04-02": ExitGate(
                    "GATE-M04-02",
                    "DGDA Antibiotic Register",
                    "ডিজিডিএ অ্যান্টিবায়োটিক রেজিস্টার",
                    "BMDC and patient address mandatory for sales",
                    True,
                ),
                "GATE-M04-03": ExitGate(
                    "GATE-M04-03",
                    "Cold-Chain Excursion Lock",
                    "কোল্ড-চেইন সীমা লঙ্ঘন অ্যালার্ট",
                    "Temperature outside 2-8C locks allocation",
                    True,
                ),
            },
        )

        # M05: Flutter Mobile Manager Experience (Phases 15-18)
        self._milestones[MilestoneId.M05_FLUTTER_MOBILE] = ReleaseMilestone(
            milestone_id=MilestoneId.M05_FLUTTER_MOBILE,
            name_en="Flutter Mobile Manager Experience",
            name_bn="ফ্লাটার মোবাইল ম্যানেজার অ্যাপ",
            phase_range="Phases 15 - 18 (P281 - P360)",
            dependencies=[MilestoneId.M03_OFFLINE_POS, MilestoneId.M04_CLINICAL_COMPLIANCE],
            gates={
                "GATE-M05-01": ExitGate(
                    "GATE-M05-01",
                    "Typed DRF Client Parity",
                    "টাইপড এপিআই ক্লায়েন্ট সমতা",
                    "Mobile app consumes typed OpenAPI client",
                    True,
                ),
                "GATE-M05-02": ExitGate(
                    "GATE-M05-02",
                    "Biometric Auth & Secure Store",
                    "বায়োমেট্রিক ও সুরক্ষিত সেশন",
                    "Encrypted mobile token storage verified",
                    True,
                ),
                "GATE-M05-03": ExitGate(
                    "GATE-M05-03",
                    "No Emoji Icon Restraint",
                    "ইমোজিবিহীন প্রফেশনাল আইকন",
                    "Strict Cupertino/SVG vector assets used",
                    True,
                ),
            },
        )

        # M06: Commercial Pilot & GA Launch (Phases 19-23)
        self._milestones[MilestoneId.M06_PILOT_AND_GA] = ReleaseMilestone(
            milestone_id=MilestoneId.M06_PILOT_AND_GA,
            name_en="Commercial Pilot & GA Launch",
            name_bn="বাণিজ্যিক পাইলট ও সাধারণ প্রকাশ",
            phase_range="Phases 19 - 23 (P361 - P460)",
            dependencies=[
                MilestoneId.M03_OFFLINE_POS,
                MilestoneId.M04_CLINICAL_COMPLIANCE,
                MilestoneId.M05_FLUTTER_MOBILE,
            ],
            gates={
                "GATE-M06-01": ExitGate(
                    "GATE-M06-01",
                    "30-Day Model Pharmacy Pilot",
                    "৩০ দিনের মডেল ফার্মেসি পাইলট",
                    "99.9% uptime across 3 live pharmacies",
                    True,
                ),
                "GATE-M06-02": ExitGate(
                    "GATE-M06-02",
                    "Checkout Latency < 200ms",
                    "২০০ মিলিসেকেন্ডের নিচে চেকআউট",
                    "Barcode scan and cart addition benchmarked",
                    True,
                ),
                "GATE-M06-03": ExitGate(
                    "GATE-M06-03",
                    "Disaster Recovery Verification",
                    "দুর্যোগ পুনরুদ্ধার পরীক্ষা",
                    "Database restore drill with zero data loss",
                    True,
                ),
            },
        )

    def get_milestone(self, milestone_id: MilestoneId) -> ReleaseMilestone:
        if milestone_id not in self._milestones:
            raise KeyError(f"Unknown milestone ID: {milestone_id}")
        return self._milestones[milestone_id]

    def list_milestones(self) -> list[ReleaseMilestone]:
        return list(self._milestones.values())

    def update_gate_status(
        self,
        milestone_id: MilestoneId,
        gate_id: str,
        status: GateStatus,
        evidence: str | None = None,
    ) -> None:
        milestone = self.get_milestone(milestone_id)
        if gate_id not in milestone.gates:
            raise FieldValidationError(
                message_en=f"Gate '{gate_id}' not found in milestone '{milestone_id.value}'.",
                message_bn=f"মাইলস্টোন '{milestone_id.value}'-এ গেট '{gate_id}' খুঁজে পাওয়া যায়নি।",
                field_errors={"gate_id": [f"Unknown gate '{gate_id}'"]},
            )

        gate = milestone.gates[gate_id]
        gate.status = status
        if evidence:
            gate.evidence = evidence

    def evaluate_milestone(self, milestone_id: MilestoneId) -> GateStatus:
        """
        Evaluates milestone readiness:
        1. Checks upstream dependencies: all must be SATISFIED.
        2. Checks mandatory gates: all must be SATISFIED.
        """
        milestone = self.get_milestone(milestone_id)

        # Check upstream DAG dependencies
        for dep_id in milestone.dependencies:
            dep_milestone = self.get_milestone(dep_id)
            if dep_milestone.status != GateStatus.SATISFIED:
                milestone.status = GateStatus.BLOCKED
                return GateStatus.BLOCKED

        # Check mandatory gates
        all_satisfied = True
        any_in_progress = False
        any_blocked = False

        for gate in milestone.gates.values():
            if gate.is_mandatory:
                if gate.status == GateStatus.BLOCKED:
                    any_blocked = True
                elif gate.status != GateStatus.SATISFIED:
                    all_satisfied = False
                    if gate.status == GateStatus.IN_PROGRESS:
                        any_in_progress = True

        if any_blocked:
            milestone.status = GateStatus.BLOCKED
        elif all_satisfied:
            milestone.status = GateStatus.SATISFIED
        elif any_in_progress:
            milestone.status = GateStatus.IN_PROGRESS
        else:
            milestone.status = GateStatus.NOT_STARTED

        return milestone.status

    def mark_milestone_satisfied(self, milestone_id: MilestoneId) -> None:
        """Explicitly transitions milestone to SATISFIED after verifying all gates and dependencies."""
        milestone = self.get_milestone(milestone_id)

        # Dependency check
        for dep_id in milestone.dependencies:
            dep_milestone = self.get_milestone(dep_id)
            if dep_milestone.status != GateStatus.SATISFIED:
                raise MilestoneDependencyError(
                    f"Cannot satisfy milestone '{milestone_id.value}': Upstream dependency '{dep_id.value}' is not satisfied (status: {dep_milestone.status.value})."
                )

        # Gate completeness check
        unsatisfied_gates = [
            g.gate_id for g in milestone.gates.values() if g.is_mandatory and g.status != GateStatus.SATISFIED
        ]
        if unsatisfied_gates:
            raise FieldValidationError(
                message_en=f"Cannot satisfy milestone '{milestone_id.value}': Mandatory gates unsatisfied: {unsatisfied_gates}.",
                message_bn=f"মাইলস্টোন '{milestone_id.value}' সন্তুষ্ট করা সম্ভব নয়: পূরণ না হওয়া গেট: {unsatisfied_gates}।",
                field_errors={"gates": unsatisfied_gates},
            )

        milestone.status = GateStatus.SATISFIED

    def sign_off_milestone(
        self,
        milestone_id: MilestoneId,
        actor_role: str,
        signed_by: str,
        actor_tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Signs off on a milestone transition to SATISFIED.
        Security: Only PLATFORM_SUPER_ADMIN or RELEASE_ENGINEER may sign off.
        Tenant-scoped users (e.g. CASHIER, TENANT_OWNER) are strictly blocked.
        """
        if actor_role not in {"PLATFORM_SUPER_ADMIN", "RELEASE_ENGINEER"}:
            raise PermissionError(
                f"Role '{actor_role}' is unauthorized to sign off milestone '{milestone_id.value}'. "
                f"Platform governance authority required."
            )

        if actor_tenant_id is not None and actor_role != "PLATFORM_SUPER_ADMIN":
            raise PermissionError(f"Tenant user '{actor_tenant_id}' cannot mutate cross-tenant release milestones.")

        # Verify eligibility and transition
        self.mark_milestone_satisfied(milestone_id)

        milestone = self.get_milestone(milestone_id)
        return {
            "milestone_id": milestone.milestone_id.value,
            "status": milestone.status.value,
            "signed_by": signed_by,
            "role": actor_role,
            "gates_verified": len(milestone.gates),
            "sign_off_certificate": f"CERT-{milestone.milestone_id.value}-SATISFIED",
        }

    def export_milestone_matrix(self) -> dict[str, Any]:
        """Exports full milestone matrix for telemetry and CI dashboard integration."""
        res: dict[str, Any] = {}
        for m in self._milestones.values():
            satisfied_gates = sum(1 for g in m.gates.values() if g.status == GateStatus.SATISFIED)
            total_gates = len(m.gates)
            res[m.milestone_id.value] = {
                "name_en": m.name_en,
                "name_bn": m.name_bn,
                "phase_range": m.phase_range,
                "status": m.status.value,
                "dependencies": [d.value for d in m.dependencies],
                "gates_summary": f"{satisfied_gates}/{total_gates}",
                "percent_complete": int((satisfied_gates / total_gates) * 100) if total_gates > 0 else 0,
            }
        return res
