"""
backend/tests/test_p078_platform_audit_search.py

Architecture Decision: ADR-0078
Integration Tests for Platform Admin Audit Search:
1. Valid scenario: Search by actor, tenant, date, entity, and reason
2. Invalid scenario: Inverted date range (date_to < date_from) returns 422
3. Unauthorized scenario: Tenant user blocked from platform audit search (403)
4. MFA enforcement: Platform admin without MFA rejected (403)
5. Redaction verification: Forbidden payloads (credentials, PINs, PHI) strictly redacted
6. Audit of audit search: Search creates immutable PLATFORM_AUDIT_SEARCH_EXECUTED record
7. Immutability: DELETE request on audit search blocked (403)
8. Two-tenant isolation: Tenant A & Tenant B users barred from platform search
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import Tenant
from apps.identity.models import PharmacyUser, UserRole


class TestP078PlatformAuditSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_base = random.randint(100000, 999999)

        # 1. Platform SuperAdmin with MFA
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p078_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+8801700{phone_base}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": True,
            },
        )

        # 2. Platform Admin WITHOUT MFA
        cls.superadmin_no_mfa, _ = PharmacyUser.objects.get_or_create(
            email=f"p078_admin_nomfa_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin No MFA {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+8801701{phone_base}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": False,
            },
        )

        # 3. Platform Auditor with MFA
        cls.platform_auditor, _ = PharmacyUser.objects.get_or_create(
            email=f"p078_auditor_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Auditor {cls.suffix}",
                "role": "PLATFORM_AUDITOR",
                "phone_number": f"+8801702{phone_base}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": True,
            },
        )

        # 4. Tenant A & Owner A
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-p078-a-{cls.suffix}",
            name=f"Audit Tenant A {cls.suffix}",
            subdomain=f"audit-a-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p078_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+8801703{phone_base}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # 5. Tenant B & Owner B
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p078-b-{cls.suffix}",
            name=f"Audit Tenant B {cls.suffix}",
            subdomain=f"audit-b-{cls.suffix}",
            plan_tier="BASIC",
            status="ACTIVE",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p078_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+8801704{phone_base}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

        # Seed test audit log entries
        cls.target_action = f"TEST_AUDIT_ACTION_{cls.suffix}"
        cls.seed_entry = AuditLogEntry.objects.create(
            action=cls.target_action,
            resource_type="TENANT_CONFIG",
            resource_id=f"cfg-{cls.suffix}",
            tenant_id=cls.tenant_a.tenant_id,
            user_id=str(cls.superadmin.user_id),
            actor_email=cls.superadmin.email,
            actor_role=cls.superadmin.role,
            status=AuditActionStatus.SUCCESS,
            request_id=f"req-seed-{cls.suffix}",
            before_state={
                "config_key": "MFA_POLICY",
                "secret_key": "VerySecretKey123",  # Forbidden credential
                "password_hash": "hash_xyz",  # Forbidden credential
            },
            after_state={
                "config_key": "MFA_POLICY",
                "otp": "999888",  # Forbidden OTP
                "clinical_notes": "Patient diagnosed with hypertension",  # Forbidden PHI
            },
            metadata_json={
                "reason": f"Operational maintenance review {cls.suffix}",
                "bkash_pin": "1234",  # Forbidden financial PIN
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_01_valid_superadmin_search_by_filters(self):
        """Valid Scenario: SuperAdmin with MFA searches audit logs by actor, tenant, entity, reason."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get(
            "/api/v2/platform/audit-search/",
            data={
                "actor": self.superadmin.email,
                "tenant": self.tenant_a.tenant_id,
                "entity_type": "TENANT_CONFIG",
                "reason": self.target_action,
            },
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertGreaterEqual(data["count"], 1)

        result = next((r for r in data["results"] if r["entry_id"] == str(self.seed_entry.entry_id)), None)
        self.assertIsNotNone(result)
        self.assertEqual(result["tenant_id"], self.tenant_a.tenant_id)
        self.assertEqual(result["action"], self.target_action)

    def test_02_forbidden_payloads_strictly_redacted(self):
        """Redaction Guardrail: Secret keys, passwords, OTPs, PINs, and PHI are redacted as [REDACTED]."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get(
            f"/api/v2/platform/audit-search/{self.seed_entry.entry_id}/",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry = response.json()

        # Credentials redacted
        before = entry.get("before_state", {})
        self.assertEqual(before.get("secret_key"), "[REDACTED]")
        self.assertEqual(before.get("password_hash"), "[REDACTED]")

        # OTP & PHI redacted
        after = entry.get("after_state", {})
        self.assertEqual(after.get("otp"), "[REDACTED]")
        self.assertIn("REDACTED", after.get("clinical_notes"))

        # Financial PIN redacted
        meta = entry.get("metadata_json", {})
        self.assertIn("REDACTED", meta.get("bkash_pin"))

    def test_03_invalid_date_range_returns_422(self):
        """Invalid Case: date_to strictly earlier than date_from returns 422."""
        self.client.force_authenticate(user=self.superadmin)
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        response = self.client.get(
            "/api/v2/platform/audit-search/",
            data={
                "date_from": now.isoformat(),
                "date_to": yesterday.isoformat(),  # Inverted!
            },
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_04_tenant_user_blocked_from_platform_audit_search(self):
        """Unauthorized Case: Pharmacy Owner attempting platform audit search receives 403."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            "/api/v2/platform/audit-search/",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error_code"], "PLATFORM_ACCESS_DENIED")

    def test_05_platform_admin_without_mfa_is_forbidden(self):
        """MFA Guardrail: Platform admin without MFA enabled is rejected with 403."""
        self.client.force_authenticate(user=self.superadmin_no_mfa)
        response = self.client.get("/api/v2/platform/audit-search/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error_code"], "MFA_AUTHENTICATION_REQUIRED")

    def test_06_auditor_can_search_and_export_manifest(self):
        """Auditor Privilege: Platform Auditor can search and export sanitized audit manifest."""
        self.client.force_authenticate(user=self.platform_auditor)
        response = self.client.post(
            "/api/v2/platform/audit-search/export/",
            data={
                "tenant": self.tenant_a.tenant_id,
                "reason": self.target_action,
            },
            format="json",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("manifest_sha256", data)
        self.assertGreaterEqual(data["exported_records"], 1)

    def test_07_audit_search_execution_is_self_audited(self):
        """Accountability: Every platform audit search generates a PLATFORM_AUDIT_SEARCH_EXECUTED entry."""
        initial_count = AuditLogEntry.objects.filter(action="PLATFORM_AUDIT_SEARCH_EXECUTED").count()
        self.client.force_authenticate(user=self.superadmin)
        self.client.get(
            "/api/v2/platform/audit-search/",
            data={"reason": self.target_action},
            HTTP_X_MFA_VERIFIED="true",
        )
        new_count = AuditLogEntry.objects.filter(action="PLATFORM_AUDIT_SEARCH_EXECUTED").count()
        self.assertGreater(new_count, initial_count)

    def test_08_immutability_delete_blocked(self):
        """Immutability Invariant: Audit entries cannot be deleted (403 ABSOLUTE_SYSTEM_BLOCK)."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.delete(
            f"/api/v2/platform/audit-search/{self.seed_entry.entry_id}/",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error_code"], "ABSOLUTE_SYSTEM_BLOCK")

    def test_09_two_tenant_isolation_privilege_separation(self):
        """Phase Gate: Verifies Tenant A and Tenant B users are strictly denied platform audit search."""
        self.client.force_authenticate(user=self.owner_a)
        res_a = self.client.get("/api/v2/platform/audit-search/")
        self.assertEqual(res_a.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.owner_b)
        res_b = self.client.get("/api/v2/platform/audit-search/")
        self.assertEqual(res_b.status_code, status.HTTP_403_FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
