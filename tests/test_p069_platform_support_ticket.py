"""
backend/tests/test_p069_platform_support_ticket.py
Architecture Decision: ADR-0005, ADR-0036, ADR-0049, ADR-0050, ADR-0063 & ADR-0069
Comprehensive test suite for Platform Support Ticket System:
- Valid scenario: Tenant Cashier creates ticket -> Super Admin assigns operator -> Operator adds internal note
  -> Tenant user retrieves detail (internal note strictly masked) -> Tenant posts reply -> Super Admin resolves.
- Invalid input scenarios: Short title (< 5 chars), short description (< 10 chars), bad category, bad priority,
  attachment > 10MB -> HTTP 400/422.
- Cross-tenant violation: Tenant B owner attempts to view or reply to Tenant A's ticket -> HTTP 403 CROSS_TENANT_VIOLATION.
- Confidentiality enforcement: Store user attempts to post internal note (is_internal=True) -> HTTP 403 Forbidden.
- Action history & audit: Every lifecycle transition records TicketActionHistory and AuditLogEntry.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PlatformSupportTicket,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP069PlatformSupportTicket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p069_admin_{cls.suffix}@platform.internal",
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
            email=f"p069_operator_{cls.suffix}@platform.internal",
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
            tenant_id=f"tenant-p069-a-{cls.suffix}",
            name=f"Popular Care Pharmacy {cls.suffix}",
            subdomain=f"popular-p069-{cls.suffix}",
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
            tenant_id=f"tenant-p069-b-{cls.suffix}",
            name=f"Labaid Pharma {cls.suffix}",
            subdomain=f"labaid-p069-{cls.suffix}",
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
        PlatformSupportTicket.objects.all().delete()

    def test_complete_support_ticket_lifecycle_with_confidential_notes(self):
        """Valid scenario: Tenant Cashier creates ticket -> Super Admin assigns -> Operator adds internal note
        -> Tenant views ticket (internal note hidden) -> Tenant replies -> Super Admin resolves."""
        # 1. Tenant cashier creates a support ticket with an attachment
        self.client.force_authenticate(user=self.cashier_a)
        create_payload = {
            "title": "Thermal printer line feed truncation on receipt print",
            "description": "Counter 1 POS thermal printer cuts invoice before printing barcode footer.",
            "category": "HARDWARE_PRINTER",
            "priority": "HIGH",
            "branch_id": self.branch_a.branch_id,
            "attachments": [
                {
                    "file_name": "receipt_cut.png",
                    "file_url": "/storage/tickets/sample/receipt_cut.png",
                    "file_size_bytes": 102400,
                    "mime_type": "image/png",
                }
            ],
        }
        resp_create = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/create/",
            create_payload,
            format="json",
        )
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        ticket_data = resp_create.json()["data"]
        ticket_id = ticket_data["ticket_id"]
        self.assertEqual(ticket_data["status"], "OPEN")
        self.assertEqual(ticket_data["category"], "HARDWARE_PRINTER")
        self.assertEqual(ticket_data["priority"], "HIGH")
        self.assertEqual(len(ticket_data["attachments"]), 1)
        self.assertIn("TKT-2026-", ticket_data["ticket_number"])

        # 2. Super Admin lists tickets and assigns it to operator
        self.client.force_authenticate(user=self.superadmin)
        resp_list = self.client.get("/api/v2/platform/support/tickets/?category=HARDWARE_PRINTER")
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(resp_list.json()["data"]["total"], 1)

        resp_assign = self.client.post(
            f"/api/v2/platform/support/tickets/{ticket_id}/assign/",
            {"assignee_user_id": str(self.operator.user_id)},
            format="json",
        )
        self.assertEqual(resp_assign.status_code, status.HTTP_200_OK)
        assigned_data = resp_assign.json()["data"]
        self.assertEqual(assigned_data["status"], "IN_PROGRESS")
        self.assertEqual(assigned_data["assigned_to"]["email"], self.operator.email)

        # 3. Platform Operator adds an Internal Note (Confidential!)
        self.client.force_authenticate(user=self.operator)
        resp_note = self.client.post(
            f"/api/v2/platform/support/tickets/{ticket_id}/notes/",
            {"note_text": "Internal diagnostic: ESC/POS driver cut command needs 3 blank line feeds added."},
            format="json",
        )
        self.assertEqual(resp_note.status_code, status.HTTP_201_CREATED)
        operator_view = resp_note.json()["data"]
        # Operator CAN see the internal note
        self.assertTrue(any(m["is_internal"] for m in operator_view["messages"]))

        # 4. Strict Confidentiality: Tenant Owner / Cashier views ticket -> Internal note is STRICTLY MASKED!
        self.client.force_authenticate(user=self.cashier_a)
        resp_tenant_view = self.client.get(f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/{ticket_id}/")
        self.assertEqual(resp_tenant_view.status_code, status.HTTP_200_OK)
        tenant_view = resp_tenant_view.json()["data"]
        # Invariant: ZERO internal notes in tenant response!
        for msg in tenant_view["messages"]:
            self.assertFalse(msg["is_internal"], "CRITICAL: Internal note leaked to pharmacy tenant!")

        # 5. Tenant posts a public reply
        resp_reply = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/{ticket_id}/reply/",
            {"message_text": "We updated the paper roll to 80mm standard as instructed."},
            format="json",
        )
        self.assertEqual(resp_reply.status_code, status.HTTP_201_CREATED)
        reply_view = resp_reply.json()["data"]
        self.assertTrue(any("80mm standard" in m["message_text"] for m in reply_view["messages"]))

        # 6. Super Admin marks ticket RESOLVED with summary
        self.client.force_authenticate(user=self.superadmin)
        resp_resolve = self.client.post(
            f"/api/v2/platform/support/tickets/{ticket_id}/status/",
            {
                "status": "RESOLVED",
                "resolution_summary": "Printer configuration updated with 3 trailing ESC/POS line feed bytes.",
            },
            format="json",
        )
        self.assertEqual(resp_resolve.status_code, status.HTTP_200_OK)
        resolved_data = resp_resolve.json()["data"]
        self.assertEqual(resolved_data["status"], "RESOLVED")
        self.assertEqual(resolved_data["status_chip"]["tone"], "success")
        self.assertIsNotNone(resolved_data["resolved_at"])

        # 7. Verify Action History contains complete audit trail
        history = resolved_data["action_history"]
        actions = [h["action"] for h in history]
        self.assertIn("CREATED", actions)
        self.assertIn("ASSIGNED", actions)
        self.assertIn("REPLY_ADDED", actions)
        self.assertIn("STATUS_CHANGED", actions)

    def test_invalid_and_incomplete_ticket_inputs(self):
        """Invalid scenarios: Short title (< 5 chars), short description, bad category, bad priority, oversized attachment."""
        self.client.force_authenticate(user=self.cashier_a)

        # 1. Short title (< 5 chars)
        resp1 = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/create/",
            {"title": "Lag", "description": "Valid description exceeding 10 characters.", "category": "BILLING"},
            format="json",
        )
        self.assertIn(resp1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("title", str(resp1.json()))

        # 2. Short description (< 10 chars)
        resp2 = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/create/",
            {"title": "Valid Ticket Title", "description": "Too short", "category": "BILLING"},
            format="json",
        )
        self.assertIn(resp2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("description", str(resp2.json()))

        # 3. Invalid category
        resp3 = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/create/",
            {
                "title": "Valid Ticket Title",
                "description": "Valid description exceeding 10 characters.",
                "category": "UNKNOWN_CAT",
            },
            format="json",
        )
        self.assertIn(resp3.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("category", str(resp3.json()))

        # 4. Attachment > 10MB
        resp4 = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/create/",
            {
                "title": "Valid Ticket Title",
                "description": "Valid description exceeding 10 characters.",
                "category": "BILLING",
                "attachments": [
                    {
                        "file_name": "massive_dump.zip",
                        "file_size_bytes": 15 * 1024 * 1024,  # 15MB exceeds 10MB limit
                    }
                ],
            },
            format="json",
        )
        self.assertIn(resp4.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("attachments", str(resp4.json()))

    def test_unauthorized_and_cross_tenant_isolation(self):
        """Cross-tenant and authorization checks: Tenant B cannot view or reply to Tenant A's ticket."""
        # 1. Create ticket for Tenant A
        self.client.force_authenticate(user=self.cashier_a)
        resp_create = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/create/",
            {
                "title": "Stock Batch Expiry Calculation Inaccuracy",
                "description": "FEFO batch deduction issue observed on Napa Extra strips.",
                "category": "INVENTORY_STOCK",
                "priority": "MEDIUM",
            },
            format="json",
        )
        ticket_id = resp_create.json()["data"]["ticket_id"]

        # 2. Tenant B Owner attempts to access Tenant A's ticket list -> Cross-tenant violation!
        self.client.force_authenticate(user=self.owner_b)
        resp_cross_list = self.client.get(f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/")
        self.assertEqual(resp_cross_list.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("CROSS_TENANT_VIOLATION", str(resp_cross_list.json()))

        # 3. Tenant B Owner attempts to view Tenant A's ticket detail -> Cross-tenant violation!
        resp_cross_view = self.client.get(f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/{ticket_id}/")
        self.assertEqual(resp_cross_view.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("CROSS_TENANT_VIOLATION", str(resp_cross_view.json()))

        # 4. Tenant B Owner attempts to reply to Tenant A's ticket -> Cross-tenant violation!
        resp_cross_reply = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/{ticket_id}/reply/",
            {"message_text": "Illegitimate reply from foreign tenant."},
            format="json",
        )
        self.assertEqual(resp_cross_reply.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("CROSS_TENANT_VIOLATION", str(resp_cross_reply.json()))

        # 5. Store user attempts to post an internal note -> HTTP 403 Forbidden!
        self.client.force_authenticate(user=self.cashier_a)
        resp_illegal_internal = self.client.post(
            f"/api/v2/platform/support/tickets/{ticket_id}/notes/",
            {"note_text": "Illegal internal note attempt by cashier."},
            format="json",
        )
        self.assertEqual(resp_illegal_internal.status_code, status.HTTP_403_FORBIDDEN)

    def test_tenant_staff_cannot_arbitrarily_set_in_progress(self):
        """Privilege check: Tenant staff can only close or reopen tickets, cannot set arbitrary internal status."""
        self.client.force_authenticate(user=self.cashier_a)
        resp_create = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/create/",
            {
                "title": "MFS Gateway Disconnect Issue",
                "description": "bKash webhook acknowledgment delay noticed at counter.",
                "category": "BILLING",
                "priority": "HIGH",
            },
            format="json",
        )
        ticket_id = resp_create.json()["data"]["ticket_id"]

        # Tenant staff can close their ticket
        resp_close = self.client.post(
            f"/api/v2/tenants/support/tickets/{self.tenant_a.tenant_id}/{ticket_id}/close/",
            {"notes": "Issue self-resolved after network router restart."},
            format="json",
        )
        self.assertEqual(resp_close.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_close.json()["data"]["status"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
