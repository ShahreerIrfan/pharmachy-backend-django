"""
backend/tests/test_p080_tenant_lifecycle_e2e.py

Architecture Decision: ADR-0080
Comprehensive End-to-End Tenant Lifecycle Test Suite:
1. Valid scenario: Complete lifecycle progression:
   Provisioning (TRIAL) -> POS Checkout allowed -> Paid Activation (ACTIVE) ->
   Commercial Plan Change (STANDARD -> PRO, quotas updated) ->
   Suspension (Lockout: POS Checkout blocked) -> Reactivation (POS Checkout re-enabled) ->
   Timeline audit inspection.
2. Incomplete/invalid inputs: Invalid plan tier, short reason rejected (400/422).
3. Unauthorized & Two-Tenant Isolation: Tenant A owner blocked from managing Tenant B (403).
4. Immutability protection: Destructive hard deletion prohibited across all personas (403).
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

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    PlanTier,
    SubscriptionRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP080TenantLifecycleEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform SuperAdmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p080_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "mfa_enabled": True,
            },
        )

        # 2. Tenant A & Owner A
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-p080-a-{cls.suffix}",
            name=f"Tenant A {cls.suffix}",
            subdomain=f"life-a-{cls.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p080_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_a.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # 3. Tenant B & Owner B
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-p080-b-{cls.suffix}",
            name=f"Tenant B {cls.suffix}",
            subdomain=f"life-b-{cls.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p080_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            tenant_id=cls.tenant_b.tenant_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
            mfa_enabled=True,
        )

        # 4. Global Medicine Product for checkout test
        cls.medicine = MedicineProduct.objects.filter(brand_name="Napa 500mg").first()
        if not cls.medicine:
            cls.medicine = MedicineProduct.objects.create(
                product_id=f"med-p080-{cls.suffix}",
                brand_name="Napa 500mg",
                generic_name="Paracetamol",
                dosage_form="Tablet",
                strength="500mg",
                manufacturer="Beximco Pharmaceuticals Ltd",
                mrp_unit=Decimal("1.20"),
                cost_unit=Decimal("0.85"),
                dgda_dar_number="DGDA-DAR-00124",
                is_prescription_only=False,
            )

    def setUp(self):
        self.client = APIClient()

    def test_01_complete_lifecycle_progression(self):
        """
        Valid end-to-end lifecycle:
        Provision TRIAL -> Checkout permitted -> ACTIVATE -> Checkout permitted ->
        PLAN CHANGE (STANDARD -> PRO) -> SUSPEND -> Checkout BLOCKED ->
        RESTORE -> Checkout permitted -> Timeline contains all audit events.
        """
        self.client.force_authenticate(user=self.superadmin)
        subdomain = f"test-pharma-{self.suffix}"
        target_tenant_id = f"tn-{subdomain}"

        # 1. Provision tenant in TRIAL mode
        prov_result = TenantProvisioningEngine.provision_tenant(
            tenant_id=target_tenant_id,
            name=f"Lifecycle Pharmacy {self.suffix}",
            subdomain=subdomain,
            owner_email=f"owner_{self.suffix}@testpharma.local",
            owner_phone=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            owner_name="Test Owner",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Head Branch",
            branch_license="DGDA-DL-P080",
            is_trial=True,
            operator_id=str(self.superadmin.user_id),
        )
        self.assertEqual(prov_result["status"], TenantLifecycleStatus.TRIAL)

        tenant = Tenant.objects.get(tenant_id=target_tenant_id)
        branch = Branch.objects.get(branch_id=f"{target_tenant_id}-br-main")

        # Seed available batch for checkout
        batch = BatchStock.objects.create(
            batch_id=f"bat-p080-{self.suffix}",
            tenant=tenant,
            branch=branch,
            product=self.medicine,
            batch_number=f"LOT-P080-{self.suffix}",
            quantity_base_units=100,
            expiry_date="2027-12-31",
            purchase_date="2026-01-01",
            cost_per_unit=Decimal("0.85"),
            status=BatchStatus.ACTIVE,
        )

        checkout_payload = {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "customer_name": "Walk-in Customer",
            "customer_phone": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            "payment_method": "CASH",
            "items": [
                {
                    "batch_id": batch.batch_id,
                    "product_id": self.medicine.product_id,
                    "quantity": 2,
                    "unit_price": "1.20",
                }
            ],
        }

        # 2. Checkout during TRIAL succeeds
        trial_checkout_resp = self.client.post(
            "/api/v2/invoices/checkout/",
            data=checkout_payload,
            format="json",
        )
        self.assertEqual(trial_checkout_resp.status_code, status.HTTP_201_CREATED, trial_checkout_resp.data)

        # 3. Super Admin activates subscription (TRIAL -> ACTIVE)
        activate_resp = self.client.post(
            f"/api/v2/tenants/{tenant.tenant_id}/activate-subscription/",
            data={
                "plan_tier": "STANDARD",
                "billing_cycle": "MONTHLY",
                "payment_reference": "TRX-BKASH-881920",
                "notes": "Verified initial commercial activation payment.",
            },
            format="json",
        )
        self.assertEqual(activate_resp.status_code, status.HTTP_200_OK, activate_resp.data)
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, TenantLifecycleStatus.ACTIVE)
        self.assertEqual(tenant.plan_tier, PlanTier.STANDARD)

        sub = SubscriptionRecord.objects.get(tenant=tenant)
        self.assertEqual(sub.amount_bdt, Decimal("6500.00"))
        self.assertEqual(sub.licensed_branches, 5)
        self.assertEqual(sub.licensed_devices, 10)

        # 4. Checkout during ACTIVE succeeds
        active_checkout_resp = self.client.post(
            "/api/v2/invoices/checkout/",
            data=checkout_payload,
            format="json",
        )
        self.assertEqual(active_checkout_resp.status_code, status.HTTP_201_CREATED, active_checkout_resp.data)

        # 5. Plan Change (STANDARD -> PRO, quotas updated)
        plan_resp = self.client.post(
            f"/api/v2/tenants/{tenant.tenant_id}/change-plan/",
            data={
                "plan_tier": "PRO",
                "billing_cycle": "MONTHLY",
                "reason": "Customer expanded into multi-branch operations.",
            },
            format="json",
        )
        self.assertEqual(plan_resp.status_code, status.HTTP_200_OK, plan_resp.data)
        tenant.refresh_from_db()
        self.assertEqual(tenant.plan_tier, PlanTier.PRO)

        sub.refresh_from_db()
        self.assertEqual(sub.amount_bdt, Decimal("15000.00"))
        self.assertEqual(sub.licensed_branches, 25)
        self.assertEqual(sub.licensed_devices, 50)

        # 6. Suspend tenant (Lockout)
        suspend_resp = self.client.post(
            f"/api/v2/tenants/{tenant.tenant_id}/suspend/",
            data={
                "reason": "Administrative suspension pending DGDA license verification renewal.",
                "reason_code": "COMPLIANCE_BREACH",
            },
            format="json",
        )
        self.assertEqual(suspend_resp.status_code, status.HTTP_200_OK, suspend_resp.data)
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, TenantLifecycleStatus.SUSPENDED)
        self.assertFalse(tenant.is_checkout_allowed())

        # 7. Checkout during SUSPENDED is strictly BLOCKED
        blocked_checkout_resp = self.client.post(
            "/api/v2/invoices/checkout/",
            data=checkout_payload,
            format="json",
        )
        # Should be blocked with 403 or permission denied error
        self.assertIn(
            blocked_checkout_resp.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY],
        )

        # 8. Reactivate / Restore tenant
        restore_resp = self.client.post(
            f"/api/v2/tenants/{tenant.tenant_id}/restore/",
            data={"reason": "DGDA license renewal confirmed and approved."},
            format="json",
        )
        self.assertEqual(restore_resp.status_code, status.HTTP_200_OK, restore_resp.data)
        tenant.refresh_from_db()
        self.assertEqual(tenant.status, TenantLifecycleStatus.ACTIVE)
        self.assertTrue(tenant.is_checkout_allowed())

        # 9. Checkout post-restoration succeeds again
        restored_checkout_resp = self.client.post(
            "/api/v2/invoices/checkout/",
            data=checkout_payload,
            format="json",
        )
        self.assertEqual(restored_checkout_resp.status_code, status.HTTP_201_CREATED, restored_checkout_resp.data)

        # 10. Audit timeline inspection
        timeline_resp = self.client.get(f"/api/v2/tenants/{tenant.tenant_id}/lifecycle-timeline/")
        self.assertEqual(timeline_resp.status_code, status.HTTP_200_OK, timeline_resp.data)
        timeline_actions = [e["action"] for e in timeline_resp.data["data"]]
        self.assertIn("TENANT_PROVISIONED", timeline_actions)
        self.assertIn("TENANT_SUBSCRIPTION_ACTIVATED", timeline_actions)
        self.assertIn("TENANT_PLAN_TIER_CHANGED", timeline_actions)
        self.assertIn("TENANT_SUSPENDED", timeline_actions)
        self.assertIn("TENANT_RESTORED", timeline_actions)

    def test_02_invalid_and_incomplete_lifecycle_inputs(self):
        """
        Invalid inputs: Invalid plan tier or reason < 5 chars rejected.
        """
        self.client.force_authenticate(user=self.superadmin)

        # Invalid plan tier
        resp1 = self.client.post(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/change-plan/",
            data={"plan_tier": "INVALID_TIER", "reason": "Valid reason explanation"},
            format="json",
        )
        self.assertIn(resp1.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Short reason (< 5 chars)
        resp2 = self.client.post(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/change-plan/",
            data={"plan_tier": "PRO", "reason": "no"},
            format="json",
        )
        self.assertIn(resp2.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Short suspension reason
        resp3 = self.client.post(
            f"/api/v2/tenants/{self.tenant_a.tenant_id}/suspend/",
            data={"reason": "bad"},
            format="json",
        )
        self.assertIn(resp3.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_03_unauthorized_and_two_tenant_isolation(self):
        """
        Two-Tenant Isolation: Tenant Owner A cannot manage or inspect Tenant B.
        """
        self.client.force_authenticate(user=self.owner_a)

        # Tenant Owner A attempting to suspend Tenant B
        resp1 = self.client.post(
            f"/api/v2/tenants/{self.tenant_b.tenant_id}/suspend/",
            data={"reason": "Malicious attempt to suspend competitor."},
            format="json",
        )
        self.assertEqual(resp1.status_code, status.HTTP_403_FORBIDDEN)

        # Tenant Owner A attempting to view Tenant B's lifecycle timeline
        resp2 = self.client.get(
            f"/api/v2/tenants/{self.tenant_b.tenant_id}/lifecycle-timeline/",
        )
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)

        # Verify cross-tenant security incident audit log was recorded
        incident = AuditLogEntry.objects.filter(
            action="SECURITY_INCIDENT_CROSS_TENANT",
            tenant_id=self.tenant_a.tenant_id,
        ).first()
        self.assertIsNotNone(incident)

    def test_04_immutability_prohibition_on_hard_deletion(self):
        """
        Immutability gate: Hard DELETE is blocked with 403 ABSOLUTE_SYSTEM_BLOCK across all roles.
        """
        # Super Admin cannot hard-delete
        self.client.force_authenticate(user=self.superadmin)
        resp = self.client.delete(f"/api/v2/tenants/{self.tenant_a.tenant_id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data["error_code"], "ABSOLUTE_SYSTEM_BLOCK")

        # Tenant Owner cannot hard-delete
        self.client.force_authenticate(user=self.owner_a)
        resp_owner = self.client.delete(f"/api/v2/tenants/{self.tenant_a.tenant_id}/")
        self.assertEqual(resp_owner.status_code, status.HTTP_403_FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
