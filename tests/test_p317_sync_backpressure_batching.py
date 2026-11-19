"""
backend/tests/test_p317_sync_backpressure_batching.py
Architecture Decision: ADR-0317 (P317).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Sync Backpressure, Adaptive Batching & Retry Backoff:
1. Policy creation & default configuration.
2. Payload size cap & batch item cap validation.
3. Exponential retry backoff calculation on sync failures and active throttle blocking.
4. Sync success resets failure counters and clears active throttle.
5. POS non-starvation heuristics: dynamic batch shrink and cooperative yield during active checkout.
6. Cross-tenant isolation: Tenant B cannot query or manipulate Tenant A's device policy.
7. DRF ViewSet endpoints (/policy, /validate, /outcome, /progress, /configure, /reset).
"""

import datetime
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    SyncBackpressurePolicy,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.sync_backpressure_service import SyncBackpressureService
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSyncBackpressureBatchingIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp = "aabbccdd" * 8

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-sbp-a-{self.suffix}",
            name=f"Backpressure Pharma A {self.suffix}",
            subdomain=f"sbp-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-sbp-a-{self.suffix}", tenant=self.tenant_a, name="Dhanmondi Branch"
        )
        self.device_a = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-sbp-a-{self.suffix}",
            device_id=f"POS-SBP-A-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-sbp-b-{self.suffix}",
            name=f"Backpressure Pharma B {self.suffix}",
            subdomain=f"sbp-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-sbp-b-{self.suffix}", tenant=self.tenant_b, name="Banani Branch"
        )
        self.device_b = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-sbp-b-{self.suffix}",
            device_id=f"POS-SBP-B-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Users
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_sbp_a_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="SBP Owner A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_sbp_b_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="SBP Owner B",
            password="Password123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

    def test_01_default_policy_creation(self):
        policy = SyncBackpressureService.get_or_create_policy(
            tenant=self.tenant_a, device_id=self.device_a.device_id
        )
        assert policy is not None
        assert policy.max_batch_size == 50
        assert policy.max_payload_bytes == 512 * 1024
        assert policy.retry_base_delay_ms == 1000
        assert policy.is_throttled is False

    def test_02_payload_caps_and_batch_limits(self):
        # Valid payload passes
        res = SyncBackpressureService.validate_batch_and_check_backpressure(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            event_count=20,
            payload_bytes=10000,
        )
        assert res["status"] == "APPROVED"

        # Exceed batch item cap
        with pytest.raises(ValidationError) as exc_batch:
            SyncBackpressureService.validate_batch_and_check_backpressure(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                event_count=51,
                payload_bytes=10000,
            )
        assert "ERR_PUSH_BATCH_LIMIT_EXCEEDED" in str(exc_batch.value)

        # Exceed payload byte size cap
        with pytest.raises(ValidationError) as exc_payload:
            SyncBackpressureService.validate_batch_and_check_backpressure(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                event_count=10,
                payload_bytes=600 * 1024,
            )
        assert "ERR_PAYLOAD_SIZE_LIMIT_EXCEEDED" in str(exc_payload.value)

    def test_03_exponential_backoff_and_active_throttle(self):
        # Record sync failure #1
        out1 = SyncBackpressureService.record_sync_outcome(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            is_success=False,
            failure_reason="HTTP 503 Gateway Timeout",
            actor_user=self.owner_a,
        )
        assert out1["is_throttled"] is True
        assert out1["consecutive_failures"] == 1
        assert out1["retry_after_seconds"] >= 1

        # Attempting push while throttled raises backpressure error
        with pytest.raises(ValidationError) as exc_throttled:
            SyncBackpressureService.validate_batch_and_check_backpressure(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                event_count=5,
                payload_bytes=1000,
            )
        assert "ERR_SYNC_BACKPRESSURE_ACTIVE" in str(exc_throttled.value)

        # Reset or successful sync clears throttle
        out_success = SyncBackpressureService.record_sync_outcome(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            is_success=True,
            actor_user=self.owner_a,
        )
        assert out_success["is_throttled"] is False
        assert out_success["consecutive_failures"] == 0

    def test_04_pos_non_starvation_heuristics(self):
        # Normal progress evaluation
        prog_idle = SyncBackpressureService.evaluate_outbox_progress(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            total_enqueued=100,
            processed_count=40,
            failed_count=0,
            is_pos_checkout_active=False,
        )
        assert prog_idle["progress_percentage"] == 40.0
        assert prog_idle["suggested_batch_size"] == 50
        assert prog_idle["cooperative_yield_ms"] == 0

        # With POS active checkout ringing
        prog_busy = SyncBackpressureService.evaluate_outbox_progress(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            total_enqueued=100,
            processed_count=40,
            failed_count=0,
            is_pos_checkout_active=True,
        )
        assert prog_busy["suggested_batch_size"] <= 10
        assert prog_busy["cooperative_yield_ms"] >= 200

    def test_05_cross_tenant_isolation(self):
        # Tenant B tries to access Tenant A's device policy
        with pytest.raises(NotFound) as exc_iso:
            SyncBackpressureService.get_or_create_policy(
                tenant=self.tenant_b, device_id=self.device_a.device_id
            )
        assert "ERR_DEVICE_NOT_FOUND" in str(exc_iso.value)

    def test_06_drf_endpoints(self):
        self.client.force_authenticate(user=self.owner_a)

        # GET /policy
        res_pol = self.client.get(
            f"/api/v2/offline-sync/backpressure/policy/?device_id={self.device_a.device_id}"
        )
        assert res_pol.status_code == status.HTTP_200_OK
        assert res_pol.json()["max_batch_size"] == 50

        # POST /validate
        res_val = self.client.post(
            "/api/v2/offline-sync/backpressure/validate/",
            data={"device_id": self.device_a.device_id, "event_count": 10, "payload_bytes": 5000},
            format="json",
        )
        assert res_val.status_code == status.HTTP_200_OK

        # POST /progress
        res_prog = self.client.post(
            "/api/v2/offline-sync/backpressure/progress/",
            data={
                "device_id": self.device_a.device_id,
                "total_enqueued": 50,
                "processed_count": 25,
                "failed_count": 0,
                "is_pos_checkout_active": True,
            },
            format="json",
        )
        assert res_prog.status_code == status.HTTP_200_OK
        assert res_prog.json()["suggested_batch_size"] <= 10

        # POST /configure
        res_cfg = self.client.post(
            "/api/v2/offline-sync/backpressure/configure/",
            data={"device_id": self.device_a.device_id, "max_batch_size": 25},
            format="json",
        )
        assert res_cfg.status_code == status.HTTP_200_OK
        assert res_cfg.json()["max_batch_size"] == 25

        # POST /reset
        res_rst = self.client.post(
            "/api/v2/offline-sync/backpressure/reset/",
            data={"device_id": self.device_a.device_id},
            format="json",
        )
        assert res_rst.status_code == status.HTTP_200_OK
        assert res_rst.json()["is_throttled"] is False
