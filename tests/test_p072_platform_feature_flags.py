"""
backend/tests/test_p072_platform_feature_flags.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0072
Comprehensive test suite for Platform Feature Flags, Tenant Targeting, and Emergency Kill Switches:
1. SuperAdmin CRUD: Platform Super Admin can create, inspect, update, and list platform feature flags.
2. Tenant Non-Admin Role Guard: Pharmacy Owner and Cashier receive 403 Forbidden when attempting platform mutations.
3. Emergency Kill Switch: Activating kill switch immediately overrides all overrides/tiers to FALSE platform-wide.
4. Targeted Tenant Override: Tenant A override takes precedence over rollout/tiers; Tenant B is isolated and unaffected.
5. Plan Tier Gating: Minimum plan tier restricts tenants below required tier (e.g. BASIC blocked from PRO feature).
6. Deterministic Rollout: Percentage rollout hashes tenant deterministically across invocations.
7. Cross-Tenant Protection: Tenant caller attempting to evaluate feature flags for a foreign tenant is blocked (403).
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
    PlatformFeatureFlag,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP072PlatformFeatureFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p072_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{phone_suffix}",
                "is_active": True,
            },
        )

        # 2. Tenant A (Dhaka Pharmacy, BASIC Tier)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-dhaka-{cls.suffix}",
            name=f"Dhaka Central Pharmacy {cls.suffix}",
            subdomain=f"dhaka-{cls.suffix}",
            plan_tier="BASIC",
            status="ACTIVE",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"b-dhaka-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Dhanmondi Branch",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p072_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"p072_cashier_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Cashier {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )

        # 3. Tenant B (Chittagong Pharmacy, PRO Tier)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-ctg-{cls.suffix}",
            name=f"Chittagong Port Pharmacy {cls.suffix}",
            subdomain=f"ctg-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
        )
        cls.branch_b = Branch.objects.create(
            branch_id=f"b-ctg-{cls.suffix}",
            tenant=cls.tenant_b,
            name="Agrabad Branch",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p072_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Chittagong Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_b.tenant_id,
            branch_id=cls.branch_b.branch_id,
            is_active=True,
        )

    def setUp(self):
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(user=self.superadmin)

        self.client_owner_a = APIClient()
        self.client_owner_a.force_authenticate(user=self.owner_a)

        self.client_cashier_a = APIClient()
        self.client_cashier_a.force_authenticate(user=self.cashier_a)

        self.client_owner_b = APIClient()
        self.client_owner_b.force_authenticate(user=self.owner_b)

        self.client_anon = APIClient()

    def test_01_superadmin_can_create_and_list_feature_flags(self):
        flag_key = f"ff_test_checkout_{self.suffix}"
        payload = {
            "flag_key": flag_key,
            "name": "POS Quick Checkout V2",
            "description": "High performance barcode scan checkout screen",
            "is_enabled_globally": True,
            "default_enabled": False,
            "rollout_percentage": 0,
            "min_plan_tier": "BASIC",
            "is_kill_switch_active": False,
        }

        # Create
        res = self.client_admin.post("/api/v2/platform/feature-flags/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data["flag_key"], flag_key)
        self.assertTrue(res.data["is_enabled_globally"])

        # List
        res_list = self.client_admin.get("/api/v2/platform/feature-flags/")
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        data = res_list.data
        if isinstance(data, dict):
            keys = [item["flag_key"] for item in data.get("results", [])]
        else:
            keys = [item["flag_key"] for item in data]
        self.assertIn(flag_key, keys)

    def test_02_non_platform_role_cannot_manage_feature_flags(self):
        flag_key = f"ff_forbidden_{self.suffix}"
        payload = {
            "flag_key": flag_key,
            "name": "Forbidden Mutation",
            "is_enabled_globally": True,
        }

        # Store owner blocked
        res_owner = self.client_owner_a.post("/api/v2/platform/feature-flags/", payload, format="json")
        self.assertEqual(res_owner.status_code, status.HTTP_403_FORBIDDEN)

        # Cashier blocked
        res_cashier = self.client_cashier_a.post("/api/v2/platform/feature-flags/", payload, format="json")
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)

        # Anonymous blocked
        res_anon = self.client_anon.post("/api/v2/platform/feature-flags/", payload, format="json")
        self.assertIn(res_anon.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_03_kill_switch_lifecycle_and_absolute_override(self):
        flag_key = f"ff_kill_test_{self.suffix}"
        PlatformFeatureFlag.objects.create(
            flag_key=flag_key,
            name="Emergency Kill Test Flag",
            is_enabled_globally=True,
            default_enabled=True,
            min_plan_tier="BASIC",
            is_kill_switch_active=False,
            updated_by=self.superadmin,
        )

        # Pre-check: Tenant A evaluates flag -> enabled
        eval_pre = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertEqual(eval_pre.status_code, status.HTTP_200_OK)
        self.assertTrue(eval_pre.data["data"]["is_enabled"])
        self.assertEqual(eval_pre.data["data"]["reason"], "GLOBAL_ENABLED")

        # 1. SuperAdmin activates kill switch
        kill_res = self.client_admin.post(
            f"/api/v2/platform/feature-flags/{flag_key}/kill-switch/",
            {"is_kill_switch_active": True, "reason": "Immediate security patch rollout"},
            format="json",
        )
        self.assertEqual(kill_res.status_code, status.HTTP_200_OK)
        self.assertTrue(kill_res.data["data"]["is_kill_switch_active"])

        # Post-check: Tenant A evaluates flag -> forcefully disabled
        eval_post = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertEqual(eval_post.status_code, status.HTTP_200_OK)
        self.assertFalse(eval_post.data["data"]["is_enabled"])
        self.assertEqual(eval_post.data["data"]["reason"], "KILL_SWITCH_ACTIVE")

        # 2. Reset kill switch
        reset_res = self.client_admin.post(
            f"/api/v2/platform/feature-flags/{flag_key}/kill-switch/",
            {"is_kill_switch_active": False, "reason": "Security patch validated"},
            format="json",
        )
        self.assertEqual(reset_res.status_code, status.HTTP_200_OK)
        self.assertFalse(reset_res.data["data"]["is_kill_switch_active"])

        # Re-check: Tenant A evaluates flag -> restored to enabled
        eval_restored = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertTrue(eval_restored.data["data"]["is_enabled"])
        self.assertEqual(eval_restored.data["data"]["reason"], "GLOBAL_ENABLED")

    def test_04_tenant_override_lifecycle_and_cross_isolation(self):
        flag_key = f"ff_beta_pilot_{self.suffix}"
        PlatformFeatureFlag.objects.create(
            flag_key=flag_key,
            name="Beta Pilot Feature",
            is_enabled_globally=False,
            default_enabled=False,
            min_plan_tier="BASIC",
            updated_by=self.superadmin,
        )

        # Tenant A initially disabled
        eval_a = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertFalse(eval_a.data["data"]["is_enabled"])

        # SuperAdmin sets override specifically for Tenant A
        override_res = self.client_admin.post(
            f"/api/v2/platform/feature-flags/{flag_key}/tenant-override/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "is_enabled": True,
                "notes": "Enrolled in early access pilot cohort",
            },
            format="json",
        )
        self.assertEqual(override_res.status_code, status.HTTP_200_OK)
        self.assertTrue(override_res.data["data"]["is_enabled"])

        # Tenant A now gets enabled via TENANT_OVERRIDE
        eval_a_after = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertTrue(eval_a_after.data["data"]["is_enabled"])
        self.assertEqual(eval_a_after.data["data"]["reason"], "TENANT_OVERRIDE")

        # Tenant B remains isolated and disabled!
        eval_b = self.client_owner_b.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertFalse(eval_b.data["data"]["is_enabled"])
        self.assertEqual(eval_b.data["data"]["reason"], "SAFE_DEFAULT")

        # Remove override for Tenant A
        del_override = self.client_admin.delete(
            f"/api/v2/platform/feature-flags/{flag_key}/tenant-override/{self.tenant_a.tenant_id}/"
        )
        self.assertEqual(del_override.status_code, status.HTTP_200_OK)

        # Tenant A reverts to disabled
        eval_a_revert = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertFalse(eval_a_revert.data["data"]["is_enabled"])

    def test_05_plan_tier_gating(self):
        flag_key = f"ff_pro_feature_{self.suffix}"
        PlatformFeatureFlag.objects.create(
            flag_key=flag_key,
            name="Pro Multi-Branch Analytics",
            is_enabled_globally=True,
            default_enabled=False,
            min_plan_tier="PRO",
            updated_by=self.superadmin,
        )

        # Tenant A (BASIC) cannot access PRO feature
        res_a = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertEqual(res_a.status_code, status.HTTP_200_OK)
        self.assertFalse(res_a.data["data"]["is_enabled"])
        self.assertEqual(res_a.data["data"]["reason"], "PLAN_TIER_INSUFFICIENT")

        # Tenant B (PRO) can access PRO feature
        res_b = self.client_owner_b.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertEqual(res_b.status_code, status.HTTP_200_OK)
        self.assertTrue(res_b.data["data"]["is_enabled"])
        self.assertEqual(res_b.data["data"]["reason"], "GLOBAL_ENABLED")

    def test_06_deterministic_rollout_and_evaluation_all(self):
        flag_key = f"ff_staged_rollout_{self.suffix}"
        PlatformFeatureFlag.objects.create(
            flag_key=flag_key,
            name="Staged Rollout Feature",
            is_enabled_globally=False,
            default_enabled=False,
            rollout_percentage=50,
            min_plan_tier="BASIC",
            updated_by=self.superadmin,
        )

        # Evaluate multiple times for Tenant A -> Must be identical (deterministic)
        res1 = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        res2 = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/{flag_key}/")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(res1.data["data"]["is_enabled"], res2.data["data"]["is_enabled"])
        self.assertEqual(res1.data["data"]["reason"], res2.data["data"]["reason"])

        # Evaluate all flags for Tenant A
        res_all = self.client_owner_a.get("/api/v2/tenants/feature-flags/eval/")
        self.assertEqual(res_all.status_code, status.HTTP_200_OK)
        self.assertIn("flags", res_all.data["data"])
        self.assertIn(flag_key, res_all.data["data"]["flags"])

    def test_07_cross_tenant_evaluation_violation_raises_error(self):
        # Tenant A user attempts to query flags for Tenant B
        res = self.client_owner_a.get(f"/api/v2/tenants/feature-flags/eval/?tenant_id={self.tenant_b.tenant_id}")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Cross-tenant", str(res.data))


if __name__ == "__main__":
    unittest.main()
