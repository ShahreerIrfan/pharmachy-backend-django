"""
backend/tests/test_p054_global_exception_handling.py
Architecture Decision: ADR-0014, ADR-0047, & ADR-0054
Comprehensive test suite for centralized global exception handling,
safe localized error messages, stable machine error codes, and trace context preservation.
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

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import Branch, Tenant


class TestP054GlobalExceptionHandling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-dhanmondi-test-54",
            defaults={"name": "Dhanmondi Test Care", "subdomain": "dh-care-54", "is_active": True},
        )
        cls.branch, _ = Branch.objects.get_or_create(
            branch_id="branch-dh-test-54",
            defaults={"tenant": cls.tenant, "name": "Main Dispensary", "is_active": True},
        )

    def setUp(self):
        self.client = APIClient()

    def test_catalog_endpoint(self):
        """Verifies GET /api/v2/exceptions/catalog/ returns registered canonical error codes."""
        resp = self.client.get("/api/v2/exceptions/catalog/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("code"), "ERROR_CATALOG_RETRIEVED")
        catalog = data.get("data", [])
        self.assertGreaterEqual(len(catalog), 20)
        codes = [e["error_code"] for e in catalog]
        self.assertIn("ERR_VALIDATION_FAILED", codes)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", codes)
        self.assertIn("ERR_DATABASE_INTEGRITY", codes)
        self.assertIn("ERR_DATABASE_UNAVAILABLE", codes)
        self.assertIn("ERR_INTERNAL_SERVER_ERROR", codes)

    def test_valid_scenario(self):
        """Verifies valid scenario returns 200 OK and standard envelope."""
        resp = self.client.post("/api/v2/exceptions/simulate/", {"scenario": "valid"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("code"), "SIMULATION_SUCCESS")
        self.assertEqual(data.get("message_en"), "Simulated valid request completed successfully.")
        self.assertIn("অনুরোধের সিমুলেশন", data.get("message_bn", ""))

    def test_input_validation_error(self):
        """Verifies 422 ERR_VALIDATION_FAILED with field_errors and CORRECT_INPUT_FIELDS."""
        resp = self.client.post("/api/v2/exceptions/simulate/", {"scenario": "validation_error"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = resp.data
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("code"), "ERR_VALIDATION_FAILED")
        self.assertEqual(data.get("recovery_action"), "CORRECT_INPUT_FIELDS")
        self.assertFalse(data.get("retryable"))
        field_errors = data.get("field_errors", {})
        self.assertIn("medicine_id", field_errors)
        self.assertIn("dispense_quantity", field_errors)
        self.assertIn("এক বা একাধিক তথ্যের মান সঠিক নয়", data.get("message_bn", ""))

    def test_cross_tenant_security_violation_and_audit(self):
        """Verifies 403 ERR_CROSS_TENANT_VIOLATION emits AuditLogEntry with DENIED status."""
        initial_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()

        resp = self.client.post(
            "/api/v2/exceptions/simulate/",
            {"scenario": "cross_tenant"},
            format="json",
            HTTP_X_REQUEST_ID="req-test-p054-cross-audit",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        data = resp.data
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("code"), "ERR_CROSS_TENANT_VIOLATION")
        self.assertEqual(data.get("recovery_action"), "ABORT_SECURITY_INCIDENT")
        self.assertIn("অন্য ফার্মেসির তথ্যে", data.get("message_bn", ""))

        new_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()
        self.assertGreater(new_count, initial_count)
        latest_audit = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").latest("timestamp")
        self.assertEqual(latest_audit.status, AuditActionStatus.DENIED)
        self.assertEqual(latest_audit.resource_type, "SECURITY_BARRIER")

    def test_database_integrity_conflict_masked(self):
        """Verifies 409 ERR_DATABASE_INTEGRITY masks table names, constraints, and SQL."""
        resp = self.client.post("/api/v2/exceptions/simulate/", {"scenario": "database_integrity"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        data = resp.data
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("code"), "ERR_DATABASE_INTEGRITY")
        self.assertFalse(data.get("retryable"))
        self.assertEqual(data.get("recovery_action"), "CORRECT_INPUT_FIELDS")
        self.assertIn("ডেটাবেজ ইন্টিগ্রিটি", data.get("message_bn", ""))

        # Assert no internal table schema leaked
        import json
        dumped = json.dumps(data)
        self.assertNotIn("core_saleinvoice", dumped)
        self.assertNotIn("DETAIL:", dumped)
        self.assertNotIn("Key (tenant_id", dumped)

    def test_database_operational_unavailable(self):
        """Verifies 503 ERR_DATABASE_UNAVAILABLE sets retryable=true and RETRY_AFTER_BACKOFF."""
        resp = self.client.post("/api/v2/exceptions/simulate/", {"scenario": "database_unavailable"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        data = resp.data
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("code"), "ERR_DATABASE_UNAVAILABLE")
        self.assertTrue(data.get("retryable"))
        self.assertEqual(data.get("recovery_action"), "RETRY_AFTER_BACKOFF")
        self.assertIn("ডেটাবেজ সেবা সাময়িকভাবে অনুপলব্ধ", data.get("message_bn", ""))

        # Assert connection string host/port not leaked
        import json
        dumped = json.dumps(data)
        self.assertNotIn("postgres.internal", dumped)
        self.assertNotIn("5432", dumped)

    def test_unhandled_server_error_masked(self):
        """Verifies 500 ERR_INTERNAL_SERVER_ERROR masks traceback and preserves correlation ID."""
        custom_req_id = "req-unhandled-trace-p054"
        resp = self.client.post(
            "/api/v2/exceptions/simulate/",
            {"scenario": "unhandled_server_error"},
            format="json",
            HTTP_X_REQUEST_ID=custom_req_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        data = resp.data
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("code"), "ERR_INTERNAL_SERVER_ERROR")
        self.assertEqual(data.get("request_id"), custom_req_id)
        self.assertFalse(data.get("retryable"))
        self.assertEqual(data.get("recovery_action"), "RETRY_AFTER_BACKOFF")
        self.assertIn("অভ্যন্তরীণ সমস্যা", data.get("message_bn", ""))

        # Assert raw traceback not leaked in client payload
        import json
        dumped = json.dumps(data)
        self.assertNotIn("RuntimeError", dumped)
        self.assertNotIn("Traceback", dumped)
        self.assertNotIn("pointer dereference", dumped)

    def test_subscription_expired_scenario(self):
        """Verifies 403 ERR_SUBSCRIPTION_EXPIRED with CONTACT_SUPPORT."""
        resp = self.client.post("/api/v2/exceptions/simulate/", {"scenario": "subscription_expired"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        data = resp.data
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("code"), "ERR_SUBSCRIPTION_EXPIRED")
        self.assertEqual(data.get("recovery_action"), "CONTACT_SUPPORT")
        self.assertIn("সাবস্ক্রিপশনের মেয়াদ শেষ", data.get("message_bn", ""))

    @override_settings(DEBUG=False)
    def test_global_handler404_api_route(self):
        """Verifies non-existent API routes return 404 ERR_RESOURCE_NOT_FOUND JSON envelope."""
        custom_req_id = "req-404-test-p054"
        resp = self.client.get(
            "/api/v2/non-existent-endpoint-route/",
            HTTP_X_REQUEST_ID=custom_req_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        data = resp.json() if hasattr(resp, "json") else resp.data
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("code"), "ERR_RESOURCE_NOT_FOUND")
        self.assertEqual(data.get("request_id"), custom_req_id)
        self.assertEqual(data.get("recovery_action"), "CORRECT_INPUT_FIELDS")
        self.assertIn("অনুরোধকৃত", data.get("message_bn", ""))

    def test_trace_context_retrieval(self):
        """Verifies GET /api/v2/exceptions/trace-context/ preserves correlation headers."""
        custom_req_id = "req-trace-context-test-54"
        custom_cor_id = "cor-uuid-54"
        resp = self.client.get(
            "/api/v2/exceptions/trace-context/",
            HTTP_X_REQUEST_ID=custom_req_id,
            HTTP_X_CORRELATION_ID=custom_cor_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        ctx = data.get("data", {})
        self.assertEqual(ctx.get("request_id"), custom_req_id)
        self.assertEqual(ctx.get("correlation_id"), custom_cor_id)


if __name__ == "__main__":
    unittest.main()
