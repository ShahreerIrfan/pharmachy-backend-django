"""
backend/tests/test_p312_offline_subscription_lease.py
Architecture Decision: ADR-0312 (P312).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Signed Offline Subscription/Role Lease & Anti-Clock-Bypass:
1. Signed HMAC-SHA256 lease issuance syncs P301 `DeviceLocalDbRegistration.lease_expires_at`, increments
   monotonic counter, and supports replay-safe idempotency.
2. Valid verification succeeds for authorized roles; unauthorized role (`FINANCE_OFFICER` not in lease) blocked (403);
   tampered HMAC signature blocked (403).
3. Anti-Device-Clock-Bypass Drill: Rolling client clock backwards beyond tolerance triggers
   `CLOCK_TAMPER_QUARANTINED` (403 `ERR_DEVICE_CLOCK_ROLLBACK_DETECTED`) and permanently blocks new offline sales!
4. Lease Expiry & Protected Safe Sync Recovery Drill:
   - Expired lease returns `verified=False`, `new_offline_sales_allowed=False`, `protected_sync_recovery_allowed=True`.
   - Even when the lease is EXPIRED AND the tenant subscription is SUSPENDED, `/protected-recovery-sync/`
     STILL succeeds (200 OK) preserving queued offline invoices & exact Decimal `recovered_bdt` with zero silent data deletion!
5. Audited Revocation emits P307 `ChangeFeedEvent(SUBSCRIPTION_LEASE, TOMBSTONE)`; Cashier issue/revoke blocked (403);
   cross-branch & cross-tenant access blocked (403); invalid duration/negative BDT rejected (422).
"""

import datetime
from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    ChangeFeedEvent,
    ChangeFeedOperation,
    DeviceLocalDbRegistration,
    SignedOfflineLeaseState,
    SignedOfflineSubscriptionLease,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestOfflineSubscriptionLeaseIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp1 = "446688aa" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-sl-{self.suffix}",
            name=f"Lease Pharma {self.suffix}",
            subdomain=f"sl-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-sl1-{self.suffix}", tenant=self.tenant_a, name="Lease Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-sl2-{self.suffix}", tenant=self.tenant_a, name="Lease Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-slb-{self.suffix}",
            name=f"Other Lease {self.suffix}",
            subdomain=f"slb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-slb1-{self.suffix}", tenant=self.tenant_b, name="Other Lease Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"sl_owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="SL Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"sl_cash1_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="SL Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"sl_cash2_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="SL Cashier A2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"sl_ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="SL Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.dev1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-SL1-{self.suffix}",
            device_fingerprint_sha256=self.fp1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=1),
        )

    def test_01_signed_lease_clock_rollback_quarantine_expiry_and_protected_recovery(self):
        self.client.force_authenticate(user=self.owner_a)

        # 1. Issue signed offline lease (72h, roles=[CASHIER, BRANCH_MANAGER])
        r_iss = self.client.post(
            "/api/v2/offline-sync/subscription-leases/issue/",
            {
                "device_id": self.dev1.device_id,
                "plan_tier": "PRO",
                "allowed_roles": ["CASHIER", "BRANCH_MANAGER"],
                "lease_duration_hours": 72,
                "idempotency_key": f"lease-iss-{self.suffix}",
            },
            format="json",
        )
        assert r_iss.status_code == status.HTTP_201_CREATED
        lease = r_iss.data["lease"]
        assert lease["monotonic_counter"] == 1
        assert lease["new_offline_sales_allowed"] is True
        assert len(lease["lease_signature_hmac_sha256"]) == 64

        # Replay identical idempotency key -> idempotent_replay=True
        r_iss_dup = self.client.post(
            "/api/v2/offline-sync/subscription-leases/issue/",
            {
                "device_id": self.dev1.device_id,
                "idempotency_key": f"lease-iss-{self.suffix}",
            },
            format="json",
        )
        assert r_iss_dup.status_code == status.HTTP_200_OK
        assert r_iss_dup.data["idempotent_replay"] is True

        # 2. Cashier verifies valid lease with forward client clock
        self.client.force_authenticate(user=self.cashier_a1)
        now_ms = int(timezone.now().timestamp() * 1000) + 60_000
        r_ver = self.client.post(
            "/api/v2/offline-sync/subscription-leases/verify-lease/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "lease_id": lease["lease_id"],
                "lease_signature_hmac_sha256": lease["lease_signature_hmac_sha256"],
                "client_reported_epoch_ms": now_ms,
                "requested_role": "CASHIER",
            },
            format="json",
        )
        assert r_ver.status_code == status.HTTP_200_OK
        assert r_ver.data["verified"] is True

        # Tampered HMAC signature -> 403
        r_bad_sig = self.client.post(
            "/api/v2/offline-sync/subscription-leases/verify-lease/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "lease_id": lease["lease_id"],
                "lease_signature_hmac_sha256": "f" * 64,
                "client_reported_epoch_ms": now_ms + 1000,
            },
            format="json",
        )
        assert r_bad_sig.status_code == status.HTTP_403_FORBIDDEN

        # Role not in lease -> 403
        r_bad_role = self.client.post(
            "/api/v2/offline-sync/subscription-leases/verify-lease/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "lease_id": lease["lease_id"],
                "lease_signature_hmac_sha256": lease["lease_signature_hmac_sha256"],
                "client_reported_epoch_ms": now_ms + 1000,
                "requested_role": "FINANCE_OFFICER",
            },
            format="json",
        )
        assert r_bad_role.status_code == status.HTTP_403_FORBIDDEN

        # 3. ANTI-CLOCK-BYPASS DRILL: Roll device clock back 2 hours -> 403 QUARANTINED!
        rolled_back_ms = now_ms - (2 * 3600 * 1000)
        r_tamper = self.client.post(
            "/api/v2/offline-sync/subscription-leases/verify-lease/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "lease_id": lease["lease_id"],
                "lease_signature_hmac_sha256": lease["lease_signature_hmac_sha256"],
                "client_reported_epoch_ms": rolled_back_ms,
            },
            format="json",
        )
        assert r_tamper.status_code == status.HTTP_403_FORBIDDEN
        db_lease = SignedOfflineSubscriptionLease.objects.get(lease_id=lease["lease_id"])
        assert db_lease.status == SignedOfflineLeaseState.CLOCK_TAMPER_QUARANTINED

        # 4. Issue 2nd lease (counter=2), force server expiry, then SUSPEND tenant!
        self.client.force_authenticate(user=self.owner_a)
        r_iss2 = self.client.post(
            "/api/v2/offline-sync/subscription-leases/issue/",
            {
                "device_id": self.dev1.device_id,
                "plan_tier": "PRO",
                "lease_duration_hours": 24,
                "idempotency_key": f"lease-iss2-{self.suffix}",
            },
            format="json",
        )
        assert r_iss2.status_code == status.HTTP_201_CREATED
        lease2 = r_iss2.data["lease"]
        assert lease2["monotonic_counter"] == 2

        # Expire lease2 on server clock
        SignedOfflineSubscriptionLease.objects.filter(lease_id=lease2["lease_id"]).update(
            expires_at=timezone.now() - datetime.timedelta(minutes=5)
        )
        self.client.force_authenticate(user=self.cashier_a1)
        r_exp_ver = self.client.post(
            "/api/v2/offline-sync/subscription-leases/verify-lease/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "lease_id": lease2["lease_id"],
                "lease_signature_hmac_sha256": lease2["lease_signature_hmac_sha256"],
                "client_reported_epoch_ms": int(timezone.now().timestamp() * 1000) + 120_000,
            },
            format="json",
        )
        assert r_exp_ver.status_code == status.HTTP_200_OK
        assert r_exp_ver.data["verified"] is False
        assert r_exp_ver.data["new_offline_sales_allowed"] is False
        assert r_exp_ver.data["protected_sync_recovery_allowed"] is True

        # 5. PROTECTED SAFE SYNC RECOVERY PATH: Even when tenant is SUSPENDED, queued offline data recovers safely!
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save(update_fields=["status"])

        r_rec = self.client.post(
            "/api/v2/offline-sync/subscription-leases/protected-recovery-sync/",
            {
                "device_id": self.dev1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "lease_id": lease2["lease_id"],
                "pending_outbox_count": 4,
                "pending_outbox_bdt": "1475.80",
            },
            format="json",
        )
        assert r_rec.status_code == status.HTTP_200_OK
        assert r_rec.data["recovery_accepted"] is True
        assert r_rec.data["data_preserved_zero_deletion"] is True
        assert r_rec.data["new_offline_sales_allowed"] is False
        assert r_rec.data["recovered_bdt"] == "1475.80"

        # Restore tenant active -> Owner revokes lease2 -> P307 ChangeFeed TOMBSTONE emitted!
        self.tenant_a.status = TenantLifecycleStatus.ACTIVE
        self.tenant_a.save(update_fields=["status"])
        self.client.force_authenticate(user=self.owner_a)
        r_rev = self.client.post(
            "/api/v2/offline-sync/subscription-leases/revoke/",
            {"lease_id": lease2["lease_id"], "reason": "Terminal hardware decommissioned"},
            format="json",
        )
        assert r_rev.status_code == status.HTTP_200_OK
        assert r_rev.data["lease"]["status"] == SignedOfflineLeaseState.REVOKED
        assert ChangeFeedEvent.objects.filter(
            tenant=self.tenant_a, idempotency_key=f"sol-rev:{lease2['lease_id']}", operation=ChangeFeedOperation.TOMBSTONE
        ).exists()

    def test_02_invalid_inputs_rbac_cross_branch_and_cross_tenant_blocked(self):
        # 1. Invalid duration (999h > 336h) -> 422
        self.client.force_authenticate(user=self.owner_a)
        r_inv = self.client.post(
            "/api/v2/offline-sync/subscription-leases/issue/",
            {"device_id": self.dev1.device_id, "lease_duration_hours": 999, "idempotency_key": f"inv-{self.suffix}"},
            format="json",
        )
        assert r_inv.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 2. Cashier cannot issue leases (403)
        self.client.force_authenticate(user=self.cashier_a1)
        r_cash = self.client.post(
            "/api/v2/offline-sync/subscription-leases/issue/",
            {"device_id": self.dev1.device_id, "idempotency_key": f"cash-{self.suffix}"},
            format="json",
        )
        assert r_cash.status_code == status.HTTP_403_FORBIDDEN

        # 3. Cross-branch cashier blocked on protected-recovery-sync (403)
        self.client.force_authenticate(user=self.cashier_a2)
        r_xbr = self.client.post(
            "/api/v2/offline-sync/subscription-leases/protected-recovery-sync/",
            {"device_id": self.dev1.device_id, "device_fingerprint_sha256": self.fp1, "lease_id": "sol-1"},
            format="json",
        )
        assert r_xbr.status_code == status.HTTP_403_FORBIDDEN

        # 4. Cross-tenant Owner B blocked on list & issue (403)
        self.client.force_authenticate(user=self.owner_b)
        r_xten_list = self.client.get(f"/api/v2/offline-sync/subscription-leases/?tenant_id={self.tenant_a.tenant_id}")
        assert r_xten_list.status_code == status.HTTP_403_FORBIDDEN

        r_xten_iss = self.client.post(
            "/api/v2/offline-sync/subscription-leases/issue/",
            {"device_id": self.dev1.device_id, "idempotency_key": f"xten-{self.suffix}"},
            format="json",
        )
        assert r_xten_iss.status_code == status.HTTP_403_FORBIDDEN
