"""
backend/tests/test_p044_mfa_privileged_users.py
Architecture Decision: ADR-0044
Backend Acceptance Tests for Privileged User Multi-Factor Authentication (MFA),
TOTP Verification, Single-Use Recovery Codes, Lockout, and Cross-Tenant Guardrails.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

import pyotp
from django.test import Client, TestCase
from rest_framework_simplejwt.tokens import AccessToken

from apps.identity.models import PharmacyUser, UserRole


class BackendTestP044MfaPrivilegedUsers(TestCase):
    """
    Acceptance test suite for DRF endpoints and security invariants of ADR-0044.
    """

    def setUp(self):
        self.client = Client()
        self.tenant_a = "tenant-dhanmondi-care"
        self.tenant_b = "tenant-uttara-pharma"
        self.password = "PrivilegedPass2026!#"

        self.owner = PharmacyUser.objects.create_user(
            email="owner@dhanmondicare.bd",
            phone_number="+8801712000001",
            password=self.password,
            full_name="Mahmudur Rahman",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a,
            pcb_registration_no="A-10293",
        )

        self.cashier = PharmacyUser.objects.create_user(
            email="cashier@dhanmondicare.bd",
            phone_number="+8801712000002",
            password=self.password,
            full_name="Fatima Khatun",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a,
        )

    def tearDown(self):
        for email in [self.owner.email, self.cashier.email]:
            for u in PharmacyUser.objects.filter(email=email):
                u.delete(force_hard_delete=True)

    def _setup_and_activate_mfa(self, user: PharmacyUser, password: str) -> tuple[str, list[str]]:
        """Helper to set up and confirm MFA for a user, returning (secret, recovery_codes)."""
        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": user.email, "password": password},
            content_type="application/json",
        )
        access_token = res_login.json()["access"]

        res_setup = self.client.post(
            "/api/v2/auth/mfa/setup/",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(res_setup.status_code, 200)
        secret = res_setup.json()["secret"]
        recovery_codes = res_setup.json()["recovery_codes"]

        totp = pyotp.TOTP(secret)
        res_act = self.client.post(
            "/api/v2/auth/mfa/activate/",
            {"code": totp.now()},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(res_act.status_code, 200)
        self.assertTrue(res_act.json()["mfa_enabled"])

        user.refresh_from_db()
        return secret, recovery_codes

    def test_unprivileged_cashier_login_bypasses_mfa_by_default(self):
        """Verifies cashier login without MFA returns full JWT access/refresh tokens immediately."""
        res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.cashier.email, "password": self.password},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertNotIn("mfa_required", data)

    def test_privileged_owner_mfa_setup_and_activation_flow(self):
        """Verifies owner can initiate MFA setup, receive 8 recovery codes, and activate with TOTP."""
        secret, recovery_codes = self._setup_and_activate_mfa(self.owner, self.password)
        self.assertEqual(len(recovery_codes), 8)
        self.assertTrue(self.owner.mfa_enabled)
        self.assertTrue(self.owner.is_privileged)

        # Status check
        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        # Login now returns challenge instead of tokens
        self.assertTrue(res_login.json()["mfa_required"])

    def test_privileged_owner_login_requires_mfa_challenge(self):
        """Verifies logging in with MFA-enabled credentials returns challenge_token and no JWT tokens."""
        self._setup_and_activate_mfa(self.owner, self.password)

        res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("mfa_required"))
        self.assertIn("challenge_token", data)
        self.assertEqual(data["method"], "TOTP")
        self.assertEqual(data["email"], self.owner.email)
        self.assertEqual(data["role"], UserRole.PHARMACY_OWNER)
        self.assertNotIn("access", data)

    def test_mfa_verify_with_totp_code_issues_jwt_tokens(self):
        """Verifies providing valid TOTP code for active challenge returns full JWT access/refresh tokens."""
        secret, _ = self._setup_and_activate_mfa(self.owner, self.password)

        # Obtain challenge
        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        challenge_token = res_login.json()["challenge_token"]

        # Verify with TOTP code
        totp = pyotp.TOTP(secret)
        res_verify = self.client.post(
            "/api/v2/auth/mfa/verify/",
            {"challenge_token": challenge_token, "code": totp.now()},
            content_type="application/json",
        )
        self.assertEqual(res_verify.status_code, 200)
        data = res_verify.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertEqual(data["user"]["email"], self.owner.email)

        # Inspect claims
        token = AccessToken(data["access"])
        self.assertEqual(token["tenant_id"], self.tenant_a)
        self.assertEqual(token["role"], UserRole.PHARMACY_OWNER)

    def test_mfa_verify_with_backup_recovery_code_and_replay_rejection(self):
        """Verifies single-use backup recovery code succeeds once and rejects replay."""
        _, recovery_codes = self._setup_and_activate_mfa(self.owner, self.password)
        used_code = recovery_codes[0]

        # 1. First redemption succeeds
        res_chal1 = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        chal_token1 = res_chal1.json()["challenge_token"]

        res_v1 = self.client.post(
            "/api/v2/auth/mfa/verify/",
            {"challenge_token": chal_token1, "code": used_code},
            content_type="application/json",
        )
        self.assertEqual(res_v1.status_code, 200)
        self.assertIn("access", res_v1.json())

        # 2. Second attempt with the SAME recovery code fails
        res_chal2 = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        chal_token2 = res_chal2.json()["challenge_token"]

        res_v2 = self.client.post(
            "/api/v2/auth/mfa/verify/",
            {"challenge_token": chal_token2, "code": used_code},
            content_type="application/json",
        )
        self.assertEqual(res_v2.status_code, 401)
        self.assertEqual(res_v2.json()["error_code"], "INVALID_MFA_CODE")

    def test_cross_tenant_mfa_challenge_rejected(self):
        """Verifies cross-tenant MFA challenge exchange is strictly blocked with 403 Forbidden."""
        secret, _ = self._setup_and_activate_mfa(self.owner, self.password)

        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        challenge_token = res_login.json()["challenge_token"]

        # Attempt verification under foreign tenant header
        res_cross = self.client.post(
            "/api/v2/auth/mfa/verify/",
            {"challenge_token": challenge_token, "code": pyotp.TOTP(secret).now()},
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_b,
        )
        self.assertEqual(res_cross.status_code, 403)
        self.assertEqual(res_cross.json()["error_code"], "CROSS_TENANT_MFA_FORBIDDEN")

    def test_lockout_after_5_failed_attempts(self):
        """Verifies 5 failed attempts locks out the challenge and user for 15 minutes."""
        self._setup_and_activate_mfa(self.owner, self.password)

        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        challenge_token = res_login.json()["challenge_token"]

        for i in range(1, 5):
            res_bad = self.client.post(
                "/api/v2/auth/mfa/verify/",
                {"challenge_token": challenge_token, "code": "000000"},
                content_type="application/json",
            )
            self.assertEqual(res_bad.status_code, 401)
            self.assertEqual(res_bad.json()["details"]["remaining_attempts"], 5 - i)

        # 5th attempt triggers lockout
        res_lockout = self.client.post(
            "/api/v2/auth/mfa/verify/",
            {"challenge_token": challenge_token, "code": "000000"},
            content_type="application/json",
        )
        self.assertEqual(res_lockout.status_code, 403)
        self.assertEqual(res_lockout.json()["error_code"], "MFA_LOCKED_OUT")

    def test_privileged_user_cannot_disable_mfa(self):
        """Verifies privileged roles are prohibited from disabling mandatory MFA."""
        secret, _ = self._setup_and_activate_mfa(self.owner, self.password)

        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.owner.email, "password": self.password},
            content_type="application/json",
        )
        chal = res_login.json()["challenge_token"]
        res_v = self.client.post(
            "/api/v2/auth/mfa/verify/",
            {"challenge_token": chal, "code": pyotp.TOTP(secret).now()},
            content_type="application/json",
        )
        access_token = res_v.json()["access"]

        res_dis = self.client.post(
            "/api/v2/auth/mfa/disable/",
            {"current_password": self.password},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(res_dis.status_code, 403)
        self.assertEqual(res_dis.json()["error_code"], "MFA_MANDATORY_FOR_PRIVILEGED_ROLE")


if __name__ == "__main__":
    import unittest

    unittest.main()
