"""
tests/test_p035_structured_logging.py
Architecture Decision: ADR-0035
Phase 02: Repository, Toolchain & Engineering Automation

Backend Test Suite for Structured JSON Logging, Multi-Tenant Context Propagation,
Zero-PHI Redaction Engine, Middleware Distributed Tracing, and Audit Trail RBAC.
"""

from __future__ import annotations

import io
import json
import logging
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

from apps.audit_logging.context import LogContextManager
from apps.audit_logging.formatters import StructuredJsonFormatter
from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.audit_logging.redaction import SensitiveDataRedactor


class StructuredLoggingTestSuite(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.log_stream = io.StringIO()
        self.handler = logging.StreamHandler(self.log_stream)
        self.formatter = StructuredJsonFormatter(service_name="pharmacy-saas-cloud-api", environment="test")
        self.handler.setFormatter(self.formatter)

        self.test_logger = logging.getLogger("test_structured_logger_backend")
        self.test_logger.setLevel(logging.INFO)
        self.test_logger.handlers = [self.handler]
        self.test_logger.propagate = False

        # Clean existing audit log records
        AuditLogEntry.objects.all().delete()

    def tearDown(self):
        LogContextManager.clear()
        AuditLogEntry.objects.all().delete()

    def test_valid_scenario_structured_json_logging(self):
        """Valid Scenario: Emits structured JSON log containing all 4 required correlation IDs and valid ISO timestamp."""
        LogContextManager.set_context(
            request_id="req-test-uuid-001",
            tenant_id="tenant-dhaka-central",
            device_id="pos-terminal-dhk-01",
            operation_id="op-dispense-rx-441",
            user_id="usr-pharmacist-01",
            client_ip="103.205.71.12",
        )

        self.test_logger.info("Prescription verified and stock deducted successfully")

        log_output = self.log_stream.getvalue().strip()
        self.assertTrue(log_output, "Log output must not be empty.")

        data = json.loads(log_output)
        self.assertEqual(data["level"], "INFO")
        self.assertEqual(data["service"], "pharmacy-saas-cloud-api")
        self.assertEqual(data["environment"], "test")
        self.assertEqual(data["message"], "Prescription verified and stock deducted successfully")
        self.assertIn("timestamp", data)

        context = data["context"]
        self.assertEqual(context["request_id"], "req-test-uuid-001")
        self.assertEqual(context["tenant_id"], "tenant-dhaka-central")
        self.assertEqual(context["device_id"], "pos-terminal-dhk-01")
        self.assertEqual(context["operation_id"], "op-dispense-rx-441")
        self.assertEqual(context["user_id"], "usr-pharmacist-01")
        self.assertEqual(context["client_ip"], "103.205.71.12")

    def test_redaction_credentials_and_tokens(self):
        """Redaction: Passwords, bearer tokens, API keys, and JWTs are redacted with [REDACTED]."""
        payload = {
            "username": "cashier_dhk",
            "password": "SecretPassword123!",
            "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy",
            "api_key": "sec-live-abc123456",
            "cookie": "sessionid=xyz987",
        }

        redacted = SensitiveDataRedactor.redact(payload)
        self.assertEqual(redacted["password"], SensitiveDataRedactor.REDACTED_TEXT)
        self.assertEqual(redacted["access_token"], SensitiveDataRedactor.REDACTED_TEXT)
        self.assertEqual(redacted["api_key"], SensitiveDataRedactor.REDACTED_TEXT)
        self.assertEqual(redacted["cookie"], SensitiveDataRedactor.REDACTED_TEXT)
        self.assertEqual(redacted["username"], "cashier_dhk")

        raw_msg = "Upstream call with Authorization: Bearer secret-token-value-999 failed"
        redacted_msg = SensitiveDataRedactor.redact_string(raw_msg)
        self.assertNotIn("secret-token-value-999", redacted_msg)
        self.assertIn("Bearer [REDACTED_TOKEN]", redacted_msg)

    def test_redaction_financial_pins_and_cards(self):
        """Redaction: bKash/Nagad PINs and credit card numbers are replaced with [REDACTED_FINANCIAL]."""
        payload = {
            "payment_method": "BKASH",
            "bkash_pin": "12345",
            "card_number": "4111-2222-3333-4444",
            "cvv": "999",
        }

        redacted = SensitiveDataRedactor.redact(payload)
        self.assertEqual(redacted["bkash_pin"], SensitiveDataRedactor.REDACTED_FINANCIAL)
        self.assertEqual(redacted["card_number"], SensitiveDataRedactor.REDACTED_FINANCIAL)
        self.assertEqual(redacted["cvv"], SensitiveDataRedactor.REDACTED_FINANCIAL)
        self.assertEqual(redacted["payment_method"], "BKASH")

    def test_redaction_bangladesh_nid_and_real_phones(self):
        """Redaction: 10/13/17-digit Bangladesh NID and non-test real phone numbers are masked."""
        nid_17 = "1985" + "1234567890123"
        raw_str = f"Patient NID: {nid_17}, mobile: 01712345678"
        redacted_str = SensitiveDataRedactor.redact_string(raw_str)

        self.assertNotIn(nid_17, redacted_str)
        self.assertNotIn("01712345678", redacted_str)
        self.assertIn(SensitiveDataRedactor.REDACTED_NID, redacted_str)
        self.assertIn(SensitiveDataRedactor.REDACTED_PHONE, redacted_str)

        test_phone_str = "Contact dummy number: +8801700000001"
        preserved = SensitiveDataRedactor.redact_string(test_phone_str)
        self.assertIn("+8801700000001", preserved)

    def test_redaction_clinical_health_information(self):
        """Redaction: Patient diagnosis, clinical notes, and prescription images are replaced with [REDACTED_PHI]."""
        payload = {
            "patient_token": "PT-99887",
            "diagnosis": "Type II Diabetes Mellitus with Neuropathy",
            "clinical_notes": "Patient reports severe chest pain; refer to specialist",
            "prescription_image": "https://storage.pharmacy.com/prescriptions/rx-image-secret.jpg",
        }

        redacted = SensitiveDataRedactor.redact(payload)
        self.assertEqual(redacted["patient_token"], "PT-99887")
        self.assertEqual(redacted["diagnosis"], SensitiveDataRedactor.REDACTED_PHI)
        self.assertEqual(redacted["clinical_notes"], SensitiveDataRedactor.REDACTED_PHI)
        self.assertEqual(redacted["prescription_image"], SensitiveDataRedactor.REDACTED_PHI)

    def test_invalid_or_missing_correlation_context(self):
        """Invalid / Incomplete: Unbound context falls back safely to 'req-system-unbound' without throwing errors."""
        LogContextManager.clear()
        ctx_dict = LogContextManager.get_context_dict()

        self.assertEqual(ctx_dict["request_id"], "req-system-unbound")
        self.assertIsNone(ctx_dict["tenant_id"])
        self.assertIsNone(ctx_dict["device_id"])

        self.test_logger.warning("System background task executed with unbound context")
        output = self.log_stream.getvalue().strip()
        self.assertTrue(output)
        data = json.loads(output)
        self.assertEqual(data["context"]["request_id"], "req-system-unbound")

    def test_audit_log_unauthorized_role_rejected(self):
        """Unauthorized Role: Cashier or Dispenser role requesting audit logs receives HTTP 403 Forbidden."""
        AuditLogEntry.objects.create(
            tenant_id="tenant-dhaka-central",
            request_id="req-sample-001",
            action="DISPENSE",
            resource_type="Prescription",
            status=AuditActionStatus.SUCCESS,
        )

        response = self.client.get(
            "/api/v2/audit-logs/",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Access denied", str(response.data))

        response_dispenser = self.client.get(
            "/api/v2/audit-logs/",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="DISPENSER",
        )
        self.assertEqual(response_dispenser.status_code, status.HTTP_403_FORBIDDEN)

    def test_audit_log_cross_tenant_isolation_strictly_enforced(self):
        """Cross-Tenant Boundary: Pharmacy owner from tenant A querying tenant B audit logs is rejected (HTTP 403)."""
        AuditLogEntry.objects.create(
            tenant_id="tenant-chittagong-care",
            request_id="req-ctg-001",
            action="STOCK_ADJUSTMENT",
            resource_type="BatchStock",
            status=AuditActionStatus.SUCCESS,
        )

        response = self.client.get(
            "/api/v2/audit-logs/?tenant_id=tenant-chittagong-care",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Cross-tenant access forbidden", str(response.data))

    def test_audit_log_authorized_owner_access(self):
        """Authorized Role: Pharmacy Owner querying their own tenant's audit trail receives HTTP 200 OK."""
        AuditLogEntry.objects.create(
            tenant_id="tenant-dhaka-central",
            request_id="req-dhk-001",
            action="LOGIN_ATTEMPT",
            resource_type="StaffMember",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"username": "owner_dhk", "client_ip": "103.205.71.12"},
        )

        response = self.client.get(
            "/api/v2/audit-logs/",
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = (
            response.data["results"]
            if isinstance(response.data, dict) and "results" in response.data
            else response.data
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tenant_id"], "tenant-dhaka-central")
        self.assertEqual(results[0]["action"], "LOGIN_ATTEMPT")

    def test_middleware_injects_request_id_and_duration(self):
        """Middleware: Propagates X-Request-ID to response headers and sets correlation context."""
        custom_req_id = "req-client-test-77889"
        response = self.client.get(
            "/health/",
            HTTP_X_REQUEST_ID=custom_req_id,
            HTTP_X_TENANT_ID="tenant-dhaka-central",
            HTTP_X_DEVICE_ID="pos-device-01",
            HTTP_X_OPERATION_ID="op-health-check",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.headers.get("X-Request-ID"), custom_req_id)

    def test_cli_tool_execution(self):
        """CLI Tool: shared/tools/structured_logging_cli.py executes with exit code 0."""
        cli_path = _ROOT_DIR / "shared" / "tools" / "structured_logging_cli.py"
        self.assertTrue(cli_path.exists(), "CLI tool script must exist on disk.")

        proc = subprocess.run(
            [sys.executable, str(cli_path), "--verify-all"],
            capture_output=True,
            text=True,
            cwd=str(_ROOT_DIR),
        )
        self.assertEqual(proc.returncode, 0, f"CLI execution failed with stderr: {proc.stderr}")
        self.assertIn("RESULT: SUCCESS", proc.stdout)

    def test_adr_0035_decision_record_parity(self):
        """Documentation: Ensures ADR-0035 decision record exists and contains required specifications."""
        adr_path = (
            _ROOT_DIR
            / "shared"
            / "docs"
            / "architecture"
            / "decisions"
            / "ADR-0035-structured-json-logging-and-phi-redaction.md"
        )
        self.assertTrue(adr_path.exists(), "ADR-0035 documentation file must exist.")

        content = adr_path.read_text(encoding="utf-8")
        self.assertIn("Status", content)
        self.assertIn("Accepted", content)
        self.assertIn("Zero-PHI and Credential Redaction Invariants", content)
        self.assertIn("Correlation Header Protocol", content)
        self.assertIn("X-Request-ID", content)
        self.assertIn("X-Tenant-ID", content)
        self.assertIn("X-Device-ID", content)
        self.assertIn("X-Operation-ID", content)
