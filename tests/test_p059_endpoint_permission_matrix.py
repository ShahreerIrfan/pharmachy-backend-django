"""
backend/tests/test_p059_endpoint_permission_matrix.py
Architecture Decision: ADR-0005 & ADR-0059
Acceptance criteria automated tests for API Endpoint Permission Matrix:
- Valid scenario: Cashier permitted to read medicines and create invoice in assigned branch.
- Incomplete/Invalid input: Missing method or path returns HTTP 400 Bad Request.
- Unauthorized / Least-privilege violation: Cashier denied resource deletion and audit logs (HTTP 403).
- Cross-branch violation: Cashier restricted from accessing unassigned branch (HTTP 403 + security audit).
- Cross-tenant violation: Caller cannot evaluate for another tenant (HTTP 403 + security audit).
- Absolute block: Destructive deletion of posted financial/stock records strictly blocked for ALL personas.
- Plan tier enforcement: Basic plan denied Pro features (HTTP 403).
- Field masking: Commercial cost price and patient PII masked for Cashier, visible to Owner.
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

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import Branch, Tenant
from apps.identity.models import PharmacyUser, UserRole


class TestP059EndpointPermissionMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p059-matrix",
            defaults={"name": "P059 Matrix Pharmacy", "subdomain": "p059-matrix", "is_active": True},
        )
        cls.branch_1, _ = Branch.objects.get_or_create(
            branch_id="branch-p059-01",
            defaults={"tenant": cls.tenant, "name": "Gulshan Branch", "is_active": True},
        )
        cls.branch_2, _ = Branch.objects.get_or_create(
            branch_id="branch-p059-02",
            defaults={"tenant": cls.tenant, "name": "Dhanmondi Branch", "is_active": True},
        )
        cls.cashier_user, _ = PharmacyUser.objects.get_or_create(
            email="p059_cashier@pharmacy.bd",
            defaults={
                "phone_number": "+8801711000059",
                "full_name": "Cashier P059",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch_1.branch_id,
            },
        )
        cls.owner_user, _ = PharmacyUser.objects.get_or_create(
            email="p059_owner@pharmacy.bd",
            defaults={
                "phone_number": "+8801722000059",
                "full_name": "Owner P059",
                "role": UserRole.PHARMACY_OWNER,
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch_1.branch_id,
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_unauthenticated_request_rejected(self):
        """Unauthenticated caller receives HTTP 401 Unauthorized."""
        resp = self.client.get("/api/v2/permissions/matrix/inventory/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_matrix_inventory_retrieval(self):
        """Authenticated caller receives full catalog conforming to least privilege."""
        self.client.force_authenticate(user=self.owner_user)
        resp = self.client.get("/api/v2/permissions/matrix/inventory/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        payload = resp.json()
        self.assertTrue(payload["success"])
        data = payload["data"]
        self.assertEqual(data["default_policy"], "DENY")
        self.assertGreater(data["total_endpoints"], 5)
        self.assertIsInstance(data["endpoints"], list)

    def test_valid_cashier_checkout_scenario(self):
        """Valid scenario: Cashier permitted to checkout invoices in assigned branch."""
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "method": "POST",
            "path": "/api/v2/invoices/",
            "target_branch_id": "branch-p059-01",
            "tenant_plan": "BASIC",
            "raw_data": {
                "supplier_unit_cost": "150.00",
                "customer_phone": "+8801712345678",
            },
        }
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        res_data = resp.json()["data"]
        self.assertTrue(res_data["is_allowed"])
        self.assertIsNone(res_data["error_code"])
        # Verify field masking applied for Cashier
        masked = res_data["masked_data"]
        self.assertEqual(masked["supplier_unit_cost"], "***REDACTED***")
        self.assertEqual(masked["customer_phone"], "+88017****5678")

    def test_invalid_incomplete_input_scenario(self):
        """Incomplete input: Missing 'method' and 'path' returns HTTP 400 or 422 with validation errors."""
        self.client.force_authenticate(user=self.cashier_user)
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", {}, format="json")
        self.assertIn(resp.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        data = resp.json()
        field_errors = data.get("field_errors", {})
        self.assertTrue("method" in field_errors or "method" in data)
        self.assertTrue("path" in field_errors or "path" in data)

    def test_unauthorized_least_privilege_scenario(self):
        """Unauthorized scenario: Cashier attempting to DELETE branch returns HTTP 403."""
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "method": "DELETE",
            "path": "/api/v2/branches/branch-01/",
        }
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        res_data = resp.json()
        self.assertFalse(res_data["success"])
        self.assertEqual(res_data["code"], "ROLE_PERMISSION_DENIED")

    def test_cross_branch_violation_scenario(self):
        """Cross-branch violation: Cashier in branch-01 attempting action in branch-02 returns HTTP 403."""
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "method": "POST",
            "path": "/api/v2/invoices/",
            "target_branch_id": "branch-p059-02",  # Mismatch with assigned branch
        }
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        res_data = resp.json()
        self.assertEqual(res_data["code"], "FORBIDDEN_BRANCH_BOUNDARY")

        # Verify audit log recorded incident
        incident = AuditLogEntry.objects.filter(
            action="SECURITY_INCIDENT_BRANCH_VIOLATION",
            tenant_id=self.tenant.tenant_id,
        ).first()
        self.assertIsNotNone(incident, "AuditLogEntry for cross-branch violation was not created.")

    def test_cross_tenant_violation_scenario(self):
        """Cross-tenant violation: Accessing another tenant's evaluation returns HTTP 403."""
        self.client.force_authenticate(user=self.cashier_user)
        payload = {
            "method": "GET",
            "path": "/api/v2/medicines/",
            "tenant_id": "tenant-p059-foreign",
        }
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # Verify audit incident
        incident = AuditLogEntry.objects.filter(
            action="SECURITY_INCIDENT_CROSS_TENANT",
            tenant_id=self.tenant.tenant_id,
        ).first()
        self.assertIsNotNone(incident, "AuditLogEntry for cross-tenant violation was not created.")

    def test_absolute_system_block_on_posted_records(self):
        """Absolute block: Destructive deletion of invoices/batches blocked across ALL roles."""
        self.client.force_authenticate(user=self.owner_user)
        payload = {
            "method": "DELETE",
            "path": "/api/v2/invoices/inv-999/",
        }
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        res_data = resp.json()
        self.assertEqual(res_data["code"], "ABSOLUTE_SYSTEM_BLOCK")

    def test_plan_tier_enforcement(self):
        """Plan tier enforcement: Basic plan accessing PRO endpoint is denied HTTP 403."""
        self.client.force_authenticate(user=self.owner_user)
        payload = {
            "method": "GET",
            "path": "/api/v2/analytics/chain-wide/",
            "tenant_plan": "BASIC",
        }
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        res_data = resp.json()
        self.assertEqual(res_data["code"], "INSUFFICIENT_PLAN_TIER")

    def test_owner_exempt_from_field_masking(self):
        """Pharmacy Owner views unmasked commercial cost price and raw phone."""
        self.client.force_authenticate(user=self.owner_user)
        payload = {
            "method": "POST",
            "path": "/api/v2/invoices/",
            "tenant_plan": "PRO",
            "raw_data": {
                "supplier_unit_cost": "210.50",
                "customer_phone": "+8801999887766",
            },
        }
        resp = self.client.post("/api/v2/permissions/matrix/evaluate/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        masked = resp.json()["data"]["masked_data"]
        self.assertEqual(masked["supplier_unit_cost"], "210.50")
        self.assertEqual(masked["customer_phone"], "+8801999887766")


if __name__ == "__main__":
    unittest.main()
