"""
tests/test_p022_env_isolation.py
Architecture Decision: ADR-0022
Phase 02: Repository, Toolchain & Engineering Automation

Verification test suite for Python runtime pinning, environment isolation,
deterministic lock policy, secret leak detection, and RBAC credential boundaries.
"""

import unittest
from pathlib import Path

from apps.core.env_isolation import (
    EnvironmentConfig,
    EnvironmentConfigValidator,
    EnvironmentSecurityBoundary,
    EnvironmentValidationError,
    PythonRuntimeValidator,
    SecretLeakDetector,
    load_env_file,
)


class TestP022BackendEnvironmentIsolation(unittest.TestCase):
    """Verifies all acceptance criteria for P022 and ADR-0022."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        self.backend_dir = self.root_dir if (self.root_dir / "manage.py").exists() else self.root_dir / "backend"
        self.repo_root = self.root_dir.parent if (self.root_dir / "manage.py").exists() else self.root_dir

        # Valid mock environment dictionary
        self.valid_env = {
            "ENVIRONMENT": "development",
            "DJANGO_SECRET_KEY": "a-sufficiently-long-secret-key-for-development-testing-32chars",
            "DJANGO_DEBUG": "True",
            "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DB_ENGINE": "postgres",
            "POSTGRES_DB": "pharmacy_erp_test",
            "POSTGRES_USER": "test_user",
            "POSTGRES_PASSWORD": "test_secure_password_123",
            "POSTGRES_HOST": "localhost",
            "POSTGRES_PORT": "5432",
            "REDIS_URL": "redis://localhost:6379/0",
            "JWT_SIGNING_KEY": "a-distinct-long-jwt-signing-key-for-auth-tokens-32chars",
        }

    # 1. Python Runtime Pinning
    def test_python_runtime_pinning_current_passes(self):
        """Current runtime (Python 3.13.x) must pass validation."""
        self.assertTrue(PythonRuntimeValidator.validate_runtime())

    def test_python_runtime_pinning_rejects_incompatible_versions(self):
        """Python versions < 3.13.0 or >= 3.14.0 must be rejected."""
        unsupported_versions = [
            (3, 10, 12),
            (3, 11, 8),
            (3, 12, 4),
            (3, 14, 0),
            (3, 15, 1),
            (4, 0, 0),
        ]
        for ver in unsupported_versions:
            with self.subTest(version=ver):
                with self.assertRaises(EnvironmentValidationError) as ctx:
                    PythonRuntimeValidator.validate_runtime(ver)
                self.assertIn("Python runtime", str(ctx.exception))

    def test_pyproject_contains_exact_runtime_pinning(self):
        """pyproject.toml must declare requires-python = '>=3.13.0,<3.14.0'."""
        pyproject_path = self.backend_dir / "pyproject.toml"
        self.assertTrue(pyproject_path.exists())
        content = pyproject_path.read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.13.0,<3.14.0"', content)

    # 2. Safe Environment Templates & Zero Secrets
    def test_safe_env_example_files_exist_and_contain_no_secrets(self):
        """Both root and backend .env.example files must exist and pass secret leak scanning."""
        root_env_example = self.repo_root / ".env.example"
        backend_env_example = self.backend_dir / ".env.example"

        self.assertTrue(root_env_example.exists(), "Root .env.example must exist")
        self.assertTrue(backend_env_example.exists(), "Backend .env.example must exist")

        for fpath in (root_env_example, backend_env_example):
            content = fpath.read_text(encoding="utf-8")
            violations = SecretLeakDetector.scan_content(content, filename=fpath.name)
            self.assertEqual(violations, [], f"{fpath.name} must not contain any leaked credentials!")

            # Ensure file is parsable and contains mandatory keys
            parsed = load_env_file(fpath)
            self.assertIn("DJANGO_SECRET_KEY", parsed)
            self.assertIn("DB_ENGINE", parsed)
            self.assertIn("REDIS_URL", parsed)
            self.assertIn("JWT_SIGNING_KEY", parsed)

    # 3. Secret Leak Detector
    def test_secret_leak_detector_flags_compromised_patterns(self):
        """Secret leak detector must catch AWS keys, private keys, and payment tokens."""
        # Dynamically constructed to prevent GitHub Push Protection triggers
        aws_leak = "AWS_SECRET_KEY=" + "AKIA" + "IOSFODNN7EXAMPLE"  # pragma: allowlist secret
        violations_aws = SecretLeakDetector.scan_content(aws_leak)
        self.assertTrue(any("AWS Access Key" in v for v in violations_aws))

        rsa_leak = "-----" + "BEGIN RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA0...\n" + "-----END RSA PRIVATE KEY-----"  # pragma: allowlist secret
        violations_rsa = SecretLeakDetector.scan_content(rsa_leak)
        self.assertTrue(any("Private Key" in v for v in violations_rsa))

        stripe_leak = "STRIPE_KEY=" + "sk_live_" + "51AbcdEFghIJklmnOPqrstUV12345"  # pragma: allowlist secret
        violations_stripe = SecretLeakDetector.scan_content(stripe_leak)
        self.assertTrue(any("Payment Live Key" in v for v in violations_stripe))

        with self.assertRaises(EnvironmentValidationError):
            SecretLeakDetector.assert_no_leaks(aws_leak)

    # 4. Valid Environment Configuration
    def test_valid_environment_dictionary_parses_to_dto(self):
        """Valid environment dictionary parses cleanly into typed EnvironmentConfig."""
        config = EnvironmentConfigValidator.validate(self.valid_env)
        self.assertIsInstance(config, EnvironmentConfig)
        self.assertEqual(config.environment, "development")
        self.assertTrue(config.django_debug)
        self.assertEqual(config.db_engine, "postgres")
        self.assertEqual(config.postgres_db, "pharmacy_erp_test")
        self.assertEqual(config.postgres_port, 5432)
        self.assertEqual(config.redis_url, "redis://localhost:6379/0")

    # 5. Missing / Invalid PostgreSQL Settings
    def test_missing_mandatory_postgres_variables_rejected(self):
        """When DB_ENGINE=postgres, missing host or password must be rejected."""
        invalid_env = dict(self.valid_env)
        del invalid_env["POSTGRES_PASSWORD"]
        with self.assertRaises(EnvironmentValidationError) as ctx:
            EnvironmentConfigValidator.validate(invalid_env)
        self.assertIn("POSTGRES_PASSWORD", str(ctx.exception))

    def test_invalid_postgres_port_rejected(self):
        """Non-numeric or out-of-range port must raise EnvironmentValidationError."""
        invalid_env = dict(self.valid_env)
        invalid_env["POSTGRES_PORT"] = "999999"
        with self.assertRaises(EnvironmentValidationError) as ctx:
            EnvironmentConfigValidator.validate(invalid_env)
        self.assertIn("POSTGRES_PORT", str(ctx.exception))

    # 6. Production Mode Hardening
    def test_production_mode_debug_true_rejected(self):
        """DJANGO_DEBUG=True in production environment must be rejected."""
        prod_env = dict(self.valid_env)
        prod_env["ENVIRONMENT"] = "production"
        prod_env["DJANGO_DEBUG"] = "True"
        with self.assertRaises(EnvironmentValidationError) as ctx:
            EnvironmentConfigValidator.validate(prod_env)
        self.assertIn("DJANGO_DEBUG must be False in production", str(ctx.exception))

    def test_production_mode_insecure_default_key_rejected(self):
        """Known default or django-insecure keys in production must be rejected."""
        prod_env = dict(self.valid_env)
        prod_env["ENVIRONMENT"] = "production"
        prod_env["DJANGO_DEBUG"] = "False"
        prod_env["DJANGO_SECRET_KEY"] = "django-insecure-dev-key-pharmacy-saas-2026-bangladesh"
        with self.assertRaises(EnvironmentValidationError) as ctx:
            EnvironmentConfigValidator.validate(prod_env)
        self.assertIn("insecure default DJANGO_SECRET_KEY", str(ctx.exception))

    def test_production_mode_wildcard_host_rejected(self):
        """Wildcard '*' in DJANGO_ALLOWED_HOSTS is strictly forbidden in production."""
        prod_env = dict(self.valid_env)
        prod_env["ENVIRONMENT"] = "production"
        prod_env["DJANGO_DEBUG"] = "False"
        prod_env["DJANGO_ALLOWED_HOSTS"] = "api.pharmacy.com.bd,*"
        with self.assertRaises(EnvironmentValidationError) as ctx:
            EnvironmentConfigValidator.validate(prod_env)
        self.assertIn("wildcard '*' is strictly forbidden", str(ctx.exception))

    def test_cryptographic_isolation_jwt_vs_django_secret_key(self):
        """JWT_SIGNING_KEY reusing DJANGO_SECRET_KEY must be rejected."""
        shared_key = "a" * 36
        env_with_shared_key = dict(self.valid_env)
        env_with_shared_key["DJANGO_SECRET_KEY"] = shared_key
        env_with_shared_key["JWT_SIGNING_KEY"] = shared_key
        with self.assertRaises(EnvironmentValidationError) as ctx:
            EnvironmentConfigValidator.validate(env_with_shared_key)
        self.assertIn("JWT_SIGNING_KEY must be distinct", str(ctx.exception))

    # 7. Role-Based Access Control (RBAC) & Credential Boundary
    def test_unauthorized_roles_blocked_from_exporting_environment(self):
        """Cashiers, pharmacists, branch managers, and tenant owners are blocked from env export."""
        config = EnvironmentConfigValidator.validate(self.valid_env)
        unauthorized_roles = ["CASHIER", "BRANCH_MANAGER", "PHARMACIST", "TENANT_OWNER", "ACCOUNTANT"]

        for role in unauthorized_roles:
            with self.subTest(role=role):
                with self.assertRaises(PermissionError) as ctx:
                    EnvironmentSecurityBoundary.export_sanitized_config(role, config)
                self.assertIn("Security Violation", str(ctx.exception))

                with self.assertRaises(PermissionError):
                    EnvironmentSecurityBoundary.export_raw_secrets(role, config)

    def test_authorized_roles_can_export_environment(self):
        """Platform Super Admin and DevOps can export sanitized or raw configuration."""
        config = EnvironmentConfigValidator.validate(self.valid_env)
        authorized_roles = ["PLATFORM_SUPER_ADMIN", "DEVOPS_ENGINEER"]

        for role in authorized_roles:
            with self.subTest(role=role):
                sanitized = EnvironmentSecurityBoundary.export_sanitized_config(role, config)
                self.assertIn("MASKED", sanitized["DJANGO_SECRET_KEY"])
                self.assertIn("MASKED", sanitized["JWT_SIGNING_KEY"])
                self.assertIn("MASKED", sanitized["POSTGRES_PASSWORD"])

                raw = EnvironmentSecurityBoundary.export_raw_secrets(role, config)
                self.assertEqual(raw["DJANGO_SECRET_KEY"], config.django_secret_key)
                self.assertEqual(raw["POSTGRES_PASSWORD"], config.postgres_password)

    # 8. Dependency Lock Enforcement
    def test_requirements_lock_file_deterministic(self):
        """backend/requirements.lock must exist and all lines must be pinned with =="""
        lock_file = self.backend_dir / "requirements.lock"
        self.assertTrue(lock_file.exists(), "backend/requirements.lock must exist")

        lines = [line.strip() for line in lock_file.read_text(encoding="utf-8").splitlines()]
        package_lines = [l for l in lines if l and not l.startswith("#")]
        self.assertGreaterEqual(len(package_lines), 10)

        for line in package_lines:
            self.assertIn("==", line, f"Lock entry '{line}' must be pinned with exact ==")


if __name__ == "__main__":
    unittest.main()
