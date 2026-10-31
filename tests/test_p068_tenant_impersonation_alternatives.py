"""
backend/tests/test_p068_tenant_impersonation_alternatives.py
Architecture Decision: ADR-0005, ADR-0036, ADR-0049, ADR-0050, ADR-0063 & ADR-0068
Comprehensive test suite for Secure Delegated Support Access and Tenant Impersonation Alternatives:
- Valid scenario 1: Operator requests support access -> Tenant Owner approves -> Delegated token generated -> Notice banner active.
- Valid scenario 2: Super Admin triggers Emergency Break-Glass access with justification -> Immediate activation -> Red/Danger alert banner.
- Invalid input scenarios: Reason < 10 chars, invalid scope, duration out of bounds (15-120), missing break-glass justification -> HTTP 400.
- Unauthorized scenarios: Store cashier attempts platform support request -> HTTP 403 PLATFORM_ACCESS_DENIED.
- Cross-tenant violation: Tenant B owner attempts to approve or revoke Tenant A's support grant -> HTTP 403 CROSS_TENANT_VIOLATION.
- Dual-person rule violation: Platform operator attempts to approve own standard request -> HTTP 403.
- Anti-pattern enforcement: Direct impersonation ('Login As') call -> Strictly rejected with HTTP 403 IMPERSONATION_FORBIDDEN.
- Emergency Kill Switch: Pharmacy owner revokes active grant -> Status REVOKED -> Token immediately invalidated.
- Token expiration: Time-bounded grant retires -> Status EXPIRED.
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

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import Branch, SupportAccessGrant, SupportAccessScope, SupportAccessStatus, Tenant
from apps.core.support_access_service import SupportAccessService
from apps.identity.models import PharmacyUser, UserRole


class TestP068TenantImpersonationAlternatives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p068_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "is_staff": True,
                "is_superuser": True,
                "phone_number": f"+88019{phone_suffix}",
            },
        )
        cls.superadmin.set_password("AdminSecurePass2026!")
        cls.superadmin.save()

        # 2. Platform Support Operator
        phone_op = random.randint(10000000, 99999999)
        cls.operator, _ = PharmacyUser.objects.get_or_create(
            email=f"p068_operator_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Support Operator {cls.suffix}",
                "role": UserRole.PLATFORM_OPERATOR,
                "phone_number": f"+88019{phone_op}",
            },
        )
        cls.operator.set_password("OperatorPass2026!")
        cls.operator.save()

        # 3. Tenant Alpha
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"tenant-p068-a-{cls.suffix}",
            name=f"Square Pharma Care {cls.suffix}",
            subdomain=f"square-p068-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
            billing_email=f"billing_a_{cls.suffix}@pharmacy.bd",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"{cls.tenant_a.tenant_id}-br-1",
            tenant=cls.tenant_a,
            name="Main Branch Dhanmondi",
            license_number=f"DGDA-DHK-{random.randint(1000, 9999)}",
            is_active=True,
        )
        phone_owner_a = random.randint(10000000, 99999999)
        cls.owner_a = PharmacyUser.objects.create(
            email=f"owner_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Dr. Owner A {cls.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            phone_number=f"+88018{phone_owner_a}",
        )
        cls.owner_a.set_password("OwnerPassword2026!")
        cls.owner_a.save()
        cls.tenant_a.owner_user_id = str(cls.owner_a.user_id)
        cls.tenant_a.save()

        phone_cashier_a = random.randint(10000000, 99999999)
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_a_{cls.suffix}@pharmacy.bd",
            full_name=f"Cashier Alpha {cls.suffix}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            phone_number=f"+88017{phone_cashier_a}",
        )

        # 4. Tenant Beta (for cross-tenant tests)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"tenant-p068-b-{cls.suffix}",
            name=f"Popular Pharma {cls.suffix}",
            subdomain=f"popular-p068-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
            billing_email=f"billing_b_{cls.suffix}@pharmacy.bd",
        )
        phone_owner_b = random.randint(10000000, 99999999)
        cls.owner_b = PharmacyUser.objects.create(
            email=f"owner_b_{cls.suffix}@pharmacy.bd",
            full_name=f"Dr. Owner B {cls.suffix}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_b.tenant_id,
            phone_number=f"+88018{phone_owner_b}",
        )

    def setUp(self):
        self.client = APIClient()
        SupportAccessGrant.objects.all().delete()

    def test_standard_support_request_approval_and_notice_lifecycle(self):
        """Valid scenario 1: Operator requests support -> Tenant Owner approves -> Notice banner active -> Token valid."""
        # 1. Operator requests support access
        self.client.force_authenticate(user=self.operator)
        req_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "scope": "DIAGNOSTIC_READ",
            "reason": "Investigate thermal printer receipt truncation reported in ticket #8812.",
            "ticket_reference": "TICKET-8812",
            "duration_minutes": 30,
        }
        resp_req = self.client.post("/api/v2/platform/support-access/request/", req_payload, format="json")
        self.assertEqual(resp_req.status_code, status.HTTP_201_CREATED)
        grant_data = resp_req.json()["data"]
        grant_id = grant_data["grant_id"]
        self.assertEqual(grant_data["status"], "PENDING")
        self.assertFalse(grant_data["is_break_glass"])
        self.assertIsNone(grant_data["access_token"])

        # 2. Before approval: Notice banner shows false
        self.client.force_authenticate(user=self.owner_a)
        resp_notice_before = self.client.get(f"/api/v2/tenants/support-access/{self.tenant_a.tenant_id}/active/")
        self.assertEqual(resp_notice_before.status_code, status.HTTP_200_OK)
        self.assertFalse(resp_notice_before.json()["data"]["show_banner"])

        # 3. Pharmacy Owner approves the support access request
        resp_approve = self.client.post(f"/api/v2/tenants/support-access/{self.tenant_a.tenant_id}/{grant_id}/approve/")
        self.assertEqual(resp_approve.status_code, status.HTTP_200_OK)
        approved_data = resp_approve.json()["data"]
        self.assertEqual(approved_data["status"], "APPROVED")
        token = approved_data["access_token"]
        self.assertIsNotNone(token)
        self.assertTrue(token.startswith("sat_"))

        # 4. After approval: Notice banner shows true with tone='warning'
        resp_notice_after = self.client.get(f"/api/v2/tenants/support-access/{self.tenant_a.tenant_id}/active/")
        self.assertEqual(resp_notice_after.status_code, status.HTTP_200_OK)
        notice = resp_notice_after.json()["data"]
        self.assertTrue(notice["show_banner"])
        self.assertEqual(notice["tone"], "warning")
        self.assertTrue(notice["can_revoke"])
        self.assertIn(self.operator.email, notice["banner_text_en"])

        # 5. Token validation passes
        is_valid, grant_obj, err = SupportAccessService.validate_support_token(token)
        self.assertTrue(is_valid)
        self.assertIsNotNone(grant_obj)
        self.assertIsNone(err)

        # 6. Audit logs recorded
        audit_approve = AuditLogEntry.objects.filter(
            action="SUPPORT_ACCESS_APPROVED",
            resource_id=grant_id,
        ).first()
        self.assertIsNotNone(audit_approve)

    def test_emergency_break_glass_access_lifecycle(self):
        """Valid scenario 2: Super Admin triggers Emergency Break-Glass access with justification -> Immediate red banner."""
        self.client.force_authenticate(user=self.superadmin)
        bg_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "scope": "BREAK_GLASS_REPAIR",
            "reason": "Emergency DB sync unblocking during active store operating hours.",
            "ticket_reference": "CRITICAL-INCIDENT-991",
            "duration_minutes": 45,
            "is_break_glass": True,
            "break_glass_justification": "Pharmacy owner unreachable via phone; POS terminal outbox blocked at counter 1.",
        }
        resp_bg = self.client.post("/api/v2/platform/support-access/request/", bg_payload, format="json")
        self.assertEqual(resp_bg.status_code, status.HTTP_200_OK)
        bg_data = resp_bg.json()["data"]
        self.assertEqual(bg_data["status"], "APPROVED")
        self.assertTrue(bg_data["is_break_glass"])
        token = bg_data["access_token"]
        self.assertTrue(token.startswith("sat_bg_"))

        # Notice banner displays danger tone
        self.client.force_authenticate(user=self.owner_a)
        resp_notice = self.client.get(f"/api/v2/tenants/support-access/{self.tenant_a.tenant_id}/active/")
        self.assertEqual(resp_notice.status_code, status.HTTP_200_OK)
        notice = resp_notice.json()["data"]
        self.assertTrue(notice["show_banner"])
        self.assertEqual(notice["tone"], "danger")
        self.assertIn("EMERGENCY BREAK-GLASS", notice["banner_text_en"])

        # Audit log verified
        audit_bg = AuditLogEntry.objects.filter(
            action="SUPPORT_ACCESS_BREAK_GLASS_TRIGGERED",
            resource_id=bg_data["grant_id"],
        ).first()
        self.assertIsNotNone(audit_bg)

    def test_invalid_input_validations_rejected(self):
        """Invalid scenarios: Short reason, bad scope, out-of-bounds duration, missing break-glass justification."""
        self.client.force_authenticate(user=self.operator)

        # 1. Short reason (< 10 chars)
        resp1 = self.client.post(
            "/api/v2/platform/support-access/request/",
            {"tenant_id": self.tenant_a.tenant_id, "scope": "DIAGNOSTIC_READ", "reason": "too short"},
            format="json",
        )
        self.assertIn(resp1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("reason", str(resp1.json()))

        # 2. Invalid scope
        resp2 = self.client.post(
            "/api/v2/platform/support-access/request/",
            {"tenant_id": self.tenant_a.tenant_id, "scope": "ROOT_ADMIN", "reason": "Valid reason exceeding ten chars"},
            format="json",
        )
        self.assertIn(resp2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("scope", str(resp2.json()))

        # 3. Excessive duration (> 120 min)
        resp3 = self.client.post(
            "/api/v2/platform/support-access/request/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "scope": "DIAGNOSTIC_READ",
                "reason": "Valid reason exceeding ten chars",
                "duration_minutes": 240,
            },
            format="json",
        )
        self.assertIn(resp3.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("duration_minutes", str(resp3.json()))

        # 4. Break-glass missing justification
        self.client.force_authenticate(user=self.superadmin)
        resp4 = self.client.post(
            "/api/v2/platform/support-access/request/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "scope": "BREAK_GLASS_REPAIR",
                "reason": "Valid reason exceeding ten chars",
                "is_break_glass": True,
                # Missing break_glass_justification
            },
            format="json",
        )
        self.assertIn(resp4.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("break_glass_justification", str(resp4.json()))

    def test_unauthorized_and_cross_tenant_violations_rejected(self):
        """Unauthorized and cross-tenant checks: Cashier cannot request, foreign owner cannot approve/reject."""
        # 1. Cashier attempts to request platform support access
        self.client.force_authenticate(user=self.cashier_a)
        resp_cashier = self.client.post(
            "/api/v2/platform/support-access/request/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "scope": "DIAGNOSTIC_READ",
                "reason": "Valid reason exceeding ten chars",
            },
            format="json",
        )
        self.assertEqual(resp_cashier.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Operator requests a legitimate grant for Tenant A
        self.client.force_authenticate(user=self.operator)
        resp_req = self.client.post(
            "/api/v2/platform/support-access/request/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "scope": "BILLING_READ",
                "reason": "Investigate subscription billing dispute for current month.",
            },
            format="json",
        )
        grant_id = resp_req.json()["data"]["grant_id"]

        # 3. Tenant B owner attempts to approve Tenant A's grant -> Cross-tenant violation!
        self.client.force_authenticate(user=self.owner_b)
        resp_cross_approve = self.client.post(
            f"/api/v2/tenants/support-access/{self.tenant_a.tenant_id}/{grant_id}/approve/"
        )
        self.assertEqual(resp_cross_approve.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("CROSS_TENANT_VIOLATION", str(resp_cross_approve.json()))

        # 4. Operator attempts to approve own request -> Dual-person violation!
        self.client.force_authenticate(user=self.operator)
        resp_self_approve = self.client.post(
            f"/api/v2/tenants/support-access/{self.tenant_a.tenant_id}/{grant_id}/approve/"
        )
        self.assertEqual(resp_self_approve.status_code, status.HTTP_403_FORBIDDEN)

    def test_direct_impersonation_strictly_prohibited(self):
        """Anti-pattern enforcement: Direct impersonation ('Login As') is strictly blocked with IMPERSONATION_FORBIDDEN."""
        self.client.force_authenticate(user=self.superadmin)
        resp_impersonate = self.client.post(
            "/api/v2/platform/support-access/impersonate/",
            {"target_user_id": str(self.owner_a.user_id), "tenant_id": self.tenant_a.tenant_id},
            format="json",
        )
        self.assertEqual(resp_impersonate.status_code, status.HTTP_403_FORBIDDEN)
        body = resp_impersonate.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error_code"], "IMPERSONATION_FORBIDDEN")

        # Verify tamper-evident audit log
        audit = AuditLogEntry.objects.filter(
            action="UNSAFE_IMPERSONATION_ATTEMPT_BLOCKED",
            resource_id=str(self.owner_a.user_id),
        ).first()
        self.assertIsNotNone(audit)

    def test_emergency_kill_switch_revocation(self):
        """Emergency Kill Switch: Pharmacy owner revokes grant -> Immediately invalidated."""
        # 1. Setup active grant
        grant = SupportAccessGrant.objects.create(
            grant_id=f"sup-kill-{self.suffix}",
            tenant=self.tenant_a,
            requested_by_operator=self.operator,
            approved_by_user=self.owner_a,
            scope=SupportAccessScope.DIAGNOSTIC_READ,
            status=SupportAccessStatus.APPROVED,
            reason="Diagnostic investigation for hardware latency.",
            duration_minutes=30,
            approved_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=30),
            access_token=f"sat_kill_{uuid.uuid4().hex}",
        )

        # 2. Pharmacy owner revokes the grant
        self.client.force_authenticate(user=self.owner_a)
        resp_revoke = self.client.post(
            f"/api/v2/tenants/support-access/{self.tenant_a.tenant_id}/{grant.grant_id}/revoke/",
            {"reason": "Troubleshooting completed by on-site staff."},
            format="json",
        )
        self.assertEqual(resp_revoke.status_code, status.HTTP_200_OK)
        data = resp_revoke.json()["data"]
        self.assertEqual(data["status"], "REVOKED")

        # 3. Token validation now fails
        is_valid, _, err = SupportAccessService.validate_support_token(grant.access_token)
        self.assertFalse(is_valid)
        self.assertEqual(err, "SUPPORT_ACCESS_REVOKED")

    def test_token_expiration_enforcement(self):
        """Expiration check: Past-due token automatically expires and fails authorization."""
        past_time = timezone.now() - timedelta(minutes=10)
        grant = SupportAccessGrant.objects.create(
            grant_id=f"sup-exp-{self.suffix}",
            tenant=self.tenant_a,
            requested_by_operator=self.operator,
            approved_by_user=self.owner_a,
            scope=SupportAccessScope.DIAGNOSTIC_READ,
            status=SupportAccessStatus.APPROVED,
            reason="Diagnostic investigation for printer buffer.",
            duration_minutes=15,
            approved_at=past_time - timedelta(minutes=15),
            expires_at=past_time,
            access_token=f"sat_exp_{uuid.uuid4().hex}",
        )

        is_valid, grant_obj, err = SupportAccessService.validate_support_token(grant.access_token)
        self.assertFalse(is_valid)
        self.assertEqual(err, "SUPPORT_ACCESS_EXPIRED")
        self.assertEqual(grant_obj.status, SupportAccessStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()
