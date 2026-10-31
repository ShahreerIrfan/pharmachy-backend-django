"""
backend/tests/test_p058_health_and_readiness.py
Architecture Decision: ADR-0058
Comprehensive test suite for Health, Liveness, and Readiness Endpoints:
- Liveness probe (/health/live/): fast process responsiveness, zero DB IO.
- Readiness probe (/health/ready/): dependency probe verifying DB/cache/storage.
- Zero secret, credential, or PII leakage guarantee.
- Authenticated diagnostic metrics (/api/v2/health/diagnostics/metrics/).
- Unauthorized 401 and cross-tenant barrier enforcement with security audit trail.
"""

from __future__ import annotations

import json
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

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import Branch, Tenant
from apps.identity.models import PharmacyUser


class TestP058HealthAndReadiness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p058-health",
            defaults={"name": "P058 Health Care", "subdomain": "p058-health", "is_active": True},
        )
        cls.branch, _ = Branch.objects.get_or_create(
            branch_id="branch-p058-01",
            defaults={"tenant": cls.tenant, "name": "Health Dispensary", "is_active": True},
        )
        cls.user, _ = PharmacyUser.objects.get_or_create(
            email="p058_admin@pharmacy.bd",
            defaults={
                "phone_number": "+8801700000058",
                "full_name": "Health Auditor P058",
                "role": "SYSTEM_ADMIN",
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch.branch_id,
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_liveness_probe_endpoint(self):
        """Verifies GET /health/live/ and /healthz return 200 OK with UP status."""
        for path in ["/health/live/", "/healthz"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            data = json.loads(resp.content)
            self.assertEqual(data.get("service"), "pharmacy-saas-cloud-api")
            self.assertEqual(data.get("status"), "UP")
            self.assertIn("timestamp", data)
            self.assertEqual(data.get("timezone"), "Asia/Dhaka")

    def test_readiness_probe_endpoint_valid(self):
        """Verifies GET /health/ready/ and /readyz return 200 OK with component checks."""
        for path in ["/health/ready/", "/readyz"]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            data = json.loads(resp.content)
            self.assertEqual(data.get("service"), "pharmacy-saas-cloud-api")
            self.assertEqual(data.get("status"), "READY")
            checks = data.get("checks", {})
            self.assertEqual(checks.get("database"), "UP")
            self.assertIn(checks.get("redis"), ["UP", "DISABLED"])
            self.assertEqual(checks.get("storage"), "UP")

    def test_readiness_zero_secrets_or_pii_exposed(self):
        """Guarantees zero internal passwords, secrets, connection strings, or PII in health responses."""
        forbidden_substrings = [
            "password",
            "secret_key",
            "postgres://",
            "redis://",
            "bearer",
            "private_key",
            "customer_phone",
            "patient_name",
        ]

        resp_ready = self.client.get("/health/ready/")
        raw_ready = resp_ready.content.decode("utf-8").lower()
        for forbidden in forbidden_substrings:
            self.assertNotIn(forbidden, raw_ready, f"Forbidden term '{forbidden}' leaked in /health/ready/!")

        resp_live = self.client.get("/health/live/")
        raw_live = resp_live.content.decode("utf-8").lower()
        for forbidden in forbidden_substrings:
            self.assertNotIn(forbidden, raw_live, f"Forbidden term '{forbidden}' leaked in /health/live/!")

    def test_authenticated_diagnostics_success(self):
        """Verifies authenticated operators can retrieve sanitized diagnostic telemetry."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            "/api/v2/health/diagnostics/metrics/",
            HTTP_X_TENANT_ID=self.tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("code"), "HEALTH_DIAGNOSTICS_RETRIEVED")
        diag = data.get("data", {})
        self.assertEqual(diag.get("status"), "HEALTHY")
        self.assertTrue(diag.get("sanitized"))
        self.assertGreaterEqual(diag.get("active_tenant_count", 0), 1)

    def test_unauthorized_diagnostics_rejected(self):
        """Verifies unauthenticated calls to diagnostics are rejected with 401 Unauthorized."""
        resp = self.client.get("/api/v2/health/diagnostics/metrics/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cross_tenant_diagnostics_rejected_and_audited(self):
        """Verifies cross-tenant diagnostic request returns 403 Forbidden and audits the incident."""
        self.client.force_authenticate(user=self.user)
        initial_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()

        resp = self.client.get(
            "/api/v2/health/diagnostics/metrics/",
            {"tenant_id": "alien-unauthorized-tenant-diagnostics"},
            HTTP_X_TENANT_ID=self.tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(resp.data.get("code"), ["ERR_PERMISSION_DENIED", "FORBIDDEN_CROSS_TENANT_ACCESS"])

        new_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()
        self.assertGreater(new_count, initial_count)


if __name__ == "__main__":
    unittest.main()
