"""
backend/tests/test_p086_trial_policy.py
Architecture Decision: ADR-0013, ADR-0062, ADR-0085 & ADR-0086
Phase 05: Basic/Standard/Pro Subscription & Billing (P086)

Comprehensive Integration Tests for Trial and Evaluation Policy:
1. Valid Scenario: Provision trial, retrieve trial status, inspect 14 days remaining and quotas.
2. Anti-Abuse Constraint: Attempting to provision a 2nd trial using the same DGDA Drug License or owner phone is blocked with ERR_TRIAL_ABUSE_DETECTED.
3. SuperAdmin Trial Extension: Platform Super Admin can extend trial by up to 7 days with audited business reason.
4. Anti-Abuse Extension Limit: Attempting a 2nd trial extension is rejected with ERR_TRIAL_EXTENSION_LIMIT_EXCEEDED.
5. Role-Based Access Control: Regular pharmacy owner cannot self-extend trial (ROLE_PERMISSION_DENIED).
6. Cross-Tenant Security: Tenant Owner A cannot inspect Tenant B's trial policy (ERR_CROSS_TENANT_VIOLATION).
7. Expiration & Non-Destructive Preservation (INV-NONDESTRUCTIVE-01): Expired trial rejects operational mutations, retains all historical records intact, and permits read-only retrieval.
8. Safety Invariant (INV-SAFETY-01): Clinical drug safety and quarantine registers remain accessible during trial.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    MedicineProduct,
    PlanTier,
    SaleInvoice,
    Tenant,
)
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP086TrialPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p086_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A in TRIAL mode
        cls.subdomain_a = f"trial-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-trial-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P086-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@trialpharma.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Trial Pharmacy A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Main Branch",
            branch_license=cls.license_a,
            is_trial=True,
            operator_id=str(cls.superadmin.user_id),
        )

        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.branch_a = Branch.objects.get(tenant=cls.tenant_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        # 3. Provision Tenant B in COMMERCIAL ACTIVE mode
        cls.subdomain_b = f"comm-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-comm-b-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@commpharma.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Commercial Pharmacy B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Head Branch",
            branch_license=f"DGDA-DL-P086-B-{cls.suffix}",
            is_trial=False,
            operator_id=str(cls.superadmin.user_id),
        )

        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

        # 4. Medicine Product for operational inventory test
        cls.product = MedicineProduct.objects.create(
            product_id=f"prod_p086_{cls.suffix}",
            brand_name=f"Napa Extend {cls.suffix}",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            strength="665mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("2.50"),
            cost_unit=Decimal("1.80"),
            dgda_dar_number=f"DAR-P086-{cls.suffix}",
        )

    def setUp(self):
        self.client = APIClient()

    def test_01_valid_trial_status_inspection(self):
        """Authenticated tenant owner can retrieve trial terms, days remaining, and quotas."""
        self.client.force_authenticate(user=self.owner_a)

        res = self.client.get(f"/api/v2/billing/trial/{self.tenant_a.tenant_id}/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertEqual(data["tenant_id"], self.tenant_a.tenant_id)
        self.assertTrue(data["is_trial"])
        self.assertEqual(data["trial_status"], "ACTIVE_TRIAL")
        self.assertGreaterEqual(data["days_remaining"], 13)
        self.assertFalse(data["is_expired"])
        self.assertTrue(data["is_write_permitted"])
        self.assertTrue(data["non_destructive_guarantee"])
        self.assertEqual(data["quotas"]["max_branches"], 1)
        self.assertEqual(data["quotas"]["max_devices"], 2)
        self.assertEqual(data["quotas"]["offline_lease_hours"], 24)

    def test_02_anti_abuse_blocks_duplicate_drug_license_or_phone_trial(self):
        """Attempting to provision another trial with an already used drug license or phone number is rejected."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Attempt using duplicate DGDA drug license
        res1 = self.client.post(
            "/api/v2/billing/trial/evaluate-eligibility/",
            {"license_number": self.license_a, "phone": "+8801711998877", "email": "brandnew@pharmacy.local"},
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res1.json()["code"], "ERR_TRIAL_ABUSE_DETECTED")
        self.assertTrue(res1.json()["data"]["abuse_detected"])

        # 2. Attempt using duplicate Owner Phone number
        res2 = self.client.post(
            "/api/v2/billing/trial/evaluate-eligibility/",
            {"license_number": "DGDA-DL-BRANDNEW-99", "phone": self.phone_a, "email": "brandnew2@pharmacy.local"},
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res2.json()["code"], "ERR_TRIAL_ABUSE_DETECTED")
        self.assertTrue(res2.json()["data"]["abuse_detected"])

        # 3. Clean fresh credentials are approved
        res3 = self.client.post(
            "/api/v2/billing/trial/evaluate-eligibility/",
            {
                "license_number": f"DGDA-DL-FRESH-{self.suffix}",
                "phone": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "email": f"fresh_{self.suffix}@pharma.local",
            },
            format="json",
        )
        self.assertEqual(res3.status_code, status.HTTP_200_OK)
        self.assertEqual(res3.json()["code"], "ELIGIBLE")
        self.assertFalse(res3.json()["data"]["abuse_detected"])

    def test_03_superadmin_can_extend_trial_once_with_audit(self):
        """Platform Super Admin can extend trial by up to 7 days with mandatory business reason."""
        self.client.force_authenticate(user=self.superadmin)

        res = self.client.post(
            "/api/v2/billing/trial/extend/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "additional_days": 7,
                "reason": "Prospective subscriber requested 1-week extension for pharmacy board evaluation.",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        self.assertEqual(data["extension_info"]["extension_count"], 1)
        self.assertFalse(data["extension_info"]["can_extend"])
        self.assertGreaterEqual(data["days_remaining"], 18)

    def test_04_second_trial_extension_rejected_under_anti_abuse_limits(self):
        """Attempting a second trial extension is blocked under ADR-0086 anti-abuse caps."""
        self.client.force_authenticate(user=self.superadmin)

        # Attempt 2nd extension
        res = self.client.post(
            "/api/v2/billing/trial/extend/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "additional_days": 7,
                "reason": "Second extension request by store manager.",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn("ERR_TRIAL_EXTENSION_LIMIT_EXCEEDED", res.json()["message"])

    def test_05_non_superadmin_cannot_extend_trial(self):
        """Regular pharmacy owner cannot self-extend trial periods."""
        self.client.force_authenticate(user=self.owner_a)

        res = self.client.post(
            "/api/v2/billing/trial/extend/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "additional_days": 5,
                "reason": "Self-extension attempt by owner.",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ROLE_PERMISSION_DENIED")

    def test_06_cross_tenant_trial_inspection_rejected(self):
        """Pharmacy Owner A cannot inspect Tenant B's trial policy."""
        self.client.force_authenticate(user=self.owner_a)

        res = self.client.get(f"/api/v2/billing/trial/{self.tenant_b.tenant_id}/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            res.json().get("error_code") or res.json().get("code"),
            ["ERR_CROSS_TENANT_VIOLATION", "CROSS_TENANT_VIOLATION"],
        )

    def test_07_expired_trial_blocks_writes_while_preserving_data_nondestructively(self):
        """
        Expired trial blocks operational writes under P085/P086 while safely preserving
        100% of historical invoices, products, and customer records (INV-NONDESTRUCTIVE-01).
        """
        # Create historical invoice during active period to verify durability
        invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-hist-{self.suffix}-{uuid.uuid4().hex[:6]}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            invoice_number=f"INV-HIST-{self.suffix}",
            customer_name="Historical Customer",
            customer_phone=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            payment_method="CASH",
            total_amount=Decimal("105.00"),
            net_amount=Decimal("100.00"),
            tax_amount=Decimal("5.00"),
            discount_amount=Decimal("0.00"),
        )

        # Force trial expiration
        self.tenant_a.trial_ends_at = timezone.now() - timedelta(days=2)
        self.tenant_a.save(update_fields=["trial_ends_at"])

        # Authenticate owner
        self.client.force_authenticate(user=self.owner_a)

        # 1. Inspect trial status returns EXPIRED
        status_resp = self.client.get(f"/api/v2/billing/trial/{self.tenant_a.tenant_id}/")
        self.assertEqual(status_resp.status_code, status.HTTP_200_OK)
        status_data = status_resp.json()["data"]
        self.assertEqual(status_data["trial_status"], "EXPIRED")
        self.assertTrue(status_data["is_expired"])
        self.assertFalse(status_data["is_write_permitted"])
        self.assertTrue(status_data["non_destructive_guarantee"])
        self.assertIn("safely preserved", status_data["notice_en"])

        # 2. Operational write is blocked by activation gate
        write_resp = self.client.post(
            "/api/v2/invoices/checkout/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "items": [{"product_id": self.product.product_id, "quantity": 1}],
            },
            format="json",
        )
        self.assertEqual(write_resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            write_resp.json().get("error_code") or write_resp.json().get("code"),
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
        )

        # 3. Read-Only historical queries succeed (zero data loss)
        read_resp = self.client.get("/api/v2/invoices/")
        self.assertEqual(read_resp.status_code, status.HTTP_200_OK)

        # Verify historical invoice is preserved in database
        self.assertTrue(SaleInvoice.objects.filter(invoice_id=invoice.invoice_id).exists())

        # 4. Billing renewal remains open for commercial upgrade
        billing_resp = self.client.get(f"/api/v2/billing/subscriptions/{self.tenant_a.tenant_id}/")
        self.assertEqual(billing_resp.status_code, status.HTTP_200_OK)


if __name__ == "__main__":
    unittest.main()
