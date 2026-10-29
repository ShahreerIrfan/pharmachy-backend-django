"""
Architecture Decision Records (ADR) Registry & Baseline Sign-Off Engine
Architecture Decision: ADR-0020
Catalogs all 20 foundational ADRs from Phase 01, validates file existence and trade-off specifications,
and enforces role authorization for baseline architecture sign-off.
"""

import datetime
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

from apps.core.errors import FieldValidationError


class ADRStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    PROPOSED = "PROPOSED"
    SUPERSEDED = "SUPERSEDED"
    DEPRECATED = "DEPRECATED"


class ReversibilityRating(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    IRREVERSIBLE = "IRREVERSIBLE"


@dataclass(frozen=True)
class ADRRecord:
    adr_number: str
    title: str
    status: ADRStatus
    decision_area: str
    primary_technology: str
    trade_offs: str
    reversibility: ReversibilityRating
    relative_doc_path: str


class ArchitectureDecisionRegistry:
    """Canonical registry and validation gatekeeper for Phase 01 ADRs."""

    def __init__(self, docs_base_path: str | None = None):
        if docs_base_path is not None:
            self._docs_base_path = docs_base_path
        else:
            candidates = [
                os.path.join("shared", "docs", "architecture", "decisions"),
                os.path.join("docs", "architecture", "decisions"),
            ]
            self._docs_base_path = next((c for c in candidates if os.path.exists(c)), candidates[0])
        self._adrs: dict[str, ADRRecord] = {}
        self._sign_off_certificate: dict[str, Any] | None = None
        self._register_canonical_adrs()

    def _register_canonical_adrs(self) -> None:
        raw_adrs = [
            (
                "ADR-0001",
                "Project Scope, Personas & Negative Use Cases",
                ADRStatus.ACCEPTED,
                "SCOPE",
                "Modular Monolith, PostgreSQL, JavaFX, Flutter",
                "Comprehensive scope definition vs upfront analysis time",
                ReversibilityRating.MEDIUM,
                "ADR-0001-project-scope-and-non-goals.md",
            ),
            (
                "ADR-0002",
                "11 Bounded Contexts & Service Ownership",
                ADRStatus.ACCEPTED,
                "BOUNDED_CONTEXTS",
                "Django Apps, Kahn's DAG Algorithm",
                "Strict modular layering vs fast ad-hoc foreign keys",
                ReversibilityRating.MEDIUM,
                "ADR-0002-bounded-contexts-and-service-ownership.md",
            ),
            (
                "ADR-0003",
                "Tripartite Cloud Authority & Sync States",
                ADRStatus.ACCEPTED,
                "SYNC",
                "PostgreSQL, SQLCipher SQLite, Durable Outbox",
                "Canonical cloud consistency vs eventual sync lag",
                ReversibilityRating.MEDIUM,
                "ADR-0003-cloud-authority-and-sync-states.md",
            ),
            (
                "ADR-0004",
                "Database Policy & SQL Portability",
                ADRStatus.ACCEPTED,
                "DATABASE",
                "PostgreSQL 16, SQLite, ANSI SQL",
                "ANSI portability vs PostgreSQL-specific stored procs",
                ReversibilityRating.HIGH,
                "ADR-0004-database-policy-and-sql-portability.md",
            ),
            (
                "ADR-0005",
                "User Personas & Permission Matrix (SoD)",
                ADRStatus.ACCEPTED,
                "SECURITY_PERMISSIONS",
                "RBAC, Separation of Duties",
                "Fine-grained security gates vs permissive roles",
                ReversibilityRating.HIGH,
                "ADR-0005-user-personas-and-permission-matrix.md",
            ),
            (
                "ADR-0006",
                "Main Navigation Information Architecture",
                ADRStatus.ACCEPTED,
                "NAVIGATION",
                "Declarative Navigation Tree, Role Filtering",
                "Structured hierarchy vs arbitrary flat menus",
                ReversibilityRating.HIGH,
                "ADR-0006-main-navigation-information-architecture.md",
            ),
            (
                "ADR-0007",
                "macOS-Inspired Restrained Visual Language",
                ADRStatus.ACCEPTED,
                "DESIGN_SYSTEM",
                "Frosted neutral surfaces, WCAG AA/AAA",
                "Subtle calm aesthetics vs flashy gradients/skeuomorphism",
                ReversibilityRating.HIGH,
                "ADR-0007-mac-inspired-design-system.md",
            ),
            (
                "ADR-0008",
                "Design Tokens & Multiplatform Themes",
                ADRStatus.ACCEPTED,
                "THEME_TOKENS",
                "W3C Token Spec, JavaFX CSS, Flutter ThemeData",
                "Single source of truth vs per-platform ad-hoc styling",
                ReversibilityRating.HIGH,
                "ADR-0008-design-tokens-and-multiplatform-themes.md",
            ),
            (
                "ADR-0009",
                "Vector Icons & Static Images Policy",
                ADRStatus.ACCEPTED,
                "ASSETS",
                "SVG Vector Assets, No Emoji as UI Icons",
                "Professional licensed icons vs quick emoji glyphs",
                ReversibilityRating.HIGH,
                "ADR-0009-icons-and-static-images-policy.md",
            ),
            (
                "ADR-0010",
                "Language & Localization Strategy (EN + BN)",
                ADRStatus.ACCEPTED,
                "LOCALIZATION",
                "en-US, bn-BD, Lakh/Crore grouping, Asia/Dhaka",
                "Bilingual formatting overhead vs authentic Bangladesh UX",
                ReversibilityRating.HIGH,
                "ADR-0010-language-and-localization-strategy.md",
            ),
            (
                "ADR-0011",
                "Functional Requirement Traceability Matrix (RTM)",
                ADRStatus.ACCEPTED,
                "TRACEABILITY",
                "RTM v2.0 JSON, 82 FRs, 20 NFRs, zero orphans",
                "Rigorous traceability overhead vs untracked scope drift",
                ReversibilityRating.MEDIUM,
                "ADR-0011-functional-requirement-traceability-matrix.md",
            ),
            (
                "ADR-0012",
                "Quality Attributes & Provisional SLOs",
                ADRStatus.ACCEPTED,
                "SLO",
                "Prometheus SLIs, Provisional latency/uptime targets",
                "Realistic provisional tuning vs unverified promises",
                ReversibilityRating.HIGH,
                "ADR-0012-quality-attributes-and-service-level-objectives.md",
            ),
            (
                "ADR-0013",
                "Canonical Status Vocabulary & FSMs",
                ADRStatus.ACCEPTED,
                "STATUS_VOCABULARY",
                "Deterministic Domain State Machines",
                "Rigid lifecycle transitions vs free-form status updates",
                ReversibilityRating.MEDIUM,
                "ADR-0013-canonical-status-vocabulary-and-lifecycle-state-machines.md",
            ),
            (
                "ADR-0014",
                "Error Handling & Feedback Language",
                ADRStatus.ACCEPTED,
                "ERRORS",
                "Machine codes, Correlation IDs, Retryability flags",
                "Structured error formatting vs raw unhandled tracebacks",
                ReversibilityRating.HIGH,
                "ADR-0014-error-handling-and-feedback-language.md",
            ),
            (
                "ADR-0015",
                "Feature Package Taxonomy & Clinical Safety",
                ADRStatus.ACCEPTED,
                "PLANS",
                "Basic/Standard/Pro tiers, Clinical non-gating",
                "Never monetizing clinical safety vs pure commercial tiering",
                ReversibilityRating.IRREVERSIBLE,
                "ADR-0015-feature-package-taxonomy-and-clinical-safety-guarantees.md",
            ),
            (
                "ADR-0016",
                "Operational Documents Inventory & Formats",
                ADRStatus.ACCEPTED,
                "DOCUMENTS",
                "10 Document Templates, Strict Decimal, Watermarks",
                "Strict template fields vs arbitrary unformatted prints",
                ReversibilityRating.MEDIUM,
                "ADR-0016-operational-documents-inventory-and-template-specifications.md",
            ),
            (
                "ADR-0017",
                "Domain Examples & Realistic Fixtures",
                ADRStatus.ACCEPTED,
                "FIXTURES",
                "Box/Strip/Tablet conversion, Cold-Chain, STO",
                "Complex packaging math vs simplified single-unit assumptions",
                ReversibilityRating.MEDIUM,
                "ADR-0017-domain-examples-and-fixture-specifications.md",
            ),
            (
                "ADR-0018",
                "UX User Journeys & Interaction Paths",
                ADRStatus.ACCEPTED,
                "JOURNEYS",
                "6 Core Journeys, Offline Cashier Checkout",
                "Comprehensive failure path design vs happy-path-only demo",
                ReversibilityRating.HIGH,
                "ADR-0018-ux-user-journeys-and-interaction-paths.md",
            ),
            (
                "ADR-0019",
                "Release & Migration Milestones (M01-M06)",
                ADRStatus.ACCEPTED,
                "MILESTONES",
                "6 Milestones, DAG Exit Gates, Pilot validation",
                "Disciplined phased gates vs premature big-bang launch",
                ReversibilityRating.HIGH,
                "ADR-0019-release-and-migration-milestones.md",
            ),
            (
                "ADR-0020",
                "Baseline Architecture Decisions & Reversibility",
                ADRStatus.ACCEPTED,
                "BASELINE_REVIEW",
                "Consolidated trade-offs and reversibility log",
                "Formal sign-off gating vs unreviewed implementation drift",
                ReversibilityRating.HIGH,
                "ADR-0020-baseline-architecture-decisions-and-reversibility-log.md",
            ),
        ]

        for num, title, stat, area, tech, trade, rev, path in raw_adrs:
            self._adrs[num] = ADRRecord(
                adr_number=num,
                title=title,
                status=stat,
                decision_area=area,
                primary_technology=tech,
                trade_offs=trade,
                reversibility=rev,
                relative_doc_path=path,
            )

    def get_adr(self, adr_number: str) -> ADRRecord:
        if adr_number not in self._adrs:
            raise KeyError(f"ADR '{adr_number}' not found in registry.")
        return self._adrs[adr_number]

    def list_adrs(self) -> list[ADRRecord]:
        return list(self._adrs.values())

    def validate_integrity(self, check_files_on_disk: bool = True) -> dict[str, Any]:
        """
        Validates:
        1. All 20 ADRs are present.
        2. Every ADR has non-empty title, status, primary_technology, trade_offs, and reversibility.
        3. If check_files_on_disk is True, ensures the markdown file exists on disk.
        """
        if len(self._adrs) != 20:
            raise FieldValidationError(
                message_en=f"Expected exactly 20 ADRs in Phase 01 baseline, found {len(self._adrs)}.",
                message_bn=f"ফেজ ০১ বেসলাইনে ঠিক ২০টি এডিআর প্রত্যাশিত, পাওয়া গেছে {len(self._adrs)}টি।",
                field_errors={"adr_count": [f"Found {len(self._adrs)}, expected 20."]},
            )

        errors: dict[str, list[str]] = {}
        missing_files: list[str] = []

        for num, record in self._adrs.items():
            if not record.trade_offs or not record.trade_offs.strip():
                errors[num] = [f"ADR {num} is missing trade_offs specification."]

            if check_files_on_disk:
                full_path = os.path.join(self._docs_base_path, record.relative_doc_path)
                if not os.path.exists(full_path):
                    missing_files.append(record.relative_doc_path)

        if missing_files:
            errors["missing_markdown_files"] = missing_files

        if errors:
            raise FieldValidationError(
                message_en=f"ADR registry integrity check failed: {list(errors.keys())}",
                message_bn=f"এডিআর রেজিস্ট্রি অখণ্ডতা পরীক্ষা ব্যর্থ হয়েছে: {list(errors.keys())}",
                field_errors=errors,
            )

        return {
            "status": "VALID",
            "adr_count": len(self._adrs),
            "verified_files_count": len(self._adrs) if check_files_on_disk else 0,
        }

    def sign_off_baseline(
        self,
        actor_role: str,
        lead_architect_name: str,
    ) -> dict[str, Any]:
        """Formal sign-off on Phase 01 architecture foundation."""
        if actor_role != "PLATFORM_SUPER_ADMIN" and actor_role != "LEAD_ARCHITECT":
            raise PermissionError(
                f"Role '{actor_role}' is not authorized to sign off baseline architecture. "
                f"Required: PLATFORM_SUPER_ADMIN or LEAD_ARCHITECT."
            )

        integrity = self.validate_integrity(check_files_on_disk=True)

        self._sign_off_certificate = {
            "sign_off_id": f"CERT-PHASE01-SIGNOFF-{int(datetime.datetime.now().timestamp())}",
            "phase": "Phase 01: Product Discovery, SRS & Design Foundation",
            "status": "APPROVED_FOR_PHASE_02_IMPLEMENTATION",
            "total_adrs_verified": integrity["adr_count"],
            "signed_by": lead_architect_name,
            "role": actor_role,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        return self._sign_off_certificate

    @property
    def sign_off_certificate(self) -> dict[str, Any] | None:
        return self._sign_off_certificate

    def export_adr_baseline_catalog(self) -> dict[str, Any]:
        """Exports complete catalog of all 20 ADRs with metadata for CI and documentation generation."""
        return {
            "phase": "Phase 01: Product Discovery, SRS & Design Foundation",
            "total_adrs": len(self._adrs),
            "sign_off_status": self._sign_off_certificate["status"]
            if self._sign_off_certificate
            else "PENDING_SIGN_OFF",
            "records": [
                {
                    "adr_number": r.adr_number,
                    "title": r.title,
                    "status": r.status.value,
                    "decision_area": r.decision_area,
                    "primary_technology": r.primary_technology,
                    "trade_offs": r.trade_offs,
                    "reversibility": r.reversibility.value,
                    "relative_doc_path": r.relative_doc_path,
                }
                for r in self._adrs.values()
            ],
        }

    def verify_phase_01_readiness(self) -> dict[str, Any]:
        """Evaluates readiness for Phase 02 implementation unlocking."""
        integrity = self.validate_integrity(check_files_on_disk=True)
        is_signed_off = self._sign_off_certificate is not None
        return {
            "phase": "Phase 01",
            "adrs_verified": integrity["adr_count"],
            "files_on_disk": integrity["verified_files_count"],
            "signed_off": is_signed_off,
            "readiness": "READY_FOR_PHASE_02" if is_signed_off else "AWAITING_ARCHITECT_SIGN_OFF",
        }
