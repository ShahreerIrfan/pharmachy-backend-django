"""
backend/tests/test_p077_platform_access_policies.py

Architecture Decision: ADR-0077
Integration Tests for Platform API Access Policies, MFA Enforcement,
Least Privilege, and Zero Implicit Tenant Access Guardrails.
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

from apps.core.models import (
    SupportAccessGrant,
    SupportAccessScope,
    SupportAccessStatus,
    Tenant,
)
from apps.core.platform_access_service import PlatformAccessPolicyService
from apps.identity.models import PharmacyUser, UserMfaDevice, UserRole


class TestP077PlatformAccessPolicies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        # synthetic-fixture: use +8801700000000 series
        phone_base = random.randint(100000, 999999)

        # 1. Super Admin with MFA enabled
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p077_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+8801700{phone_base}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": True,
            },
        )
        UserMfaDevice.objects.update_or_create(
            user=cls.superadmin,
            defaults={
                "secret_key": "JBSWY3DPEHPK3PXP",  # standard base32 test secret
                "is_confirmed": True,
            },
        )

        # 2. Super Admin WITHOUT MFA
        cls.superadmin_no_mfa, _ = PharmacyUser.objects.get_or_create(
            email=f"p077_admin_nomfa_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin No MFA {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+8801701{phone_base}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": False,
            },
        )

        # 3. Platform Auditor (Least Privilege - Read Only)
        cls.platform_auditor, _ = PharmacyUser.objects.get_or_create(
            email=f"p077_auditor_{cls.suffix}@platform.internal",
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
            tenant_id=f"t-p077-a-{cls.suffix}",
            name=f"Access Tenant A {cls.suffix}",
            subdomain=f"access-a-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p077_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+8801703{phone_base}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # 5. Tenant B & Owner B
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p077-b-{cls.suffix}",
            name=f"Access Tenant B {cls.suffix}",
            subdomain=f"access-b-{cls.suffix}",
            plan_tier="BASIC",
            status="ACTIVE",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p077_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+8801704{phone_base}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

        # Seed default policies
        PlatformAccessPolicyService.seed_default_policies()

    def setUp(self):
        self.client = APIClient()

    def test_01_valid_superadmin_with_mfa_can_list_policies(self):
        """Valid Scenario: SuperAdmin with verified MFA successfully lists policies."""
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get(
            "/api/v2/platform/access-policies/",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertGreaterEqual(data["count"], 1)

    def test_02_superadmin_without_mfa_is_forbidden(self):
        """Unauthorized Scenario: SuperAdmin without MFA enabled is rejected with 403."""
        self.client.force_authenticate(user=self.superadmin_no_mfa)
        response = self.client.get("/api/v2/platform/access-policies/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error_code"], "MFA_AUTHENTICATION_REQUIRED")

    def test_03_tenant_owner_cannot_access_platform_policies(self):
        """Unauthorized Scenario: Pharmacy owner attempting to access platform API receives 403."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/platform/access-policies/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error_code"], "PLATFORM_ACCESS_DENIED")

    def test_04_least_privilege_auditor_cannot_create_policy(self):
        """Least Privilege: Platform Auditor can read policies but is blocked from mutating them."""
        self.client.force_authenticate(user=self.platform_auditor)
        # GET is allowed for auditor
        get_resp = self.client.get(
            "/api/v2/platform/access-policies/",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)

        # POST mutation is forbidden
        payload = {
            "name": "Auditor Forbidden Policy",
            "endpoint_pattern": "^/api/v2/platform/test/.*$",
            "http_method": "GET",
            "allowed_roles": ["SUPERADMIN"],
        }
        post_resp = self.client.post(
            "/api/v2/platform/access-policies/",
            data=payload,
            format="json",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(post_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_05_invalid_incomplete_payload_returns_422(self):
        """Invalid Case: Missing required name or invalid regex pattern returns 422."""
        self.client.force_authenticate(user=self.superadmin)
        invalid_payload = {
            "name": "",  # invalid
            "endpoint_pattern": "[invalid(regex",  # unparseable regex
        }
        response = self.client.post(
            "/api/v2/platform/access-policies/",
            data=invalid_payload,
            format="json",
            HTTP_X_MFA_VERIFIED="true",
            HTTP_X_MFA_CODE="123456",
        )
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_06_step_up_mfa_challenge_enforced_on_policy_creation(self):
        """Step-Up MFA: Policy creation requires step-up TOTP verification."""
        self.client.force_authenticate(user=self.superadmin)
        payload = {
            "name": "Audit Search Policy",
            "endpoint_pattern": "^/api/v2/platform/audit-search/.*$",
            "http_method": "*",
            "allowed_roles": ["SUPERADMIN", "PLATFORM_OPERATOR"],
            "require_mfa": True,
            "require_step_up": False,
        }

        # 1. Missing step-up code -> 403 STEP_UP_MFA_REQUIRED
        resp_no_stepup = self.client.post(
            "/api/v2/platform/access-policies/",
            data=payload,
            format="json",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(resp_no_stepup.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_no_stepup.json()["error_code"], "STEP_UP_MFA_REQUIRED")

        # 2. Invalid step-up code -> 403 INVALID_STEP_UP_MFA_CODE
        resp_bad_code = self.client.post(
            "/api/v2/platform/access-policies/",
            data=payload,
            format="json",
            HTTP_X_MFA_VERIFIED="true",
            HTTP_X_MFA_CODE="000000",
        )
        self.assertEqual(resp_bad_code.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_bad_code.json()["error_code"], "INVALID_STEP_UP_MFA_CODE")

        # 3. Valid step-up code -> 201 Created
        resp_valid = self.client.post(
            "/api/v2/platform/access-policies/",
            data=payload,
            format="json",
            HTTP_X_MFA_VERIFIED="true",
            HTTP_X_MFA_CODE="123456",
        )
        self.assertEqual(resp_valid.status_code, status.HTTP_201_CREATED)
        self.assertTrue(resp_valid.json()["policy_id"].startswith("pol-"))

    def test_07_zero_implicit_tenant_access_blocked_without_support_grant(self):
        """Zero Implicit Tenant Access: Platform Admin cannot access tenant private data without delegated grant."""
        self.client.force_authenticate(user=self.superadmin)
        target_path = f"/api/v2/tenants/{self.tenant_a.tenant_id}/orders/"

        # 1. Probe evaluate endpoint without grant token -> 403 ZERO_IMPLICIT_TENANT_ACCESS_DENIED
        resp_blocked = self.client.post(
            "/api/v2/platform/access-policies/evaluate/",
            data={"path": target_path, "method": "GET"},
            format="json",
            HTTP_X_MFA_VERIFIED="true",
        )
        self.assertEqual(resp_blocked.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp_blocked.json()["error_code"], "ZERO_IMPLICIT_TENANT_ACCESS_DENIED")

        # 2. Provision an active SupportAccessGrant (ADR-0068)
        grant = SupportAccessGrant.objects.create(
            grant_id=f"grant-{uuid.uuid4().hex[:12]}",
            tenant=self.tenant_a,
            requested_by_operator=self.superadmin,
            approved_by_user=self.owner_a,
            scope=SupportAccessScope.DIAGNOSTIC_READ,
            status=SupportAccessStatus.APPROVED,
            reason="Diagnostic investigation of sync queue",
            duration_minutes=30,
            approved_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=30),
            access_token=f"tok-{uuid.uuid4().hex}",
        )

        # 3. Probe evaluate endpoint with valid grant token -> 200 OK
        resp_allowed = self.client.post(
            "/api/v2/platform/access-policies/evaluate/",
            data={"path": target_path, "method": "GET"},
            format="json",
            HTTP_X_MFA_VERIFIED="true",
            HTTP_X_SUPPORT_GRANT_TOKEN=grant.access_token,
        )
        self.assertEqual(resp_allowed.status_code, status.HTTP_200_OK)
        self.assertTrue(resp_allowed.json()["allowed"])

    def test_08_two_tenant_isolation_and_privilege_separation(self):
        """Phase Gate: Verifies strict isolation between Tenant A and Tenant B."""
        # Tenant A user cannot access platform policies
        self.client.force_authenticate(user=self.owner_a)
        res_a = self.client.get("/api/v2/platform/access-policies/")
        self.assertEqual(res_a.status_code, status.HTTP_403_FORBIDDEN)

        # Tenant B user cannot access platform policies
        self.client.force_authenticate(user=self.owner_b)
        res_b = self.client.get("/api/v2/platform/access-policies/")
        self.assertEqual(res_b.status_code, status.HTTP_403_FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
