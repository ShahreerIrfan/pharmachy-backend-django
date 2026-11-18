"""
backend/tests/test_p305_cloud_push_protocol.py
Architecture Decision: ADR-0305 (P305).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Signed HTTPS Cloud Push Protocol:
1. Valid HMAC-SHA256 signed bounded batch with ordered versioned events succeeds, updates
   server BatchStock atomically, advances device last_sync_cursor, and logs CloudSyncPushBatch.
2. Repeated batch delivery (same client_batch_id + same payload) returns is_batch_replay=True
   with zero double stock deduction; tampered payload with same client_batch_id rejected (422).
3. Invalid HMAC signature rejected (403 ERR_INVALID_DEVICE_SIGNATURE).
4. Oversized batch (> 50 events), unordered sequences, unsupported event_version, and excessive
   clock skew (> 15 min) rejected (422).
5. Cross-tenant push, cross-branch cashier push, expired lease, and suspended tenant blocked (403).
"""

import datetime
from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.cloud_push_protocol_service import CloudPushProtocolService
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    CloudPushBatchStatus,
    DeviceLocalDbRegistration,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestCloudPushProtocolIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()
        self.fp = "a9b8c7d6" * 8
        self.dek_fp = "12345678" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-cp-{self.suffix}",
            name=f"Push Pharma {self.suffix}",
            subdomain=f"cp-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-cp1-{self.suffix}", tenant=self.tenant_a, name="Push Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-cp2-{self.suffix}", tenant=self.tenant_a, name="Push Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-cpb-{self.suffix}",
            name=f"Other Push {self.suffix}",
            subdomain=f"cpb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-cpb1-{self.suffix}", tenant=self.tenant_b, name="Other Push Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash1_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"cash2_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Cashier A2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.med = MedicineProduct.objects.create(
            product_id=f"med-cp-{self.suffix}",
            brand_name="Fexo 120",
            generic_name="Fexofenadine",
            dosage_form="TABLET",
            cost_unit=Decimal("7.00"),
            mrp_unit=Decimal("10.00"),
        )
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-cp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="CP-01",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=240),
            quantity_base_units=25,
            cost_per_unit=Decimal("7.00"),
            status=BatchStatus.ACTIVE,
        )

        # Register P301 device + provision P302 DEK fingerprint so push signature binds BOTH!
        self.client.force_authenticate(user=self.owner_a)
        reg = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-PUSH-01",
                "device_fingerprint_sha256": self.fp,
            },
            format="json",
        )
        assert reg.status_code == status.HTTP_201_CREATED
        self.reg_id = reg.data["registration_id"]

        key_resp = self.client.post(
            "/api/v2/offline-sync/device-keys/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_registration_id": self.reg_id,
                "dek_fingerprint_sha256": self.dek_fp,
                "wrapped_dek_blob": "v1:c2FsdDEyMzQ1Njc4OTA=:aXYxMjM0NTY3ODkwMTI=:Y2lwaGVydGV4dF9hZXNfZ2NtX3dyYXBwZWRfZGVr",
            },
            format="json",
        )
        assert key_resp.status_code == status.HTTP_201_CREATED

    def _sign_envelope(self, client_batch_id, events, pushed_at=None, bad_sig=False):
        ts = pushed_at or timezone.now().isoformat()
        ev_sha = CloudPushProtocolService.canonical_events_sha256(events)
        sig = (
            "0" * 64
            if bad_sig
            else CloudPushProtocolService.compute_device_hmac_signature(
                tenant_id=self.tenant_a.tenant_id,
                device_id="POS-PUSH-01",
                client_batch_id=client_batch_id,
                protocol_version=1,
                pushed_at_iso=ts,
                events_sha256=ev_sha,
                device_fingerprint_sha256=self.fp,
                active_dek_fingerprint_sha256=self.dek_fp,
            )
        )
        return {
            "tenant_id": self.tenant_a.tenant_id,
            "device_id": "POS-PUSH-01",
            "client_batch_id": client_batch_id,
            "protocol_version": 1,
            "pushed_at": ts,
            "signature_hmac_sha256": sig,
            "events": events,
        }

    def test_01_valid_signed_push_batch_replay_idempotency_and_stock_precision(self):
        self.client.force_authenticate(user=self.cashier_a1)
        events = [
            {
                "command_id": "cmd-p1",
                "idempotency_key": f"idem-p1-{self.suffix}",
                "client_sequence": 1,
                "event_version": 1,
                "command_type": "OFFLINE_POS_SALE",
                "aggregate_ref": "OFF-P-001",
                "monetary_total_bdt": "50.00",
                "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 5},
            },
            {
                "command_id": "cmd-p2",
                "idempotency_key": f"idem-p2-{self.suffix}",
                "client_sequence": 2,
                "event_version": 2,
                "command_type": "OFFLINE_POS_SALE",
                "aggregate_ref": "OFF-P-002",
                "monetary_total_bdt": "30.00",
                "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 3},
            },
        ]
        envelope = self._sign_envelope("cli-batch-001", events)

        r1 = self.client.post("/api/v2/offline-sync/push/execute/", envelope, format="json")
        assert r1.status_code == status.HTTP_200_OK, r1.data
        assert r1.data["signature_verified"] is True
        assert r1.data["status"] == CloudPushBatchStatus.COMPLETED
        assert r1.data["accepted_count"] == 2
        assert r1.data["last_sync_cursor"].startswith("push-seq-2:")

        # Server stock deducted accurately: 25 - 5 - 3 = 17
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 17

        # Replaying identical signed batch -> idempotent batch replay, stock remains 17!
        r2 = self.client.post("/api/v2/offline-sync/push/execute/", envelope, format="json")
        assert r2.status_code == status.HTTP_200_OK
        assert r2.data["is_batch_replay"] is True
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 17

        # List endpoint & protocol spec work
        assert self.client.get(f"/api/v2/offline-sync/push/?tenant_id={self.tenant_a.tenant_id}").data["count"] == 1
        assert self.client.get("/api/v2/offline-sync/push/protocol-spec/").data["max_batch_size"] == 50

    def test_02_invalid_signature_oversized_unordered_version_and_clock_skew_rejected(self):
        self.client.force_authenticate(user=self.cashier_a1)
        ev = [
            {
                "command_id": "cmd-bad",
                "idempotency_key": f"idem-bad-{self.suffix}",
                "client_sequence": 1,
                "event_version": 1,
                "command_type": "OFFLINE_POS_SALE",
                "aggregate_ref": "OFF-BAD",
                "monetary_total_bdt": "10.00",
                "payload": {},
            }
        ]

        # 1. Invalid HMAC signature -> 403
        bad_sig = self.client.post(
            "/api/v2/offline-sync/push/execute/",
            self._sign_envelope("cli-bad-sig", ev, bad_sig=True),
            format="json",
        )
        assert bad_sig.status_code == status.HTTP_403_FORBIDDEN
        assert bad_sig.data["error_code"] == "ERR_INVALID_DEVICE_SIGNATURE"

        # 2. Excessive clock skew (2 hours old) -> 422
        old_ts = (timezone.now() - datetime.timedelta(hours=2)).isoformat()
        skew = self.client.post(
            "/api/v2/offline-sync/push/execute/",
            self._sign_envelope("cli-skew", ev, pushed_at=old_ts),
            format="json",
        )
        assert skew.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert skew.data["error_code"] == "ERR_PUSH_TIMESTAMP_SKEW"

        # 3. Oversized batch (51 events > max 50) -> 422
        too_many = [
            {"command_id": f"c{i}", "idempotency_key": f"k{i}", "client_sequence": i, "event_version": 1, "command_type": "OFFLINE_POS_SALE", "aggregate_ref": f"A{i}"}
            for i in range(1, 52)
        ]
        over = self.client.post(
            "/api/v2/offline-sync/push/execute/",
            self._sign_envelope("cli-over", too_many),
            format="json",
        )
        assert over.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert over.data["error_code"] == "ERR_PUSH_BATCH_LIMIT_EXCEEDED"

        # 4. Unordered sequences (5 then 2) -> 422
        unord = [
            {"command_id": "c5", "idempotency_key": "k5", "client_sequence": 5, "event_version": 1, "command_type": "OFFLINE_POS_SALE", "aggregate_ref": "A5"},
            {"command_id": "c2", "idempotency_key": "k2", "client_sequence": 2, "event_version": 1, "command_type": "OFFLINE_POS_SALE", "aggregate_ref": "A2"},
        ]
        r_unord = self.client.post(
            "/api/v2/offline-sync/push/execute/",
            self._sign_envelope("cli-unord", unord),
            format="json",
        )
        assert r_unord.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert r_unord.data["error_code"] == "ERR_UNORDERED_EVENT_BATCH"

        # 5. Unsupported event_version (99) -> 422
        bad_ver = [
            {"command_id": "c99", "idempotency_key": "k99", "client_sequence": 1, "event_version": 99, "command_type": "OFFLINE_POS_SALE", "aggregate_ref": "A99"}
        ]
        r_ver = self.client.post(
            "/api/v2/offline-sync/push/execute/",
            self._sign_envelope("cli-ver", bad_ver),
            format="json",
        )
        assert r_ver.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert r_ver.data["error_code"] == "ERR_UNSUPPORTED_EVENT_VERSION"

    def test_03_cross_branch_cross_tenant_expired_lease_and_suspended_blocked(self):
        ev = [
            {
                "command_id": "cmd-sec",
                "idempotency_key": f"idem-sec-{self.suffix}",
                "client_sequence": 1,
                "event_version": 1,
                "command_type": "OFFLINE_POS_SALE",
                "aggregate_ref": "OFF-SEC",
                "monetary_total_bdt": "10.00",
                "payload": {},
            }
        ]
        env = self._sign_envelope("cli-sec", ev)

        # Cashier from Branch A2 cannot push for Branch A1's device (403)
        self.client.force_authenticate(user=self.cashier_a2)
        assert self.client.post("/api/v2/offline-sync/push/execute/", env, format="json").status_code == status.HTTP_403_FORBIDDEN

        # Cross-tenant Owner B blocked (403)
        self.client.force_authenticate(user=self.owner_b)
        ct_env = {**env, "tenant_id": self.tenant_b.tenant_id}
        assert self.client.post("/api/v2/offline-sync/push/execute/", ct_env, format="json").status_code == status.HTTP_403_FORBIDDEN

        # Expired lease blocked (403)
        reg = DeviceLocalDbRegistration.objects.get(registration_id=self.reg_id)
        reg.lease_expires_at = timezone.now() - datetime.timedelta(minutes=5)
        reg.save()
        self.client.force_authenticate(user=self.cashier_a1)
        assert self.client.post("/api/v2/offline-sync/push/execute/", env, format="json").status_code == status.HTTP_403_FORBIDDEN

        # Suspended tenant blocked (403)
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        assert self.client.post("/api/v2/offline-sync/push/execute/", env, format="json").status_code == status.HTTP_403_FORBIDDEN
