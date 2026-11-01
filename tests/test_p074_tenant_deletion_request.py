"""
backend/tests/test_p074_tenant_deletion_request.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0074
Comprehensive test suite for Tenant Deletion Request Assessment & Statutory Retention Holds:
1. Deletion Submission: Pharmacy Owner can submit a deletion request with operational justification.
2. Role Protection: Cashiers and store staff receive 403 Forbidden; cannot submit or cancel deletion requests.
3. Statutory Retention Hold Detection: Automatically identifies active DGDA prescriptions and NBR tax records.
4. Hard Deletion Guardrail: Irreversible purge is strictly blocked (400/422) if statutory retention holds exist.
5. Clean Tenant Purge Execution: Eligible tenant without active holds can be permanently purged after multi-step confirmation.
6. Rejection Workflow: Super Admin can reject deletion requests with mandatory justification.
7. Cancellation Workflow: Pharmacy Owner can withdraw pending deletion requests.
8. Cross-Tenant Protection: Tenant A cannot inspect or manipulate Tenant B's deletion request.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from decimal import Decimal
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
    Branch,
    PrescriptionRecord,
    SaleInvoice,
    Tenant,
    TenantDeletionRequest,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP074TenantDeletionRequest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p074_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{phone_suffix}",
                "is_active": True,
            },
        )

        # 2. Tenant A (Dhaka Pharmacy - Active Clinical & Financial Records)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-dhaka-{cls.suffix}",
            name=f"Dhaka Pharmacy With Active Holds {cls.suffix}",
            subdomain=f"dhaka-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"b-dhaka-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Dhanmondi Branch",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p074_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"p074_cashier_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Cashier {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )

        # Seed active prescription within 730 days and posted invoice
        cls.rx_a = PrescriptionRecord.objects.create(
            prescription_id=f"RX-DHK-{cls.suffix}-01",
            tenant=cls.tenant_a,
            branch=cls.branch_a,
            patient_token="PT-DHK-99",
            doctor_name="Dr. Kamal Hossain",
            doctor_bmdc_no="BMDC-12345",
            diagnosis="Hypertension",
            notes="Active patient under treatment",
            prescription_date=timezone.now().date(),
            status="VERIFIED",
        )
        cls.sale_a = SaleInvoice.objects.create(
            invoice_id=f"inv-{cls.suffix}-01",
            invoice_number=f"INV-DHK-{cls.suffix}-01",
            tenant=cls.tenant_a,
            branch=cls.branch_a,
            customer_name="Mr. Patient Rahman",
            customer_phone="+8801712345678",
            total_amount=Decimal("350.00"),
            net_amount=Decimal("350.00"),
            status="POSTED",
        )

        # 3. Tenant B (Clean Decommissioned Pharmacy - Zero Clinical/Financial Records)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-ctg-{cls.suffix}",
            name=f"Clean Chittagong Pharmacy {cls.suffix}",
            subdomain=f"ctg-{cls.suffix}",
            plan_tier="BASIC",
            status="ACTIVE",
        )
        cls.branch_b = Branch.objects.create(
            branch_id=f"b-ctg-{cls.suffix}",
            tenant=cls.tenant_b,
            name="Agrabad Branch",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p074_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Chittagong Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_b.tenant_id,
            branch_id=cls.branch_b.branch_id,
            is_active=True,
        )

    def setUp(self):
        TenantDeletionRequest.objects.all().delete()

        self.client_admin = APIClient()
        self.client_admin.force_authenticate(user=self.superadmin)

        self.client_owner_a = APIClient()
        self.client_owner_a.force_authenticate(user=self.owner_a)

        self.client_cashier_a = APIClient()
        self.client_cashier_a.force_authenticate(user=self.cashier_a)

        self.client_owner_b = APIClient()
        self.client_owner_b.force_authenticate(user=self.owner_b)

    def test_01_owner_can_submit_deletion_request_and_list(self):
        payload = {"reason": "Store shutting down permanently due to lease termination."}

        # Submit
        res = self.client_owner_a.post("/api/v2/tenants/deletion-requests/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        data = res.data["data"]
        self.assertTrue(data["request_id"].startswith("tdr-"))
        self.assertEqual(data["tenant_id"], self.tenant_a.tenant_id)
        self.assertIn("Store shutting down", data["reason"])

        # List
        res_list = self.client_owner_a.get("/api/v2/tenants/deletion-requests/")
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        results = res_list.data.get("results", res_list.data) if isinstance(res_list.data, dict) else res_list.data
        ids = [item["request_id"] for item in results]
        self.assertIn(data["request_id"], ids)

    def test_02_cashier_and_staff_roles_blocked(self):
        payload = {"reason": "Unauthorized deletion attempt by store staff"}

        # Cashier POST blocked
        res_cashier_post = self.client_cashier_a.post("/api/v2/tenants/deletion-requests/", payload, format="json")
        self.assertEqual(res_cashier_post.status_code, status.HTTP_403_FORBIDDEN)

        # Cashier GET list blocked
        res_cashier_get = self.client_cashier_a.get("/api/v2/tenants/deletion-requests/")
        self.assertEqual(res_cashier_get.status_code, status.HTTP_403_FORBIDDEN)

        # Store owner blocked from platform admin console
        res_owner_platform = self.client_owner_a.get("/api/v2/platform/deletion-assessments/")
        self.assertEqual(res_owner_platform.status_code, status.HTTP_403_FORBIDDEN)

    def test_03_active_statutory_hold_detection(self):
        # Tenant A has active prescriptions and posted invoices
        res = self.client_owner_a.post(
            "/api/v2/tenants/deletion-requests/",
            {"reason": "Decommission request for store with active records"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.data["data"]

        # Statutory retention hold MUST be detected
        self.assertTrue(data["has_active_holds"])
        self.assertEqual(data["status"], "HOLD_ACTIVE")
        self.assertIn("DGDA National Accreditation Guideline 2024", str(data["active_holds_summary"]))
        self.assertIn("NBR Value Added Tax", str(data["active_holds_summary"]))

    def test_04_hard_purge_blocked_when_holds_active(self):
        res = self.client_owner_a.post(
            "/api/v2/tenants/deletion-requests/",
            {"reason": "Attempting deletion on tenant with active clinical holds"},
            format="json",
        )
        request_id = res.data["data"]["request_id"]

        # SuperAdmin attempts to execute purge on request with HOLD_ACTIVE
        res_purge = self.client_admin.post(
            f"/api/v2/platform/deletion-assessments/{request_id}/purge/",
            {
                "confirmation_code": "DEL-FAKE-CODE",
                "confirmation_text": "CONFIRM_PERMANENT_DELETION",
            },
            format="json",
        )
        # MUST be rejected due to statutory retention hold
        self.assertIn(res_purge.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])
        self.assertIn("statutory retention", str(res_purge.data).lower())
        self.assertIn("STATUTORY_HOLD_PREVENTS_PURGE", str(res_purge.data))

        # Tenant remains ACTIVE / not purged
        self.tenant_a.refresh_from_db()
        self.assertNotEqual(self.tenant_a.status, "PURGED")

    def test_05_clean_tenant_eligible_for_purge_and_execution(self):
        # Tenant B has NO clinical or financial records
        res_req = self.client_owner_b.post(
            "/api/v2/tenants/deletion-requests/",
            {"reason": "Clean test tenant decommissioning with zero records"},
            format="json",
        )
        self.assertEqual(res_req.status_code, status.HTTP_201_CREATED)
        data = res_req.data["data"]

        # Zero holds -> ELIGIBLE_FOR_PURGE
        self.assertFalse(data["has_active_holds"])
        self.assertEqual(data["status"], "ELIGIBLE_FOR_PURGE")
        confirmation_code = data["confirmation_code"]
        self.assertTrue(confirmation_code.startswith("DEL-"))

        # SuperAdmin executes purge with valid confirmation code and phrase
        res_purge = self.client_admin.post(
            f"/api/v2/platform/deletion-assessments/{data['request_id']}/purge/",
            {
                "confirmation_code": confirmation_code,
                "confirmation_text": "CONFIRM_PERMANENT_DELETION",
            },
            format="json",
        )
        self.assertEqual(res_purge.status_code, status.HTTP_200_OK)
        self.assertEqual(res_purge.data["data"]["status"], "PURGED")

        # Tenant status updated to PURGED
        self.tenant_b.refresh_from_db()
        self.assertEqual(self.tenant_b.status, "PURGED")
        self.assertFalse(self.tenant_b.is_active)

    def test_06_rejection_workflow(self):
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/deletion-requests/",
            {"reason": "Request that will be rejected by compliance"},
            format="json",
        )
        request_id = res_req.data["data"]["request_id"]

        # SuperAdmin rejects request
        res_rej = self.client_admin.post(
            f"/api/v2/platform/deletion-assessments/{request_id}/reject/",
            {"reason": "Ongoing narcotics register audit prevents tenant closure."},
            format="json",
        )
        self.assertEqual(res_rej.status_code, status.HTTP_200_OK)
        self.assertEqual(res_rej.data["data"]["status"], "REJECTED")
        self.assertIn("narcotics register audit", res_rej.data["data"]["rejection_reason"])

    def test_07_cancellation_workflow(self):
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/deletion-requests/",
            {"reason": "Request to be cancelled by owner"},
            format="json",
        )
        request_id = res_req.data["data"]["request_id"]

        # Owner withdraws request
        res_cancel = self.client_owner_a.post(
            f"/api/v2/tenants/deletion-requests/{request_id}/cancel/",
            {"notes": "Decided to renew pharmacy lease instead."},
            format="json",
        )
        self.assertEqual(res_cancel.status_code, status.HTTP_200_OK)
        self.assertEqual(res_cancel.data["data"]["status"], "CANCELLED")

    def test_08_cross_tenant_isolation(self):
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/deletion-requests/",
            {"reason": "Tenant A private deletion request"},
            format="json",
        )
        request_id_a = res_req.data["data"]["request_id"]

        # Tenant B attempts to retrieve Tenant A's deletion request -> 404
        res_b = self.client_owner_b.get(f"/api/v2/tenants/deletion-requests/{request_id_a}/")
        self.assertEqual(res_b.status_code, status.HTTP_404_NOT_FOUND)

        # Tenant B attempts to query Tenant A's deletion requests via query param -> 403
        res_cross = self.client_owner_b.get(f"/api/v2/tenants/deletion-requests/?tenant_id={self.tenant_a.tenant_id}")
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
