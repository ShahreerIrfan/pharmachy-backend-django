"""
backend/tests/test_p081_plan_catalogue.py
Architecture Decision: ADR-0015 & ADR-0081
Phase 05: Basic/Standard/Pro Subscription & Billing (P081)

Comprehensive Integration Tests for Plan Catalogue Schema and Immutable Applied Entitlements:
1. Valid Scenario: Plan catalogue discovery, pricing in BDT, plan application, and quota snapshot.
2. Clinical Invariant (INV-SAFETY-01): Clinical safety features are never gated across any plan.
3. Commercial Tiering: Advanced analytics / consolidated reporting gated to Pro.
4. Invalid / Incomplete Inputs: Invalid billing cycle or unknown plan code rejected.
5. Unauthorized / Cross-Tenant Scenarios: Cross-tenant entitlement access and non-owner plan changes blocked.
6. Immutability Protection: Catalogue price changes do not corrupt active tenant snapshot.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    PlanCatalogueRecord,
    PlanTier,
    SubscriptionRecord,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole


class TestP081PlanCatalogue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p081_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Tenant A (Dhaka Pharma)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-p081-a-{cls.suffix}",
            name=f"Dhaka Central Pharma {cls.suffix}",
            subdomain=f"plan-a-{cls.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p081_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"p081_cashier_a_{cls.suffix}@pharmacy.com",
            full_name=f"Cashier A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.CASHIER,
            is_active=True,
            mfa_enabled=True,
        )

        # 3. Tenant B (Chittagong Care)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p081-b-{cls.suffix}",
            name=f"Chittagong Care {cls.suffix}",
            subdomain=f"plan-b-{cls.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p081_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        cls.client_super = APIClient()
        cls.client_super.force_authenticate(user=cls.superadmin)

        cls.client_a = APIClient()
        cls.client_a.force_authenticate(user=cls.owner_a)

        cls.client_a_cashier = APIClient()
        cls.client_a_cashier.force_authenticate(user=cls.cashier_a)

        cls.client_b = APIClient()
        cls.client_b.force_authenticate(user=cls.owner_b)

        cls.anon_client = APIClient()

    def test_01_catalogue_discovery_and_canonical_specifications(self):
        """
        Valid scenario: Public plan catalogue is accessible, lists Basic, Standard, and Pro
        specifications, BDT pricing, supported cycles, and quotas.
        """
        resp = self.anon_client.get("/api/v2/billing/plans/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data.get("success"))
        plans = {p["plan_code"]: p for p in resp.data.get("data", [])}

        self.assertIn("PLAN_BASIC", plans)
        self.assertIn("PLAN_STANDARD", plans)
        self.assertIn("PLAN_PRO", plans)

        # Verify Basic plan
        basic = plans["PLAN_BASIC"]
        self.assertEqual(basic["tier"], "BASIC")
        self.assertEqual(basic["monthly_price_bdt"], "1500.00")
        self.assertEqual(basic["annual_price_bdt"], "15000.00")
        self.assertEqual(basic["currency"], "BDT")
        self.assertEqual(basic["quotas"]["max_branches"], 1)
        self.assertEqual(basic["quotas"]["max_devices_per_branch"], 1)
        self.assertEqual(basic["quotas"]["max_total_devices"], 1)
        self.assertEqual(basic["quotas"]["max_staff_users"], 3)

        # Verify Standard plan
        standard = plans["PLAN_STANDARD"]
        self.assertEqual(standard["tier"], "STANDARD")
        self.assertEqual(standard["monthly_price_bdt"], "3500.00")
        self.assertEqual(standard["annual_price_bdt"], "35000.00")
        self.assertEqual(standard["quotas"]["max_branches"], 3)
        self.assertEqual(standard["quotas"]["max_devices_per_branch"], 5)
        self.assertEqual(standard["quotas"]["max_total_devices"], 15)

        # Verify Pro plan
        pro = plans["PLAN_PRO"]
        self.assertEqual(pro["tier"], "PRO")
        self.assertEqual(pro["monthly_price_bdt"], "7500.00")
        self.assertEqual(pro["annual_price_bdt"], "75000.00")
        self.assertEqual(pro["quotas"]["max_branches"], 50)
        self.assertEqual(pro["quotas"]["max_total_devices"], 250)

        # Retrieve single plan
        single_resp = self.client_a.get("/api/v2/billing/plans/PLAN_STANDARD/")
        self.assertEqual(single_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(single_resp.data["data"]["name"], "Standard Pharmacy")

    def test_02_apply_plan_and_immutable_entitlement_snapshot(self):
        """
        Valid scenario: Owner applies a plan, creating an immutable snapshot and updating subscriptions.
        """
        apply_resp = self.client_a.post(
            f"/api/v2/billing/entitlements/{self.tenant_a.tenant_id}/apply/",
            data={
                "plan_code": "PLAN_PRO",
                "billing_cycle": "ANNUAL",
                "notes": "Upgraded to Pro for chain expansion.",
            },
            format="json",
        )
        self.assertEqual(apply_resp.status_code, status.HTTP_200_OK, apply_resp.data)
        data = apply_resp.data["data"]

        self.assertEqual(data["plan_code"], "PLAN_PRO")
        self.assertEqual(data["plan_tier"], "PRO")
        self.assertEqual(data["billing_cycle"], "ANNUAL")
        self.assertEqual(data["unit_price_bdt"], "75000.00")
        self.assertEqual(data["currency"], "BDT")
        self.assertEqual(data["quotas"]["max_branches"], 50)
        self.assertEqual(data["quotas"]["max_total_devices"], 250)

        # Verify Tenant updated
        self.tenant_a.refresh_from_db()
        self.assertEqual(self.tenant_a.plan_tier, PlanTier.PRO)

        # Verify SubscriptionRecord updated
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.plan_tier, PlanTier.PRO)
        self.assertEqual(sub.amount_bdt, Decimal("75000.00"))
        self.assertEqual(sub.billing_cycle, "ANNUAL")

        # Verify Audit log entry
        audit_entry = AuditLogEntry.objects.filter(
            tenant_id=self.tenant_a.tenant_id,
            action="TENANT_ENTITLEMENT_APPLIED",
        ).first()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry.metadata_json["plan_code"], "PLAN_PRO")

        # Inspect active entitlement endpoint
        ent_resp = self.client_a.get(f"/api/v2/billing/entitlements/{self.tenant_a.tenant_id}/")
        self.assertEqual(ent_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(ent_resp.data["data"]["plan_code"], "PLAN_PRO")
        self.assertEqual(ent_resp.data["data"]["is_active"], True)

    def test_03_non_gated_clinical_safety_invariant(self):
        """
        Cardinal Invariant INV-SAFETY-01: Clinical safety features are never gated behind paid tiers.
        """
        # Ensure tenant B is on BASIC
        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=self.tenant_b.tenant_id,
            plan_code="PLAN_BASIC",
            billing_cycle="MONTHLY",
        )

        clinical_features = [
            "ANTIBIOTIC_REGISTER",
            "BATCH_EXPIRY_BLOCK",
            "RECALL_QUARANTINE",
            "BANNED_DRUG_RESTRICTION",
            "RX_VERIFICATION",
        ]

        for feature in clinical_features:
            resp = self.client_b.get(
                f"/api/v2/billing/entitlements/{self.tenant_b.tenant_id}/verify-feature/?feature={feature}"
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertTrue(
                resp.data["is_entitled"],
                f"Feature '{feature}' MUST be entitled on Basic plan!",
            )

        # Commercial feature gating: CONSOLIDATED_REPORTING is False on Basic
        comm_resp = self.client_b.get(
            f"/api/v2/billing/entitlements/{self.tenant_b.tenant_id}/verify-feature/?feature=CONSOLIDATED_REPORTING"
        )
        self.assertEqual(comm_resp.status_code, status.HTTP_200_OK)
        self.assertFalse(comm_resp.data["is_entitled"])

    def test_04_invalid_inputs_rejected(self):
        """
        Invalid / incomplete cases: Unsupported billing cycle or invalid plan code rejected with 400.
        """
        # 1. Unsupported billing cycle
        resp_invalid_cycle = self.client_a.post(
            f"/api/v2/billing/entitlements/{self.tenant_a.tenant_id}/apply/",
            data={
                "plan_code": "PLAN_BASIC",
                "billing_cycle": "QUARTERLY",  # Invalid
            },
            format="json",
        )
        self.assertIn(
            resp_invalid_cycle.status_code,
            [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )

        # 2. Unknown plan code
        resp_unknown_plan = self.client_a.post(
            f"/api/v2/billing/entitlements/{self.tenant_a.tenant_id}/apply/",
            data={
                "plan_code": "PLAN_NON_EXISTENT_99",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertIn(
            resp_unknown_plan.status_code,
            [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )

    def test_05_cross_tenant_and_unauthorized_boundaries(self):
        """
        Unauthorized & cross-tenant cases:
        1. Tenant A Owner cannot inspect Tenant B entitlement.
        2. Tenant A Owner cannot apply plan to Tenant B.
        3. Cashier cannot apply plan.
        4. Tenant Owner cannot create a plan in catalogue.
        """
        # 1. Cross-tenant inspection
        cross_get = self.client_a.get(f"/api/v2/billing/entitlements/{self.tenant_b.tenant_id}/")
        self.assertEqual(cross_get.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Cross-tenant plan application
        cross_post = self.client_a.post(
            f"/api/v2/billing/entitlements/{self.tenant_b.tenant_id}/apply/",
            data={"plan_code": "PLAN_STANDARD", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(cross_post.status_code, status.HTTP_403_FORBIDDEN)

        # 3. Cashier cannot apply plan
        cashier_post = self.client_a_cashier.post(
            f"/api/v2/billing/entitlements/{self.tenant_a.tenant_id}/apply/",
            data={"plan_code": "PLAN_STANDARD", "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertEqual(cashier_post.status_code, status.HTTP_403_FORBIDDEN)

        # 4. Tenant Owner cannot create plan in catalogue
        owner_create_plan = self.client_a.post(
            "/api/v2/billing/plans/",
            data={
                "plan_code": "PLAN_CUSTOM_HACK",
                "tier": "BASIC",
                "name": "Hacked Plan",
                "monthly_price_bdt": "10.00",
                "annual_price_bdt": "100.00",
                "currency": "BDT",
                "supported_billing_cycles": ["MONTHLY"],
                "max_branches": 1,
                "max_devices_per_branch": 1,
                "max_total_devices": 1,
                "max_staff_users": 5,
                "features": {},
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(owner_create_plan.status_code, status.HTTP_403_FORBIDDEN)

    def test_06_plan_catalogue_evolution_does_not_mutate_existing_snapshot(self):
        """
        Immutability test: Price inflation in catalogue does NOT alter existing tenant snapshot terms.
        """
        # Ensure tenant A has PRO applied at 75,000 BDT
        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=self.tenant_a.tenant_id,
            plan_code="PLAN_PRO",
            billing_cycle="ANNUAL",
        )
        initial_ent = TenantAppliedEntitlement.objects.get(tenant=self.tenant_a, is_active=True)
        self.assertEqual(initial_ent.unit_price_bdt, Decimal("75000.00"))

        # Platform Super Admin updates catalogue price for PLAN_PRO to 90,000 BDT
        plan_pro = PlanCatalogueRecord.objects.get(plan_code="PLAN_PRO")
        plan_pro.annual_price_bdt = Decimal("90000.00")
        plan_pro.save()

        # Tenant A's existing snapshot must still have unit_price_bdt = 75000.00
        initial_ent.refresh_from_db()
        self.assertEqual(initial_ent.unit_price_bdt, Decimal("75000.00"))


if __name__ == "__main__":
    unittest.main()
