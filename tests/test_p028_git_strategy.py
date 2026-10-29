"""
tests/test_p028_git_strategy.py
Architecture Decision: ADR-0028
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for Git Strategy, Branch Protection, PR Checklists,
Database Migration Safety Reviews, and Release Governance:
1. Valid branch naming & semantic grammar.
2. Invalid branch naming rejection.
3. Protected branch direct push prevention.
4. Valid PR checklist & small batch change discipline.
5. Incomplete / invalid PR checklist rejection.
6. Oversized PR diff (> 800 lines) warning.
7. Safe database migration review.
8. Destructive migration (RemoveField, DeleteModel, raw DROP) detection.
9. Migration review sign-off RBAC authorization.
10. Security & PHI review gate triggers and approvals.
11. Release package validation & platform owner sign-off.
12. Unauthorized role & cross-tenant release denial.
13. Repository governance files on disk (.github/pull_request_template.md, CODEOWNERS).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from apps.core.git_strategy import (
    BranchPolicyValidator,
    BranchType,
    GitPolicyValidationError,
    GitStrategySecurityError,
    MigrationSafetyReviewer,
    PullRequestReview,
    PullRequestValidator,
    ReleaseGovernanceEngine,
    ReleasePackage,
    SecurityReviewGate,
)


class TestP028GitStrategy(unittest.TestCase):
    # --------------------------------------------------------------------------
    # 1. Valid Branch Naming Scenario
    # --------------------------------------------------------------------------
    def test_valid_branch_naming_conventions(self):
        """Standard branches adhering to <type>/<issue_id>-<slug> or release/vX.Y.Z must pass."""
        valid_cases = [
            ("feat/P028-git-strategy", BranchType.FEAT, "P028", "git-strategy"),
            ("fix/P015-entitlement-bug", BranchType.FIX, "P015", "entitlement-bug"),
            ("migration/P004-add-batch-index", BranchType.MIGRATION, "P004", "add-batch-index"),
            ("security/P005-bkash-webhook", BranchType.SECURITY, "P005", "bkash-webhook"),
            ("chore/P021-monorepo-deps", BranchType.CHORE, "P021", "monorepo-deps"),
            ("refactor/P008-theme-tokens", BranchType.REFACTOR, "P008", "theme-tokens"),
            ("hotfix/P013-pos-sync-retry", BranchType.HOTFIX, "P013", "pos-sync-retry"),
            ("release/v1.0.0", BranchType.RELEASE, "1.0.0", "1.0.0"),
            ("release/v2.1.0-rc1", BranchType.RELEASE, "2.1.0-rc1", "2.1.0-rc1"),
        ]

        for name, expected_type, expected_issue, expected_slug in valid_cases:
            res = BranchPolicyValidator.validate_branch_name(name)
            self.assertTrue(res.is_valid, f"Expected {name} to be valid.")
            self.assertEqual(res.branch_type, expected_type)
            self.assertEqual(res.issue_id, expected_issue)
            self.assertEqual(res.slug, expected_slug)
            self.assertFalse(res.is_protected)

    # --------------------------------------------------------------------------
    # 2. Invalid Branch Naming Scenario
    # --------------------------------------------------------------------------
    def test_invalid_branch_names_are_rejected(self):
        """Malformed branch names must be flagged with clear remediation error messages."""
        invalid_names = [
            "direct-fix",
            "my_feature",
            "P028-git-strategy",
            "feat-P028-git-strategy",
            "hotfix_bug",
            "feature/something",
            "feat/something-without-issue",
            "release/v1",
        ]

        for name in invalid_names:
            res = BranchPolicyValidator.validate_branch_name(name)
            self.assertFalse(res.is_valid, f"Expected '{name}' to be rejected.")
            self.assertIsNotNone(res.error_message)
            self.assertIn("violates ADR-0028 branch naming convention", res.error_message)

    # --------------------------------------------------------------------------
    # 3. Protected Branch Direct Push Prevention
    # --------------------------------------------------------------------------
    def test_protected_branches_block_direct_pushes(self):
        """Direct push to master, main, or staging is forbidden for all roles."""
        for branch in ["master", "main", "staging"]:
            res = BranchPolicyValidator.validate_branch_name(branch)
            self.assertTrue(res.is_valid)
            self.assertTrue(res.is_protected)

            with self.assertRaises(GitStrategySecurityError) as ctx:
                BranchPolicyValidator.assert_direct_push_allowed(branch, actor_role="LEAD_ARCHITECT")
            self.assertIn("Direct push to protected branch", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 4. Valid Pull Request Checklist Scenario
    # --------------------------------------------------------------------------
    def test_valid_pull_request_checklist_passes(self):
        """A complete, verified PR checklist with small batch size must pass with zero violations."""
        pr = PullRequestReview(
            pr_id="PR-101",
            title="feat(toolchain): implement ADR-0028 git strategy and review policies",
            branch_name="feat/P028-git-strategy-and-reviews",
            srs_traceability_id="P028",
            target_workspaces=["backend", "tools"],
            change_type=BranchType.FEAT,
            lines_added=180,
            lines_deleted=15,
            contracts_verified=True,
            financial_precision_verified=True,
            migrations_safe=True,
            security_privacy_verified=True,
            tests_passed=True,
            rollback_plan="Revert pull request commit via git revert, run migrations down if applied.",
            estimated_rollback_minutes=5,
            author="lead-architect",
        )

        violations = PullRequestValidator.validate_pull_request(pr)
        self.assertEqual(len(violations), 0, f"Expected 0 violations, got: {violations}")

    # --------------------------------------------------------------------------
    # 5. Invalid / Incomplete PR Checklist Scenarios
    # --------------------------------------------------------------------------
    def test_incomplete_pull_request_checklist_is_flagged(self):
        """PRs with missing traceability, unverified contracts, or missing rollback plans are flagged."""
        pr = PullRequestReview(
            pr_id="PR-102",
            title="incomplete pr",
            branch_name="direct-broken-branch",
            srs_traceability_id="",  # Missing traceability
            target_workspaces=[],  # Missing workspace
            change_type=BranchType.FIX,
            lines_added=50,
            lines_deleted=10,
            contracts_verified=False,  # Unverified contracts
            financial_precision_verified=False,  # Unverified financial precision
            migrations_safe=False,
            security_privacy_verified=False,
            tests_passed=False,
            rollback_plan="",  # Missing rollback plan
            estimated_rollback_minutes=0,
            author="dev",
        )

        violations = PullRequestValidator.validate_pull_request(pr)
        self.assertTrue(len(violations) >= 5, f"Expected multiple violations, got {len(violations)}")
        err_text = " ".join(violations)
        self.assertIn("SRS Traceability ID", err_text)
        self.assertIn("target workspace", err_text)
        self.assertIn("contracts", err_text)
        self.assertIn("Financial precision", err_text)
        self.assertIn("rollback plan", err_text)

    # --------------------------------------------------------------------------
    # 6. Small Batch Discipline & Oversized PR Warning
    # --------------------------------------------------------------------------
    def test_oversized_pull_request_diff_is_rejected(self):
        """PR exceeding the hard maximum diff limit (> 800 lines) is rejected."""
        pr = PullRequestReview(
            pr_id="PR-103",
            title="massive monolithic change",
            branch_name="feat/P028-massive-refactor",
            srs_traceability_id="P028",
            target_workspaces=["backend"],
            change_type=BranchType.FEAT,
            lines_added=700,
            lines_deleted=250,  # Total = 950 lines > 800
            contracts_verified=True,
            financial_precision_verified=True,
            migrations_safe=True,
            security_privacy_verified=True,
            tests_passed=True,
            rollback_plan="Revert pull request commit immediately.",
            estimated_rollback_minutes=5,
            author="dev",
        )

        violations = PullRequestValidator.validate_pull_request(pr)
        self.assertTrue(any("exceeds hard maximum limit" in v for v in violations))

    # --------------------------------------------------------------------------
    # 7. Safe Database Migration Review
    # --------------------------------------------------------------------------
    def test_safe_database_migration_passes_review(self):
        """Safe additive migration with nullable fields or defaults passes safety review."""
        safe_migration = (
            "from django.db import migrations, models\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('catalog', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.AddField(\n"
            "            model_name='medicine',\n"
            "            name='generic_bn',\n"
            "            field=models.CharField(max_length=255, null=True, blank=True),\n"
            "        ),\n"
            "    ]\n"
        )

        report = MigrationSafetyReviewer.audit_migration_content(
            "backend/catalog/migrations/0002_add_generic_bn.py", safe_migration, has_rollback_plan=True
        )
        self.assertTrue(report.is_safe)
        self.assertEqual(len(report.hazardous_patterns), 0)
        self.assertEqual(report.sign_off_status, "APPROVED")

    # --------------------------------------------------------------------------
    # 8. Destructive Migration Flagged
    # --------------------------------------------------------------------------
    def test_destructive_migration_detected_and_rejected(self):
        """Migrations with RemoveField, DeleteModel, or raw DROP are flagged as hazardous."""
        destructive_migration = (
            "from django.db import migrations, models\n\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [\n"
            "        migrations.RemoveField(model_name='batch', name='cost_price'),\n"
            "        migrations.DeleteModel(name='SupplierLedger'),\n"
            "    ]\n"
        )

        report = MigrationSafetyReviewer.audit_migration_content(
            "backend/catalog/migrations/0003_drop_cost.py", destructive_migration, has_rollback_plan=False
        )
        self.assertFalse(report.is_safe)
        self.assertEqual(len(report.hazardous_patterns), 2)
        self.assertEqual(report.sign_off_status, "REJECTED")

        # Attempting to sign off an unsafe migration must raise GitPolicyValidationError
        with self.assertRaises(GitPolicyValidationError):
            MigrationSafetyReviewer.sign_off_migration(report, actor_role="DBA_LEAD", actor_name="dba-lead")

    # --------------------------------------------------------------------------
    # 9. Migration Review Sign-off RBAC Authorization
    # --------------------------------------------------------------------------
    def test_unauthorized_role_cannot_sign_off_migration(self):
        """Non-DBA roles (CASHIER, BRANCH_MANAGER, TENANT_OWNER) cannot sign off migrations."""
        safe_migration = (
            "from django.db import migrations, models\n\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [\n"
            "        migrations.AddField(model_name='item', name='barcode', field=models.CharField(max_length=64, null=True)),\n"
            "    ]\n"
        )
        report = MigrationSafetyReviewer.audit_migration_content("0002_barcode.py", safe_migration)

        unauthorized_roles = ["CASHIER", "BRANCH_MANAGER", "TENANT_OWNER", "DEVELOPER"]
        for role in unauthorized_roles:
            with self.assertRaises(GitStrategySecurityError):
                MigrationSafetyReviewer.sign_off_migration(report, actor_role=role, actor_name="user")

        # Authorized DBA_LEAD can sign off
        signed = MigrationSafetyReviewer.sign_off_migration(report, actor_role="DBA_LEAD", actor_name="dba-master")
        self.assertEqual(signed.sign_off_status, "SIGNED_OFF")
        self.assertIn("dba-master (DBA_LEAD)", signed.signed_by)

    # --------------------------------------------------------------------------
    # 10. Security & PHI Review Gate
    # --------------------------------------------------------------------------
    def test_security_review_gate_requires_security_lead_approval(self):
        """PR touching security or patient PHI modules requires explicit SECURITY_LEAD approval."""
        sensitive_files = [
            "apps/core/tenant_security.py",
            "backend/billing/services/payment_bkash.py",
        ]
        requires_review, paths = SecurityReviewGate.requires_security_review(sensitive_files)
        self.assertTrue(requires_review)
        self.assertEqual(len(paths), 2)

        # Missing security lead approval
        missing_approvals = {"DEVELOPER": "dev-user", "TECH_LEAD": "lead-user"}
        with self.assertRaises(GitPolicyValidationError) as ctx:
            SecurityReviewGate.verify_security_sign_off(sensitive_files, missing_approvals)
        self.assertIn("lacks required sign-off from SECURITY_LEAD", str(ctx.exception))

        # With SECURITY_LEAD approval
        valid_approvals = {"SECURITY_LEAD": "sec-officer"}
        self.assertTrue(SecurityReviewGate.verify_security_sign_off(sensitive_files, valid_approvals))

    # --------------------------------------------------------------------------
    # 11. Release Governance & Platform Owner Sign-off
    # --------------------------------------------------------------------------
    def test_release_governance_package_validation_and_signoff(self):
        """Release package requires valid SemVer, changelog, rollback playbook, and owner sign-off."""
        pkg = ReleasePackage(
            release_tag="v1.0.0",
            target_branch="master",
            changelog_summary="Production MVP release: Cloud DRF, OpenJFX POS, and Flutter manager app.",
            rollback_playbook="Execute canary drain, rollback migration down, deploy previous docker image.",
            regression_tests_passed=True,
            migrations_verified=True,
        )

        violations = ReleaseGovernanceEngine.validate_release_package(pkg)
        self.assertEqual(len(violations), 0)

        # Non-owner cannot sign off
        with self.assertRaises(GitStrategySecurityError):
            ReleaseGovernanceEngine.sign_off_release(pkg, actor_role="TENANT_OWNER", actor_name="pharmacy-owner")

        # Platform Super Admin signs off
        signed_pkg = ReleaseGovernanceEngine.sign_off_release(
            pkg, actor_role="PLATFORM_SUPER_ADMIN", actor_name="lead-admin"
        )
        self.assertTrue(signed_pkg.platform_owner_signed)
        self.assertIn("PLATFORM_SUPER_ADMIN", signed_pkg.signed_by)

    # --------------------------------------------------------------------------
    # 12. Cross-Tenant Release Denial
    # --------------------------------------------------------------------------
    def test_tenant_actor_cannot_authorize_global_release(self):
        """A tenant-specific user cannot sign off global release packages."""
        pkg = ReleasePackage(
            release_tag="v1.1.0",
            target_branch="master",
            changelog_summary="Global enterprise update for all multi-tenant pharmacy clusters.",
            rollback_playbook="Roll back canary container and restore replica database.",
            regression_tests_passed=True,
            migrations_verified=True,
            target_tenant_scope="ALL_TENANTS",
        )

        with self.assertRaises(GitStrategySecurityError) as ctx:
            ReleaseGovernanceEngine.sign_off_release(
                pkg,
                actor_role="PLATFORM_SUPER_ADMIN",
                actor_name="fake-admin",
                requesting_tenant_id="tenant-chittagong-corp",
            )
        self.assertIn("Tenant-scoped actor cannot authorize release", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 13. Governance Files On Disk
    # --------------------------------------------------------------------------
    def test_governance_templates_exist_and_conform(self):
        """Repository .github/pull_request_template.md and .github/CODEOWNERS must exist on disk."""
        repo_root = Path(__file__).resolve().parent.parent
        pr_template = repo_root / ".github" / "pull_request_template.md"
        codeowners = repo_root / ".github" / "CODEOWNERS"

        self.assertTrue(pr_template.is_file(), ".github/pull_request_template.md must exist")
        self.assertTrue(codeowners.is_file(), ".github/CODEOWNERS must exist")

        pr_content = pr_template.read_text(encoding="utf-8")
        self.assertIn("SRS Traceability ID", pr_content)
        self.assertIn("Reversibility & Rollback Plan", pr_content)
        self.assertIn("Decimal", pr_content)

        co_content = codeowners.read_text(encoding="utf-8")
        self.assertIn("@lead-architect", co_content)
        self.assertIn("@security-lead", co_content)
        self.assertIn("@dba-lead", co_content)


if __name__ == "__main__":
    unittest.main()
