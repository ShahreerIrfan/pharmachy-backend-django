"""
backend/tests/test_p043_authentication_endpoints.py
Architecture Decision: ADR-0043
Backend Acceptance Tests for DRF Authentication Endpoints, Token Lifecycle,
JWT Claims, Blacklist Rotation, and Anti-Enumeration Password Reset.
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

from django.test import Client, TestCase
from rest_framework_simplejwt.tokens import AccessToken

from apps.identity.models import PharmacyUser, UserRole


class BackendTestP043AuthenticationEndpoints(TestCase):
    """
    Acceptance test suite for DRF authentication endpoints and token lifecycles (ADR-0043).
    """

    def setUp(self):
        self.client = Client()
        self.tenant_id = "tenant-dhanmondi-rx"
        self.branch_id = "branch-dhanmondi-01"
        self.email = "pharmacist.lead@dhanmondirx.bd"
        self.phone = "+8801711223344"
        self.password = "DhanmondiPass2026!#"

        from apps.core.models import Branch, Tenant
        self.tenant, _ = Tenant.objects.get_or_create(
            tenant_id=self.tenant_id,
            defaults={"name": "Dhanmondi Rx", "subdomain": "dh-rx-43", "is_active": True},
        )
        self.branch, _ = Branch.objects.get_or_create(
            branch_id=self.branch_id,
            defaults={"tenant": self.tenant, "name": "Main Dispensary", "is_active": True},
        )

        self.user = PharmacyUser.objects.create_user(
            email=self.email,
            phone_number=self.phone,
            password=self.password,
            full_name="Shahriar Ahmed",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_id,
            branch_id=self.branch_id,
            pcb_registration_no="A-45678",
        )

    def tearDown(self):
        for u in PharmacyUser.objects.filter(email=self.email):
            u.delete(force_hard_delete=True)

    def test_login_success_with_email_and_custom_jwt_claims(self):
        """Verifies login via email returns JWT tokens with embedded claims."""
        res = self.client.post(
            "/api/v2/auth/login/",
            {
                "identifier": self.email,
                "password": self.password,
                "device_id": "pos-terminal-a",
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertEqual(data["token_type"], "Bearer")
        self.assertEqual(data["expires_in"], 900)

        # Verify decoded JWT claims
        token = AccessToken(data["access"])
        self.assertEqual(token["tenant_id"], self.tenant_id)
        self.assertEqual(token["branch_id"], self.branch_id)
        self.assertEqual(token["role"], UserRole.STAFF_PHARMACIST)
        self.assertEqual(token["email"], self.email)

    def test_login_success_with_bangladesh_phone_number(self):
        """Verifies login via local BD phone number format."""
        res = self.client.post(
            "/api/v2/auth/login/",
            {
                "identifier": "01711223344",  # unnormalized local input
                "password": self.password,
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("access", data)
        self.assertEqual(data["user"]["phone_number"], self.phone)

    def test_login_failure_invalid_credentials_returns_client_safe_401(self):
        """Verifies invalid password returns client-safe 401 without exposing internal details."""
        res = self.client.post(
            "/api/v2/auth/login/",
            {
                "identifier": self.email,
                "password": "WrongPassword2026!#",
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertIn("error_code", data)
        self.assertEqual(data["error_code"], "INVALID_CREDENTIALS")
        self.assertNotIn("traceback", data)

    def test_login_failure_deactivated_user(self):
        """Verifies deactivated / archived user cannot log in."""
        self.user.is_active = False
        self.user.save()

        res = self.client.post(
            "/api/v2/auth/login/",
            {
                "identifier": self.email,
                "password": self.password,
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 401)

    def test_profile_retrieval_and_role_escalation_guard(self):
        """Verifies profile retrieval and prevents unauthorized role escalation."""
        # Login to obtain access token
        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.email, "password": self.password},
            content_type="application/json",
        )
        access_token = res_login.json()["access"]

        # 1. Unauthenticated profile request fails
        res_anon = self.client.get("/api/v2/auth/profile/")
        self.assertEqual(res_anon.status_code, 401)

        # 2. Authenticated profile request succeeds
        res_prof = self.client.get(
            "/api/v2/auth/profile/",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(res_prof.status_code, 200)
        self.assertEqual(res_prof.json()["email"], self.email)

        # 3. Patch profile updates benign fields but ignores role escalation
        res_patch = self.client.patch(
            "/api/v2/auth/profile/",
            {
                "full_name": "Shahriar Ahmed, Ph.D.",
                "role": "SYSTEM_ADMIN",  # Attempted escalation
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(res_patch.status_code, 200)
        updated_data = res_patch.json()
        self.assertEqual(updated_data["full_name"], "Shahriar Ahmed, Ph.D.")
        self.assertEqual(updated_data["role"], UserRole.STAFF_PHARMACIST)  # Role remained untouched!

    def test_token_refresh_rotation_and_old_token_revocation(self):
        """Verifies refresh token rotation and replay protection."""
        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.email, "password": self.password},
            content_type="application/json",
        )
        refresh_1 = res_login.json()["refresh"]

        # Refresh token
        res_ref = self.client.post(
            "/api/v2/auth/token/refresh/",
            {"refresh": refresh_1},
            content_type="application/json",
        )
        self.assertEqual(res_ref.status_code, 200)
        refresh_2 = res_ref.json().get("refresh")
        self.assertIsNotNone(refresh_2)
        self.assertNotEqual(refresh_1, refresh_2)

        # Replaying refresh_1 must be rejected
        res_replay = self.client.post(
            "/api/v2/auth/token/refresh/",
            {"refresh": refresh_1},
            content_type="application/json",
        )
        self.assertEqual(res_replay.status_code, 401)

    def test_logout_blacklists_refresh_token(self):
        """Verifies logout blacklists the provided refresh token."""
        res_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.email, "password": self.password},
            content_type="application/json",
        )
        access = res_login.json()["access"]
        refresh = res_login.json()["refresh"]

        # Logout
        res_logout = self.client.post(
            "/api/v2/auth/logout/",
            {"refresh": refresh},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(res_logout.status_code, 200)
        self.assertTrue(res_logout.json().get("revoked"))

        # Attempting refresh with blacklisted token must fail
        res_ref = self.client.post(
            "/api/v2/auth/token/refresh/",
            {"refresh": refresh},
            content_type="application/json",
        )
        self.assertEqual(res_ref.status_code, 401)

    def test_password_reset_anti_enumeration(self):
        """Verifies identical 200 OK responses regardless of user existence."""
        # 1. Existing user
        res_exist = self.client.post(
            "/api/v2/auth/password-reset/request/",
            {"identifier": self.email},
            content_type="application/json",
        )
        self.assertEqual(res_exist.status_code, 200)
        self.assertEqual(res_exist.json()["status"], "DISPATCHED")

        # 2. Non-existent identifier
        res_non_exist = self.client.post(
            "/api/v2/auth/password-reset/request/",
            {"identifier": "nonexistent_pharmacist_99@gmail.com"},
            content_type="application/json",
        )
        self.assertEqual(res_non_exist.status_code, 200)
        self.assertEqual(res_non_exist.json()["status"], "DISPATCHED")

    def test_password_reset_confirm_flow_and_token_invalidation(self):
        """Verifies complete reset token lifecycle, single-use, and password update."""
        res_req = self.client.post(
            "/api/v2/auth/password-reset/request/",
            {"identifier": self.email},
            content_type="application/json",
        )
        reset_token = res_req.json().get("dev_reset_token")
        self.assertIsNotNone(reset_token)

        # 1. Reject invalid reset token
        res_bad = self.client.post(
            "/api/v2/auth/password-reset/confirm/",
            {
                "reset_token": "invalid-token-uuid-1234",
                "new_password": "NewStrongPass2026!#",
            },
            content_type="application/json",
        )
        self.assertEqual(res_bad.status_code, 400)

        # 2. Accept valid reset token
        new_password = "UpdatedPass2026!#$"
        res_ok = self.client.post(
            "/api/v2/auth/password-reset/confirm/",
            {
                "reset_token": reset_token,
                "new_password": new_password,
            },
            content_type="application/json",
        )
        self.assertEqual(res_ok.status_code, 200)
        self.assertEqual(res_ok.json().get("status"), "SUCCESS")

        # 3. Token cannot be reused (single-use constraint)
        res_reuse = self.client.post(
            "/api/v2/auth/password-reset/confirm/",
            {
                "reset_token": reset_token,
                "new_password": "AnotherPassword2026!",
            },
            content_type="application/json",
        )
        self.assertEqual(res_reuse.status_code, 400)

        # 4. Old password fails, new password succeeds
        res_old_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.email, "password": self.password},
            content_type="application/json",
        )
        self.assertEqual(res_old_login.status_code, 401)

        res_new_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.email, "password": new_password},
            content_type="application/json",
        )
        self.assertEqual(res_new_login.status_code, 200)


if __name__ == "__main__":
    import unittest

    unittest.main()
