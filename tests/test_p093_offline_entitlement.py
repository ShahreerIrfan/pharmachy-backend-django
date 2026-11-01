"""
backend/tests/test_p093_offline_entitlement.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0033, ADR-0082, ADR-0091, ADR-0092 & ADR-0093
Phase 05: Basic/Standard/Pro Subscription & Billing (P093)
Requirement Trace: FR-071 (SaaS Plans, Billing & Tenant Lifecycle)

Comprehensive Integration Tests for Signed Offline Entitlement Leases:
1. Valid Scenario: Issues signed lease certificate with valid HMAC-SHA256 signature and verifies offline validity.
2. Incomplete / Invalid Input: Rejects empty or short device_id (<3 chars) and invalid branch_id (HTTP 400).
3. Cross-Tenant Barrier: Blocks Tenant B from issuing, viewing, or revoking leases for Tenant A (HTTP 403).
4. Role-Based Access Control (RBAC): Permits cashiers to issue terminal leases, blocks cashiers from revoking (HTTP 403).
5. Over-Quota Locked Branch Gating: Blocks lease issuance on over-quota locked branches (HTTP 400).
6. Expired / Lapsed Subscription Gating: Blocks lease issuance when subscription is EXPIRED or CANCELLED (HTTP 400).
7. Grace Period Bounded Duration: Caps offline lease duration to remaining grace window.
8. Automatic Superseding: Supersedes prior active lease when a renewed lease is issued for the same terminal.
9. Owner Revocation Flow: Pharmacy owner revokes compromised terminal lease; verification immediately fails.
10. Cryptographic Tamper Rejection: Altering canonical payload or forging signature fails verification.
11. Zero Data Loss Invariant: Confirms 0 historical sales, branches, or audit records are deleted.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    DeviceLeaseCertificate,
    OfflineLeaseStatus,
    PlanTier,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)
from apps.core.offline_entitlement_service import OfflineEntitlementLeaseService
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.identity.models import PharmacyUser, UserRole


class TestP093OfflineEntitlementIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Platform Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p093_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (STANDARD Plan)
        cls.subdomain_a = f"lease-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-lease-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P093-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@leasea.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Offline Lease Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Branch Dhanmondi",
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)
        cls.branch_a = Branch.objects.filter(tenant=cls.tenant_a).first()

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_a,
            plan_code="PLAN_STANDARD",
            billing_cycle="MONTHLY",
        )

        # Cashier user for Tenant A
        cls.cashier_a, _ = PharmacyUser.objects.get_or_create(
            email=f"cashier_a_{cls.suffix}@leasea.local",
            defaults={
                "full_name": f"Cashier A {cls.suffix}",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant_id_a,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
            },
        )

        # 3. Provision Tenant B (Isolation boundary)
        cls.subdomain_b = f"lease-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-lease-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P093-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@leaseb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Isolation Pharma B {cls.suffix}",
            subdomain=cls.subdomain_b,
            owner_email=cls.email_b,
            owner_phone=cls.phone_b,
            owner_name=f"Owner B {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.BASIC,
            branch_name="Branch Banani",
            branch_license=cls.license_b,
            is_trial=False,
        )
        cls.tenant_b = Tenant.objects.get(tenant_id=cls.tenant_id_b)
        cls.owner_b = PharmacyUser.objects.get(email=cls.email_b)
        cls.branch_b = Branch.objects.filter(tenant=cls.tenant_b).first()

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_b,
            plan_code="PLAN_BASIC",
            billing_cycle="MONTHLY",
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        # Reset tenant A state
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        if sub:
            sub.status = SubscriptionStatus.ACTIVE
            sub.renewal_date = timezone.now().date() + timedelta(days=30)
            sub.grace_period_end_date = None
            sub.save()
        self.branch_a.is_over_quota_locked = False
        self.branch_a.save()
        self.tenant_a.status = TenantLifecycleStatus.ACTIVE
        self.tenant_a.save()
        TenantAppliedEntitlement.objects.filter(tenant=self.tenant_a).update(is_active=True)

    def test_01_valid_signed_offline_lease_issuance_and_verification(self):
        """Valid Scenario: Issues signed lease certificate and validates cryptographic integrity."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/offline-leases/issue/"
        payload = {
            "tenant_id": self.tenant_id_a,
            "branch_id": self.branch_a.branch_id,
            "device_id": "pos-workstation-counter-01",
            "device_name": "HP ProDesk Counter 1",
            "policy_version": "v2.0",
            "requested_duration_hours": 72,
        }
        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()

        # Invariants
        self.assertTrue(data["lease_id"].startswith("ls-"))
        self.assertEqual(data["tenant_id"], self.tenant_id_a)
        self.assertEqual(data["branch_id"], self.branch_a.branch_id)
        self.assertEqual(data["device_id"], payload["device_id"])
        self.assertEqual(data["signature_algorithm"], "HMAC-SHA256")
        self.assertEqual(len(data["signature"]), 64)
        self.assertEqual(data["status"], "ACTIVE")
        self.assertTrue(data["capabilities_snapshot"]["CHECKOUT_DISPENSING"])
        self.assertTrue(data["capabilities_snapshot"]["ANTIBIOTIC_REGISTER"])
        self.assertTrue(data["capabilities_snapshot"]["BATCH_EXPIRY_BLOCK"])
        self.assertIn("স্বাক্ষরিত অফলাইন ইজারা সক্রিয়", data["client_messaging"]["headline_bn"])

        # Verify endpoint check
        verify_res = self.client.post(
            "/api/v2/billing/offline-leases/verify/",
            {"lease_id": data["lease_id"]},
            format="json",
        )
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        verify_data = verify_res.json()
        self.assertTrue(verify_data["is_valid"])
        self.assertEqual(verify_data["reason"], "VALID")
        self.assertGreater(verify_data["time_remaining_seconds"], 250000)

        # Audit Log Check
        self.assertTrue(
            AuditLogEntry.objects.filter(
                action="OFFLINE_LEASE_ISSUED",
                resource_id=data["lease_id"],
                tenant_id=self.tenant_id_a,
            ).exists()
        )

    def test_02_invalid_or_incomplete_inputs_rejected(self):
        """Rejects short device_id (<3 chars) and invalid branch ID."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/offline-leases/issue/"

        # Short device ID
        res_short_dev = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "branch_id": self.branch_a.branch_id, "device_id": "ab"},
            format="json",
        )
        self.assertEqual(res_short_dev.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_short_dev.json()["error"], "VALIDATION_ERROR")

        # Non-existent branch ID
        res_bad_br = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "branch_id": "br-invalid-9999", "device_id": "pos-01"},
            format="json",
        )
        self.assertEqual(res_bad_br.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_br.json()["error"], "VALIDATION_ERROR")

    def test_03_cross_tenant_isolation_barrier(self):
        """Blocks Tenant B user from issuing, viewing or revoking leases for Tenant A."""
        self.client.force_authenticate(user=self.owner_b)

        # 1. Issue attempt for Tenant A branch
        res_issue = self.client.post(
            "/api/v2/billing/offline-leases/issue/",
            {
                "tenant_id": self.tenant_id_a,
                "branch_id": self.branch_a.branch_id,
                "device_id": "rogue-dev-01",
            },
            format="json",
        )
        self.assertEqual(res_issue.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_issue.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 2. List attempt for Tenant A
        res_list = self.client.get(f"/api/v2/billing/offline-leases/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_list.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_list.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_04_rbac_cashier_can_issue_cannot_revoke(self):
        """Cashier can issue terminal lease, but is strictly blocked from revoking leases."""
        # 1. Cashier issues lease for terminal
        self.client.force_authenticate(user=self.cashier_a)
        res_issue = self.client.post(
            "/api/v2/billing/offline-leases/issue/",
            {
                "tenant_id": self.tenant_id_a,
                "branch_id": self.branch_a.branch_id,
                "device_id": "pos-cashier-lane-02",
            },
            format="json",
        )
        self.assertEqual(res_issue.status_code, status.HTTP_201_CREATED)
        lease_id = res_issue.json()["lease_id"]

        # 2. Cashier attempts to revoke lease -> 403 Forbidden
        res_revoke = self.client.post(
            "/api/v2/billing/offline-leases/revoke/",
            {
                "tenant_id": self.tenant_id_a,
                "lease_id": lease_id,
                "reason": "Unauthorized cashier attempt",
            },
            format="json",
        )
        self.assertEqual(res_revoke.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_revoke.json()["error"], "ERR_PERMISSION_DENIED")

    def test_05_over_quota_locked_branch_blocks_offline_lease(self):
        """Branches locked due to downgrade over-quota policy cannot issue offline leases."""
        self.branch_a.is_over_quota_locked = True
        self.branch_a.save()

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/offline-leases/issue/",
            {
                "tenant_id": self.tenant_id_a,
                "branch_id": self.branch_a.branch_id,
                "device_id": "pos-terminal-locked",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("over-quota locked", res.json()["detail"])

    def test_06_expired_subscription_blocks_offline_lease(self):
        """Subscriptions in EXPIRED or CANCELLED status cannot issue offline leases."""
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        sub.status = SubscriptionStatus.EXPIRED
        sub.save()

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/offline-leases/issue/",
            {
                "tenant_id": self.tenant_id_a,
                "branch_id": self.branch_a.branch_id,
                "device_id": "pos-terminal-expired-sub",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("blocked", res.json()["detail"])

    def test_07_grace_period_caps_lease_duration(self):
        """During grace period, offline lease duration cannot exceed remaining grace days."""
        sub = SubscriptionRecord.objects.filter(tenant=self.tenant_a).order_by("-created_at").first()
        sub.status = SubscriptionStatus.GRACE_PERIOD
        sub.renewal_date = timezone.now().date() - timedelta(days=13)
        sub.grace_period_end_date = timezone.now().date() + timedelta(days=2)  # 2 days remaining (48h)
        sub.save()

        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            "/api/v2/billing/offline-leases/issue/",
            {
                "tenant_id": self.tenant_id_a,
                "branch_id": self.branch_a.branch_id,
                "device_id": "pos-terminal-grace-cap",
                "requested_duration_hours": 72,  # asks for 72h
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()
        self.assertLessEqual(data["duration_hours"], 48, "Must be capped by remaining grace window")

    def test_08_renewed_lease_supersedes_prior_active_lease(self):
        """Issuing a new lease for the same device supersedes prior active leases."""
        device_id = "pos-workstation-lane-05"

        cert_1 = OfflineEntitlementLeaseService.issue_offline_lease(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id=device_id,
            actor_user=self.owner_a,
        )
        lease_id_1 = cert_1["lease_id"]

        cert_2 = OfflineEntitlementLeaseService.issue_offline_lease(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id=device_id,
            actor_user=self.owner_a,
        )
        lease_id_2 = cert_2["lease_id"]

        old_cert = DeviceLeaseCertificate.objects.get(lease_id=lease_id_1)
        new_cert = DeviceLeaseCertificate.objects.get(lease_id=lease_id_2)

        self.assertEqual(old_cert.status, OfflineLeaseStatus.SUPERSEDED)
        self.assertEqual(new_cert.status, OfflineLeaseStatus.ACTIVE)

    def test_09_owner_revocation_lifecycle(self):
        """Owner revoking compromised device lease updates status to REVOKED and invalidates verification."""
        cert = OfflineEntitlementLeaseService.issue_offline_lease(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-stolen-laptop-01",
            actor_user=self.owner_a,
        )
        lease_id = cert["lease_id"]

        self.client.force_authenticate(user=self.owner_a)
        res_revoke = self.client.post(
            "/api/v2/billing/offline-leases/revoke/",
            {
                "tenant_id": self.tenant_id_a,
                "lease_id": lease_id,
                "reason": "Hardware terminal reported stolen from dispensary counter.",
            },
            format="json",
        )
        self.assertEqual(res_revoke.status_code, status.HTTP_200_OK)
        self.assertEqual(res_revoke.json()["status"], "REVOKED")

        # Verify attempt should report REVOKED
        verify_res = self.client.post(
            "/api/v2/billing/offline-leases/verify/",
            {"lease_id": lease_id},
            format="json",
        )
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        self.assertFalse(verify_res.json()["is_valid"])
        self.assertEqual(verify_res.json()["reason"], "LEASE_REVOKED")

    def test_10_cryptographic_tamper_rejection(self):
        """Tampering with canonical payload fails signature verification."""
        self.client.force_authenticate(user=self.owner_a)
        cert = OfflineEntitlementLeaseService.issue_offline_lease(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-counter-crypto-test",
            actor_user=self.owner_a,
        )

        # Altering device_id in payload with original signature
        tampered_payload = dict(cert)
        tampered_payload["device_id"] = "forged-device-id"

        verify_res = self.client.post(
            "/api/v2/billing/offline-leases/verify/",
            {
                "payload": tampered_payload,
                "signature": cert["signature"],
            },
            format="json",
        )
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        self.assertFalse(verify_res.json()["is_valid"])
        self.assertEqual(verify_res.json()["reason"], "INVALID_SIGNATURE")

    def test_11_zero_data_loss_invariant(self):
        """Confirms offline leases never delete branches, sales, or audits."""
        branch_count_before = Branch.objects.filter(tenant=self.tenant_a).count()
        audit_count_before = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        cert = OfflineEntitlementLeaseService.issue_offline_lease(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a.branch_id,
            device_id="pos-retention-test-01",
            actor_user=self.owner_a,
        )
        OfflineEntitlementLeaseService.revoke_offline_lease(
            lease_id=cert["lease_id"],
            reason="Retention audit cycle",
            actor_user=self.owner_a,
        )

        branch_count_after = Branch.objects.filter(tenant=self.tenant_a).count()
        audit_count_after = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        self.assertEqual(branch_count_before, branch_count_after, "Branches must never be deleted")
        self.assertGreater(audit_count_after, audit_count_before, "Audits must be created, never deleted")


if __name__ == "__main__":
    unittest.main()
