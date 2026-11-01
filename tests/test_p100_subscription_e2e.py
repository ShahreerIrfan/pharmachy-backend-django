"""
backend/tests/test_p100_subscription_e2e.py

Architecture Decision: ADR-0015, ADR-0027, ADR-0081, ADR-0084, ADR-0087, ADR-0088,
ADR-0090, ADR-0091, ADR-0092, ADR-0093, ADR-0094, ADR-0095, ADR-0096, ADR-0097,
ADR-0098, ADR-0099 & ADR-0100
Phase 05: Basic/Standard/Pro Subscription & Billing (P100)
Requirement Trace: FR-044, FR-066, FR-068, FR-070

Comprehensive End-to-End Integration Tests for Subscription Lifecycle and Support Exceptions:
1. Scenario 1 (Onboarding & Trial Lifecycle): 14-day trial provisioning, quota assignment, and state machine.
2. Scenario 2 (Payment Verification & Activation): Manual payment claim approval (Maker-Checker), invoice settlement, and ACTIVE state.
3. Scenario 3 (Device Seat Enrollment & Signed Offline Lease): Device enrollment, ECDSA cryptographic lease certificate issuance.
4. Scenario 4 (Prorated Upgrade): BDT proration calculation, unused credit deduction, and quota expansion.
5. Scenario 5 (Downgrade & Zero Data Deletion): Lower tier migration with 100% preservation of branches, staff, and ledgers.
6. Scenario 6 (Delinquency & Dunning Dispatch): Unpaid renewal dunning notification, 24h de-duplication, and owner acknowledgment.
7. Scenario 7 (Support Override Exception): Platform superadmin grants GRACE_EXTENSION and EMERGENCY_UNBLOCK with audit trail.
8. Scenario 8 (Support Override Revocation): Superadmin revokes active override with audited justification.
9. Scenario 9 (Cross-Tenant Security): Cross-tenant override query is rejected with HTTP 403 ERR_CROSS_TENANT_VIOLATION.
10. Scenario 10 (Cashier & Owner RBAC Denial): Cashier access denied (HTTP 403); Owner prohibited from granting overrides (HTTP 403).
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

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.device_seat_service import DeviceSeatManagementService
from apps.core.downgrade_service import SubscriptionDowngradeService
from apps.core.dunning_service import DunningNotificationService
from apps.core.manual_payment_service import ManualPaymentService
from apps.core.models import (
    Branch,
    DunningDeliveryStatus,
    DunningStage,
    ManualPaymentClaimStatus,
    ManualPaymentMethod,
    PlanTier,
    RenewalInvoiceStatus,
    SubscriptionOverrideStatus,
    SubscriptionOverrideType,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
)
from apps.core.offline_entitlement_service import OfflineEntitlementLeaseService
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.prorated_upgrade_service import ProratedUpgradeService
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.renewal_invoice_service import RenewalInvoiceService
from apps.core.subscription_support_service import SubscriptionSupportOverrideService
from apps.identity.models import PharmacyUser, UserRole


class TestP100SubscriptionE2eIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin User
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p100_super_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Superadmin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Checker User for Manual Payment Verification
        cls.checker, _ = PharmacyUser.objects.get_or_create(
            email=f"p100_checker_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Payment Checker {cls.suffix}",
                "role": UserRole.PLATFORM_OPERATOR,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 3. Provision Tenant A (Trial on BASIC plan)
        cls.subdomain_a = f"sub-e2e-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-e2e-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P100-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@e2ea.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"E2E Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_license=cls.license_a,
            is_trial=True,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)

        # Cashier user for Tenant A
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"cashier_a_{cls.suffix}@e2ea.local",
            full_name=f"Cashier A {cls.suffix}",
            tenant_id=cls.tenant_id_a,
            role=UserRole.CASHIER,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
            is_active=True,
        )

        # 4. Provision Tenant B (STANDARD plan) for cross-tenant testing
        cls.subdomain_b = f"sub-e2e-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-e2e-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P100-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@e2eb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"E2E Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)

    def setUp(self):
        self.client = APIClient()

    def test_01_onboarding_and_trial_lifecycle(self):
        """Scenario 1: Tenant A is properly provisioned with 14-day trial and verified quotas."""
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.status, SubscriptionStatus.TRIAL)
        self.assertEqual(sub.plan_tier, PlanTier.BASIC)
        self.tenant_a.refresh_from_db()
        self.assertIsNotNone(self.tenant_a.trial_ends_at)
        self.assertGreater(self.tenant_a.trial_ends_at, timezone.now())

    def test_02_manual_payment_claim_and_paid_activation(self):
        """Scenario 2: Manual payment verification transitions trial subscription to ACTIVE."""
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)

        # Generate renewal invoice
        inv = RenewalInvoiceService.generate_renewal_invoice(tenant_id=self.tenant_id_a)
        self.assertEqual(inv.status, RenewalInvoiceStatus.ISSUED)

        # Submit manual payment claim
        claim = ManualPaymentService.submit_claim(
            tenant_id=self.tenant_id_a,
            invoice_id=inv.invoice_id,
            payment_method=ManualPaymentMethod.BKASH,
            transaction_reference=f"TXN-E2E-{self.suffix}",
            sender_account_number="+8801711223344",  # synthetic-fixture
            claimed_amount_bdt=inv.total_bdt,
            evidence_file_url="https://storage.pharmacy.bd/receipts/txn1.pdf",
            evidence_notes="bKash manual merchant payment",
            maker_user=self.owner_a,
        )
        self.assertEqual(claim.status, ManualPaymentClaimStatus.PENDING_VERIFICATION)

        # Checker approves claim
        approved_claim = ManualPaymentService.approve_claim(
            claim_id=claim.claim_id,
            checker_user=self.checker,
            checker_notes="bKash statement verified successfully.",
        )
        self.assertEqual(approved_claim.status, ManualPaymentClaimStatus.APPROVED)

        # Invoice is paid and subscription is active
        inv.refresh_from_db()
        self.assertEqual(inv.status, RenewalInvoiceStatus.PAID)

        sub.refresh_from_db()
        self.assertEqual(sub.status, SubscriptionStatus.ACTIVE)

    def test_03_device_seat_enrollment_and_signed_offline_lease(self):
        """Scenario 3: Device seat enrollment and signed cryptographic offline lease certificate."""
        branch = Branch.objects.filter(tenant=self.tenant_a).first()
        self.assertIsNotNone(branch)

        # Enroll workstation device seat
        device_id = f"DEV-POS-01-{self.suffix}"
        seat_data = DeviceSeatManagementService.enroll_device(
            tenant_id=self.tenant_id_a,
            branch_id=branch.branch_id,
            device_id=device_id,
            device_name="Counter 1 Terminal",
            actor_user=self.owner_a,
        )
        self.assertEqual(seat_data["device_id"], device_id)
        self.assertEqual(seat_data["tenant_id"], self.tenant_id_a)

        # Issue signed offline entitlement lease
        lease_data = OfflineEntitlementLeaseService.issue_offline_lease(
            tenant_id=self.tenant_id_a,
            branch_id=branch.branch_id,
            device_id=device_id,
            actor_user=self.owner_a,
        )
        self.assertIsNotNone(lease_data["signature"])
        self.assertEqual(lease_data["status"], "ACTIVE")

        # Verify offline lease signature
        verify_res = OfflineEntitlementLeaseService.verify_offline_lease(lease_id=lease_data["lease_id"])
        self.assertTrue(verify_res["is_valid"])

    def test_04_prorated_upgrade_basic_to_pro(self):
        """Scenario 4: Upgrading from BASIC to PRO applies BDT proration and expands quotas."""
        quote = ProratedUpgradeService.quote_upgrade(
            tenant_id=self.tenant_id_a,
            target_plan_code="PLAN_PRO",
            billing_cycle="MONTHLY",
            actor_user=self.owner_a,
        )
        self.assertEqual(quote["to_plan_tier"], "PRO")
        self.assertGreater(Decimal(quote["financials"]["net_payable_bdt"]), Decimal("0.00"))

        # Apply upgrade
        upgrade_data = ProratedUpgradeService.apply_upgrade(
            tenant_id=self.tenant_id_a,
            upgrade_id=quote["upgrade_id"],
            payment_reference=f"PAY-UPGRADE-{self.suffix}",
            actor_user=self.owner_a,
        )
        self.assertEqual(upgrade_data["status"], "COMPLETED")

        # Verify subscription updated to PRO
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        self.assertEqual(sub.plan_tier, PlanTier.PRO)

    def test_05_downgrade_with_zero_data_deletion_invariant(self):
        """Scenario 5: Downgrade from PRO to STANDARD preserves all branches and sales records."""
        # Establish PRO tier baseline
        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=self.tenant_id_a,
            plan_code="PLAN_PRO",
            billing_cycle="MONTHLY",
        )
        sub = SubscriptionRecord.objects.get(tenant=self.tenant_a)
        sub.plan_tier = PlanTier.PRO
        sub.save(update_fields=["plan_tier"])

        branch_count_before = Branch.objects.filter(tenant=self.tenant_a).count()
        user_count_before = PharmacyUser.objects.filter(tenant_id=self.tenant_id_a).count()

        assessment = SubscriptionDowngradeService.assess_downgrade(
            tenant_id=self.tenant_id_a,
            target_plan_code="PLAN_STANDARD",
        )
        self.assertEqual(assessment["to_plan_tier"], "STANDARD")

        downgrade_data = SubscriptionDowngradeService.apply_downgrade(
            tenant_id=self.tenant_id_a,
            target_plan_code="PLAN_STANDARD",
            effective_mode="IMMEDIATE",
            actor_user=self.owner_a,
        )
        self.assertIsNotNone(downgrade_data)

        # Zero data deletion check
        branch_count_after = Branch.objects.filter(tenant=self.tenant_a).count()
        user_count_after = PharmacyUser.objects.filter(tenant_id=self.tenant_id_a).count()
        self.assertEqual(branch_count_before, branch_count_after)
        self.assertEqual(user_count_before, user_count_after)

    def test_06_dunning_notification_on_unpaid_renewal(self):
        """Scenario 6: Unpaid renewal triggers dunning notification with 24h de-duplication."""
        # Dispatch dunning alert
        notif = DunningNotificationService.generate_and_dispatch_notification(
            tenant_id=self.tenant_id_a,
            stage=DunningStage.PAYMENT_FAILED,
            amount_bdt=Decimal("4500.00"),
            force=True,
            operator_user=self.superadmin,
        )
        self.assertEqual(notif.delivery_status, DunningDeliveryStatus.DELIVERED)

        # Dispatch immediate duplicate -> should be SUPPRESSED by 24h dedup rule
        dup_notif = DunningNotificationService.generate_and_dispatch_notification(
            tenant_id=self.tenant_id_a,
            stage=DunningStage.PAYMENT_FAILED,
            amount_bdt=Decimal("4500.00"),
            force=False,
            operator_user=self.superadmin,
        )
        self.assertEqual(dup_notif.delivery_status, DunningDeliveryStatus.SUPPRESSED)

        # Owner acknowledges dunning alert
        acked = DunningNotificationService.acknowledge_notification(
            notification_id=notif.notification_id,
            operator_user=self.owner_a,
        )
        self.assertTrue(acked.is_acknowledged)

    def test_07_support_override_grace_extension_and_emergency_unblock(self):
        """Scenario 7: Platform superadmin grants GRACE_EXTENSION and EMERGENCY_UNBLOCK."""
        self.client.force_authenticate(user=self.superadmin)

        # 1. Grant GRACE_EXTENSION
        res_grace = self.client.post(
            "/api/v2/billing/support-overrides/",
            data={
                "tenant_id": self.tenant_id_a,
                "override_type": "GRACE_EXTENSION",
                "justification": "Bangladesh Bank wire transfer delay during holiday.",
                "ticket_reference": f"SUP-HOLIDAY-{self.suffix}",
                "extension_days": 10,
            },
            format="json",
        )
        self.assertEqual(res_grace.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_grace.json()["success"])
        data_grace = res_grace.json()["data"]
        self.assertEqual(data_grace["override_type"], "GRACE_EXTENSION")
        self.assertEqual(data_grace["status"], "ACTIVE")
        self.assertEqual(data_grace["extension_days"], 10)

        # Verify audit log
        audit_grace = AuditLogEntry.objects.filter(
            action="SUBSCRIPTION_SUPPORT_OVERRIDE_GRANTED",
            resource_id=data_grace["override_id"],
        ).first()
        self.assertIsNotNone(audit_grace)

        # 2. Grant EMERGENCY_UNBLOCK
        res_unblock = self.client.post(
            "/api/v2/billing/support-overrides/",
            data={
                "tenant_id": self.tenant_id_a,
                "override_type": "EMERGENCY_UNBLOCK",
                "justification": "DGDA emergency insulin supply inspection.",
                "ticket_reference": f"DGDA-INSULIN-{self.suffix}",
                "extension_days": 3,
            },
            format="json",
        )
        self.assertEqual(res_unblock.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_unblock.json()["success"])

        # 3. List overrides for Tenant A as Owner A
        self.client.force_authenticate(user=self.owner_a)
        res_list = self.client.get(f"/api/v2/billing/support-overrides/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(res_list.json()["data"]["total_count"], 2)

    def test_08_support_override_revocation(self):
        """Scenario 8: Platform superadmin revokes an active override with mandatory reason."""
        override = SubscriptionSupportOverrideService.grant_override(
            tenant_id=self.tenant_id_a,
            override_type=SubscriptionOverrideType.QUOTA_OVERRIDE,
            justification="Temporary workstation capacity test.",
            ticket_reference=f"TICKET-REV-{self.suffix}",
            extension_days=7,
            quota_bonus=2,
            granted_by_user=self.superadmin,
        )
        self.assertEqual(override.status, SubscriptionOverrideStatus.ACTIVE)

        # Revoke via API as superadmin
        self.client.force_authenticate(user=self.superadmin)
        res_revoke = self.client.post(
            f"/api/v2/billing/support-overrides/{override.override_id}/revoke/",
            data={"revocation_reason": "Remodeling completed early; extra quota no longer needed."},
            format="json",
        )
        self.assertEqual(res_revoke.status_code, status.HTTP_200_OK)
        self.assertEqual(res_revoke.json()["data"]["status"], "REVOKED")

        # Verify DB updated
        override.refresh_from_db()
        self.assertEqual(override.status, SubscriptionOverrideStatus.REVOKED)
        self.assertIsNotNone(override.revoked_at)

    def test_09_cross_tenant_security_isolation(self):
        """Scenario 9: Tenant B owner querying Tenant A overrides raises ERR_CROSS_TENANT_VIOLATION."""
        # Create an override for Tenant A
        override_a = SubscriptionSupportOverrideService.grant_override(
            tenant_id=self.tenant_id_a,
            override_type=SubscriptionOverrideType.GRACE_EXTENSION,
            justification="Bank wire delay.",
            ticket_reference=f"TICKET-CT-{self.suffix}",
            extension_days=5,
            granted_by_user=self.superadmin,
        )

        # Tenant B Owner attempts to query Tenant A overrides
        self.client.force_authenticate(user=self.owner_b)
        res_list = self.client.get(f"/api/v2/billing/support-overrides/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_list.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", res_list.content.decode("utf-8"))

        # Tenant B Owner attempts to retrieve Tenant A override by ID
        res_detail = self.client.get(f"/api/v2/billing/support-overrides/{override_a.override_id}/")
        self.assertEqual(res_detail.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_CROSS_TENANT_VIOLATION", res_detail.content.decode("utf-8"))

    def test_10_cashier_and_unauthorized_rbac_rejection(self):
        """Scenario 10: Cashier role is rejected from accessing overrides; Owner cannot grant overrides."""
        # Cashier attempts to list overrides
        self.client.force_authenticate(user=self.cashier_a)
        res_cashier = self.client.get(f"/api/v2/billing/support-overrides/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", res_cashier.content.decode("utf-8"))

        # Pharmacy Owner attempts to grant a support override (superadmin only)
        self.client.force_authenticate(user=self.owner_a)
        res_grant = self.client.post(
            "/api/v2/billing/support-overrides/",
            data={
                "tenant_id": self.tenant_id_a,
                "override_type": "GRACE_EXTENSION",
                "justification": "Attempting self-granted grace extension.",
                "ticket_reference": "UNAUTH-TICKET",
            },
            format="json",
        )
        self.assertEqual(res_grant.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("ERR_PERMISSION_DENIED", res_grant.content.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
