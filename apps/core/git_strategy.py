"""
apps/core/git_strategy.py
Architecture Decision: ADR-0028
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Git Branching Strategy, Review Policies, Migration Review Gating,
and Reversible Release Governance:
- Branch naming convention & branch protection validator.
- Pull Request checklist verification & small batch discipline.
- Database migration safety review (lock-free, zero raw drops, reversible rollbacks).
- Security and compliance review gating (payments, auth, patient PHI).
- Release governance engine with platform owner approval.
- Role-based access control (RBAC) on review sign-offs and cross-tenant boundaries.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class GitStrategySecurityError(PermissionError):
    """Raised when an unauthorized actor attempts to approve PR reviews, migrations, or release changes."""

    pass


class GitPolicyValidationError(Exception):
    """Raised when branch naming, PR checklist, migration safety, or release criteria are violated."""

    pass


class BranchType(str, Enum):
    FEAT = "feat"
    FIX = "fix"
    CHORE = "chore"
    REFACTOR = "refactor"
    MIGRATION = "migration"
    SECURITY = "security"
    RELEASE = "release"
    HOTFIX = "hotfix"


# ==============================================================================
# 1. BRANCH POLICY & NAMING CONVENTION VALIDATOR
# ==============================================================================
@dataclass(frozen=True)
class BranchValidationResult:
    is_valid: bool
    branch_name: str
    branch_type: Optional[BranchType]
    issue_id: Optional[str]
    slug: Optional[str]
    is_protected: bool
    error_message: Optional[str] = None


class BranchPolicyValidator:
    """Validates branch naming conventions and enforces branch protection rules."""

    PROTECTED_BRANCHES: Set[str] = {"master", "main", "staging"}

    # Grammar: <type>/<issue_id>-<slug> or release/v<semver>
    BRANCH_REGEX = re.compile(
        r"^(?P<type>feat|fix|chore|refactor|migration|security|release|hotfix)\/"
        r"(?:(?P<issue>P\d{3}|[A-Z]+-\d+)-(?P<slug>[a-z0-9\-]+)|v(?P<version>\d+\.\d+\.\d+(?:-[a-z0-9\.]+)?))$"
    )

    @classmethod
    def validate_branch_name(cls, branch_name: str) -> BranchValidationResult:
        trimmed = branch_name.strip()

        # Check if it is a canonical protected branch
        if trimmed in cls.PROTECTED_BRANCHES:
            return BranchValidationResult(
                is_valid=True,
                branch_name=trimmed,
                branch_type=None,
                issue_id=None,
                slug=None,
                is_protected=True,
            )

        match = cls.BRANCH_REGEX.match(trimmed)
        if not match:
            return BranchValidationResult(
                is_valid=False,
                branch_name=trimmed,
                branch_type=None,
                issue_id=None,
                slug=None,
                is_protected=False,
                error_message=(
                    f"Branch name '{trimmed}' violates ADR-0028 branch naming convention. "
                    f"Expected pattern: '<type>/<issue_id>-<slug>' (e.g. 'feat/P028-git-strategy') "
                    f"or 'release/vX.Y.Z'."
                ),
            )

        b_type_str = match.group("type")
        issue = match.group("issue") or match.group("version")
        slug = match.group("slug") or match.group("version")

        return BranchValidationResult(
            is_valid=True,
            branch_name=trimmed,
            branch_type=BranchType(b_type_str),
            issue_id=issue,
            slug=slug,
            is_protected=False,
        )

    @classmethod
    def assert_direct_push_allowed(cls, branch_name: str, actor_role: str = "DEVELOPER") -> None:
        """Enforces branch protection: direct pushes to protected branches are strictly forbidden."""
        if branch_name.strip() in cls.PROTECTED_BRANCHES:
            raise GitStrategySecurityError(
                f"Direct push to protected branch '{branch_name}' is forbidden for all roles (including '{actor_role}'). "
                f"Changes must be submitted via a Pull Request with mandatory CODEOWNERS review and CI gates."
            )


# ==============================================================================
# 2. PULL REQUEST CHECKLIST & SMALL BATCH VALIDATOR
# ==============================================================================
@dataclass
class PullRequestReview:
    pr_id: str
    title: str
    branch_name: str
    srs_traceability_id: str
    target_workspaces: List[str]
    change_type: BranchType
    lines_added: int
    lines_deleted: int
    contracts_verified: bool
    financial_precision_verified: bool
    migrations_safe: bool
    security_privacy_verified: bool
    tests_passed: bool
    rollback_plan: str
    estimated_rollback_minutes: int
    author: str
    approvals: Dict[str, str] = field(default_factory=dict)  # role -> reviewer_name

    @property
    def total_diff_lines(self) -> int:
        return self.lines_added + self.lines_deleted

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["change_type"] = self.change_type.value
        d["total_diff_lines"] = self.total_diff_lines
        return d


class PullRequestValidator:
    """Validates PR checklist completeness, traceability, and batch size discipline."""

    MAX_RECOMMENDED_DIFF_LINES: int = 400
    HARD_MAX_DIFF_LINES: int = 800

    @classmethod
    def validate_pull_request(
        cls,
        pr: PullRequestReview,
        touched_files: Optional[List[str]] = None,
    ) -> List[str]:
        violations: List[str] = []

        # 1. Branch naming
        branch_res = BranchPolicyValidator.validate_branch_name(pr.branch_name)
        if not branch_res.is_valid:
            violations.append(branch_res.error_message or "Invalid branch name.")

        # 2. SRS Traceability
        if not pr.srs_traceability_id or not pr.srs_traceability_id.strip():
            violations.append("PR is missing mandatory SRS Traceability ID (e.g. 'P028' or 'FR-018').")

        # 3. Target workspaces
        valid_workspaces = {
            "backend",
            "desktop",
            "mobile",
            "desktop-javafx",
            "mobile-flutter",
            "contracts",
            "infra",
            "tools",
            "docs",
        }
        if not pr.target_workspaces or not any(w in valid_workspaces for w in pr.target_workspaces):
            violations.append("PR must specify at least one valid target workspace.")

        # 4. Mandatory checklist booleans
        if not pr.contracts_verified:
            violations.append("Shared contracts validation must be explicitly verified.")
        if not pr.financial_precision_verified:
            violations.append("Financial precision (Decimal-only, zero float) must be explicitly confirmed.")
        if not pr.security_privacy_verified:
            violations.append("Security & patient privacy (zero leaks, tenant isolation) must be verified.")
        if not pr.tests_passed:
            violations.append("Automated test suite execution must be verified as passing.")

        # 5. Rollback plan
        if not pr.rollback_plan or len(pr.rollback_plan.strip()) < 10:
            violations.append("PR must document a clear, actionable reversibility and rollback plan (> 10 chars).")
        if pr.estimated_rollback_minutes <= 0 or pr.estimated_rollback_minutes > 60:
            violations.append("Estimated rollback time must be between 1 and 60 minutes.")

        # 6. Small batch discipline
        if pr.total_diff_lines > cls.HARD_MAX_DIFF_LINES:
            violations.append(
                f"PR size exceeds hard maximum limit: {pr.total_diff_lines} lines > {cls.HARD_MAX_DIFF_LINES} lines. "
                f"Break this pull request into smaller, independently reviewable vertical slices."
            )

        return violations


# ==============================================================================
# 3. DATABASE MIGRATION SAFETY REVIEW GATE
# ==============================================================================
@dataclass
class MigrationSafetyReport:
    is_safe: bool
    file_path: str
    operations_detected: List[str]
    hazardous_patterns: List[str]
    has_rollback_script: bool
    sign_off_status: str
    signed_by: Optional[str] = None


class MigrationSafetyReviewer:
    """Enforces zero destructive schema drops, safe defaults, and rollback readiness."""

    HAZARDOUS_OPERATIONS = [
        (re.compile(r"""migrations\.RemoveField\(""", re.IGNORECASE), "Destructive RemoveField operation detected."),
        (re.compile(r"""migrations\.DeleteModel\(""", re.IGNORECASE), "Destructive DeleteModel operation detected."),
        (
            re.compile(r"""DROP\s+(?:TABLE|COLUMN)\b""", re.IGNORECASE),
            "Raw SQL DROP TABLE or DROP COLUMN operation detected.",
        ),
        (
            re.compile(r"""migrations\.AddField\([^)]*null\s*=\s*False[^)]*\)""", re.IGNORECASE),
            "AddField with null=False without explicit safe default locks PostgreSQL table.",
        ),
    ]

    @classmethod
    def audit_migration_content(
        cls,
        file_path: str,
        content: str,
        has_rollback_plan: bool = True,
    ) -> MigrationSafetyReport:
        hazardous: List[str] = []
        operations: List[str] = []

        # Check hazardous patterns
        for pattern, msg in cls.HAZARDOUS_OPERATIONS:
            if pattern.search(content):
                hazardous.append(msg)

        # Detect operations
        op_matches = re.findall(r"""migrations\.([A-Za-z]+)\(""", content)
        operations.extend(list(set(op_matches)))

        is_safe = len(hazardous) == 0 and has_rollback_plan

        return MigrationSafetyReport(
            is_safe=is_safe,
            file_path=file_path,
            operations_detected=operations,
            hazardous_patterns=hazardous,
            has_rollback_script=has_rollback_plan,
            sign_off_status="APPROVED" if is_safe else "REJECTED",
        )

    @classmethod
    def sign_off_migration(
        cls,
        report: MigrationSafetyReport,
        actor_role: str,
        actor_name: str,
    ) -> MigrationSafetyReport:
        """Signs off migration review with mandatory role authorization."""
        if actor_role not in {"DBA_LEAD", "LEAD_ARCHITECT", "PLATFORM_SUPER_ADMIN"}:
            raise GitStrategySecurityError(
                f"Role '{actor_role}' is not authorized to sign off database migrations. "
                f"Required: DBA_LEAD, LEAD_ARCHITECT, or PLATFORM_SUPER_ADMIN."
            )

        if not report.is_safe:
            raise GitPolicyValidationError(
                f"Cannot sign off unsafe migration with hazardous patterns: {report.hazardous_patterns}"
            )

        report.signed_by = f"{actor_name} ({actor_role})"
        report.sign_off_status = "SIGNED_OFF"
        return report


# ==============================================================================
# 4. SECURITY & COMPLIANCE REVIEW GATE
# ==============================================================================
class SecurityReviewGate:
    """Enforces mandatory security sign-off when sensitive modules or patient PHI are touched."""

    SENSITIVE_PATH_PATTERNS = [
        re.compile(r"apps\/core\/.*security", re.IGNORECASE),
        re.compile(r"apps\/core\/.*tenant", re.IGNORECASE),
        re.compile(r"backend\/.*payment", re.IGNORECASE),
        re.compile(r"backend\/.*auth", re.IGNORECASE),
        re.compile(r".*prescription", re.IGNORECASE),
        re.compile(r".*antibiotic", re.IGNORECASE),
    ]

    @classmethod
    def requires_security_review(cls, touched_files: List[str]) -> Tuple[bool, List[str]]:
        sensitive_files: List[str] = []
        for f in touched_files:
            for pattern in cls.SENSITIVE_PATH_PATTERNS:
                if pattern.search(f):
                    sensitive_files.append(f)
                    break
        return len(sensitive_files) > 0, sensitive_files

    @classmethod
    def verify_security_sign_off(
        cls,
        touched_files: List[str],
        approvals: Dict[str, str],
    ) -> bool:
        required, sensitive = cls.requires_security_review(touched_files)
        if not required:
            return True

        # Must have SECURITY_LEAD or LEAD_ARCHITECT approval
        authorized_roles = {"SECURITY_LEAD", "LEAD_ARCHITECT", "PLATFORM_SUPER_ADMIN"}
        has_authorized_approval = any(role in authorized_roles for role in approvals.keys())

        if not has_authorized_approval:
            raise GitPolicyValidationError(
                f"PR touches sensitive security/payment/patient-PHI modules {sensitive} "
                f"but lacks required sign-off from SECURITY_LEAD or LEAD_ARCHITECT."
            )
        return True


# ==============================================================================
# 5. RELEASE GOVERNANCE ENGINE
# ==============================================================================
@dataclass
class ReleasePackage:
    release_tag: str  # e.g. "v1.0.0"
    target_branch: str
    changelog_summary: str
    rollback_playbook: str
    regression_tests_passed: bool
    migrations_verified: bool
    platform_owner_signed: bool = False
    signed_by: Optional[str] = None
    target_tenant_scope: str = "ALL_TENANTS"


class ReleaseGovernanceEngine:
    """Governs production release creation, small batch releases, and platform owner approval."""

    RELEASE_TAG_REGEX = re.compile(
        r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<prerelease>[a-z0-9\.]+))?$"
    )

    @classmethod
    def validate_release_package(cls, pkg: ReleasePackage) -> List[str]:
        violations: List[str] = []

        if not cls.RELEASE_TAG_REGEX.match(pkg.release_tag):
            violations.append(
                f"Release tag '{pkg.release_tag}' is invalid. Expected SemVer format e.g. 'v1.0.0' or 'v1.2.0-rc1'."
            )

        if not pkg.changelog_summary or len(pkg.changelog_summary.strip()) < 15:
            violations.append("Release package must include an informative changelog summary (> 15 chars).")

        if not pkg.rollback_playbook or len(pkg.rollback_playbook.strip()) < 15:
            violations.append("Release package must include a documented rollback playbook (> 15 chars).")

        if not pkg.regression_tests_passed:
            violations.append("Release cannot proceed without verified full regression test execution.")

        if not pkg.migrations_verified:
            violations.append("Release cannot proceed without verified database migration reversibility.")

        return violations

    @classmethod
    def sign_off_release(
        cls,
        pkg: ReleasePackage,
        actor_role: str,
        actor_name: str,
        requesting_tenant_id: Optional[str] = None,
    ) -> ReleasePackage:
        """Formal sign-off on a production release by the Platform Super Admin or Lead Architect."""
        # 1. RBAC Check
        if actor_role not in {"PLATFORM_SUPER_ADMIN", "LEAD_ARCHITECT"}:
            raise GitStrategySecurityError(
                f"Role '{actor_role}' is not authorized to sign off releases. "
                f"Required: PLATFORM_SUPER_ADMIN or LEAD_ARCHITECT."
            )

        # 2. Cross-tenant scope guard: tenant actors cannot deploy custom release packages across tenants
        if requesting_tenant_id and requesting_tenant_id != "platform-global":
            raise GitStrategySecurityError(
                f"Tenant-scoped actor cannot authorize release for scope '{pkg.target_tenant_scope}'."
            )

        # 3. Validation
        violations = cls.validate_release_package(pkg)
        if violations:
            raise GitPolicyValidationError(f"Release package validation failed: {violations}")

        pkg.platform_owner_signed = True
        pkg.signed_by = f"{actor_name} ({actor_role})"
        return pkg
