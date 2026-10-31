"""
backend/tests/test_p045_session_token_rotation.py
Architecture Decision: ADR-0045
Backend Acceptance Tests for Session Management, Rotating Refresh Tokens,
Device Binding, Token Replay Detection, and Multi-Session Revocation.
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

from django.test import Client, TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.identity.models import PharmacyUser, UserRole, UserSession


class BackendTestP045SessionTokenRotation(TestCase):
    """
    Acceptance test suite for DRF endpoints and security invariants of ADR-0045.
    """

    def setUp(self):
        self.client = Client()
        self.tenant_a = "tenant-dhanmondi-care"
        self.tenant_b = "tenant-uttara-pharma"
        self.password = "ValidSessionPass2026!#"

        self.user_a = PharmacyUser.objects.create_user(
            email="pharmacist@dhanmondicare.bd",
            phone_number="+8801713000001",
            password=self.password,
            full_name="Dr. Anisur Rahman",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a,
            branch_id="b-dhanmondi-01",
        )

        self.user_b = PharmacyUser.objects.create_user(
            email="manager@uttarapharma.bd",
            phone_number="+8801713000002",
            password=self.password,
            full_name="Tanvir Hossain",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b,
            branch_id="b-uttara-01",
        )

    def tearDown(self):
        for email in [self.user_a.email, self.user_b.email]:
            for u in PharmacyUser.objects.filter(email=email):
                UserSession.objects.filter(user=u).delete()
                u.delete(force_hard_delete=True)

    def test_login_creates_active_user_session_with_device_binding(self):
        """Verifies login creates an active UserSession with device binding and token claims."""
        res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-term-dhaka-01",
            HTTP_X_DEVICE_NAME="Front Desk POS 1",
            HTTP_X_CLIENT_PLATFORM="DESKTOP_POS",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)

        session = UserSession.objects.filter(user=self.user_a, is_active=True).first()
        self.assertIsNotNone(session)
        self.assertEqual(session.device_id, "pos-term-dhaka-01")
        self.assertEqual(session.device_name, "Front Desk POS 1")
        self.assertEqual(session.client_platform, "DESKTOP_POS")
        self.assertEqual(session.tenant_id, self.tenant_a)
        self.assertTrue(session.is_active)

        # Inspect token payload
        refresh = RefreshToken(data["refresh"])
        self.assertEqual(refresh.payload.get("session_id"), str(session.session_id))
        self.assertEqual(refresh.payload.get("device_id"), "pos-term-dhaka-01")

    def test_token_refresh_rotates_tokens_and_updates_session(self):
        """Verifies token refresh issues new refresh token and records old JTI in rotated list."""
        login_res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-term-dhaka-01",
        )
        refresh_1 = login_res.json()["refresh"]
        token_1_jti = str(RefreshToken(refresh_1).payload.get("jti"))

        refresh_res = self.client.post(
            "/api/v2/auth/token/refresh/",
            {"refresh": refresh_1},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-term-dhaka-01",
        )
        self.assertEqual(refresh_res.status_code, 200)
        data = refresh_res.json()
        refresh_2 = data["refresh"]
        token_2_jti = str(RefreshToken(refresh_2).payload.get("jti"))

        self.assertNotEqual(refresh_1, refresh_2)
        self.assertNotEqual(token_1_jti, token_2_jti)

        session = UserSession.objects.filter(user=self.user_a, is_active=True).first()
        self.assertEqual(session.current_refresh_jti, token_2_jti)
        self.assertIn(token_1_jti, session.rotated_jtis)

    def test_token_replay_attack_revokes_token_family_and_session(self):
        """Verifies presenting an already rotated refresh token triggers security revocation (401)."""
        login_res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-term-dhaka-01",
        )
        refresh_1 = login_res.json()["refresh"]

        # Legitimate rotation
        refresh_res_1 = self.client.post(
            "/api/v2/auth/token/refresh/",
            {"refresh": refresh_1},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-term-dhaka-01",
        )
        self.assertEqual(refresh_res_1.status_code, 200)

        # Malicious replay of old refresh_1
        replay_res = self.client.post(
            "/api/v2/auth/token/refresh/",
            {"refresh": refresh_1},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-term-dhaka-01",
        )
        self.assertEqual(replay_res.status_code, 401)
        self.assertEqual(replay_res.json().get("error_code"), "TOKEN_REPLAY_DETECTED")

        session = UserSession.objects.filter(user=self.user_a).first()
        self.assertFalse(session.is_active)
        self.assertEqual(session.revocation_reason, "REPLAY_ATTACK_DETECTED")

    def test_device_binding_mismatch_forbidden(self):
        """Verifies token refresh from an unauthorized device is rejected with 403."""
        login_res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-term-dhaka-01",
        )
        refresh_token = login_res.json()["refresh"]

        mismatch_res = self.client.post(
            "/api/v2/auth/token/refresh/",
            {"refresh": refresh_token},
            content_type="application/json",
            HTTP_X_DEVICE_ID="malicious-rogue-terminal",
        )
        self.assertEqual(mismatch_res.status_code, 403)
        self.assertEqual(mismatch_res.json().get("error_code"), "DEVICE_BINDING_MISMATCH")

    def test_list_sessions_endpoint(self):
        """Verifies authenticated user can list their active sessions."""
        login_1 = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-device-a",
        )
        access_token = login_1.json()["access"]

        login_2 = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-device-b",
        )
        self.assertEqual(login_2.status_code, 200)

        res = self.client.get(
            "/api/v2/auth/sessions/",
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(res.status_code, 200)
        sessions = res.json()
        self.assertIsInstance(sessions, list)
        self.assertEqual(len(sessions), 2)
        device_ids = [s["device_id"] for s in sessions]
        self.assertIn("pos-device-a", device_ids)
        self.assertIn("pos-device-b", device_ids)

    def test_revoke_single_session_endpoint(self):
        """Verifies user can revoke a specific session."""
        login_res = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="pos-device-temp",
        )
        access = login_res.json()["access"]
        session = UserSession.objects.filter(user=self.user_a, is_active=True).first()

        res_revoke = self.client.post(
            f"/api/v2/auth/sessions/{session.session_id}/revoke/",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(res_revoke.status_code, 200)
        self.assertTrue(res_revoke.json()["revoked"])

        session.refresh_from_db()
        self.assertFalse(session.is_active)
        self.assertEqual(session.revocation_reason, "MANUAL_REVOCATION")

    def test_logout_all_terminates_all_active_sessions(self):
        """Verifies logout-all endpoint terminates all active sessions for the user."""
        for d in ["term-1", "term-2", "term-3"]:
            self.client.post(
                "/api/v2/auth/login/",
                {"identifier": self.user_a.email, "password": self.password},
                content_type="application/json",
                HTTP_X_DEVICE_ID=d,
            )

        active_count = UserSession.objects.filter(user=self.user_a, is_active=True).count()
        self.assertEqual(active_count, 3)

        # Login again to get fresh access token
        fresh_login = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="term-4",
        )
        access = fresh_login.json()["access"]

        res = self.client.post(
            "/api/v2/auth/sessions/logout-all/",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(res.json().get("revoked_sessions_count", 0), 4)

        remaining_active = UserSession.objects.filter(user=self.user_a, is_active=True).count()
        self.assertEqual(remaining_active, 0)

    def test_cross_tenant_session_revocation_denied(self):
        """Verifies attempting to revoke another tenant's session returns 403."""
        login_b = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_b.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="uttara-term-01",
        )
        self.assertEqual(login_b.status_code, 200)
        session_b = UserSession.objects.filter(user=self.user_b, is_active=True).first()

        login_a = self.client.post(
            "/api/v2/auth/login/",
            {"identifier": self.user_a.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="dhanmondi-term-01",
        )
        access_a = login_a.json()["access"]

        res = self.client.post(
            f"/api/v2/auth/sessions/{session_b.session_id}/revoke/",
            HTTP_AUTHORIZATION=f"Bearer {access_a}",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json().get("error_code"), "CROSS_TENANT_SESSION_ACCESS_DENIED")


if __name__ == "__main__":
    unittest.main()

