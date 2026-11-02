"""
Integration tests for P114: Password and recovery UX, accessible setup,
one-time tokens, sliding-window throttling, and regulatory audit evidence (ADR-0114).
"""

import json
import random
import uuid
from datetime import timedelta
from pathlib import Path

from django.test import Client, TestCase
from django.utils import timezone
from jsonschema import validate
from rest_framework import status

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PasswordResetToken, PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "password_recovery_ux.schema.json"


class PasswordRecoveryUxIntegrationTests(TestCase):
    """End-to-end integration tests for P114 Password and Recovery UX."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p114-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-p114-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status="ACTIVE",
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p114-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Dispense",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Staff User A
        self.user_a = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Fatima Pharmacist",
            password="InitialPass123!#",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 3. Inactive/Deactivated User
        self.user_inactive = PharmacyUser.objects.create_user(
            email=f"deactivated_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Deactivated Staff",
            password="InitialPass123!#",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.user_inactive.is_active = False
        self.user_inactive.save()

    def test_anti_enumeration_reset_request_and_schema_validation(self):
        """Reset request returns uniform 200 OK conforming to schema regardless of existence."""
        resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": self.user_a.email},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["status"], "DISPATCHED")

        # Validate against JSON Schema
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

        # Non-existent user also returns 200 OK (anti-enumeration)
        non_existent_resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": "nonexistent_person_999@yahoo.com"},
            content_type="application/json",
        )
        self.assertEqual(non_existent_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(non_existent_resp.json()["status"], "DISPATCHED")

    def test_proactive_token_verification(self):
        """Clients can proactively verify token validity before presenting form."""
        req_resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": self.user_a.email},
            content_type="application/json",
        )
        token = req_resp.json().get("dev_reset_token")
        self.assertTrue(token)

        # Valid token verify
        v_resp = self.client.get(f"/api/v2/auth/password/verify-token/?token={token}")
        self.assertEqual(v_resp.status_code, status.HTTP_200_OK)
        v_data = v_resp.json()
        self.assertTrue(v_data["token_valid"])
        self.assertEqual(v_data["status"], "VALID_TOKEN")
        self.assertEqual(v_data["user_email"], self.user_a.email)

        # Invalid token verify
        v_bad = self.client.get("/api/v2/auth/password/verify-token/?token=fake-bogus-token-xyz")
        self.assertEqual(v_bad.status_code, status.HTTP_200_OK)
        self.assertFalse(v_bad.json()["token_valid"])

    def test_weak_password_rejected(self):
        """Weak password confirmation is rejected with ERR_WEAK_PASSWORD."""
        req_resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": self.user_a.email},
            content_type="application/json",
        )
        token = req_resp.json().get("dev_reset_token")

        confirm_resp = self.client.post(
            "/api/v2/auth/password-reset/confirm/",
            data={"reset_token": token, "new_password": "weak"},
            content_type="application/json",
        )
        self.assertEqual(confirm_resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(confirm_resp.json()["error_code"], "ERR_WEAK_PASSWORD")

    def test_successful_reset_and_subsequent_login(self):
        """Valid password reset updates credentials and enables login with new password."""
        req_resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": self.user_a.email},
            content_type="application/json",
        )
        token = req_resp.json().get("dev_reset_token")
        new_pwd = "BrandNewSecure2026!#"

        confirm_resp = self.client.post(
            "/api/v2/auth/password-reset/confirm/",
            data={"reset_token": token, "new_password": new_pwd},
            content_type="application/json",
        )
        self.assertEqual(confirm_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(confirm_resp.json()["status"], "SUCCESS")

        # Subsequent login with new password succeeds
        login_resp = self.client.post(
            "/api/v2/auth/login/",
            data={"identifier": self.user_a.email, "password": new_pwd},
            content_type="application/json",
            HTTP_X_DEVICE_ID="dev-test",
        )
        self.assertEqual(login_resp.status_code, status.HTTP_200_OK)

        # Re-using the token fails (single-use)
        reuse_resp = self.client.post(
            "/api/v2/auth/password-reset/confirm/",
            data={"reset_token": token, "new_password": new_pwd},
            content_type="application/json",
        )
        self.assertEqual(reuse_resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sliding_window_throttling_triggers_429(self):
        """More than 5 reset requests in sliding window triggers 429 ERR_RATE_LIMIT_EXCEEDED."""
        for _ in range(5):
            r = self.client.post(
                "/api/v2/auth/password-reset/request/",
                data={"identifier": self.user_a.email},
                content_type="application/json",
            )
            self.assertEqual(r.status_code, status.HTTP_200_OK)

        # 6th attempt within window is throttled
        throttled_resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": self.user_a.email},
            content_type="application/json",
        )
        self.assertEqual(throttled_resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(throttled_resp.json()["error_code"], "ERR_RATE_LIMIT_EXCEEDED")

    def test_suspended_tenant_subscription_blocks_recovery(self):
        """Suspended tenant subscription blocks password reset with ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED."""
        self.tenant_a.status = "SUSPENDED"
        self.tenant_a.save()

        resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": self.user_a.email},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    def test_inactive_staff_role_denied(self):
        """Inactive/deactivated staff account is rejected with ERR_STAFF_ROLE_DENIED."""
        resp = self.client.post(
            "/api/v2/auth/password-reset/request/",
            data={"identifier": self.user_inactive.email},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_STAFF_ROLE_DENIED")

    def test_password_setup_flow_and_audit_logging(self):
        """New staff setup endpoint activates account and logs immutable AuditLogEntry."""
        # Create unactivated user with reset/setup token
        user_new = PharmacyUser.objects.create_user(
            email=f"invited_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Invited Cashier",
            password="DummyPass123!#",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
        )
        user_new.is_active = False
        user_new.save()

        setup_tok = PasswordResetToken.objects.create(
            user=user_new,
            token="setup-tok-test-12345",
            expires_at=timezone.now() + timedelta(hours=24),
        )

        setup_resp = self.client.post(
            "/api/v2/auth/password/setup/",
            data={"setup_token": setup_tok.token, "new_password": "MyInitialSecurePass2026!#"},
            content_type="application/json",
        )
        self.assertEqual(setup_resp.status_code, status.HTTP_200_OK)
        user_new.refresh_from_db()
        self.assertTrue(user_new.is_active)

        # Audit log verification
        audit_exists = AuditLogEntry.objects.filter(
            action="PASSWORD_SETUP_COMPLETED",
            user_id=str(user_new.user_id),
        ).exists()
        self.assertTrue(audit_exists)
