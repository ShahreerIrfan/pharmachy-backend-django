"""
backend/tests/test_p057_async_task_idempotency.py
Architecture Decision: ADR-0031 & ADR-0057
Comprehensive test suite for Asynchronous Task Idempotency, Retry Controls, and Dead-Letter Handling:
- Deterministic replay safety via tenant-scoped idempotency keys (cached responses returned).
- Bounded retry controls and Dead-Letter Queue (DLQ) routing upon retry exhaustion.
- Operator DLQ reprocessing with forensic audit trail.
- Boundary enforcement: core synchronous ledger/stock mutations are forbidden in async tasks.
- Unauthenticated 401 rejection and cross-tenant barrier enforcement.
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

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import Branch, Tenant
from apps.identity.models import PharmacyUser


class TestP057AsyncTaskIdempotency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p057-async",
            defaults={"name": "P057 Async Care", "subdomain": "p057-async", "is_active": True},
        )
        cls.branch, _ = Branch.objects.get_or_create(
            branch_id="branch-p057-01",
            defaults={"tenant": cls.tenant, "name": "Async Dispensary", "is_active": True},
        )
        cls.user, _ = PharmacyUser.objects.get_or_create(
            email="p057_admin@pharmacy.bd",
            defaults={
                "phone_number": "+8801700000057",
                "full_name": "Async Operator P057",
                "role": "BRANCH_MANAGER",
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch.branch_id,
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_dispatch_task_valid_and_idempotent_replay(self):
        """Verifies initial task dispatch and replay interception returning cached result."""
        import uuid

        self.client.force_authenticate(user=self.user)
        idempotency_key = f"sms-inv-{uuid.uuid4().hex[:8]}"

        payload = {
            "tenant_id": self.tenant.tenant_id,
            "idempotency_key": idempotency_key,
            "category": "NOTIFICATION",
            "task_name": "SEND_CUSTOMER_INVOICE_SMS",
            "payload": {
                "customer_phone": "+8801711223344",
                "invoice_number": "INV-2026-909",
                "amount": "1520.00",
            },
        }

        # 1. Initial execution
        resp1 = self.client.post("/api/v2/async-tasks/dispatch/", payload, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        data1 = resp1.data
        self.assertTrue(data1.get("success"))
        self.assertEqual(data1.get("code"), "TASK_DISPATCHED")
        self.assertFalse(data1["data"]["is_replay"])
        self.assertFalse(data1["data"]["cached"])
        self.assertEqual(data1["data"]["status"], "SUCCESS")
        job_id_initial = data1["data"]["job_id"]

        # 2. Duplicate re-dispatch with identical idempotency key
        resp2 = self.client.post("/api/v2/async-tasks/dispatch/", payload, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        data2 = resp2.data
        self.assertTrue(data2.get("success"))
        self.assertEqual(data2.get("code"), "TASK_CACHED_REPLAY")
        self.assertTrue(data2["data"]["is_replay"])
        self.assertTrue(data2["data"]["cached"])
        self.assertEqual(data2["data"]["status"], "SUCCESS")
        self.assertEqual(data2["data"]["job_id"], job_id_initial)
        self.assertIn("ডুপ্লিকেট টাস্ক প্রতিহত", data2["data"]["message_bn"])

    def test_retry_controls_and_dead_letter_routing(self):
        """Verifies bounded retry controls, routing to DLQ upon exhaustion, and operator reprocessing."""
        import uuid

        self.client.force_authenticate(user=self.user)
        idempotency_key = f"webhook-dlq-{uuid.uuid4().hex[:8]}"

        # 1. Dispatch initial failing task
        dispatch_payload = {
            "tenant_id": self.tenant.tenant_id,
            "idempotency_key": idempotency_key,
            "category": "NOTIFICATION",
            "task_name": "SEND_WEBHOOK_EVENT",
            "payload": {"webhook_url": "https://api.external.bd/v1/notify"},
            "max_retries": 3,
            "simulate_failure": True,
        }
        resp1 = self.client.post("/api/v2/async-tasks/dispatch/", dispatch_payload, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        job_id = resp1.data["data"]["job_id"]
        self.assertEqual(resp1.data["data"]["retry_count"], 1)

        # 2. Retry 2 (under max_retries 3) -> Status: RETRYING
        retry_payload = {
            "tenant_id": self.tenant.tenant_id,
            "job_id": job_id,
            "error_message": "Gateway timeout 504 on attempt 2",
        }
        resp2 = self.client.post("/api/v2/async-tasks/retry-or-fail/", retry_payload, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.data["data"]["status"], "RETRYING")
        self.assertEqual(resp2.data["data"]["retry_count"], 2)

        # 3. Retry 3 (reaches max_retries 3) -> Status: DEAD_LETTER
        retry_payload["error_message"] = "Third-party server unavailable 503 terminal"
        resp3 = self.client.post("/api/v2/async-tasks/retry-or-fail/", retry_payload, format="json")
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        data3 = resp3.data["data"]
        self.assertEqual(data3["status"], "DEAD_LETTER")
        self.assertEqual(data3["retry_count"], 3)
        self.assertEqual(data3["action_taken"], "ROUTED_TO_DEAD_LETTER_QUEUE")

        # Verify AuditLogEntry for DLQ routing
        dlq_audit = AuditLogEntry.objects.filter(action="ASYNC_JOB_DEAD_LETTERED", resource_id=job_id).latest(
            "timestamp"
        )
        self.assertEqual(dlq_audit.status, AuditActionStatus.ERROR)

        # 4. Inspect Dead-Letter Queue
        resp_dlq = self.client.get(
            "/api/v2/async-tasks/dead-letters/",
            {"tenant_id": self.tenant.tenant_id},
        )
        self.assertEqual(resp_dlq.status_code, status.HTTP_200_OK)
        dlq_tasks = resp_dlq.data["data"]["tasks"]
        dlq_job_ids = [t["job_id"] for t in dlq_tasks]
        self.assertIn(job_id, dlq_job_ids)

        # 5. Operator reprocesses dead-lettered job
        resp_reprocess = self.client.post(
            "/api/v2/async-tasks/reprocess-dlq/",
            {"tenant_id": self.tenant.tenant_id, "job_id": job_id},
            format="json",
        )
        self.assertEqual(resp_reprocess.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_reprocess.data["data"]["status"], "SUCCESS")

        # Verify AuditLogEntry for reprocess
        reprocess_audit = AuditLogEntry.objects.filter(action="ASYNC_JOB_DLQ_REPROCESSED", resource_id=job_id).latest(
            "timestamp"
        )
        self.assertEqual(reprocess_audit.status, AuditActionStatus.SUCCESS)

    def test_source_specific_business_rule_synchronous_mutation_blocked(self):
        """Verifies core synchronous ledger/stock mutations are forbidden in async background jobs."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "idempotency_key": "violating-stock-key",
            "category": "NOTIFICATION",
            "task_name": "ASYNC_STOCK_ALLOCATION_MUTATION",  # FORBIDDEN!
            "payload": {"batch_id": "B-101", "quantity": 50},
        }
        resp = self.client.post("/api/v2/async-tasks/dispatch/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        field_errors = resp.data.get("field_errors", {})
        self.assertIn("boundary_violation", field_errors)

    def test_invalid_or_incomplete_input_rejected(self):
        """Verifies 422 Unprocessable Entity when mandatory fields are missing."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/api/v2/async-tasks/dispatch/", {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        field_errors = resp.data.get("field_errors", {})
        self.assertIn("idempotency_key", field_errors)
        self.assertIn("category", field_errors)
        self.assertIn("task_name", field_errors)

    def test_unauthenticated_request_rejected(self):
        """Verifies unauthenticated calls return 401 Unauthorized."""
        resp = self.client.get("/api/v2/async-tasks/dead-letters/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cross_tenant_access_denied_and_audited(self):
        """Verifies cross-tenant async task dispatch returns 403 Forbidden and audits the incident."""
        self.client.force_authenticate(user=self.user)
        initial_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()

        resp = self.client.post(
            "/api/v2/async-tasks/dispatch/",
            {
                "tenant_id": "foreign-alien-tenant-async-violation",
                "idempotency_key": "cross-tenant-key-1",
                "category": "NOTIFICATION",
                "task_name": "SEND_SMS",
            },
            format="json",
            HTTP_X_TENANT_ID=self.tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(resp.data.get("code"), ["ERR_PERMISSION_DENIED", "FORBIDDEN_CROSS_TENANT_ACCESS"])

        new_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()
        self.assertGreater(new_count, initial_count)


if __name__ == "__main__":
    unittest.main()
