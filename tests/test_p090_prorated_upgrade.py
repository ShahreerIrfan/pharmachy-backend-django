"""
backend/tests/test_p090_prorated_upgrade.py
Architecture Decision: ADR-0013, ADR-0015, ADR-0027, ADR-0081, ADR-0083, ADR-0087 & ADR-0090
Phase 05: Basic/Standard/Pro Subscription & Billing (P090)

Comprehensive Integration Tests for Prorated Subscription Upgrades:
1. Valid Prorated Upgrade Quote: Accurate daily rates, unused credit calculation, and net payable.
2. Valid Upgrade Execution: Immediate entitlement unlock, quota expansion, invoice adjustment with credit discount, and renewal horizon preservation.
3. Cycle Upgrade (Monthly to Annual): Deduces unused monthly credit from annual charge and extends horizon by 365 days.
4. Downgrade Attempt Rejection: Attempting to downgrade (e.g. STANDARD -> BASIC) rejected with HTTP 400.
5. Identical Plan Rejection: Attempting to select current plan and cycle rejected with HTTP 400.
6. Cross-Tenant Barrier: Tenant B cannot quote or apply upgrades for Tenant A (HTTP 403 ERR_CROSS_TENANT_VIOLATION).
7. Non-Billing Role Barrier: Cashier or staff cannot execute plan upgrades (HTTP 403 ERR_PERMISSION_DENIED).
8. Missing Payment Reference: Paid upgrade with empty or insufficient reference rejected with HTTP 400.
9. Upgrade History and Retrieval: Auditable list of upgrade transitions accessible only by authorized tenant owners.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import timedelta
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    PlanTier,
    RenewalInvoiceStatus,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    Tenant,
    TenantAppliedEntitlement,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP090ProratedUpgradeIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p090_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A on BASIC Plan (1500 BDT/month)
        cls.subdomain_a = f"upg-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-upg-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P090-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@upga.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Upgrade Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Main Branch A",
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        # Apply canonical PLAN_BASIC entitlement to Tenant A
        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_a,
            plan_code="PLAN_BASIC",
            billing_cycle="MONTHLY",
        )

        # Adjust Tenant A subscription timestamps to simulate 10 days used out of 30 days
        now = timezone.now()
        cls.sub_a = SubscriptionRecord.objects.filter(tenant=cls.tenant_a).first()
        cls.sub_a.amount_bdt = Decimal("1500.00")
        cls.sub_a.start_date = (now - timedelta(days=10)).date()
        cls.sub_a.renewal_date = (now + timedelta(days=20)).date()
        cls.sub_a.save(update_fields=["amount_bdt", "start_date", "renewal_date"])

        # Cashier user for Tenant A (non-billing role)
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_a_{cls.suffix}@upga.local",
            full_name=f"Cashier A {cls.suffix}",
            role=UserRole.CASHIER,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_id_a,
            is_active=True,
        )

        # 3. Provision Tenant B on STANDARD Plan
        cls.subdomain_b = f"upg-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-upg-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P090-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@upgb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Upgrade Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Main Branch B",
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_b,
            plan_code="PLAN_STANDARD",
            billing_cycle="MONTHLY",
        )

    def setUp(self):
        self.client = APIClient()

    def test_01_quote_upgrade_basic_to_standard(self):
        """Owner can request upgrade quote; proration calculations and remaining days must be exact."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/upgrade/quote/",
            {"target_plan_code": "PLAN_STANDARD", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["from_plan_tier"], "BASIC")
        self.assertEqual(data["to_plan_tier"], "STANDARD")
        self.assertEqual(data["status"], "QUOTED")
        self.assertFalse(data["immediate_entitlement_granted"])

        proration = data["proration_days"]
        self.assertEqual(proration["total_days"], 30)
        self.assertEqual(proration["remaining_days"], 20)

        financials = data["financials"]
        self.assertEqual(financials["current_plan_amount_bdt"], 1500.0)
        # 1500 * 20 / 30 = 1000.00
        self.assertEqual(financials["unused_credit_bdt"], 1000.0)
        # 3500 * 20 / 30 = 2333.33
        self.assertEqual(financials["prorated_target_gross_bdt"], 2333.33)
        # 2333.33 - 1000.00 = 1333.33
        self.assertEqual(financials["net_payable_bdt"], 1333.33)

    def test_02_apply_upgrade_executes_immediately(self):
        """Executing upgrade creates adjusted invoice, settles payment, and unlocks immediate entitlements."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Quote
        quote_res = self.client.post(
            "/api/v2/billing/upgrade/quote/",
            {"target_plan_code": "PLAN_STANDARD", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(quote_res.status_code, status.HTTP_200_OK)
        upgrade_id = quote_res.json()["upgrade_id"]

        # 2. Apply with payment reference
        apply_res = self.client.post(
            "/api/v2/billing/upgrade/apply/",
            {
                "upgrade_id": upgrade_id,
                "payment_method": "BKASH",
                "payment_reference": f"TRX-UPG-{self.suffix}-01",
            },
            format="json",
        )
        self.assertEqual(apply_res.status_code, status.HTTP_200_OK)
        data = apply_res.json()

        self.assertEqual(data["status"], "COMPLETED")
        self.assertTrue(data["immediate_entitlement_granted"])
        self.assertIsNotNone(data["invoice_number"])

        # Verify DB: Adjusted invoice created
        invoice = SubscriptionRenewalInvoice.objects.get(invoice_number=data["invoice_number"])
        self.assertEqual(invoice.status, RenewalInvoiceStatus.PAID)
        self.assertEqual(invoice.subtotal_bdt, Decimal("2333.33"))
        self.assertEqual(invoice.discount_bdt, Decimal("1000.00"))
        self.assertEqual(invoice.total_bdt, Decimal("1333.33"))

        # Verify DB: Active entitlement snapshot upgraded to STANDARD
        active_ent = TenantAppliedEntitlement.objects.get(tenant=self.tenant_a, is_active=True)
        self.assertEqual(active_ent.plan_tier, PlanTier.STANDARD)
        self.assertEqual(active_ent.max_branches, 3)
        self.assertEqual(active_ent.max_total_devices, 15)

        # Verify DB: Subscription record upgraded
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.plan_tier, PlanTier.STANDARD)
        self.assertEqual(sub.amount_bdt, Decimal("3500.00"))

        # Verify Audit Log entry
        audit_entry = AuditLogEntry.objects.filter(
            action="SUBSCRIPTION_UPGRADE_EXECUTED",
            resource_id=upgrade_id,
        ).first()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry.metadata_json["to_tier"], "STANDARD")

    def test_03_monthly_to_annual_cycle_upgrade(self):
        """Monthly to annual upgrade deducts unused monthly credit from annual invoice and extends horizon 365 days."""
        # Tenant B is on STANDARD Monthly
        sub_b = SubscriptionRecord.objects.filter(tenant=self.tenant_b).first()
        now = timezone.now()
        sub_b.start_date = (now - timedelta(days=15)).date()
        sub_b.renewal_date = (now + timedelta(days=15)).date()
        sub_b.amount_bdt = Decimal("3500.00")
        sub_b.billing_cycle = "MONTHLY"
        sub_b.save(update_fields=["start_date", "renewal_date", "amount_bdt", "billing_cycle"])

        self.client.force_authenticate(user=self.owner_b)
        res = self.client.post(
            "/api/v2/billing/upgrade/apply/",
            {
                "target_plan_code": "PLAN_STANDARD",
                "billing_cycle": "ANNUAL",
                "payment_method": "NAGAD",
                "payment_reference": f"TRX-UPG-ANNUAL-{self.suffix}",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["to_billing_cycle"], "ANNUAL")
        self.assertTrue(data["immediate_entitlement_granted"])

        # 3500 * 15 / 30 = 1750.00 credit
        # Net payable: 35000.00 - 1750.00 = 33250.00
        financials = data["financials"]
        self.assertEqual(financials["unused_credit_bdt"], 1750.0)
        self.assertEqual(financials["target_plan_amount_bdt"], 35000.0)
        self.assertEqual(financials["net_payable_bdt"], 33250.0)

        # Renewal date extended ~365 days
        sub_b.refresh_from_db()
        self.assertEqual(sub_b.billing_cycle, "ANNUAL")
        self.assertGreaterEqual((sub_b.renewal_date - now.date()).days, 360)

    def test_04_downgrade_attempt_rejected(self):
        """Attempting to choose lower tier plan is classified as downgrade and rejected with HTTP 400."""
        # Tenant B is currently on STANDARD (or Annual STANDARD), attempts downgrade to BASIC
        self.client.force_authenticate(user=self.owner_b)
        res = self.client.post(
            "/api/v2/billing/upgrade/quote/",
            {"target_plan_code": "PLAN_BASIC", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        data = res.json()
        self.assertIn("downgrade", str(data["detail"]).lower())

    def test_05_identical_plan_and_cycle_rejected(self):
        """Attempting to upgrade to identical plan and cycle rejected with HTTP 400."""
        # Tenant A is on BASIC (or upgraded tier), requesting identical tier and cycle
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        plan_code = f"PLAN_{sub.plan_tier}"
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/upgrade/quote/",
            {"target_plan_code": plan_code, "billing_cycle": sub.billing_cycle},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        data = res.json()
        self.assertIn("identical", str(data["detail"]).lower())

    def test_06_cross_tenant_isolation(self):
        """Tenant B owner cannot quote or apply an upgrade for Tenant A (HTTP 403 CrossTenantViolation)."""
        self.client.force_authenticate(user=self.owner_b)
        res = self.client.post(
            "/api/v2/billing/upgrade/quote/",
            {
                "tenant_id": self.tenant_id_a,
                "target_plan_code": "PLAN_PRO",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_07_unauthorized_role_barrier(self):
        """Cashier or staff cannot execute subscription upgrade quotes or applications (HTTP 403)."""
        self.client.force_authenticate(user=self.cashier_a)
        res = self.client.post(
            "/api/v2/billing/upgrade/quote/",
            {"target_plan_code": "PLAN_PRO", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error"], "ERR_PERMISSION_DENIED")

    def test_08_missing_payment_reference_rejected(self):
        """Paid upgrade without payment reference rejected with HTTP 400."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/upgrade/apply/",
            {
                "target_plan_code": "PLAN_PRO",
                "billing_cycle": "MONTHLY",
                "payment_reference": "",  # Missing reference
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("payment reference", str(res.json()["detail"]).lower())

    def test_09_list_and_retrieve_upgrade_history(self):
        """Tenant owner can view upgrade audit history."""
        self.client.force_authenticate(user=self.owner_a)
        # Create an upgrade record first
        self.client.post(
            "/api/v2/billing/upgrade/apply/",
            {
                "target_plan_code": "PLAN_STANDARD",
                "billing_cycle": "MONTHLY",
                "payment_method": "BKASH",
                "payment_reference": f"TRX-HIST-{self.suffix}",
            },
            format="json",
        )

        res = self.client.get("/api/v2/billing/upgrade/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json()["results"]
        self.assertGreaterEqual(len(results), 1)

        upgrade_id = results[0]["upgrade_id"]
        detail_res = self.client.get(f"/api/v2/billing/upgrade/{upgrade_id}/")
        self.assertEqual(detail_res.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_res.json()["upgrade_id"], upgrade_id)
