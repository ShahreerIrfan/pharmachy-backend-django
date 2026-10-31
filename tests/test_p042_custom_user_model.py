"""
backend/tests/test_p042_custom_user_model.py
Architecture Decision: ADR-0042
Backend Acceptance Tests for PharmacyUser DRF Endpoints, Dual Auth, RBAC, and Safe Deletion.
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

from apps.identity.models import ActivationStatus, PharmacyUser, UserRole


class BackendTestP042CustomUserModel(TestCase):
    """
    Acceptance test suite for DRF endpoints and RBAC boundaries of ADR-0042.
    """

    def setUp(self):
        self.client = Client()
        self.tenant_a = "tenant-uttara-care"
        self.tenant_b = "tenant-mirpur-pharma"

        self.user_a = PharmacyUser.objects.create_user(
            email="pharmacist.a@uttaracare.bd",
            phone_number="+8801711122233",
            password="UttaraSecure2026!#",
            full_name="Tariqul Islam",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a,
            pcb_registration_no="A-11029",
        )

    def test_valid_scenario_create_user(self):
        """Verifies authorized Pharmacy Owner can create a new staff user."""
        res = self.client.post(
            "/api/v2/identity/users/",
            {
                "email": "cashier.new@uttaracare.bd",
                "phone_number": "01812345678",
                "password": "CashierPass2026!",
                "full_name": "Sadia Jahan",
                "role": "CASHIER",
                "tenant_id": self.tenant_a,
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["email"], "cashier.new@uttaracare.bd")
        self.assertEqual(data["phone_number"], "+8801812345678")
        self.assertEqual(data["role"], "CASHIER")
        self.assertFalse(data["is_archived"])

    def test_valid_scenario_authenticate_via_email_and_phone(self):
        """Verifies user can authenticate using either email or normalized mobile phone."""
        # 1. Via Email
        res_email = self.client.post(
            "/api/v2/identity/users/authenticate/",
            {
                "identifier": "pharmacist.a@uttaracare.bd",
                "password": "UttaraSecure2026!#",
            },
            content_type="application/json",
        )
        self.assertEqual(res_email.status_code, 200)
        self.assertTrue(res_email.json()["authenticated"])
        self.assertEqual(res_email.json()["auth_method"], "EMAIL")

        # 2. Via Phone
        res_phone = self.client.post(
            "/api/v2/identity/users/authenticate/",
            {
                "identifier": "01711122233",
                "password": "UttaraSecure2026!#",
            },
            content_type="application/json",
        )
        self.assertEqual(res_phone.status_code, 200)
        self.assertTrue(res_phone.json()["authenticated"])
        self.assertEqual(res_phone.json()["auth_method"], "PHONE")

    def test_invalid_input_bad_phone_rejected(self):
        """Invalid Case: Creating a user with an invalid phone format returns HTTP 400."""
        res = self.client.post(
            "/api/v2/identity/users/",
            {
                "email": "bad.phone@uttaracare.bd",
                "phone_number": "0123456789",  # Invalid 012 operator
                "password": "SecurePassword2026!",
                "full_name": "Invalid User",
                "role": "CASHIER",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("phone_number", res.json())

    def test_unauthorized_cashier_role_forbidden(self):
        """Unauthorized Case: Cashier cannot create other users."""
        res = self.client.post(
            "/api/v2/identity/users/",
            {
                "email": "hacker@uttaracare.bd",
                "phone_number": "01712345678",
                "password": "SomePassword123!",
                "full_name": "Unauthorized Attempt",
                "role": "ADMIN",
            },
            content_type="application/json",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="CASHIER",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Cashiers are not authorized", res.json().get("detail", ""))

    def test_cross_tenant_access_forbidden(self):
        """Cross-tenant Case: Tenant B cannot access or modify Tenant A's user."""
        res = self.client.get(
            f"/api/v2/identity/users/{self.user_a.user_id}/",
            HTTP_X_TENANT_ID=self.tenant_b,
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Cross-tenant access forbidden", res.json().get("detail", ""))

    def test_safe_deletion_via_api(self):
        """Verifies DELETE endpoint performs safe soft-deletion/archival."""
        res = self.client.delete(
            f"/api/v2/identity/users/{self.user_a.user_id}/",
            HTTP_X_TENANT_ID=self.tenant_a,
            HTTP_X_USER_ROLE="PHARMACY_OWNER",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["is_archived"])

        # Check DB record is still preserved
        self.user_a.refresh_from_db()
        self.assertTrue(self.user_a.is_archived)
        self.assertFalse(self.user_a.is_active)
        self.assertEqual(self.user_a.activation_status, ActivationStatus.DEACTIVATED)


if __name__ == "__main__":
    unittest.main()
