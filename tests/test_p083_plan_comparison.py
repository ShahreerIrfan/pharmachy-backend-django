"""
backend/tests/test_p083_plan_comparison.py
Architecture Decision: ADR-0015, ADR-0081 & ADR-0083
Phase 05: Basic/Standard/Pro Subscription & Billing (P083)

Comprehensive Integration Tests for Plan Comparison UI, Annual Savings Calculation, and Checkout Validation:
1. Public Comparison Matrix: Side-by-side plans, BDT pricing, annual savings (16.67% / 2 months free), quotas, and universal clinical safety invariants (INV-SAFETY-01).
2. Quote Calculation: Verified quote generation for monthly and annual billing cycles with exact savings breakdowns and renewal dates.
3. Quota Compliance Evaluation: Detects when existing tenant resources (branches/staff) violate downgrade plan quotas.
4. Invalid/Incomplete Inputs: Rejection of bad plan codes, unsupported cycles, or malformed payloads with 400/422.
5. Cross-Tenant Security: Cross-tenant quote initiation rejected with HTTP 403 CrossTenantViolationError.
6. Role-Based Access Control: Non-owner staff member blocked with HTTP 403 ROLE_PERMISSION_DENIED.
7. Super Admin Authorization: Platform Super Admin can generate quotes for any tenant.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole


class TestP083PlanComparison(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p083_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Tenant A (Dhaka Pharma - 1 Branch)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-p083-a-{cls.suffix}",
            name=f"Dhaka Central Pharma {cls.suffix}",
            subdomain=f"comp-a-{cls.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Dhanmondi Branch",
            is_active=True,
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p083_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )
        cls.staff_a = PharmacyUser.objects.create(
            email=f"p083_staff_a_{cls.suffix}@pharmacy.com",
            full_name=f"Staff A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.CASHIER,
            is_active=True,
        )

        # 3. Tenant B (Chittagong Pharma - 3 Branches)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p083-b-{cls.suffix}",
            name=f"Chittagong Pharma {cls.suffix}",
            subdomain=f"comp-b-{cls.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        for i in range(1, 4):
            Branch.objects.create(
                branch_id=f"br-b{i}-{cls.suffix}",
                tenant=cls.tenant_b,
                name=f"CTG Branch {i}",
                is_active=True,
            )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p083_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # Seed catalogue
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        self.client = APIClient()

    def test_01_public_comparison_matrix_retrieval(self):
        """Unauthenticated clients can retrieve the side-by-side comparison matrix with safety invariants."""
        res = self.client.get("/api/v2/billing/plans/comparison/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]

        # 1. Plans side-by-side
        plans = data["plans"]
        self.assertEqual(len(plans), 3)
        plan_codes = [p["plan_code"] for p in plans]
        self.assertIn("PLAN_BASIC", plan_codes)
        self.assertIn("PLAN_STANDARD", plan_codes)
        self.assertIn("PLAN_PRO", plan_codes)

        # Basic plan calculations
        basic = next(p for p in plans if p["plan_code"] == "PLAN_BASIC")
        self.assertEqual(basic["monthly_price_bdt"], "1500.00")
        self.assertEqual(basic["annual_price_bdt"], "15000.00")
        self.assertEqual(basic["effective_monthly_bdt"], "1250.00")
        self.assertEqual(basic["annual_savings_bdt"], "3000.00")
        self.assertEqual(basic["savings_percentage"], "16.67%")
        self.assertEqual(basic["quotas"]["max_branches"], 1)

        # Standard plan calculations
        std = next(p for p in plans if p["plan_code"] == "PLAN_STANDARD")
        self.assertEqual(std["monthly_price_bdt"], "3500.00")
        self.assertEqual(std["annual_price_bdt"], "35000.00")
        self.assertEqual(std["effective_monthly_bdt"], "2916.67")
        self.assertEqual(std["annual_savings_bdt"], "7000.00")
        self.assertEqual(std["savings_percentage"], "16.67%")
        self.assertEqual(std["quotas"]["max_branches"], 3)

        # Pro plan calculations
        pro = next(p for p in plans if p["plan_code"] == "PLAN_PRO")
        self.assertEqual(pro["monthly_price_bdt"], "7500.00")
        self.assertEqual(pro["annual_price_bdt"], "75000.00")
        self.assertEqual(pro["effective_monthly_bdt"], "6250.00")
        self.assertEqual(pro["annual_savings_bdt"], "15000.00")
        self.assertEqual(pro["savings_percentage"], "16.67%")
        self.assertEqual(pro["quotas"]["max_branches"], 50)

        # 2. Universal Clinical Safety Invariants (INV-SAFETY-01)
        invariants = data["clinical_safety_invariants"]
        self.assertEqual(len(invariants), 5)
        invariant_keys = [inv["key"] for inv in invariants]
        for expected_key in [
            "ANTIBIOTIC_REGISTER",
            "BATCH_EXPIRY_BLOCK",
            "RECALL_QUARANTINE",
            "BANNED_DRUG_RESTRICTION",
            "RX_VERIFICATION",
        ]:
            self.assertIn(expected_key, invariant_keys)
        for inv in invariants:
            self.assertTrue(inv["guaranteed"])

        # 3. Billing cycles
        cycles = data["billing_cycles"]
        self.assertEqual(len(cycles), 2)
        cycle_names = [c["cycle"] for c in cycles]
        self.assertIn("MONTHLY", cycle_names)
        self.assertIn("ANNUAL", cycle_names)

    def test_02_quote_calculation_monthly_and_annual_savings(self):
        """Owner can generate a quote with full annual discount math and safety invariants."""
        self.client.force_authenticate(user=self.owner_a)

        # A. Annual Quote for Standard Plan
        res_annual = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "plan_code": "PLAN_STANDARD",
                "billing_cycle": "ANNUAL",
            },
            format="json",
        )
        self.assertEqual(res_annual.status_code, status.HTTP_200_OK)
        q_annual = res_annual.json()["data"]

        self.assertEqual(q_annual["plan_code"], "PLAN_STANDARD")
        self.assertEqual(q_annual["plan_tier"], "STANDARD")
        self.assertEqual(q_annual["billing_cycle"], "ANNUAL")
        self.assertEqual(q_annual["subtotal_bdt"], "42000.00")
        self.assertEqual(q_annual["discount_bdt"], "7000.00")
        self.assertEqual(q_annual["total_bdt"], "35000.00")
        self.assertEqual(q_annual["effective_monthly_bdt"], "2916.67")
        self.assertEqual(q_annual["savings_percentage"], "16.67%")
        self.assertEqual(q_annual["renewal_period_days"], 365)
        self.assertTrue(q_annual["safety_features_guaranteed"])
        self.assertTrue(q_annual["quota_compliance"]["is_compliant"])
        self.assertEqual(len(q_annual["quota_compliance"]["violations"]), 0)

        # B. Monthly Quote for Standard Plan
        res_monthly = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "plan_code": "PLAN_STANDARD",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertEqual(res_monthly.status_code, status.HTTP_200_OK)
        q_monthly = res_monthly.json()["data"]

        self.assertEqual(q_monthly["plan_code"], "PLAN_STANDARD")
        self.assertEqual(q_monthly["billing_cycle"], "MONTHLY")
        self.assertEqual(q_monthly["subtotal_bdt"], "3500.00")
        self.assertEqual(q_monthly["discount_bdt"], "0.00")
        self.assertEqual(q_monthly["total_bdt"], "3500.00")
        self.assertEqual(q_monthly["effective_monthly_bdt"], "3500.00")
        self.assertEqual(q_monthly["savings_percentage"], "0.00%")
        self.assertEqual(q_monthly["renewal_period_days"], 30)

    def test_03_quote_quota_compliance_warning_on_downgrade(self):
        """Downgrading a tenant whose active resources exceed target limits marks quote non-compliant."""
        self.client.force_authenticate(user=self.owner_b)

        # Tenant B has 3 branches; target plan PLAN_BASIC only allows 1 branch
        res = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_b.tenant_id,
                "plan_code": "PLAN_BASIC",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        quote = res.json()["data"]

        self.assertFalse(quote["quota_compliance"]["is_compliant"])
        self.assertEqual(len(quote["quota_compliance"]["violations"]), 1)
        self.assertIn("exceed target plan limit", quote["quota_compliance"]["violations"][0])

    def test_04_invalid_inputs_rejected(self):
        """Malformed requests, unknown plan codes, or unsupported billing cycles are rejected with 400/422."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Missing plan_code
        res1 = self.client.post(
            "/api/v2/billing/plans/quote/",
            {"tenant_id": self.tenant_a.tenant_id, "billing_cycle": "MONTHLY"},
            format="json",
        )
        self.assertIn(res1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 2. Unsupported billing cycle
        res2 = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "plan_code": "PLAN_BASIC",
                "billing_cycle": "SEMI_ANNUAL",
            },
            format="json",
        )
        self.assertIn(res2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # 3. Non-existent plan code
        res3 = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "plan_code": "PLAN_UNKNOWN_XYZ",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertIn(res3.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_05_cross_tenant_isolation_blocked(self):
        """Tenant owner A cannot generate quotes for Tenant B."""
        self.client.force_authenticate(user=self.owner_a)

        res = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_b.tenant_id,
                "plan_code": "PLAN_STANDARD",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        body = res.json()
        self.assertIn(
            body.get("error_code") or body.get("code"),
            ["ERR_CROSS_TENANT_VIOLATION", "CROSS_TENANT_VIOLATION"],
        )

    def test_06_non_owner_role_permission_denied(self):
        """Staff member without owner role cannot generate checkout quotes."""
        self.client.force_authenticate(user=self.staff_a)

        res = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "plan_code": "PLAN_STANDARD",
                "billing_cycle": "MONTHLY",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        body = res.json()
        self.assertIn(
            body.get("error_code") or body.get("code"),
            ["ROLE_PERMISSION_DENIED", "ERR_PERMISSION_DENIED"],
        )

    def test_07_superadmin_can_generate_quote_for_any_tenant(self):
        """Platform Super Admin can generate quotes across any tenant."""
        self.client.force_authenticate(user=self.superadmin)

        res = self.client.post(
            "/api/v2/billing/plans/quote/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "plan_code": "PLAN_PRO",
                "billing_cycle": "ANNUAL",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()["data"]
        self.assertEqual(data["plan_code"], "PLAN_PRO")
        self.assertEqual(data["total_bdt"], "75000.00")
        self.assertEqual(data["savings_percentage"], "16.67%")


if __name__ == "__main__":
    unittest.main()
