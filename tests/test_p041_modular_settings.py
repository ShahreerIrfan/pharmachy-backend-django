"""
backend/tests/test_p041_modular_settings.py
Architecture Decision: ADR-0041
Backend Acceptance Tests for Modular Settings DRF ViewSets, Audits, and RBAC Boundaries.
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

from django.test import Client, TestCase

from apps.environment_governance.models import EnvironmentProfileAudit, EnvironmentType


class BackendTestP041ModularSettings(TestCase):
    """
    Acceptance test suite for DRF endpoints and RBAC boundaries of ADR-0041.
    """

    def setUp(self):
        self.client = Client()
        self.tenant_a = "tenant-dhanmondi-pharma"
        self.tenant_b = "tenant-sylhet-care"

        self.audit_a = EnvironmentProfileAudit.objects.create(
            tenant_id=self.tenant_a,
            environment=EnvironmentType.DEVELOPMENT,
            debug=True,
            database_engine="sqlite3",
            database_name="db.sqlite3",
            is_hardened=False,
            ssl_redirect=False,
            session_cookie_secure=False,
            csrf_cookie_secure=False,
            secrets_validated=False,
            inspected_by="ADMIN",
        )

    def test_valid_scenario_current_profile_inspection(self):
        """Verifies authorized Admin can inspect active environment profile and creates audit."""
        res = self.client.get(
            "/api/v2/environment/profile/current/",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("environment", data)
        self.assertIn("debug", data)
        self.assertIn("database_engine", data)
        self.assertIn("audit_id", data)
        self.assertEqual(data["tenant_id"], self.tenant_a)

        # Verify audit record persisted
        audit_exists = EnvironmentProfileAudit.objects.filter(audit_id=data["audit_id"]).exists()
        self.assertTrue(audit_exists)

    def test_valid_scenario_simulate_development_profile(self):
        """Verifies simulation of development profile passes."""
        res = self.client.post(
            "/api/v2/environment/profile/simulate/",
            {
                "target_profile": "development",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "VALID")
        self.assertEqual(data["simulated_profile"], "development")

    def test_invalid_input_simulate_production_with_bad_secrets_rejected(self):
        """
        Invalid Case: Simulating production with insecure default secret key
        must be rejected with HTTP 400 Bad Request.
        """
        res = self.client.post(
            "/api/v2/environment/profile/simulate/",
            {
                "target_profile": "production",
                "simulated_secret_key": "django-insecure-bad-dev-key",
                "simulated_postgres_password": "pharmacy_secure_pass",
                "simulated_allowed_hosts": ["*"],
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertIn("production_secrets", data)

    def test_unauthorized_cashier_role_forbidden(self):
        """Verifies Cashier role is rejected with HTTP 403 Forbidden."""
        res = self.client.get(
            "/api/v2/environment/profile/current/",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Cashiers are not authorized", res.json().get("detail", ""))

    def test_cross_tenant_access_forbidden(self):
        """Verifies Tenant B cannot retrieve Tenant A's environment audit record."""
        res = self.client.get(
            f"/api/v2/environment/profile/{self.audit_a.audit_id}/",
            HTTP_X_TENANT_ID=self.tenant_b,
            HTTP_X_USER_ROLE="ADMIN",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Cross-tenant access forbidden", res.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()
