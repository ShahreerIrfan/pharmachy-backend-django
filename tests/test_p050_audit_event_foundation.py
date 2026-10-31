"""
backend/tests/test_p050_audit_event_foundation.py
Architecture Decision: ADR-0050
Comprehensive Test Suite for Append-Only Audit Event Foundation, Actor/Role Attribution,
Before/After Snapshots, Zero-PHI Redaction, and Protected Retention.
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

from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit_logging.models import AuditLogEntry
from apps.core.errors import AuditImmutabilityError
from apps.core.models import Branch, Tenant
from apps.identity.models import ActivationStatus, PharmacyUser, UserRole


class TestP050AuditEventFoundation(unittest.TestCase):
    """
    Validates append-only immutability, actor/role attribution, before/after snapshots,
    zero-PHI redaction, and DGDA protected retention compliance under ADR-0050.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = APIClient()

        cls.t_id = "tenant-dhanmondi-test-50"
        cls.t_foreign_id = "tenant-foreign-test-50"
        cls.b_id = "b-dhanmondi-50-01"

    def setUp(self):
        Branch.objects.filter(branch_id=self.b_id).delete()
        PharmacyUser.objects.filter(email__in=["owner50t@dhanmondi.com", "cashier50t@dhanmondi.com"]).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_id, self.t_foreign_id]).delete()

        self.tenant = Tenant.objects.create(
            tenant_id=self.t_id,
            name="Dhanmondi Test Care 50",
            subdomain="dhanmondi-test-50",
            plan_tier="PRO",
            is_active=True,
        )
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=self.t_foreign_id,
            name="Foreign Test Care 50",
            subdomain="foreign-test-50",
            plan_tier="STANDARD",
            is_active=True,
        )

        self.branch = Branch.objects.create(
            branch_id=self.b_id,
            tenant=self.tenant,
            name="Main Branch",
            license_number="DGDA-50-01",
            is_active=True,
        )

        self.owner = PharmacyUser.objects.create_user(
            email="owner50t@dhanmondi.com",
            phone_number="+8801716660001",
            password="OwnerPassword123!",
            tenant_id=self.t_id,
            branch_id=self.b_id,
            full_name="Pharmacy Owner 50",
            role=UserRole.PHARMACY_OWNER,
            activation_status=ActivationStatus.ACTIVE,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email="cashier50t@dhanmondi.com",
            phone_number="+8801716660002",
            password="CashierPassword123!",
            tenant_id=self.t_id,
            branch_id=self.b_id,
            full_name="Pharmacy Cashier 50",
            role=UserRole.CASHIER,
            activation_status=ActivationStatus.ACTIVE,
        )

        # Tokens
        r_owner = RefreshToken.for_user(self.owner)
        r_owner["tenant_id"] = self.t_id
        r_owner["branch_id"] = self.b_id
        r_owner["role"] = UserRole.PHARMACY_OWNER
        self.token_owner = str(r_owner.access_token)

        r_cashier = RefreshToken.for_user(self.cashier)
        r_cashier["tenant_id"] = self.t_id
        r_cashier["branch_id"] = self.b_id
        r_cashier["role"] = UserRole.CASHIER
        self.token_cashier = str(r_cashier.access_token)

    def tearDown(self):
        Branch.objects.filter(branch_id=self.b_id).delete()
        PharmacyUser.objects.filter(email__in=["owner50t@dhanmondi.com", "cashier50t@dhanmondi.com"]).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_id, self.t_foreign_id]).delete()

    def test_01_append_only_event_creation_with_before_after_state(self):
        """Valid Scenario: Emitting an audit event creates an immutable entry with before/after state."""
        payload = {
            "action": "PRICE_OVERRIDE",
            "resource_type": "MEDICINE_PRODUCT",
            "resource_id": "MED-TEST-001",
            "status": "SUCCESS",
            "before_state": {"mrp_unit": "100.00"},
            "after_state": {"mrp_unit": "90.00"},
            "retention_days": 730,
        }
        res = self.client.post(
            "/api/v2/audit-logs/events/",
            data=payload,
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["tenant_id"], self.t_id)
        self.assertEqual(data["actor_role"], "PHARMACY_OWNER")
        self.assertEqual(data["before_state"]["mrp_unit"], "100.00")
        self.assertEqual(data["after_state"]["mrp_unit"], "90.00")
        self.assertTrue(data["is_immutable"])
        self.assertIsNotNone(data["retention_until"])

    def test_02_immutability_guard_blocks_update_to_existing_entry(self):
        """Immutability Scenario: Attempting to UPDATE an existing audit record raises AuditImmutabilityError."""
        entry = AuditLogEntry.objects.create(
            tenant_id=self.t_id,
            action="DISPENSE_MEDICINE",
            resource_type="SALE_INVOICE",
            user_id=str(self.owner.user_id),
            actor_role="PHARMACY_OWNER",
            request_id="req-test-update",
        )
        with self.assertRaises(AuditImmutabilityError):
            entry.action = "TAMPERED_ACTION"
            entry.save()

    def test_03_retention_guard_blocks_deletion_of_entry(self):
        """Retention Scenario: Attempting to DELETE an existing audit record raises AuditImmutabilityError."""
        entry = AuditLogEntry.objects.create(
            tenant_id=self.t_id,
            action="DISPENSE_MEDICINE",
            resource_type="SALE_INVOICE",
            user_id=str(self.owner.user_id),
            actor_role="PHARMACY_OWNER",
            request_id="req-test-delete",
        )
        with self.assertRaises(AuditImmutabilityError):
            entry.delete()

    def test_04_zero_phi_redaction_scrubs_sensitive_keys(self):
        """Data Privacy Scenario: Sensitive passwords and diagnostic notes in state snapshots are redacted."""
        payload = {
            "action": "PRESCRIPTION_UPDATE",
            "resource_type": "PRESCRIPTION",
            "resource_id": "RX-TEST-001",
            "before_state": {"patient_name": "Karim Uddin", "diagnosis": "Infection"},
            "after_state": {"password": "PlainSecretPassword!", "card_number": "1234567890123456"},
        }
        res = self.client.post(
            "/api/v2/audit-logs/events/",
            data=payload,
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["before_state"]["patient_name"], "[REDACTED_PHI]")
        self.assertEqual(data["before_state"]["diagnosis"], "[REDACTED_PHI]")
        self.assertEqual(data["after_state"]["password"], "[REDACTED]")
        self.assertEqual(data["after_state"]["card_number"], "[REDACTED_FINANCIAL]")

    def test_05_authorized_roles_can_query_audit_logs(self):
        """Permission Scenario: PHARMACY_OWNER is authorized to query tenant audit logs."""
        AuditLogEntry.objects.create(
            tenant_id=self.t_id,
            action="BATCH_QUARANTINE",
            resource_type="BATCH_STOCK",
            user_id=str(self.owner.user_id),
            actor_role="PHARMACY_OWNER",
            request_id="req-query-1",
        )
        res = self.client.get(
            "/api/v2/audit-logs/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 200)

    def test_06_unauthorized_cashier_role_blocked_from_querying_logs(self):
        """Permission Scenario: CASHIER role is forbidden from querying regulatory audit logs."""
        res = self.client.get(
            "/api/v2/audit-logs/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
        )
        self.assertEqual(res.status_code, 403)

    def test_07_cross_tenant_audit_query_returns_403_cross_tenant_violation(self):
        """Cross-Tenant Scenario: Attempting to query foreign tenant logs returns 403 ERR_CROSS_TENANT_VIOLATION."""
        res = self.client.get(
            f"/api/v2/audit-logs/?tenant_id={self.t_foreign_id}",
            HTTP_AUTHORIZATION=f"Bearer {self.token_owner}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_08_retention_policy_endpoint_contract(self):
        """Contract Scenario: Retention policy endpoint returns DGDA compliant policy matrix."""
        res = self.client.get("/api/v2/audit-logs/retention-policy/")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["standard_retention_days"], 730)
        self.assertEqual(data["controlled_substance_retention_days"], 1825)
        self.assertTrue(data["append_only"])
