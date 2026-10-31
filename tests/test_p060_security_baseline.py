"""
backend/tests/test_p060_security_baseline.py
Architecture Decision: ADR-0060
Comprehensive test suite for Backend Smoke and Security Baseline:
- Valid scenario: Privileged Owner runs security baseline suite and receives PASSED status across all 7 pillars.
- Incomplete/Invalid input: Malformed target_suite or non-boolean parameters return HTTP 400 or 422.
- Unauthorized scenario: Cashier is strictly denied from running or inspecting security baseline (HTTP 403).
- Cross-tenant violation: Caller cannot trigger or view baseline for another tenant (HTTP 403 + security audit).
- Posture summary: GET /api/v2/security/baseline/summary/ provides sanitized security posture.
- Unauthenticated rejection: Anonymous caller receives HTTP 401 Unauthorized.
- Migration integrity: Verifies zero unapplied migrations in the database.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import Branch, Tenant
from apps.identity.models import PharmacyUser, UserRole, normalize_bangladesh_phone


class TestP060SecurityBaseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p060-sec",
            defaults={"name": "P060 Security Pharmacy", "subdomain": "p060-sec", "is_active": True},
        )
        cls.branch, _ = Branch.objects.get_or_create(
            branch_id="branch-p060-01",
            defaults={"tenant": cls.tenant, "name": "Security Dispensary", "is_active": True},
        )
        cls.owner_user, _ = PharmacyUser.objects.get_or_create(
            email="p060_owner@pharmacy.bd",
            defaults={
                "phone_number": "+8801700000060",
                "full_name": "Security Owner P060",
                "role": UserRole.PHARMACY_OWNER,
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch.branch_id,
            },
        )
        cls.cashier_user, _ = PharmacyUser.objects.get_or_create(
            email="p060_cashier@pharmacy.bd",
            defaults={
                "phone_number": "+8801711000060",
                "full_name": "Cashier P060",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch.branch_id,
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_unauthenticated_request_rejected(self):
        """Unauthenticated caller receives HTTP 401 Unauthorized."""
        resp = self.client.get("/api/v2/security/baseline/summary/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_valid_owner_runs_security_baseline_suite(self):
        """Valid scenario: Pharmacy Owner executes security suite and passes all 7 pillars."""
        self.client.force_authenticate(user=self.owner_user)
        payload = {
            "target_suite": "backend-security-baseline-suite",
            "fast_mode": True,
        }
        resp = self.client.post("/api/v2/security/baseline/run-suite/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertEqual(data["overall_status"], "PASSED")
        self.assertEqual(data["checks_failed"], 0)
        self.assertGreaterEqual(data["checks_passed"], 7)

        # Confirm all 7 pillars are audited and PASS
        audited_pillars = {c["pillar"] for c in data["checks"]}
        expected_pillars = {
            "AUTH_GATE",
            "AUTHORIZATION_GATE",
            "VALIDATION_GATE",
            "ISOLATION_GATE",
            "AUDIT_GATE",
            "PRIVACY_GATE",
            "MIGRATION_GATE",
        }
        self.assertEqual(audited_pillars, expected_pillars)

        for c in data["checks"]:
            self.assertEqual(c["status"], "PASS")

    def test_posture_summary_endpoint(self):
        """Privileged operator retrieves high-level security configuration summary."""
        self.client.force_authenticate(user=self.owner_user)
        resp = self.client.get("/api/v2/security/baseline/summary/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertTrue(body["success"])
        posture = body["data"]["posture"]
        self.assertEqual(posture["default_authorization_policy"], "DENY")
        self.assertTrue(posture["branch_scoping_enforced"])
        self.assertTrue(posture["absolute_deletion_blocks"])
        self.assertEqual(posture["unapplied_migrations_count"], 0)

    def test_invalid_input_suite_name_rejected(self):
        """Incomplete / invalid input: Unknown suite name or invalid fast_mode returns 400 or 422."""
        self.client.force_authenticate(user=self.owner_user)
        payload = {
            "target_suite": "unknown-nonexistent-suite",
            "fast_mode": "not-a-boolean",
        }
        resp = self.client.post("/api/v2/security/baseline/run-suite/", payload, format="json")
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        body = resp.json()
        field_errors = body.get("field_errors", {})
        self.assertTrue("target_suite" in field_errors or "target_suite" in body)

    def test_cashier_denied_security_baseline_access(self):
        """Unauthorized scenario: Cashier is rejected from running or viewing security baseline."""
        self.client.force_authenticate(user=self.cashier_user)

        # GET summary denied
        get_resp = self.client.get("/api/v2/security/baseline/summary/")
        self.assertEqual(get_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            "PERMISSION_DENIED" in get_resp.json().get("code", "") or "ROLE_PERMISSION_DENIED" in str(get_resp.json())
        )

        # POST run-suite denied
        post_resp = self.client.post("/api/v2/security/baseline/run-suite/", {}, format="json")
        self.assertEqual(post_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            "PERMISSION_DENIED" in post_resp.json().get("code", "") or "ROLE_PERMISSION_DENIED" in str(post_resp.json())
        )

    def test_cross_tenant_violation_audited(self):
        """Cross-tenant violation: Caller cannot run baseline for another tenant."""
        self.client.force_authenticate(user=self.owner_user)
        payload = {
            "target_suite": "backend-security-baseline-suite",
            "tenant_id": "tenant-p060-foreign",
        }
        resp = self.client.post("/api/v2/security/baseline/run-suite/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # Verify audit incident
        incident = AuditLogEntry.objects.filter(
            action="SECURITY_INCIDENT_CROSS_TENANT",
            tenant_id=self.tenant.tenant_id,
        ).first()
        self.assertIsNotNone(incident, "Cross-tenant audit incident was not logged.")

    def test_negative_input_phone_validation(self):
        """Negative input case: Invalid operator prefix is rejected by validation contract."""
        with self.assertRaises(ValidationError):
            normalize_bangladesh_phone("01112345678")  # 011 is not a valid operator

        with self.assertRaises(ValidationError):
            normalize_bangladesh_phone("invalid-phone")

    def test_zero_unapplied_database_migrations(self):
        """Verifies database schema integrity: Zero pending or unapplied migrations."""
        executor = MigrationExecutor(connection)
        unapplied = executor.migration_plan(executor.loader.graph.leaf_nodes())
        self.assertEqual(len(unapplied), 0, f"Found unapplied migrations: {unapplied}")


if __name__ == "__main__":
    unittest.main()
