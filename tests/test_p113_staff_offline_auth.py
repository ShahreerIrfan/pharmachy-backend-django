"""
Integration tests for P113: Staff offline authorization, secure cached credentials,
signed role snapshots, expiry, and risk-limited permissions (ADR-0113).
"""

import json
import uuid
from datetime import timedelta
from pathlib import Path

from django.test import Client, TestCase
from django.utils import timezone
from jsonschema import validate
from rest_framework import status

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    StaffLifecycleStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.staff_offline_auth_service import StaffOfflineAuthorizationService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "staff_offline_authorization.schema.json"


class StaffOfflineAuthIntegrationTests(TestCase):
    """End-to-end integration tests for P113 Staff Offline Authorization."""

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
            tenant_id=f"tn-p113-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-a-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status="ACTIVE",
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Dispense Dhanmondi",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Staff Member A (Active Cashier)
        self.staff_cashier = StaffMember.objects.create(
            staff_id=f"stf-c-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            username=f"cashier_{self.suffix}",
            full_name="Kader Cashier",
            role=StaffRole.CASHIER,
            mobile_number="01711000111",
            lifecycle_status=StaffLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 3. Staff Member A2 (Terminated Pharmacist)
        self.staff_terminated = StaffMember.objects.create(
            staff_id=f"stf-term-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            username=f"terminated_{self.suffix}",
            full_name="Bad Staff",
            role=StaffRole.PHARMACIST,
            mobile_number="01711000222",
            lifecycle_status=StaffLifecycleStatus.TERMINATED,
            is_active=False,
        )

        # 4. User Account for Owner A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@lazz.local",
            phone_number="01711000333",
            password="StrongPassword123!",
            full_name="Pharmacy Owner A",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        # 5. Tenant B (Competitor)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p113-b-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-b-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status="ACTIVE",
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@popular.local",
            phone_number="01711000444",
            password="StrongPassword123!",
            full_name="Pharmacy Owner B",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

    def _login(self, user):
        resp = self.client.post(
            "/api/v2/auth/login/",
            data={"identifier": user.email, "password": "StrongPassword123!"},
            content_type="application/json",
            HTTP_X_DEVICE_ID="test-device",
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        return resp.json()["access"]

    def test_valid_offline_authorization_issuance_and_schema(self):
        """Valid issuance returns cryptographically signed lease conforming to JSON Schema."""
        token = self._login(self.owner_a)
        resp = self.client.post(
            "/api/v2/staff/offline-authorizations/",
            data={
                "tenant_id": self.tenant_a.tenant_id,
                "staff_id": self.staff_cashier.staff_id,
                "device_id": "pos-counter-01",
                "pin_or_passcode": "4321",
                "branch_id": self.branch_a.branch_id,
                "duration_hours": 48,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()

        self.assertEqual(data["staff_id"], self.staff_cashier.staff_id)
        self.assertEqual(data["role"], "CASHIER")
        self.assertIn("pos.sale.create", data["permitted_actions"])
        self.assertIn("pos.discount.override", data["denied_actions"])
        self.assertEqual(data["risk_limits"]["max_offline_discount_percent"], 5.0)
        self.assertTrue(data["signature"])

        # Never stores plaintext PIN
        self.assertNotIn("4321", json.dumps(data))
        self.assertNotIn("pin_or_passcode", data)

        # Schema validation
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

    def test_invalid_input_rejected(self):
        """Incomplete input or short PIN is rejected."""
        token = self._login(self.owner_a)

        # Missing PIN
        resp = self.client.post(
            "/api/v2/staff/offline-authorizations/",
            data={"tenant_id": self.tenant_a.tenant_id, "staff_id": self.staff_cashier.staff_id, "device_id": "pos-01"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error_code"], "ERR_INVALID_OFFLINE_AUTH_INPUT")

        # Insufficient PIN length (< 4)
        resp = self.client.post(
            "/api/v2/staff/offline-authorizations/",
            data={
                "tenant_id": self.tenant_a.tenant_id,
                "staff_id": self.staff_cashier.staff_id,
                "device_id": "pos-01",
                "pin_or_passcode": "12",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["error_code"], "ERR_INSUFFICIENT_PIN_LENGTH")

    def test_cross_tenant_issuance_forbidden(self):
        """Owner of Tenant B cannot issue offline authorization for staff in Tenant A."""
        token_b = self._login(self.owner_b)
        resp = self.client.post(
            "/api/v2/staff/offline-authorizations/",
            data={
                "tenant_id": self.tenant_a.tenant_id,
                "staff_id": self.staff_cashier.staff_id,
                "device_id": "pos-counter-01",
                "pin_or_passcode": "4321",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token_b}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_terminated_or_inactive_staff_role_denied(self):
        """Terminated staff member is rejected with ERR_STAFF_ROLE_DENIED."""
        token = self._login(self.owner_a)
        resp = self.client.post(
            "/api/v2/staff/offline-authorizations/",
            data={
                "tenant_id": self.tenant_a.tenant_a_id if hasattr(self.tenant_a, "tenant_a_id") else self.tenant_a.tenant_id,
                "staff_id": self.staff_terminated.staff_id,
                "device_id": "pos-counter-01",
                "pin_or_passcode": "4321",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_STAFF_ROLE_DENIED")

    def test_suspended_tenant_subscription_blocks_issuance(self):
        """Suspended tenant subscription blocks offline authorization issuance."""
        token = self._login(self.owner_a)
        self.tenant_a.status = "SUSPENDED"
        self.tenant_a.save()

        resp = self.client.post(
            "/api/v2/staff/offline-authorizations/",
            data={
                "tenant_id": self.tenant_a.tenant_id,
                "staff_id": self.staff_cashier.staff_id,
                "device_id": "pos-counter-01",
                "pin_or_passcode": "4321",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    def test_offline_verification_flow(self):
        """Simulates offline desktop verification with valid PIN, device matching, and expiry."""
        payload = StaffOfflineAuthorizationService.issue_offline_authorization(
            actor_user=self.owner_a,
            tenant_id=self.tenant_a.tenant_id,
            staff_id=self.staff_cashier.staff_id,
            device_id="pos-counter-01",
            pin_or_passcode="4321",
        )

        # 1. Valid offline check
        res = StaffOfflineAuthorizationService.verify_offline_credentials(
            payload=payload,
            pin_or_passcode="4321",
            current_device_id="pos-counter-01",
        )
        self.assertTrue(res["is_authorized"])
        self.assertEqual(res["auth_mode"], "OFFLINE_SIGNED_LEASE")

        # 2. Wrong PIN fails
        with self.assertRaises(Exception) as ctx:
            StaffOfflineAuthorizationService.verify_offline_credentials(
                payload=payload,
                pin_or_passcode="0000",
                current_device_id="pos-counter-01",
            )
        self.assertEqual(ctx.exception.detail["error_code"], "ERR_INVALID_CREDENTIALS")

        # 3. Device mismatch fails
        with self.assertRaises(Exception) as ctx:
            StaffOfflineAuthorizationService.verify_offline_credentials(
                payload=payload,
                pin_or_passcode="4321",
                current_device_id="unregistered-foreign-device",
            )
        self.assertEqual(ctx.exception.detail["error_code"], "ERR_OFFLINE_DEVICE_MISMATCH")

        # 4. Expired lease fails
        future_time = timezone.now() + timedelta(days=10)
        with self.assertRaises(Exception) as ctx:
            StaffOfflineAuthorizationService.verify_offline_credentials(
                payload=payload,
                pin_or_passcode="4321",
                current_device_id="pos-counter-01",
                current_time=future_time,
            )
        self.assertEqual(ctx.exception.detail["error_code"], "ERR_OFFLINE_AUTH_EXPIRED")
