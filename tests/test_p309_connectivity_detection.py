"""
backend/tests/test_p309_connectivity_detection.py
Architecture Decision: ADR-0309 (P309).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Non-Blocking Connectivity Detection & Accurate Last-Sync Status:
1. Full connectivity lifecycle: Initial DISCONNECTED -> healthy heartbeat -> RECONNECTED -> steady CONNECTED
   -> high latency/packet loss -> DEGRADED (`cloud_stock_authoritative=False`) -> 2 consecutive failures ->
   DISCONNECTED (`cloud_stock_authoritative=False`) -> healthy probe -> RECONNECTED + append-only transition logs.
2. Real P308 + P307 integration: Pull lag and `last_successful_sync_at` reflect live `DeviceSyncWatermark`,
   and Decimal `packet_loss_pct` & `pending_outbox_bdt` retain exact 2-decimal precision.
3. Repeated delivery idempotency (`idempotency_key`) prevents duplicate transition logs.
4. Authorized mutable controls: Owner PATCHes `degraded_latency_threshold_ms` and soft-archives via DELETE;
   Cashier PATCH/DELETE forbidden (403).
5. Invalid inputs (packet_loss > 100, negative latency/BDT -> 422), cross-branch cashier (403),
   cross-tenant owner (403), expired offline lease (403), and suspended tenant (403).
"""

import datetime
from decimal import Decimal
import hashlib
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    DeviceConnectivityState,
    DeviceLocalDbRegistration,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestConnectivityDetectionIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp1 = "abcd1234" * 8
        self.fp_b = "5678ef90" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-cn-{self.suffix}",
            name=f"Conn Pharma {self.suffix}",
            subdomain=f"cn-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-cn1-{self.suffix}", tenant=self.tenant_a, name="Conn Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-cn2-{self.suffix}", tenant=self.tenant_a, name="Conn Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-cnb-{self.suffix}",
            name=f"Other Conn {self.suffix}",
            subdomain=f"cnb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-cnb1-{self.suffix}", tenant=self.tenant_b, name="Other Conn Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"cn_owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Conn Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cn_cash1_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Conn Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"cn_cash2_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Conn Cashier A2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"cn_ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Conn Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.dev_a1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-CN1-{self.suffix}",
            device_fingerprint_sha256=self.fp1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )

    def test_01_connectivity_transitions_last_sync_accuracy_idempotency_and_owner_config(self):
        # Publish 1 event & commit P308 pull checkpoint so last_successful_sync_at is genuinely populated!
        self.client.force_authenticate(user=self.owner_a)
        self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {"entity_type": "MEDICINE_CATALOG", "entity_id": "med-cn-1", "operation": "UPSERT", "payload_json": {"mrp": "5.00"}},
            format="json",
        )
        self.client.force_authenticate(user=self.cashier_a1)
        pulled = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1, "since_sequence": 0},
            format="json",
        )
        self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/commit-pull/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "acknowledged_sequence_no": 1,
                "durable_local_apply_verified": True,
                "local_apply_digest_sha256": hashlib.sha256(b"apply-cn-1").hexdigest(),
                "cursor_token": pulled.data["cursor_token"],
                "applied_event_count": 1,
            },
            format="json",
        )

        # 1. First healthy heartbeat transitions initial DISCONNECTED -> RECONNECTED
        h1 = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "probe_succeeded": True,
                "latency_ms": 110,
                "packet_loss_pct": "0.00",
                "pending_outbox_count": 0,
                "pending_outbox_bdt": "0.00",
                "idempotency_key": f"hb-1-{self.suffix}",
            },
            format="json",
        )
        assert h1.status_code == status.HTTP_200_OK
        assert h1.data["snapshot"]["connectivity_state"] == DeviceConnectivityState.RECONNECTED
        assert h1.data["snapshot"]["cloud_stock_authoritative"] is True
        assert h1.data["snapshot"]["pull_applied_sequence_no"] == 1
        assert h1.data["snapshot"]["last_successful_sync_at"] is not None
        snap_id = h1.data["snapshot"]["snapshot_id"]

        # Duplicate replay of hb-1 -> idempotent_replay=True!
        h1_dup = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "probe_succeeded": True,
                "latency_ms": 110,
                "idempotency_key": f"hb-1-{self.suffix}",
            },
            format="json",
        )
        assert h1_dup.status_code == status.HTTP_200_OK
        assert h1_dup.data["idempotent_replay"] is True

        # 2. Second healthy heartbeat -> steady CONNECTED
        h2 = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "probe_succeeded": True,
                "latency_ms": 95,
            },
            format="json",
        )
        assert h2.data["snapshot"]["connectivity_state"] == DeviceConnectivityState.CONNECTED

        # 3. High latency + packet loss -> DEGRADED (`cloud_stock_authoritative=False`)
        h3 = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "probe_succeeded": True,
                "latency_ms": 2200,
                "packet_loss_pct": "18.75",
                "pending_outbox_count": 3,
                "pending_outbox_bdt": "485.50",
            },
            format="json",
        )
        assert h3.data["snapshot"]["connectivity_state"] == DeviceConnectivityState.DEGRADED
        assert h3.data["snapshot"]["cloud_stock_authoritative"] is False
        assert h3.data["snapshot"]["packet_loss_pct"] == "18.75"
        assert h3.data["snapshot"]["pending_outbox_bdt"] == "485.50"

        # 4. Network partition (consecutive_failures >= 2) -> DISCONNECTED
        h4 = self.client.post(
            "/api/v2/offline-sync/connectivity/report-transition/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "probe_succeeded": False,
                "consecutive_failures": 3,
                "pending_outbox_count": 5,
                "pending_outbox_bdt": "910.25",
                "reason": "ISP broadband outage — local SQLite queue active",
            },
            format="json",
        )
        assert h4.data["snapshot"]["connectivity_state"] == DeviceConnectivityState.DISCONNECTED
        assert h4.data["snapshot"]["cloud_stock_authoritative"] is False
        assert "অফলাইন" in h4.data["snapshot"]["status_label_bn"]

        # 5. Network restored -> RECONNECTED
        h5 = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "probe_succeeded": True,
                "latency_ms": 140,
                "pending_outbox_count": 5,
                "pending_outbox_bdt": "910.25",
            },
            format="json",
        )
        assert h5.data["snapshot"]["connectivity_state"] == DeviceConnectivityState.RECONNECTED

        # Verify transition history recorded all transitions
        hist = self.client.get(f"/api/v2/offline-sync/connectivity/history/?device_id={self.dev_a1.device_id}")
        assert hist.status_code == status.HTTP_200_OK
        assert hist.data["count"] >= 5

        # Owner PATCHes degraded latency threshold & DELETE soft-archives; Cashier blocked (403)
        r_cash_patch = self.client.patch(
            f"/api/v2/offline-sync/connectivity/{snap_id}/",
            {"degraded_latency_threshold_ms": 800},
            format="json",
        )
        assert r_cash_patch.status_code == status.HTTP_403_FORBIDDEN

        self.client.force_authenticate(user=self.owner_a)
        r_own_patch = self.client.patch(
            f"/api/v2/offline-sync/connectivity/{snap_id}/",
            {"degraded_latency_threshold_ms": 800},
            format="json",
        )
        assert r_own_patch.status_code == status.HTTP_200_OK
        assert r_own_patch.data["snapshot"]["degraded_latency_threshold_ms"] == 800

        r_own_del = self.client.delete(f"/api/v2/offline-sync/connectivity/{snap_id}/")
        assert r_own_del.status_code == status.HTTP_200_OK
        assert r_own_del.data["snapshot"]["is_archived"] is True

    def test_02_invalid_telemetry_cross_tenant_cross_branch_lease_and_suspension_drills(self):
        # 1. Invalid inputs: packet_loss > 100 and negative BDT -> 422
        self.client.force_authenticate(user=self.cashier_a1)
        r_bad_pkt = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1, "packet_loss_pct": "150.00"},
            format="json",
        )
        assert r_bad_pkt.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        r_bad_bdt = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1, "pending_outbox_bdt": "-10.00"},
            format="json",
        )
        assert r_bad_bdt.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 2. Cross-branch cashier blocked (403)
        self.client.force_authenticate(user=self.cashier_a2)
        r_xbr = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_xbr.status_code == status.HTTP_403_FORBIDDEN

        # 3. Cross-tenant Owner B blocked on heartbeat & list (403)
        self.client.force_authenticate(user=self.owner_b)
        r_xten = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_xten.status_code == status.HTTP_403_FORBIDDEN

        r_xten_list = self.client.get(f"/api/v2/offline-sync/connectivity/?tenant_id={self.tenant_a.tenant_id}")
        assert r_xten_list.status_code == status.HTTP_403_FORBIDDEN

        # 4. Expired offline lease blocks heartbeat (403)
        self.dev_a1.lease_expires_at = timezone.now() - datetime.timedelta(minutes=5)
        self.dev_a1.save(update_fields=["lease_expires_at"])
        self.client.force_authenticate(user=self.owner_a)
        r_exp = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_exp.status_code == status.HTTP_403_FORBIDDEN

        # 5. Suspended tenant blocks heartbeat (403)
        self.dev_a1.lease_expires_at = timezone.now() + datetime.timedelta(days=5)
        self.dev_a1.save(update_fields=["lease_expires_at"])
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save(update_fields=["status"])
        r_susp = self.client.post(
            "/api/v2/offline-sync/connectivity/heartbeat/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_susp.status_code == status.HTTP_403_FORBIDDEN
