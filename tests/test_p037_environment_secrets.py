"""
tests/test_p037_environment_secrets.py
Architecture Decision: ADR-0037
Phase 02: Repository, Toolchain & Engineering Automation

Backend Test Suite for Environment Secrets Architecture, Multi-Tier Isolation,
Missing-Secret Fail-Fast Invariant, Dual-Key Rotation, and Exposure Scanning.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Add project root and backend to sys.path
_CANDIDATE = Path(__file__).resolve().parent.parent
_ROOT_DIR = _CANDIDATE.parent if (_CANDIDATE.parent / "backend").exists() else _CANDIDATE
_BACKEND_DIR = _ROOT_DIR / "backend"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR / "shared") not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR / "shared"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

try:
    django.setup()
except RuntimeError:
    pass

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.secrets_vault.models import (
    SecretRotationAudit,
    SecretType,
    TenantSecretRecord,
)
from apps.secrets_vault.rotation import DualKeyRotationValidator, SecretRotationEngine
from apps.secrets_vault.scanner import (
    SecretExposureScanner,
)
from apps.secrets_vault.validator import (
    FailFastSecretValidator,
    InsecureSecretError,
    MissingSecretError,
)
from apps.secrets_vault.vault import SecretStore, SecretTier


class EnvironmentSecretsTestSuite(TestCase):
    def setUp(self):
        self.client = APIClient()
        TenantSecretRecord.objects.all().delete()
        SecretRotationAudit.objects.all().delete()

    def tearDown(self):
        TenantSecretRecord.objects.all().delete()
        SecretRotationAudit.objects.all().delete()

    def test_valid_scenario_tier_separation_and_secret_store(self):
        """Valid Scenario: Verifies tier definitions, authenticated encryption, and masked previews."""
        self.assertEqual(SecretTier.DEVELOPMENT.value, "development")
        self.assertEqual(SecretTier.STAGING.value, "staging")
        self.assertEqual(SecretTier.PRODUCTION.value, "production")

        raw_token = "dgda-live-compliance-token-998877665544332211"
        encrypted = SecretStore.encrypt(raw_token)
        self.assertNotEqual(raw_token, encrypted)
        decrypted = SecretStore.decrypt(encrypted)
        self.assertEqual(raw_token, decrypted)

        preview = SecretStore.make_preview(raw_token)
        self.assertEqual(preview, "dgda-****-2211")
        self.assertNotIn("compliance-token", preview)

        valid_prod_env = {
            "DJANGO_SECRET_KEY": "ProdSecretKey2026WithSufficientEntropyAnd32Chars!!",
            "POSTGRES_PASSWORD": "PostgresLiveSecurePassword2026WithDigits99!",
            "REDIS_URL": "rediss://:StrongRedisPass2026@cloud.redis.com:6380/0",
            "JWT_SIGNING_KEY": "JwtSigningKey2026HighEntropyProductionToken123456",
        }
        FailFastSecretValidator.assert_environment_secrets(SecretTier.PRODUCTION, valid_prod_env)

    def test_missing_secret_fail_fast_in_production(self):
        """Missing-Secret Fail-Fast: Omission of required production secret raises MissingSecretError."""
        incomplete_env = {
            "DJANGO_SECRET_KEY": "ValidProductionSecretKeyWith32CharsMinimumAndDigits1!",
            "POSTGRES_PASSWORD": "ValidPostgresPassword123!",
        }
        with self.assertRaises(MissingSecretError) as ctx:
            FailFastSecretValidator.assert_environment_secrets(SecretTier.PRODUCTION, incomplete_env)

        self.assertIn("REDIS_URL", str(ctx.exception))
        self.assertIn("JWT_SIGNING_KEY", str(ctx.exception))

    def test_insecure_default_secret_rejected_in_production(self):
        """Insecure Secret Rejection: Known development or default keys raise InsecureSecretError in production."""
        insecure_env = {
            "DJANGO_SECRET_KEY": "django-insecure-dev-key-pharmacy-saas-2026-bangladesh",
            "POSTGRES_PASSWORD": "ValidPostgresPassword123!",
            "REDIS_URL": "redis://localhost:6379/0",
            "JWT_SIGNING_KEY": "ValidJwtSigningKeyWith32Characters12345!",
        }
        with self.assertRaises(InsecureSecretError) as ctx:
            FailFastSecretValidator.assert_environment_secrets(SecretTier.PRODUCTION, insecure_env)

        self.assertIn("insecure default", str(ctx.exception))

    def test_low_entropy_secret_rejected_in_production(self):
        """Entropy Rejection: Short secret (<32 chars) or lacking digits/letters raises InsecureSecretError."""
        short_env = {
            "DJANGO_SECRET_KEY": "short-secret-key-1234",
            "POSTGRES_PASSWORD": "ValidPostgresPassword123!",
            "REDIS_URL": "redis://localhost:6379/0",
            "JWT_SIGNING_KEY": "ValidJwtSigningKeyWith32Characters12345!",
        }
        with self.assertRaises(InsecureSecretError) as ctx:
            FailFastSecretValidator.assert_environment_secrets(SecretTier.PRODUCTION, short_env)

        self.assertIn("insufficient length", str(ctx.exception))

    def test_unauthorized_cashier_role_rejected(self):
        """RBAC Boundary: Cashier and Dispenser roles are denied secret access (HTTP 403 Forbidden)."""
        TenantSecretRecord.objects.create(
            secret_id="sec-dhk-001",
            tenant_id="tenant-dhaka-central",
            secret_type=SecretType.BKASH_MERCHANT_SECRET,
            encrypted_value=SecretStore.encrypt("bkash-sec-live-sample-12345"),
            key_preview="bkas-****-2345",
        )

        response = self.client.get(
            "/api/v2/secrets/",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Access denied", str(response.data))

    def test_cross_tenant_secret_access_strictly_blocked(self):
        """Cross-Tenant Boundary: Pharmacy owner from tenant A querying tenant B secrets is blocked (HTTP 403)."""
        TenantSecretRecord.objects.create(
            secret_id="sec-ctg-001",
            tenant_id="tenant-chittagong-care",
            secret_type=SecretType.DGDA_PORTAL_TOKEN,
            encrypted_value=SecretStore.encrypt("dgda-live-token-secret-ctg"),
            key_preview="dgda-****--ctg",
        )

        response = self.client.get(
            "/api/v2/secrets/?tenant_id=tenant-chittagong-care",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Cross-tenant access forbidden", str(response.data))

    def test_cross_tenant_secret_rotate_strictly_blocked(self):
        """Cross-Tenant Boundary: Owner from tenant A cannot rotate a secret owned by tenant B."""
        record = TenantSecretRecord.objects.create(
            secret_id="sec-ctg-rot-001",
            tenant_id="tenant-chittagong-care",
            secret_type=SecretType.BKASH_MERCHANT_SECRET,
            encrypted_value=SecretStore.encrypt("bkash-sec-old-secret-value"),
            key_preview="bkas-****-alue",
        )

        response = self.client.post(
            f"/api/v2/secrets/{record.secret_id}/rotate/",
            {"new_secret_value": "bkash-sec-new-super-secret-rotated"},
            format="json",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_source_specific_rule_dual_key_rotation(self):
        """Source-Specific Rule: During secret rotation, dual-key verification validates in-flight tokens."""
        key_v1 = SecretRotationEngine.generate_secure_secret("key-v1", 32)
        key_v2 = SecretRotationEngine.generate_secure_secret("key-v2", 32)

        validator = DualKeyRotationValidator(primary_key=key_v1)
        msg = "tenant-dhaka-central|invoice-post-2026"
        token_v1 = validator.sign(msg)

        self.assertTrue(validator.verify(msg, token_v1))

        validator.rotate(key_v2)
        self.assertEqual(validator.version, 2)

        token_v2 = validator.sign(msg)
        self.assertTrue(validator.verify(msg, token_v2))
        self.assertTrue(validator.verify(msg, token_v1))
        self.assertFalse(validator.verify(msg, "tampered-bogus-token"))

    def test_secret_exposure_scanner_blocks_leaks(self):
        """Exposure Scanner: Detects unmasked AWS keys, private keys, and live payment credentials."""
        # Dynamically constructed to prevent test file itself from triggering pre-commit static scanners
        aws_content = "AWS_SECRET = '" + "AKIA" + "IOSFODNN7EXAMPLE'"
        self.assertTrue(len(SecretExposureScanner.scan_string(aws_content)) > 0)

        pk_content = "-----" + "BEGIN RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA..."
        self.assertTrue(len(SecretExposureScanner.scan_string(pk_content)) > 0)

        pay_content = "STRIPE_KEY = '" + "sk_live_" + "51Abcdefghijklmnopqrstuvw'"
        self.assertTrue(len(SecretExposureScanner.scan_string(pay_content)) > 0)

        clean_content = "DEBUG = True\nPORT = 8000\nSERVICE = 'pharmacy-saas'"
        self.assertEqual(len(SecretExposureScanner.scan_string(clean_content)), 0)

    def test_tenant_secret_rest_crud_and_rotation(self):
        """REST API: Validates encrypted secret creation, safe metadata responses, and rotation audit."""
        create_res = self.client.post(
            "/api/v2/secrets/",
            {
                "secret_type": "DGDA_PORTAL_TOKEN",
                "raw_secret_value": "dgda-api-live-token-998877665544",
            },
            format="json",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(create_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_res.data["secret_type"], "DGDA_PORTAL_TOKEN")
        self.assertEqual(create_res.data["version"], 1)
        self.assertEqual(create_res.data["key_preview"], "dgda-****-5544")
        self.assertNotIn("raw_secret_value", create_res.data)

        secret_id = create_res.data["secret_id"]

        rotate_res = self.client.post(
            f"/api/v2/secrets/{secret_id}/rotate/",
            {"new_secret_value": "dgda-api-live-token-rotated-223344"},
            format="json",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(rotate_res.status_code, status.HTTP_200_OK)
        self.assertEqual(rotate_res.data["version"], 2)
        self.assertEqual(rotate_res.data["key_preview"], "dgda-****-3344")

        audits_res = self.client.get(
            "/api/v2/secrets/audits/",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(audits_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(audits_res.data), 1)
        self.assertEqual(audits_res.data[0]["old_version"], 1)
        self.assertEqual(audits_res.data[0]["new_version"], 2)

    def test_cli_tool_execution(self):
        """CLI Tool: shared/tools/secrets_cli.py executes with exit code 0."""
        cli_tool = _ROOT_DIR / "shared" / "tools" / "secrets_cli.py"
        self.assertTrue(cli_tool.exists())

        proc = subprocess.run(
            [sys.executable, str(cli_tool), "--verify-all"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT_DIR),
        )
        self.assertEqual(proc.returncode, 0, f"CLI execution failed: {proc.stderr}")
        self.assertIn("RESULT: SUCCESS", proc.stdout)

    def test_adr_0037_decision_record_parity(self):
        """Documentation: Ensures ADR-0037 decision record exists and contains required specifications."""
        adr_path = (
            _ROOT_DIR
            / "shared"
            / "docs"
            / "architecture"
            / "decisions"
            / "ADR-0037-environment-secrets-architecture-and-rotation.md"
        )
        self.assertTrue(adr_path.exists())
        content = adr_path.read_text(encoding="utf-8")
        self.assertIn("Status", content)
        self.assertIn("Accepted", content)
        self.assertIn("Multi-Tier Environment Classification", content)
        self.assertIn("Missing-Secret Fail-Fast Invariant", content)
        self.assertIn("Dual-Key Secret Rotation", content)
        self.assertIn("Exposure Scanner Guardrail", content)
