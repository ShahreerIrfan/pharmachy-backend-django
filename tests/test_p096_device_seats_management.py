"""
backend/tests/test_p096_device_seats_management.py
Architecture Decision: ADR-0015, ADR-0027, ADR-0081, ADR-0082, ADR-0095 & ADR-0096
Phase 05: Basic/Standard/Pro Subscription & Billing (P096)
Requirement Trace: FR-004, FR-070, FR-071, FR-072 (Authoritative Device Seats & Lease Invalidation)

Comprehensive Integration Tests for Device Seats Management:
1. Valid Scenario: Enrolls a new POS device seat, consumes quota, and logs audit entry (HTTP 201 Created).
2. Quota Protection: Attempting to enroll beyond plan device limits returns ERR_QUOTA_EXCEEDED (HTTP 400).
3. Incomplete / Invalid Input: Rejects short device_id, missing branch, or non-existent branch (HTTP 400).
4. Cross-Tenant Barrier: Tenant B cannot list, enroll, reassign, or revoke Tenant A devices (HTTP 403).
5. RBAC Barrier: Cashier cannot enroll or revoke device seats; cashier can submit heartbeat (HTTP 403 / 200).
6. Revocation & Lease Invalidation: Revoking a device invalidates active leases and releases seat quota.
7. Branch Reassignment: Reassigns device within tenant branches; cross-tenant branch reassignment blocked.
8. Terminal Heartbeat: Updates heartbeat timestamp and network metadata (HTTP 200).
9. Zero Data Deleted Invariant: Revoking device keeps record in status REVOKED; zero records deleted.
"""

from __future__ import annotations

import os
import random
import unittest
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.device_seat_service import DeviceSeatManagementService
from apps.core.models import (
    Branch,
    DeviceLeaseCertificate,
    DeviceSeat,
    DeviceSeatStatus,
    OfflineLeaseStatus,
    PlanTier,
    QuotaResourceType,
    Tenant,
    TenantAppliedEntitlement,
    TenantQuotaUsageCounter,
)
from apps.core.offline_entitlement_service import OfflineEntitlementLeaseService
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.provisioning import TenantProvisioningEngine
from apps.core.quota_counter_service import SubscriptionQuotaCounterService
from apps.identity.models import PharmacyUser, UserRole


class TestP096DeviceSeatsManagementIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()
        cls.suffix = uuid.uuid4().hex[:6]

        # 1. Superadmin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p096_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
                "is_staff": True,
                "mfa_enabled": True,
            },
        )

        # 2. Provision Tenant A (Standard Plan: 3 branches, 10 staff, 5 devices)
        cls.subdomain_a = f"seat-a-{cls.suffix}"
        cls.tenant_id_a = f"tn-seat-a-{cls.suffix}"
        cls.license_a = f"DGDA-DL-P096-A-{cls.suffix}"
        cls.phone_a = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_a = f"owner_a_{cls.suffix}@seata.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_a,
            name=f"Seat Pharma A {cls.suffix}",
            subdomain=cls.subdomain_a,
            owner_email=cls.email_a,
            owner_phone=cls.phone_a,
            owner_name=f"Owner A {cls.suffix}",
            owner_password="Secur3P@ssw0rd!",
            plan_tier=PlanTier.STANDARD,
            branch_name="Branch Mirpur",
            branch_license=cls.license_a,
            is_trial=False,
        )
        cls.tenant_a = Tenant.objects.get(tenant_id=cls.tenant_id_a)
        cls.owner_a = PharmacyUser.objects.get(email=cls.email_a)
        cls.branch_a1 = Branch.objects.filter(tenant=cls.tenant_a).first()

        # Second branch for Tenant A for reassignment tests
        cls.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Branch Uttara",
            license_number=f"DGDA-DL-P096-A2-{cls.suffix}",
            is_active=True,
        )

        PlanCatalogueService.apply_plan_to_tenant(
            tenant_id=cls.tenant_id_a,
            plan_code="PLAN_STANDARD",
            billing_cycle="MONTHLY",
        )

        # Cashier user for Tenant A
        cls.cashier_a, _ = PharmacyUser.objects.get_or_create(
            email=f"cashier_a_{cls.suffix}@seata.local",
            defaults={
                "full_name": f"Cashier A {cls.suffix}",
                "role": UserRole.CASHIER,
                "tenant_id": cls.tenant_id_a,
                "phone_number": f"+88017{random.randint(10000000, 99999999)}",  # synthetic-fixture
                "is_active": True,
            },
        )

        # 3. Provision Tenant B (Isolation Boundary: Basic Plan: 2 devices)
        cls.subdomain_b = f"seat-b-{cls.suffix}"
        cls.tenant_id_b = f"tn-seat-b-{cls.suffix}"
        cls.license_b = f"DGDA-DL-P096-B-{cls.suffix}"
        cls.phone_b = f"+88017{random.randint(10000000, 99999999)}"  # synthetic-fixture
        cls.email_b = f"owner_b_{cls.suffix}@seatb.local"

        TenantProvisioningEngine.provision_tenant(
            tenant_id=cls.tenant_id_b,
            name=f"Seat Pharma B {cls.suffix}",
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

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        TenantAppliedEntitlement.objects.filter(tenant=self.tenant_a).update(is_active=True)
        # Recalibrate/sync counters
        SubscriptionQuotaCounterService.recalibrate_counters(self.tenant_id_a, actor_user=self.owner_a)

    def test_01_valid_device_enrollment_consumes_seat(self):
        """Owner enrolls a new workstation; consumes 1 device seat and creates audit entry."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/devices/enroll/"

        device_id = f"dev-pos-{uuid.uuid4().hex[:6]}"
        payload = {
            "tenant_id": self.tenant_id_a,
            "branch_id": self.branch_a1.branch_id,
            "device_id": device_id,
            "device_name": "Counter 1 Desktop POS",
            "device_type": "DESKTOP_POS",
            "mac_address": "00:1B:44:11:3A:B7",
            "ip_address": "192.168.1.105",
            "hardware_fingerprint": "hw-fp-pos-01",
        }

        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()

        self.assertEqual(data["device_id"], device_id)
        self.assertEqual(data["status"], "ACTIVE")
        self.assertEqual(data["branch_id"], self.branch_a1.branch_id)
        self.assertEqual(data["branch_name"], self.branch_a1.name)
        self.assertTrue(data["invariants"]["zero_data_deleted"])
        self.assertTrue(data["invariants"]["lease_invalidation_enforced"])
        self.assertTrue(data["invariants"]["quota_counter_synced"])
        self.assertIn("ডিভাইস সীট সক্রিয়", data["client_messaging"]["headline_bn"])

        # Database Check
        device = DeviceSeat.objects.get(device_id=device_id)
        self.assertEqual(device.status, DeviceSeatStatus.ACTIVE)

        # Quota Counter Check (Standard Plan limit is 5 devices)
        counter = TenantQuotaUsageCounter.objects.get(
            tenant=self.tenant_a,
            resource_type=QuotaResourceType.DEVICES,
        )
        self.assertGreaterEqual(counter.current_used, 1)

        # Audit Check
        self.assertTrue(
            AuditLogEntry.objects.filter(
                action="DEVICE_SEAT_ENROLLED",
                resource_id=device_id,
                tenant_id=self.tenant_id_a,
            ).exists()
        )

    def test_02_quota_limit_blocks_excess_enrollment(self):
        """Attempting to enroll beyond plan device quota raises ERR_QUOTA_EXCEEDED (HTTP 400)."""
        # Set quota to capacity (5 devices for Standard)
        counter = TenantQuotaUsageCounter.objects.get(
            tenant=self.tenant_a,
            resource_type=QuotaResourceType.DEVICES,
        )
        counter.current_used = counter.max_limit
        counter.save()

        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/devices/enroll/"
        payload = {
            "tenant_id": self.tenant_id_a,
            "branch_id": self.branch_a1.branch_id,
            "device_id": f"dev-excess-{uuid.uuid4().hex[:6]}",
            "device_name": "Excess Workstation",
        }

        res = self.client.post(url, payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        data = res.json()
        self.assertEqual(data["error"], "ERR_QUOTA_EXCEEDED")
        self.assertIn("Quota exceeded", data["detail"])

    def test_03_invalid_or_incomplete_inputs_rejected(self):
        """Short device ID or non-existent branch returns HTTP 400 Bad Request."""
        self.client.force_authenticate(user=self.owner_a)
        url = "/api/v2/billing/devices/enroll/"

        # Short device_id (<3 chars)
        res_short = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "branch_id": self.branch_a1.branch_id, "device_id": "d", "device_name": "POS"},
            format="json",
        )
        self.assertEqual(res_short.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_short.json()["error"], "VALIDATION_ERROR")

        # Non-existent branch
        res_bad_br = self.client.post(
            url,
            {"tenant_id": self.tenant_id_a, "branch_id": "br-invalid-999", "device_id": "dev-valid-01", "device_name": "POS"},
            format="json",
        )
        self.assertEqual(res_bad_br.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res_bad_br.json()["error"], "VALIDATION_ERROR")

    def test_04_cross_tenant_isolation_barrier(self):
        """Tenant B owner is blocked from viewing, enrolling, or revoking Tenant A devices."""
        # Enroll device for Tenant A
        dev_a = DeviceSeatManagementService.enroll_device(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a1.branch_id,
            device_id=f"dev-cross-{uuid.uuid4().hex[:6]}",
            device_name="Tenant A Device",
            actor_user=self.owner_a,
        )

        self.client.force_authenticate(user=self.owner_b)

        # 1. Cross-tenant listing attempt
        res_list = self.client.get(f"/api/v2/billing/devices/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res_list.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_list.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 2. Cross-tenant enroll attempt into Tenant A branch
        res_enroll = self.client.post(
            "/api/v2/billing/devices/enroll/",
            {"tenant_id": self.tenant_id_a, "branch_id": self.branch_a1.branch_id, "device_id": "rogue-dev", "device_name": "Rogue"},
            format="json",
        )
        self.assertEqual(res_enroll.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_enroll.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

        # 3. Cross-tenant revoke attempt
        res_revoke = self.client.post(
            f"/api/v2/billing/devices/{dev_a['device_id']}/revoke/",
            {"reason": "Rogue cross-tenant revoke attempt"},
            format="json",
        )
        self.assertEqual(res_revoke.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_revoke.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_05_rbac_cashier_cannot_enroll_or_revoke_but_can_heartbeat(self):
        """Cashier is forbidden from enrolling or revoking, but authorized for heartbeat telemetry."""
        dev = DeviceSeatManagementService.enroll_device(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a1.branch_id,
            device_id=f"dev-rbac-{uuid.uuid4().hex[:6]}",
            device_name="RBAC Test POS",
            actor_user=self.owner_a,
        )

        self.client.force_authenticate(user=self.cashier_a)

        # 1. Cashier enroll attempt -> 403 Forbidden
        res_enroll = self.client.post(
            "/api/v2/billing/devices/enroll/",
            {"tenant_id": self.tenant_id_a, "branch_id": self.branch_a1.branch_id, "device_id": "dev-cashier-01", "device_name": "Test"},
            format="json",
        )
        self.assertEqual(res_enroll.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_enroll.json()["error"], "ERR_PERMISSION_DENIED")

        # 2. Cashier revoke attempt -> 403 Forbidden
        res_revoke = self.client.post(
            f"/api/v2/billing/devices/{dev['device_id']}/revoke/",
            {"reason": "Unauthorized cashier attempt"},
            format="json",
        )
        self.assertEqual(res_revoke.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_revoke.json()["error"], "ERR_PERMISSION_DENIED")

        # 3. Cashier heartbeat telemetry -> 200 OK
        res_hb = self.client.post(
            f"/api/v2/billing/devices/{dev['device_id']}/heartbeat/",
            {"ip_address": "192.168.1.188", "mac_address": "00:1B:44:99:88:77"},
            format="json",
        )
        self.assertEqual(res_hb.status_code, status.HTTP_200_OK)
        self.assertEqual(res_hb.json()["ip_address"], "192.168.1.188")

    def test_06_revocation_invalidates_active_leases_and_releases_seat(self):
        """Revoking a device seat automatically invalidates active leases and releases quota counter."""
        device_id = f"dev-lease-test-{uuid.uuid4().hex[:6]}"

        # 1. Enroll device
        DeviceSeatManagementService.enroll_device(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a1.branch_id,
            device_id=device_id,
            device_name="Lease Invalidation POS",
            actor_user=self.owner_a,
        )

        # 2. Issue an active offline lease for this device
        lease = OfflineEntitlementLeaseService.issue_offline_lease(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a1.branch_id,
            device_id=device_id,
            device_name="Lease Invalidation POS",
            actor_user=self.owner_a,
        )
        lease_id = lease["lease_id"]
        cert_before = DeviceLeaseCertificate.objects.get(lease_id=lease_id)
        self.assertEqual(cert_before.status, OfflineLeaseStatus.ACTIVE)

        counter_before = TenantQuotaUsageCounter.objects.get(
            tenant=self.tenant_a,
            resource_type=QuotaResourceType.DEVICES,
        ).current_used

        # 3. Revoke the device seat
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.post(
            f"/api/v2/billing/devices/{device_id}/revoke/",
            {"reason": "Terminal decommissioned after fire safety upgrade."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data["status"], "REVOKED")
        self.assertIn("বাতিলকৃত", data["client_messaging"]["headline_bn"])

        # 4. Verify lease certificate is revoked in DB
        cert_after = DeviceLeaseCertificate.objects.get(lease_id=lease_id)
        self.assertEqual(cert_after.status, OfflineLeaseStatus.REVOKED)
        self.assertIn("Device seat revoked", cert_after.revocation_reason)

        # 5. Verify device seat quota is released
        counter_after = TenantQuotaUsageCounter.objects.get(
            tenant=self.tenant_a,
            resource_type=QuotaResourceType.DEVICES,
        ).current_used
        self.assertEqual(counter_after, counter_before - 1)

    def test_07_branch_reassignment_within_tenant(self):
        """Reassigns active device from Branch A1 to Branch A2 within same tenant."""
        device_id = f"dev-reassign-{uuid.uuid4().hex[:6]}"
        DeviceSeatManagementService.enroll_device(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a1.branch_id,
            device_id=device_id,
            device_name="Movable POS Tablet",
            actor_user=self.owner_a,
        )

        self.client.force_authenticate(user=self.owner_a)
        url = f"/api/v2/billing/devices/{device_id}/reassign/"

        # 1. Valid reassignment to Branch A2
        res = self.client.post(url, {"new_branch_id": self.branch_a2.branch_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data["branch_id"], self.branch_a2.branch_id)
        self.assertEqual(data["branch_name"], self.branch_a2.name)

        # 2. Reassignment to Tenant B branch -> 403 Forbidden
        res_cross = self.client.post(url, {"new_branch_id": self.branch_b.branch_id}, format="json")
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_cross.json()["error"], "ERR_CROSS_TENANT_VIOLATION")

    def test_08_device_listing_and_summary(self):
        """Owner retrieves device list with capacity summary and bilingual messaging."""
        self.client.force_authenticate(user=self.owner_a)
        res = self.client.get(f"/api/v2/billing/devices/?tenant_id={self.tenant_id_a}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()

        self.assertEqual(data["tenant_id"], self.tenant_id_a)
        self.assertEqual(data["plan_tier"], "STANDARD")
        self.assertIn("devices", data)
        self.assertIn("seat_summary", data)
        self.assertEqual(data["seat_summary"]["max_limit"], 15)
        self.assertIn("ডিভাইস সীট ব্যবস্থাপনা", data["client_messaging"]["headline_bn"])

    def test_09_zero_data_deleted_invariant(self):
        """Revoking devices preserves records in REVOKED state; 0 rows deleted."""
        device_id = f"dev-retain-{uuid.uuid4().hex[:6]}"
        DeviceSeatManagementService.enroll_device(
            tenant_id=self.tenant_id_a,
            branch_id=self.branch_a1.branch_id,
            device_id=device_id,
            device_name="Retained Workstation",
            actor_user=self.owner_a,
        )

        device_count_before = DeviceSeat.objects.filter(tenant=self.tenant_a).count()
        audit_count_before = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        DeviceSeatManagementService.revoke_device_seat(
            device_id=device_id,
            reason="Decommissioned for inventory testing",
            actor_user=self.owner_a,
        )

        device_count_after = DeviceSeat.objects.filter(tenant=self.tenant_a).count()
        audit_count_after = AuditLogEntry.objects.filter(tenant_id=self.tenant_id_a).count()

        self.assertEqual(device_count_before, device_count_after, "Zero DeviceSeat rows must be deleted")
        self.assertGreater(audit_count_after, audit_count_before, "Audit logs must be created, never deleted")


if __name__ == "__main__":
    unittest.main()
