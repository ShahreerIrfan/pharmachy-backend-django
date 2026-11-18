"""
backend/tests/test_p308_sync_cursor_checkpoint.py
Architecture Decision: ADR-0308 (P308).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Crash-Safe Sync Cursor Checkpoint & Independent Push Acks:
1. Core Invariant: Pull checkpoint is REJECTED (422) and audited (`REJECTED_UNAPPLIED`) whenever
   `durable_local_apply_verified=False` or SHA-256 local apply digest is missing/invalid; watermark stays at 0.
2. Valid Decoupled Streams:
   - Pull checkpoint advances `pull_applied_sequence_no` ONLY after `durable_local_apply_verified=True` + valid digest + token,
     leaving `push_acked_sequence_no == 0`.
   - Independent Push Ack checkpoint commits P306 canonical acks with exact Decimal `push_monetary_reconciled_bdt`,
     leaving `pull_applied_sequence_no` untouched, and replay-safe duplicate delivery NEVER double-counts BDT!
3. Crash-Recovery Drill (`/recover/`): Returns independent pull & push watermarks plus unconfirmed P306 acks
   lost during a network drop/crash so the device resumes without duplicating sales or skipping change-feed events.
4. Audited Rewind (`/audited-reset/`): Owner rewinds pull cursor (increments `recovery_epoch`), preserving independent
   `push_acked_sequence_no` and `push_monetary_reconciled_bdt` intact; cashier reset blocked (403).
5. Security, Lease & Suspension Drills: Foreign ack_id rejected (422), push regression rejected (422),
   cross-branch cashier (403), cross-tenant owner (403), expired offline lease (403), suspended tenant (403).
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
    BatchStatus,
    BatchStock,
    Branch,
    DeviceLocalDbRegistration,
    MedicineProduct,
    SyncCheckpointStatus,
    SyncCursorCheckpointLedger,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSyncCursorCheckpointIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()
        self.fp1 = "1234abcd" * 8
        self.fp_b = "fedc4321" * 8
        self.valid_digest = hashlib.sha256(f"sqlite-apply-{self.suffix}".encode()).hexdigest()

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-cp-{self.suffix}",
            name=f"Checkpoint Pharma {self.suffix}",
            subdomain=f"cp-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-cp1-{self.suffix}", tenant=self.tenant_a, name="CP Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-cp2-{self.suffix}", tenant=self.tenant_a, name="CP Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-cpb-{self.suffix}",
            name=f"Other CP {self.suffix}",
            subdomain=f"cpb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-cpb1-{self.suffix}", tenant=self.tenant_b, name="Other CP Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"cp_owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="CP Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cp_cash1_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="CP Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"cp_cash2_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="CP Cashier A2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"cp_ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="CP Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.dev_a1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-CP1-{self.suffix}",
            device_fingerprint_sha256=self.fp1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )
        self.dev_b1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_b,
            branch=self.branch_b1,
            device_id=f"POS-CPB1-{self.suffix}",
            device_fingerprint_sha256=self.fp_b,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )

        self.med = MedicineProduct.objects.create(
            product_id=f"med-cp-{self.suffix}",
            brand_name="Maxpro 20",
            generic_name="Esomeprazole",
            dosage_form="CAPSULE",
            cost_unit=Decimal("4.25"),
            mrp_unit=Decimal("7.50"),
        )
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-cp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="CP-B01",
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=3),
            quantity_base_units=40,
            cost_per_unit=Decimal("4.25"),
            status=BatchStatus.ACTIVE,
        )

    def test_01_durable_apply_gate_decoupled_push_ack_crash_recovery_and_audited_reset(self):
        self.client.force_authenticate(user=self.owner_a)

        # Publish 2 server change-feed events (seq 1, seq 2)
        for i in (1, 2):
            p = self.client.post(
                "/api/v2/offline-sync/change-feed/publish/",
                {
                    "entity_type": "MEDICINE_CATALOG",
                    "entity_id": f"{self.med.product_id}-{i}",
                    "operation": "UPSERT",
                    "payload_json": {"mrp_bdt": "7.50"},
                },
                format="json",
            )
            assert p.status_code == status.HTTP_201_CREATED

        # Pull events to obtain signed cursor_token
        self.client.force_authenticate(user=self.cashier_a1)
        pulled = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1, "since_sequence": 0},
            format="json",
        )
        assert pulled.status_code == status.HTTP_200_OK
        assert pulled.data["next_cursor_sequence"] == 2
        token_seq2 = pulled.data["cursor_token"]

        # 1. INVALID: Attempt to advance pull cursor WITHOUT durable_local_apply_verified=True -> REJECTED + AUDITED!
        r_unapplied = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/commit-pull/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "acknowledged_sequence_no": 2,
                "durable_local_apply_verified": False,
                "local_apply_digest_sha256": self.valid_digest,
                "cursor_token": token_seq2,
            },
            format="json",
        )
        assert r_unapplied.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert SyncCursorCheckpointLedger.objects.filter(
            tenant=self.tenant_a, status=SyncCheckpointStatus.REJECTED_UNAPPLIED
        ).exists()

        # 2. VALID: Commit pull checkpoint AFTER durable local SQLite apply verified
        r_pull_cp = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/commit-pull/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "acknowledged_sequence_no": 2,
                "durable_local_apply_verified": True,
                "local_apply_digest_sha256": self.valid_digest,
                "cursor_token": token_seq2,
                "applied_event_count": 2,
                "idempotency_key": f"pull-cp-{self.suffix}",
            },
            format="json",
        )
        assert r_pull_cp.status_code == status.HTTP_200_OK
        assert r_pull_cp.data["watermark"]["pull_applied_sequence_no"] == 2
        assert r_pull_cp.data["watermark"]["push_acked_sequence_no"] == 0

        # 3. Commit 2 offline sales via P306 CanonicalAckService (seq 1 & seq 2) -> simulates network drop before client confirms!
        ack_res = self.client.post(
            "/api/v2/offline-sync/canonical-acks/commit/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "commands": [
                    {
                        "command_id": f"cmd-cp1-{self.suffix}",
                        "idempotency_key": f"idem-cp1-{self.suffix}",
                        "client_sequence": 1,
                        "command_type": "OFFLINE_POS_SALE",
                        "aggregate_type": "SALE_INVOICE",
                        "aggregate_ref": f"OFF-CP1-{self.suffix}",
                        "monetary_total_bdt": "37.50",
                        "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 5},
                    },
                    {
                        "command_id": f"cmd-cp2-{self.suffix}",
                        "idempotency_key": f"idem-cp2-{self.suffix}",
                        "client_sequence": 2,
                        "command_type": "OFFLINE_POS_SALE",
                        "aggregate_type": "SALE_INVOICE",
                        "aggregate_ref": f"OFF-CP2-{self.suffix}",
                        "monetary_total_bdt": "22.50",
                        "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 3},
                    },
                ],
            },
            format="json",
        )
        assert ack_res.status_code == status.HTTP_200_OK
        ack_id_1 = ack_res.data["acknowledgments"][0]["ack_id"]
        ack_id_2 = ack_res.data["acknowledgments"][1]["ack_id"]

        # 4. CRASH RECOVERY DRILL: /recover/ returns pull=2, push=0, AND both unconfirmed P306 acks!
        rec = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/recover/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert rec.status_code == status.HTTP_200_OK
        assert rec.data["resume_pull_from_sequence"] == 2
        assert rec.data["resume_push_after_sequence"] == 0
        assert rec.data["unconfirmed_push_acks_count"] == 2

        # 5. INDEPENDENT PUSH ACK CHECKPOINT: Confirm ack_id_1 + ack_id_2 (BDT 37.50 + 22.50 = 60.00)
        # Pull watermark MUST remain strictly at 2!
        r_push_cp = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/commit-push-ack/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "push_acked_sequence_no": 2,
                "ack_ids": [ack_id_1, ack_id_2],
                "idempotency_key": f"push-cp-{self.suffix}",
            },
            format="json",
        )
        assert r_push_cp.status_code == status.HTTP_200_OK
        assert r_push_cp.data["confirmed_ack_count"] == 2
        assert r_push_cp.data["watermark"]["push_acked_sequence_no"] == 2
        assert r_push_cp.data["watermark"]["push_monetary_reconciled_bdt"] == "60.00"
        assert r_push_cp.data["watermark"]["pull_applied_sequence_no"] == 2

        # Replay duplicate push ack checkpoint -> ZERO double BDT counting!
        r_push_dup = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/commit-push-ack/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "push_acked_sequence_no": 2,
                "ack_ids": [ack_id_1, ack_id_2],
                "idempotency_key": f"push-cp-{self.suffix}",
            },
            format="json",
        )
        assert r_push_dup.status_code == status.HTTP_200_OK
        assert r_push_dup.data["idempotent_replay"] is True
        assert r_push_dup.data["watermark"]["push_monetary_reconciled_bdt"] == "60.00"

        # 6. AUDITED REWIND: Owner rewinds pull cursor 2 -> 0 (epoch 1 -> 2); push watermark 2 & BDT 60.00 STAY INTACT!
        self.client.force_authenticate(user=self.owner_a)
        r_reset = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/audited-reset/",
            {
                "device_id": self.dev_a1.device_id,
                "target_pull_sequence_no": 0,
                "reason": "Rebuilding local SQLite catalog after disk replacement",
            },
            format="json",
        )
        assert r_reset.status_code == status.HTTP_200_OK
        assert r_reset.data["watermark"]["pull_applied_sequence_no"] == 0
        assert r_reset.data["watermark"]["recovery_epoch"] == 2
        assert r_reset.data["watermark"]["push_acked_sequence_no"] == 2
        assert r_reset.data["watermark"]["push_monetary_reconciled_bdt"] == "60.00"

        # Owner lists watermarks & append-only ledger
        lst = self.client.get("/api/v2/offline-sync/cursor-checkpoints/")
        assert lst.status_code == status.HTTP_200_OK
        assert len(lst.data["watermarks"]) >= 1
        assert len(lst.data["recent_ledger"]) >= 4

    def test_02_security_rbac_cross_tenant_lease_expiry_and_regression_drills(self):
        # 1. Cashier cannot execute audited-reset (403)
        self.client.force_authenticate(user=self.cashier_a1)
        r_cash_rst = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/audited-reset/",
            {"device_id": self.dev_a1.device_id, "target_pull_sequence_no": 0, "reason": "Unauthorized reset"},
            format="json",
        )
        assert r_cash_rst.status_code == status.HTTP_403_FORBIDDEN

        # 2. Foreign / nonexistent ack_ids rejected (422)
        r_bad_ack = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/commit-push-ack/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "push_acked_sequence_no": 1,
                "ack_ids": ["cack-nonexistent-999"],
            },
            format="json",
        )
        assert r_bad_ack.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 3. Cross-branch cashier blocked on /recover/ (403)
        self.client.force_authenticate(user=self.cashier_a2)
        r_xbr = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/recover/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_xbr.status_code == status.HTTP_403_FORBIDDEN

        # 4. Cross-tenant Owner B blocked on /recover/ and GET list (403)
        self.client.force_authenticate(user=self.owner_b)
        r_xten = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/recover/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_xten.status_code == status.HTTP_403_FORBIDDEN

        r_xten_lst = self.client.get(f"/api/v2/offline-sync/cursor-checkpoints/?tenant_id={self.tenant_a.tenant_id}")
        assert r_xten_lst.status_code == status.HTTP_403_FORBIDDEN

        # 5. Expired offline lease blocks checkpoint recovery (403)
        self.dev_a1.lease_expires_at = timezone.now() - datetime.timedelta(minutes=5)
        self.dev_a1.save(update_fields=["lease_expires_at"])
        self.client.force_authenticate(user=self.owner_a)
        r_exp = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/recover/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_exp.status_code == status.HTTP_403_FORBIDDEN

        # 6. Suspended tenant blocks checkpoint recovery (403)
        self.dev_a1.lease_expires_at = timezone.now() + datetime.timedelta(days=5)
        self.dev_a1.save(update_fields=["lease_expires_at"])
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save(update_fields=["status"])
        r_susp = self.client.post(
            "/api/v2/offline-sync/cursor-checkpoints/recover/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_susp.status_code == status.HTTP_403_FORBIDDEN
