"""
tests/test_p027_precommit_hooks.py
# test-allowlist-secrets
Architecture Decision: ADR-0027
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for Pre-Commit Quality, Security, and Compliance Hooks:
1. Valid scenario: Clean staging passes all 7 guardrails.
2. Risky files: .env, .sqlite, .pem, .sql detected; .env.example allowed.
3. Oversized assets: Files > 5MB threshold flagged.
4. Secrets detection: AWS, bKash, Nagad, JWT, DB URIs, Django SECRET_KEY detected.
5. Patient data / PHI: Bangladesh NID (10, 13, 17 digit), phone numbers, DICOM, unredacted Rx images detected.
6. Syntax validation: Python AST, JSON, YAML error handling.
7. Unauthorized RBAC: Cashier / Branch Manager blocked from hook administration.
8. Cross-tenant isolation: Foreign tenant payload leak detected.
9. Git hook installation: .git/hooks/pre-commit and .githooks/pre-commit verification.
10. .pre-commit-config.yaml validation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from apps.core.precommit_toolchain import (
    MAX_FILE_SIZE_BYTES,
    OversizedAssetScanner,
    PatientDataScanner,
    PreCommitRunner,
    PreCommitSecurityBoundary,
    PreCommitSecurityError,
    RiskyFileScanner,
    SecretScanner,
    SyntaxValidator,
)


class TestP027PreCommitHooks(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # --------------------------------------------------------------------------
    # 1. Valid Scenario
    # --------------------------------------------------------------------------
    def test_valid_scenario_passes_all_guardrails(self):
        """Clean files with valid syntax, formatting, and no secrets or PHI must pass cleanly."""
        clean_py = self.test_root / "clean_module.py"
        clean_py.write_text(
            '"""Clean Python module."""\n\n'
            "def calculate_tax(subtotal: float) -> float:\n"
            "    return round(subtotal * 0.05, 2)\n",
            encoding="utf-8",
        )

        clean_json = self.test_root / "config.json"
        clean_json.write_text('{"currency": "BDT", "tax_rate": 0.05}\n', encoding="utf-8")

        clean_yaml = self.test_root / "settings.yaml"
        clean_yaml.write_text("environment: production\ntimezone: Asia/Dhaka\n", encoding="utf-8")

        runner = PreCommitRunner(root_dir=self.test_root)
        report = runner.run_guardrails([clean_py, clean_json, clean_yaml], run_smoke_test=False)

        self.assertTrue(report.is_valid, f"Expected valid report, got violations: {report.violations}")
        self.assertEqual(len(report.violations), 0)
        self.assertEqual(report.checked_files_count, 3)
        self.assertEqual(report.guardrail_status["RISKY_FILES"], "PASS")
        self.assertEqual(report.guardrail_status["OVERSIZED_ASSETS"], "PASS")
        self.assertEqual(report.guardrail_status["SECRETS"], "PASS")
        self.assertEqual(report.guardrail_status["PATIENT_DATA"], "PASS")
        self.assertEqual(report.guardrail_status["SYNTAX"], "PASS")

    # --------------------------------------------------------------------------
    # 2. Risky Files Guard
    # --------------------------------------------------------------------------
    def test_risky_files_forbidden_filenames_detected(self):
        """Environment files and OS debris must be blocked from Git commits."""
        risky_names = [".env", ".env.production", ".env.staging", ".DS_Store", "Thumbs.db", "id_rsa"]
        for name in risky_names:
            violations = RiskyFileScanner.scan_path(self.test_root / name)
            self.assertTrue(len(violations) > 0, f"Expected risky file violation for {name}")
            self.assertEqual(violations[0].guardrail, "RISKY_FILES")

    def test_risky_files_allowed_template_files(self):
        """.env.example and .env.template must be allowed."""
        for allowed in [".env.example", ".env.template", ".env.sample"]:
            violations = RiskyFileScanner.scan_path(self.test_root / allowed)
            self.assertEqual(len(violations), 0, f"{allowed} should be permitted as a sanitized template")

    def test_risky_files_forbidden_extensions_detected(self):
        """Database files, SQL dumps, and private key files must be blocked."""
        forbidden_extensions = ["pos.sqlite3", "database.db", "dump.sql", "key.pem", "server.key"]
        for filename in forbidden_extensions:
            violations = RiskyFileScanner.scan_path(self.test_root / filename)
            self.assertTrue(len(violations) > 0, f"Expected forbidden extension violation for {filename}")
            self.assertEqual(violations[0].guardrail, "RISKY_FILES")

    # --------------------------------------------------------------------------
    # 3. Oversized Assets Guard
    # --------------------------------------------------------------------------
    def test_oversized_asset_exceeding_threshold_is_flagged(self):
        """Files exceeding 5MB threshold must be rejected."""
        large_file = self.test_root / "large_asset.bin"
        # Create a file just over 5 MB
        large_file.write_bytes(b"0" * (MAX_FILE_SIZE_BYTES + 1024))

        violations = OversizedAssetScanner.scan_file(large_file)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].guardrail, "OVERSIZED_ASSETS")
        self.assertIn("exceeds maximum repository size limit", violations[0].message_en)

    def test_asset_within_threshold_passes(self):
        """Files under 5MB threshold must pass."""
        small_file = self.test_root / "logo.png"
        small_file.write_bytes(b"PNG_DATA" * 100)

        violations = OversizedAssetScanner.scan_file(small_file)
        self.assertEqual(len(violations), 0)

    # --------------------------------------------------------------------------
    # 4. Secret Detection Guard
    # --------------------------------------------------------------------------
    def test_secret_detection_catches_payment_gateway_credentials(self):
        """bKash, Nagad, and SSLCommerz API credentials must be flagged."""
        content = (
            'bkash_app_key = "bk_live_key_99238472938472938"\n'
            'nagad_private_key = "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQD0"\n'
            'sslcommerz_store_passwd = "live_store_pass_xyz"\n'
        )
        violations = SecretScanner.scan_content(content, file_path="config.py")
        self.assertEqual(len(violations), 3)
        rules_triggered = {v.message_en for v in violations}
        self.assertTrue(any("bKash" in r for r in rules_triggered))
        self.assertTrue(any("Nagad" in r for r in rules_triggered))
        self.assertTrue(any("SSLCommerz" in r for r in rules_triggered))

    def test_secret_detection_catches_cloud_and_database_credentials(self):
        """AWS keys, database passwords in URIs, and JWT secrets must be flagged."""
        content = (
            'AWS_KEY = "' + "AKIA" + 'IOSFODNN7EXAMPLE"\n'
            'DATABASE_URL = "postgres://admin_usr:SuperSecretPass123@db.prod.internal:5432/pharmacy_prod"\n'
            'jwt_secret_key = "very_confidential_signing_secret_9999"\n'
            'SECRET_KEY = "django-insecure-prod-key-with-high-entropy-1234567890abcdef"\n'
        )
        violations = SecretScanner.scan_content(content, file_path="settings.py")
        self.assertEqual(len(violations), 4)

    def test_secret_detection_honors_allowlist_pragma(self):
        """Lines with pragma: allowlist secret must be excused."""
        content = 'AWS_KEY = "' + "AKIA" + 'IOSFODNN7EXAMPLE"  # pragma: allowlist secret\n'
        violations = SecretScanner.scan_content(content, file_path="test_mock.py")
        self.assertEqual(len(violations), 0)

    # --------------------------------------------------------------------------
    # 5. Patient Data & PHI Guard (Healthcare Privacy)
    # --------------------------------------------------------------------------
    def test_patient_data_bangladesh_nid_detected(self):
        """Bangladesh Smart NID (10 digits) and legacy NID (13/17 digits) must be detected."""
        content = (
            'patient_nid: "1928374650"\n'  # 10 digits
            'national_id = "19901234567890124"\n'  # 17 digits
        )
        violations = PatientDataScanner.scan_content(content, file_path="patient_record.yaml")
        self.assertEqual(len(violations), 2)
        self.assertEqual(violations[0].guardrail, "PATIENT_DATA")

    def test_patient_data_bangladesh_patient_phone_detected(self):
        """Real patient mobile numbers in patient context must be flagged."""
        content = 'patient_mobile = "+8801712345678"\n'
        violations = PatientDataScanner.scan_content(content, file_path="prescription.json")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].guardrail, "PATIENT_DATA")

    def test_patient_data_synthetic_fixtures_permitted(self):
        """Synthetic test fixtures marked with synthetic tag must pass without false alarms."""
        content = (
            "# synthetic-fixture\n"
            '{"patient_nid": "0000000000", "patient_phone": "01700000000", "name": "Test Patient"}\n'
        )
        violations = PatientDataScanner.scan_content(content, file_path="test_fixture.json")
        self.assertEqual(len(violations), 0)

    def test_patient_data_dicom_and_prescription_images_blocked(self):
        """DICOM scans and unredacted prescription images must be flagged by path."""
        dcm_file = self.test_root / "chest_xray.dcm"
        rx_file = self.test_root / "prescription_karim_choudhury.jpg"

        v_dcm = PatientDataScanner.scan_path(dcm_file)
        self.assertEqual(len(v_dcm), 1)
        self.assertEqual(v_dcm[0].guardrail, "PATIENT_DATA")

        v_rx = PatientDataScanner.scan_path(rx_file)
        self.assertEqual(len(v_rx), 1)
        self.assertEqual(v_rx[0].guardrail, "PATIENT_DATA")

    # --------------------------------------------------------------------------
    # 6. Syntax Validation Guard
    # --------------------------------------------------------------------------
    def test_syntax_validation_flags_python_syntax_error(self):
        """Invalid Python AST must be flagged with line number."""
        bad_py = self.test_root / "broken.py"
        bad_py.write_text("def broken_func(\n    return 42\n", encoding="utf-8")

        violations = SyntaxValidator.validate_file(bad_py)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].guardrail, "SYNTAX")
        self.assertIsNotNone(violations[0].line_number)

    def test_syntax_validation_flags_broken_json(self):
        """Invalid JSON syntax must be caught."""
        bad_json = self.test_root / "broken.json"
        bad_json.write_text('{ "name": "pharmacy", trailing_comma: }', encoding="utf-8")

        violations = SyntaxValidator.validate_file(bad_json)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].guardrail, "SYNTAX")

    def test_syntax_validation_flags_broken_yaml(self):
        """Malformed YAML indentation must be caught."""
        bad_yaml = self.test_root / "broken.yaml"
        bad_yaml.write_text("services:\n  backend:\n  port: [unmatched_bracket\n", encoding="utf-8")

        violations = SyntaxValidator.validate_file(bad_yaml)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].guardrail, "SYNTAX")

    # --------------------------------------------------------------------------
    # 7. Unauthorized RBAC Boundary
    # --------------------------------------------------------------------------
    def test_unauthorized_role_cannot_administer_git_hooks(self):
        """Non-admin roles (CASHIER, BRANCH_MANAGER, TENANT_OWNER) must be denied hook administration."""
        unauthorized_roles = ["CASHIER", "BRANCH_MANAGER", "TENANT_OWNER", "PHARMACIST"]
        for role in unauthorized_roles:
            with self.assertRaises(PreCommitSecurityError):
                PreCommitSecurityBoundary.authorize_hook_administration(role)

    def test_authorized_platform_admin_can_administer_git_hooks(self):
        """PLATFORM_SUPER_ADMIN and LEAD_ARCHITECT are authorized."""
        for role in ["PLATFORM_SUPER_ADMIN", "LEAD_ARCHITECT", "DEVOPS_ENGINEER"]:
            # Should not raise exception
            PreCommitSecurityBoundary.authorize_hook_administration(role)

    # --------------------------------------------------------------------------
    # 8. Cross-Tenant Isolation Boundary
    # --------------------------------------------------------------------------
    def test_cross_tenant_isolation_boundary_flags_foreign_tenant_leak(self):
        """Foreign tenant ID in payload without cross-tenant tag is flagged."""
        foreign_payload = '{"tenant_id": "tenant-dhaka-corp", "action": "export"}'
        violations = PreCommitSecurityBoundary.validate_tenant_isolation(
            foreign_payload, active_tenant_id="tenant-chittagong-central"
        )
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].guardrail, "TENANT_ISOLATION")

    # --------------------------------------------------------------------------
    # 9. Git Hook Installation & Script Integrity
    # --------------------------------------------------------------------------
    def test_git_hook_installation_writes_correct_scripts(self):
        """install_git_hook must write executable shell scripts to .git/hooks and .githooks."""
        runner = PreCommitRunner(root_dir=self.test_root)
        hook_path = runner.install_git_hook(actor_role="PLATFORM_SUPER_ADMIN")

        self.assertTrue(hook_path.exists())
        content = hook_path.read_text(encoding="utf-8")
        self.assertIn("#!/usr/bin/env sh", content)
        self.assertIn("tools/pre_commit_check.py --staged", content)
        self.assertIn("EXIT_CODE", content)

        # Tracked .githooks version must also exist
        tracked_hook = self.test_root / ".githooks" / "pre-commit"
        self.assertTrue(tracked_hook.exists())
        self.assertEqual(tracked_hook.read_text(encoding="utf-8"), content)

    # --------------------------------------------------------------------------
    # 10. .pre-commit-config.yaml File Validation
    # --------------------------------------------------------------------------
    def test_precommit_config_yaml_file_integrity(self):
        """The repository .pre-commit-config.yaml must be valid YAML with all essential hooks."""
        backend_root = Path(__file__).resolve().parent.parent
        repo_root = backend_root.parent if (backend_root / "manage.py").exists() else backend_root
        config_path = repo_root / ".pre-commit-config.yaml"
        self.assertTrue(config_path.exists(), ".pre-commit-config.yaml must exist at repo root")

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertIn("repos", data)

        repo_urls = [r.get("repo") for r in data["repos"]]
        self.assertIn("https://github.com/pre-commit/pre-commit-hooks", repo_urls)
        self.assertIn("https://github.com/astral-sh/ruff-pre-commit", repo_urls)
        self.assertIn("local", repo_urls)

        # Verify local hooks
        local_repo = next(r for r in data["repos"] if r.get("repo") == "local")
        local_hook_ids = {h["id"] for h in local_repo.get("hooks", [])}
        self.assertIn("pharmacy-risky-files", local_hook_ids)
        self.assertIn("pharmacy-secret-guard", local_hook_ids)
        self.assertIn("pharmacy-patient-data-guard", local_hook_ids)
        self.assertIn("pharmacy-smoke-test", local_hook_ids)


if __name__ == "__main__":
    unittest.main()
