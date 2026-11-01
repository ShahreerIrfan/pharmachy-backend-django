"""
backend/tests/test_p082_feature_entitlement.py
Architecture Decision: ADR-0015, ADR-0072, ADR-0080, ADR-0081 & ADR-0082
Phase 05: Basic/Standard/Pro Subscription & Billing (P082)

Comprehensive Integration Tests for Feature Entitlement Engine:
1. Valid Scenario: Effective capabilities, dynamic limits, quotas, and 72h offline lease evaluation.
2. Clinical Invariant (INV-SAFETY-01): Clinical safety features remain active even when tenant is SUSPENDED.
3. Commercial Dispensing Lockout: CHECKOUT_DISPENSING is False when tenant is SUSPENDED.
4. Invalid / Incomplete Inputs: Malformed device ID or unknown tenant rejected with 400/404/422.
5. Cross-Tenant Boundaries: Cross-tenant entitlement inspection blocked with 403 Forbidden.
6. Platform Feature Flag Integration: Central overrides merge seamlessly with applied entitlements.
7. Super Admin Privileges: Platform Super Admin can inspect any tenant without cross-tenant restriction.
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
    PlatformFeatureFlag,
    StaffMember,
    StaffRole,
    Tenant,
    TenantFeatureFlagOverride,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole


class TestP082FeatureEntitlement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p082_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Tenant A (Active, Standard Tier)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-p082-a-{cls.suffix}",
            name=f"Uttara Health Pharma {cls.suffix}",
            subdomain=f"ent-a-{cls.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p082_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )
        cls.branch_a1 = Branch.objects.create(
            branch_id=f"br-p082-a1-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Branch",
            is_active=True,
        )
        cls.staff_a1 = StaffMember.objects.create(
            staff_id=f"st-p082-a1-{cls.suffix}",
            tenant=cls.tenant_a,
            branch=cls.branch_a1,
            username=f"owner_a_{cls.suffix}",
            full_name=cls.owner_a.full_name,
            mobile_number=cls.owner_a.phone_number,
            role=StaffRole.TENANT_OWNER,
            is_active=True,
        )

        # Seed catalogue and apply Standard plan to Tenant A
        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_a.tenant_id,
            plan_code="PLAN_STANDARD",
            billing_cycle="MONTHLY",
        )

        # 3. Tenant B (Suspended, Basic Tier)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p082-b-{cls.suffix}",
            name=f"Mirpur MediCare {cls.suffix}",
            subdomain=f"ent-b-{cls.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p082_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )
        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_b.tenant_id,
            plan_code="PLAN_BASIC",
            billing_cycle="MONTHLY",
        )
        # Keep status as SUSPENDED
        cls.tenant_b.status = TenantLifecycleStatus.SUSPENDED
        cls.tenant_b.save(update_fields=["status"])

        cls.client_super = APIClient()
        cls.client_super.force_authenticate(user=cls.superadmin)

        cls.client_a = APIClient()
        cls.client_a.force_authenticate(user=cls.owner_a)

        cls.client_b = APIClient()
        cls.client_b.force_authenticate(user=cls.owner_b)

    def test_01_valid_effective_entitlement_evaluation(self):
        """
        Valid scenario: Authenticated request returns effective capabilities, dynamic quotas,
        and bounded 72-hour offline lease for Tenant A.
        """
        resp = self.client_a.get(
            f"/api/v2/billing/effective-entitlements/{self.tenant_a.tenant_id}/",
            {"device_id": "pos-device-01", "branch_id": self.branch_a1.branch_id},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data.get("success"))
        data = resp.data["data"]

        # 1. Identity & Context
        self.assertEqual(data["tenant_id"], self.tenant_a.tenant_id)
        self.assertEqual(data["device_id"], "pos-device-01")
        self.assertEqual(data["branch_id"], self.branch_a1.branch_id)

        # 2. Subscription state
        sub = data["subscription"]
        self.assertEqual(sub["status"], "ACTIVE")
        self.assertEqual(sub["plan_tier"], "STANDARD")
        self.assertEqual(sub["plan_code"], "PLAN_STANDARD")
        self.assertTrue(sub["is_checkout_allowed"])

        # 3. Dynamic quotas
        limits = data["limits"]
        self.assertEqual(limits["branches"]["max"], 3)
        self.assertEqual(limits["branches"]["used"], 1)
        self.assertEqual(limits["branches"]["remaining"], 2)
        self.assertFalse(limits["branches"]["is_over_quota"])

        self.assertEqual(limits["devices"]["max"], 15)
        self.assertEqual(limits["devices"]["max_per_branch"], 5)
        self.assertFalse(limits["devices"]["is_over_quota"])

        self.assertEqual(limits["staff"]["max"], 10)
        self.assertEqual(limits["staff"]["used"], 1)
        self.assertFalse(limits["staff"]["is_over_quota"])

        # 4. Offline lease
        lease = data["offline_lease"]
        self.assertTrue(lease["is_offline_capable"])
        self.assertEqual(lease["lease_duration_hours"], 72)
        self.assertEqual(lease["lease_status"], "ACTIVE")
        self.assertIsNotNone(lease["lease_expires_at"])

        # 5. Capabilities
        caps = data["capabilities"]
        # Clinical controls
        self.assertTrue(caps["ANTIBIOTIC_REGISTER"])
        self.assertTrue(caps["BATCH_EXPIRY_BLOCK"])
        self.assertTrue(caps["RECALL_QUARANTINE"])
        self.assertTrue(caps["BANNED_DRUG_RESTRICTION"])
        self.assertTrue(caps["RX_VERIFICATION"])
        # Commercial controls
        self.assertTrue(caps["CHECKOUT_DISPENSING"])
        self.assertTrue(caps["PURCHASE_ORDERS"])
        self.assertTrue(caps["FLUTTER_OWNER_APP"])
        self.assertFalse(caps["CONSOLIDATED_REPORTING"])  # Pro only

    def test_02_clinical_invariant_and_commercial_lockout_under_suspension(self):
        """
        Critical invariant INV-SAFETY-01 & ADR-0080:
        When tenant is SUSPENDED:
        1. CHECKOUT_DISPENSING is False (counter dispensing blocked)
        2. Offline lease status is SUSPENDED
        3. Mandatory clinical safety controls remain TRUE!
        """
        resp = self.client_b.get(f"/api/v2/billing/effective-entitlements/{self.tenant_b.tenant_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        data = resp.data["data"]

        # Counter sales blocked
        self.assertFalse(data["subscription"]["is_checkout_allowed"])
        self.assertFalse(data["capabilities"]["CHECKOUT_DISPENSING"])
        self.assertEqual(data["offline_lease"]["lease_status"], "SUSPENDED")
        self.assertIsNone(data["offline_lease"]["lease_expires_at"])

        # Clinical safety features MUST remain active and unblocked
        self.assertTrue(data["capabilities"]["ANTIBIOTIC_REGISTER"])
        self.assertTrue(data["capabilities"]["BATCH_EXPIRY_BLOCK"])
        self.assertTrue(data["capabilities"]["RECALL_QUARANTINE"])
        self.assertTrue(data["capabilities"]["BANNED_DRUG_RESTRICTION"])
        self.assertTrue(data["capabilities"]["RX_VERIFICATION"])

    def test_03_invalid_inputs_rejected(self):
        """
        Invalid / incomplete cases:
        1. Malformed device ID (< 3 chars or > 64 chars) rejected with 400/422
        2. Non-existent tenant rejected with 404
        """
        # Malformed device ID
        bad_dev_resp = self.client_a.get(
            f"/api/v2/billing/effective-entitlements/{self.tenant_a.tenant_id}/",
            {"device_id": "ab"},  # Too short (< 3 chars)
        )
        self.assertIn(
            bad_dev_resp.status_code,
            [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )

        # Non-existent tenant (Superadmin querying)
        not_found_resp = self.client_super.get("/api/v2/billing/effective-entitlements/tn-does-not-exist-999/")
        self.assertEqual(not_found_resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_04_cross_tenant_boundaries_enforced(self):
        """
        Unauthorized & cross-tenant case:
        Tenant A Owner querying Tenant B's effective entitlements is rejected with HTTP 403 Forbidden.
        """
        cross_resp = self.client_a.get(f"/api/v2/billing/effective-entitlements/{self.tenant_b.tenant_id}/")
        self.assertEqual(cross_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_05_platform_feature_flag_override_integration(self):
        """
        Merges platform feature flags (ADR-0072) with tenant applied entitlements.
        """
        # Create a platform feature flag
        flag_key = f"flag_promo_{self.suffix}"
        flag = PlatformFeatureFlag.objects.create(
            flag_key=flag_key,
            name="Special Promo Module",
            is_enabled_globally=False,
            default_enabled=False,
        )

        # Create explicit tenant override enabling this flag for Tenant A
        TenantFeatureFlagOverride.objects.create(
            override_id=f"tfo-{uuid.uuid4().hex[:12]}",
            flag=flag,
            tenant=self.tenant_a,
            is_enabled=True,
            created_by=self.superadmin,
        )

        # Evaluate Tenant A entitlements
        resp = self.client_a.get(f"/api/v2/billing/effective-entitlements/{self.tenant_a.tenant_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        caps = resp.data["data"]["capabilities"]
        self.assertTrue(
            caps.get(flag_key.upper()),
            f"Overridden feature flag '{flag_key.upper()}' must be evaluated to True for Tenant A!",
        )

    def test_06_superadmin_can_inspect_any_tenant(self):
        """
        Platform SuperAdmin can inspect any tenant's effective entitlements without cross-tenant restriction.
        """
        resp_a = self.client_super.get(f"/api/v2/billing/effective-entitlements/{self.tenant_a.tenant_id}/")
        self.assertEqual(resp_a.status_code, status.HTTP_200_OK)

        resp_b = self.client_super.get(f"/api/v2/billing/effective-entitlements/{self.tenant_b.tenant_id}/")
        self.assertEqual(resp_b.status_code, status.HTTP_200_OK)


if __name__ == "__main__":
    unittest.main()
