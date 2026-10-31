"""
backend/tests/test_p056_soft_deletion_and_archival.py
Architecture Decision: ADR-0056
Comprehensive test suite for Regulatory Soft-Deletion and Archival Policy:
- Disallows destructive deletion of posted financial (SaleInvoice), inventory, and dispensing (PrescriptionRecord) records.
- Soft-deletion lifecycle (archive, restore) for mutable catalogs with audit trail.
- Strict input validation, unauthenticated 401 envelope, and cross-tenant barrier enforcement.
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


class TestP056SoftDeletionAndArchival(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p056-archival",
            defaults={"name": "P056 Archival Care", "subdomain": "p056-archival", "is_active": True},
        )
        cls.branch, _ = Branch.objects.get_or_create(
            branch_id="branch-p056-01",
            defaults={"tenant": cls.tenant, "name": "Archival Dispensary", "is_active": True},
        )
        cls.user, _ = PharmacyUser.objects.get_or_create(
            email="p056_compliance@pharmacy.bd",
            defaults={
                "phone_number": "+8801700000056",
                "full_name": "Compliance Officer P056",
                "role": "BRANCH_MANAGER",
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch.branch_id,
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_policy_catalog_endpoint(self):
        """Verifies GET /api/v2/archival/policy/ returns registered retention policies."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            "/api/v2/archival/policy/",
            HTTP_X_TENANT_ID=self.tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("code"), "ARCHIVAL_POLICY_RETRIEVED")
        policies = data.get("data", {}).get("policies", [])
        self.assertGreaterEqual(len(policies), 5)
        resource_types = [p["resource_type"] for p in policies]
        self.assertIn("SALE_INVOICE", resource_types)
        self.assertIn("PRESCRIPTION_RECORD", resource_types)
        self.assertIn("AUDIT_LOG_ENTRY", resource_types)
        self.assertIn("MEDICINE_CATALOG", resource_types)

    def test_valid_soft_delete_and_restore_workflow(self):
        """Verifies soft-deletion of mutable catalog item and subsequent restoration."""
        self.client.force_authenticate(user=self.user)

        # 1. Archive resource
        archive_payload = {
            "tenant_id": self.tenant.tenant_id,
            "resource_type": "MEDICINE_CATALOG",
            "resource_id": "MED-NAPA-500",
            "reason": "Manufacturer recalled packaging variant due to DGDA notice",
        }
        resp1 = self.client.post("/api/v2/archival/archive/", archive_payload, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        data1 = resp1.data
        self.assertTrue(data1.get("success"))
        self.assertEqual(data1.get("code"), "RESOURCE_ARCHIVED")
        self.assertTrue(data1["data"]["is_archived"])
        self.assertIn("archived_at", data1["data"])

        # Check AuditLogEntry
        audit1 = AuditLogEntry.objects.filter(action="RESOURCE_ARCHIVED", resource_id="MED-NAPA-500").latest(
            "timestamp"
        )
        self.assertEqual(audit1.status, AuditActionStatus.SUCCESS)

        # 2. Restore resource
        restore_payload = {
            "tenant_id": self.tenant.tenant_id,
            "resource_type": "MEDICINE_CATALOG",
            "resource_id": "MED-NAPA-500",
        }
        resp2 = self.client.post("/api/v2/archival/restore/", restore_payload, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        data2 = resp2.data
        self.assertTrue(data2.get("success"))
        self.assertEqual(data2.get("code"), "RESOURCE_RESTORED")
        self.assertFalse(data2["data"]["is_archived"])

        # Check AuditLogEntry for restore
        audit2 = AuditLogEntry.objects.filter(action="RESOURCE_RESTORED", resource_id="MED-NAPA-500").latest(
            "timestamp"
        )
        self.assertEqual(audit2.status, AuditActionStatus.SUCCESS)

    def test_destructive_delete_on_immutable_record_prohibited(self):
        """Verifies destructive physical delete on posted financial/clinical record returns 409 Conflict."""
        self.client.force_authenticate(user=self.user)
        initial_incident_count = AuditLogEntry.objects.filter(
            action="SECURITY_INCIDENT_DESTRUCTIVE_DELETE_ATTEMPT"
        ).count()

        # 1. Attempt destructive delete on SaleInvoice
        resp1 = self.client.post(
            "/api/v2/archival/attempt-delete/",
            {
                "tenant_id": self.tenant.tenant_id,
                "resource_type": "SALE_INVOICE",
                "resource_id": "INV-2026-9999",
                "reason": "Cashier attempted manual removal of posted invoice",
            },
            format="json",
        )
        self.assertEqual(resp1.status_code, status.HTTP_409_CONFLICT)
        data1 = resp1.data
        self.assertFalse(data1.get("success", False))
        self.assertEqual(data1.get("code"), "ERR_DESTRUCTIVE_DELETE_FORBIDDEN")
        self.assertIn("স্থায়ীভাবে মুছে ফেলা নিষিদ্ধ", data1.get("message_bn", ""))

        # 2. Attempt archive on PrescriptionRecord (immutable clinical record)
        resp2 = self.client.post(
            "/api/v2/archival/archive/",
            {
                "tenant_id": self.tenant.tenant_id,
                "resource_type": "PRESCRIPTION_RECORD",
                "resource_id": "RX-2026-8888",
                "reason": "Attempting to archive prescription log",
            },
            format="json",
        )
        self.assertEqual(resp2.status_code, status.HTTP_409_CONFLICT)
        data2 = resp2.data
        self.assertEqual(data2.get("code"), "ERR_DESTRUCTIVE_DELETE_FORBIDDEN")

        # Verify audit records created
        new_incident_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_DESTRUCTIVE_DELETE_ATTEMPT").count()
        self.assertGreater(new_incident_count, initial_incident_count)

    def test_invalid_or_incomplete_input_rejected(self):
        """Verifies 422 Unprocessable Entity when input is missing or archival reason is short."""
        self.client.force_authenticate(user=self.user)

        # Missing fields
        resp1 = self.client.post("/api/v2/archival/archive/", {}, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        field_errors = resp1.data.get("field_errors", {})
        self.assertIn("resource_type", field_errors)
        self.assertIn("resource_id", field_errors)
        self.assertIn("reason", field_errors)

        # Reason too short (< 5 chars)
        resp2 = self.client.post(
            "/api/v2/archival/archive/",
            {
                "tenant_id": self.tenant.tenant_id,
                "resource_type": "MEDICINE_CATALOG",
                "resource_id": "MED-1",
                "reason": "no",
            },
            format="json",
        )
        self.assertEqual(resp2.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn("reason", resp2.data.get("field_errors", {}))

    def test_unauthenticated_request_rejected(self):
        """Verifies unauthenticated requests receive 401 Unauthorized."""
        resp = self.client.get("/api/v2/archival/policy/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cross_tenant_violation_rejected_and_audited(self):
        """Verifies cross-tenant archival attempt returns 403 Forbidden and audits the incident."""
        self.client.force_authenticate(user=self.user)
        initial_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()

        resp = self.client.post(
            "/api/v2/archival/archive/",
            {
                "tenant_id": "foreign-alien-tenant-violation",
                "resource_type": "MEDICINE_CATALOG",
                "resource_id": "MED-TARGET-01",
                "reason": "Attempting cross-tenant unauthorized archival",
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
