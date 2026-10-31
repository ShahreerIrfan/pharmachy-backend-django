"""
backend/tests/test_p049_tenant_context_middleware.py
Architecture Decision: ADR-0049
Comprehensive Test Suite for Tenant Context Resolution Middleware, Signed Claims,
and Client-Supplied Tenant ID Disallowance.
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

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import Branch, Tenant
from apps.identity.models import ActivationStatus, PharmacyUser, UserRole


class TestP049TenantContextMiddleware(unittest.TestCase):
    """
    Validates that tenant context is strictly resolved from trusted signed claims.
    Ensures client-supplied tenant IDs are never trusted alone and cross-tenant
    tampering is blocked with HTTP 403 ERR_CROSS_TENANT_VIOLATION.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = APIClient()

        cls.t_dhaka_id = "tenant-dhanmondi-test-49"
        cls.t_sylhet_id = "tenant-sylhet-test-49"
        cls.b_dhaka_id = "b-dhanmondi-49-01"
        cls.b_sylhet_id = "b-sylhet-49-01"

    def setUp(self):
        # Cleanup any lingering test fixtures
        Branch.objects.filter(branch_id__in=[self.b_dhaka_id, self.b_sylhet_id]).delete()
        PharmacyUser.objects.filter(
            email__in=["owner49@dhanmondi.com", "super49@pharmacy.com", "cashier49@dhanmondi.com"]
        ).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_dhaka_id, self.t_sylhet_id]).delete()

        # Create Active Tenants
        self.t_dhaka = Tenant.objects.create(
            tenant_id=self.t_dhaka_id,
            name="Dhanmondi Care Pharmacy",
            subdomain="dhanmondi-test-49",
            plan_tier="PRO",
            is_active=True,
        )
        self.t_sylhet = Tenant.objects.create(
            tenant_id=self.t_sylhet_id,
            name="Sylhet Care Pharmacy",
            subdomain="sylhet-test-49",
            plan_tier="STANDARD",
            is_active=True,
        )

        # Create Branches
        self.b_dhaka = Branch.objects.create(
            branch_id=self.b_dhaka_id,
            tenant=self.t_dhaka,
            name="Dhanmondi Main Branch",
            license_number="DGDA-DH-4901",
            is_active=True,
        )
        self.b_sylhet = Branch.objects.create(
            branch_id=self.b_sylhet_id,
            tenant=self.t_sylhet,
            name="Sylhet Main Branch",
            license_number="DGDA-SY-4902",
            is_active=True,
        )

        # Create Cashier
        self.user_cashier = PharmacyUser.objects.create_user(
            email="cashier49@dhanmondi.com",
            phone_number="+8801718880001",
            password="CashierPassword123!",
            tenant_id=self.t_dhaka_id,
            branch_id=self.b_dhaka_id,
            full_name="Dhanmondi Cashier",
            role=UserRole.CASHIER,
            activation_status=ActivationStatus.ACTIVE,
        )

        # Create Superadmin
        self.user_super = PharmacyUser.objects.create_superuser(
            email="super49@pharmacy.com",
            phone_number="+8801718880002",
            password="SuperPassword123!",
            full_name="Platform Superadmin",
        )

        # Generate Tokens
        refresh_c = RefreshToken.for_user(self.user_cashier)
        refresh_c["tenant_id"] = self.t_dhaka_id
        refresh_c["branch_id"] = self.b_dhaka_id
        refresh_c["role"] = UserRole.CASHIER
        self.token_cashier = str(refresh_c.access_token)

        refresh_s = RefreshToken.for_user(self.user_super)
        refresh_s["tenant_id"] = self.user_super.tenant_id
        refresh_s["role"] = UserRole.SUPERADMIN
        self.token_super = str(refresh_s.access_token)

    def tearDown(self):
        Branch.objects.filter(branch_id__in=[self.b_dhaka_id, self.b_sylhet_id]).delete()
        PharmacyUser.objects.filter(
            email__in=["owner49@dhanmondi.com", "super49@pharmacy.com", "cashier49@dhanmondi.com"]
        ).delete()
        Tenant.objects.filter(tenant_id__in=[self.t_dhaka_id, self.t_sylhet_id]).delete()

    def test_01_authenticated_request_resolves_trusted_claims(self):
        """Valid Scenario: Trusted signed JWT claims accurately resolve tenant and branch context."""
        res = self.client.get(
            "/api/v2/tenant-context/current/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json().get("data", {})
        self.assertEqual(data["tenant_id"], self.t_dhaka_id)
        self.assertEqual(data["branch_id"], self.b_dhaka_id)
        self.assertEqual(data["resolution_source"], "JWT_SIGNED_CLAIMS")
        self.assertTrue(data["is_active"])

    def test_02_client_matching_tenant_header_succeeds(self):
        """Valid Scenario: Client sends matching X-Tenant-ID and succeeds without error."""
        res = self.client.get(
            "/api/v2/tenant-context/current/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
            HTTP_X_TENANT_ID=self.t_dhaka_id,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["tenant_id"], self.t_dhaka_id)

    def test_03_forged_or_invalid_jwt_returns_401(self):
        """Invalid Scenario: Forged or malformed JWT token is rejected with 401 ERR_UNAUTHORIZED."""
        res = self.client.get(
            "/api/v2/tenant-context/current/",
            HTTP_AUTHORIZATION="Bearer forged-or-expired-token-12345",
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error_code"], "ERR_UNAUTHORIZED")

    def test_04_cross_tenant_spoofing_attempt_returns_403(self):
        """Cross-Tenant Scenario: Tampering with X-Tenant-ID returns 403 ERR_CROSS_TENANT_VIOLATION and logs audit event."""
        res = self.client.get(
            "/api/v2/tenant-context/current/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
            HTTP_X_TENANT_ID=self.t_sylhet_id,  # Tampering with foreign tenant
        )
        self.assertEqual(res.status_code, 403)
        body = res.json()
        self.assertEqual(body["error_code"], "ERR_CROSS_TENANT_VIOLATION")
        self.assertEqual(body["details"]["trusted_tenant_id"], self.t_dhaka_id)
        self.assertEqual(body["details"]["supplied_tenant_id"], self.t_sylhet_id)

        # Confirm audit trail recorded
        audit = AuditLogEntry.objects.filter(
            action="CROSS_TENANT_ACCESS_ATTEMPT",
            tenant_id=self.t_dhaka_id,
            status=AuditActionStatus.DENIED,
        ).first()
        self.assertIsNotNone(audit, "Expected security audit entry for cross-tenant breach attempt")

    def test_05_cross_branch_access_violation_returns_403(self):
        """Cross-Branch Scenario: Requesting foreign branch returns 403 ERR_CROSS_TENANT_VIOLATION."""
        res = self.client.get(
            "/api/v2/tenant-context/current/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
            HTTP_X_BRANCH_ID=self.b_sylhet_id,  # Foreign branch
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_06_inactive_tenant_account_returns_403(self):
        """Inactive Tenant Scenario: Suspended tenant account returns 403 ERR_TENANT_INACTIVE."""
        self.t_dhaka.is_active = False
        self.t_dhaka.save()

        res = self.client.get(
            "/api/v2/tenant-context/current/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error_code"], "ERR_TENANT_INACTIVE")

    def test_07_validate_access_endpoint_contract(self):
        """Validates authorized and rejected cross-tenant calls on /api/v2/tenant-context/validate-access/."""
        # Happy path: matching tenant
        res_ok = self.client.post(
            "/api/v2/tenant-context/validate-access/",
            data={"target_tenant_id": self.t_dhaka_id, "target_branch_id": self.b_dhaka_id},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
        )
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.json()["data"]["authorized"])

        # Cross-tenant violation
        res_fail = self.client.post(
            "/api/v2/tenant-context/validate-access/",
            data={"target_tenant_id": self.t_sylhet_id},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_cashier}",
        )
        self.assertEqual(res_fail.status_code, 403)
        self.assertEqual(res_fail.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_08_superadmin_authorized_tenant_override_audited(self):
        """Superadmin Scenario: Platform superadmin can switch tenant context with conspicuous audit record."""
        res = self.client.get(
            "/api/v2/tenant-context/current/",
            HTTP_AUTHORIZATION=f"Bearer {self.token_super}",
            HTTP_X_TENANT_ID=self.t_sylhet_id,
        )
        self.assertEqual(res.status_code, 200)
        data = res.json().get("data", {})
        self.assertEqual(data["tenant_id"], self.t_sylhet_id)
        self.assertEqual(data["resolution_source"], "SUPERADMIN_OVERRIDE")

        # Verify audit trail
        audit = AuditLogEntry.objects.filter(
            action="SUPERADMIN_TENANT_CONTEXT_SWITCH",
            tenant_id=self.t_sylhet_id,
        ).first()
        self.assertIsNotNone(audit, "Superadmin switch must be audited in AuditLogEntry")
