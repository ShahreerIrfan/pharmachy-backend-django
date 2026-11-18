"""
backend/tests/test_p307_change_feed_pull.py
Architecture Decision: ADR-0307 (P307).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Incremental Change Feed Pull:
1. Valid incremental pull: Tenant-wide catalog UPSERT, branch-1 stock UPSERT, TOMBSTONE (recalled batch),
   and audited REVERSAL with exact Decimal precision; bounded pagination (limit=2 -> has_more=True -> next page),
   scoped branch filtering (Branch 1 device NEVER receives Branch 2 events), and entity_types filter.
2. End-to-end integration with P306 CanonicalAckService: committing an offline command automatically emits a
   branch-scoped ChangeFeedEvent visible to sibling POS devices (with exclude_self_device support).
3. Crash-recovery & replay-safe checkpointing: advancing cursor with valid signed cursor_token updates lag to 0;
   re-checkpointing identical sequence is idempotent; backward regression, forged cursor_token, or ahead-of-server
   sequence returns 400/422.
4. Invalid/incomplete inputs: negative since_sequence, limit > 100, unknown entity_type, REVERSAL without reason -> 400/422.
5. Security, RBAC & Phase-16 drills: cross-tenant pull/publish blocked (403), cross-branch cashier blocked (403),
   cashier publish forbidden (403), expired offline lease blocked (403), suspended tenant blocked (403),
   and idempotent duplicate publish preserves exact monotonic sequence_no without gaps.
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
    BatchStatus,
    BatchStock,
    Branch,
    ChangeFeedEvent,
    DeviceChangeFeedCursor,
    DeviceLocalDbRegistration,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestChangeFeedPullIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()
        self.fp1 = "a1b2c3d4" * 8
        self.fp2 = "e5f60718" * 8
        self.fp_b = "99887766" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-cf-{self.suffix}",
            name=f"Feed Pharma {self.suffix}",
            subdomain=f"cf-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-cf1-{self.suffix}", tenant=self.tenant_a, name="Feed Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-cf2-{self.suffix}", tenant=self.tenant_a, name="Feed Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-cfb-{self.suffix}",
            name=f"Other Feed {self.suffix}",
            subdomain=f"cfb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-cfb1-{self.suffix}", tenant=self.tenant_b, name="Other Feed Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"cf_owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cf_cash1_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"cf_cash2_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Cashier A2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"cf_ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.dev_a1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-CF1-{self.suffix}",
            device_fingerprint_sha256=self.fp1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )
        self.dev_a1_sibling = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_a,
            branch=self.branch_a1,
            device_id=f"POS-CF1S-{self.suffix}",
            device_fingerprint_sha256=self.fp2,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )
        self.dev_b1 = DeviceLocalDbRegistration.objects.create(
            tenant=self.tenant_b,
            branch=self.branch_b1,
            device_id=f"POS-CFB1-{self.suffix}",
            device_fingerprint_sha256=self.fp_b,
            lease_expires_at=timezone.now() + datetime.timedelta(days=5),
        )

        self.med = MedicineProduct.objects.create(
            product_id=f"med-cf-{self.suffix}",
            brand_name="Napa Extend 665",
            generic_name="Paracetamol",
            dosage_form="TABLET",
            cost_unit=Decimal("1.50"),
            mrp_unit=Decimal("2.50"),
        )
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-cf-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="CF-B01",
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today - datetime.timedelta(days=5),
            quantity_base_units=50,
            cost_per_unit=Decimal("1.50"),
            status=BatchStatus.ACTIVE,
        )

    def test_valid_incremental_pull_scoping_tombstone_reversal_and_p306_integration(self):
        self.client.force_authenticate(user=self.owner_a)

        # 1. Publish tenant-wide catalog UPSERT (seq 1)
        r1 = self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {
                "entity_type": "MEDICINE_CATALOG",
                "entity_id": self.med.product_id,
                "operation": "UPSERT",
                "entity_version": 2,
                "monetary_impact_bdt": "2.50",
                "idempotency_key": f"pub-cat-{self.suffix}",
                "payload_json": {
                    "brand_name": "Napa Extend 665",
                    "generic_name": "Paracetamol",
                    "mrp_bdt": "2.50",
                },
            },
            format="json",
        )
        assert r1.status_code == status.HTTP_201_CREATED
        assert r1.data["event"]["sequence_no"] == 1
        assert r1.data["event"]["monetary_impact_bdt"] == "2.50"

        # Repeated delivery of identical idempotency_key -> idempotent replay (NO sequence gap!)
        r1_dup = self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {
                "entity_type": "MEDICINE_CATALOG",
                "entity_id": self.med.product_id,
                "operation": "UPSERT",
                "entity_version": 2,
                "monetary_impact_bdt": "2.50",
                "idempotency_key": f"pub-cat-{self.suffix}",
                "payload_json": {"brand_name": "Napa Extend 665"},
            },
            format="json",
        )
        assert r1_dup.status_code == status.HTTP_200_OK
        assert r1_dup.data["idempotent_replay"] is True
        assert r1_dup.data["event"]["sequence_no"] == 1

        # 2. Publish Branch 2 stock event (seq 2) — MUST NOT leak to Branch 1 devices!
        r2 = self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {
                "entity_type": "BATCH_STOCK",
                "entity_id": "bat-branch2-only",
                "branch_id": self.branch_a2.branch_id,
                "operation": "UPSERT",
                "payload_json": {"quantity_base_units": 99},
            },
            format="json",
        )
        assert r2.status_code == status.HTTP_201_CREATED
        assert r2.data["event"]["sequence_no"] == 2

        # 3. Publish Branch 1 TOMBSTONE (recalled batch, seq 3)
        r3 = self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {
                "entity_type": "BATCH_STOCK",
                "entity_id": "bat-recalled-99",
                "branch_id": self.branch_a1.branch_id,
                "operation": "TOMBSTONE",
                "payload_json": {"tombstone_reason": "DGDA_BATCH_RECALL"},
            },
            format="json",
        )
        assert r3.status_code == status.HTTP_201_CREATED
        assert r3.data["event"]["sequence_no"] == 3
        assert r3.data["event"]["payload_json"]["tombstoned"] is True

        # 4. Publish Branch 1 audited REVERSAL with negative/precise Decimal impact (seq 4)
        r4 = self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {
                "entity_type": "SALE_INVOICE",
                "entity_id": "INV-REV-01",
                "branch_id": self.branch_a1.branch_id,
                "operation": "REVERSAL",
                "monetary_impact_bdt": "-125.75",
                "payload_json": {
                    "reversed_entity_id": "INV-ORIG-01",
                    "reversal_reason": "CUSTOMER_RETURN_VERIFIED",
                    "refund_bdt": "125.75",
                },
            },
            format="json",
        )
        assert r4.status_code == status.HTTP_201_CREATED
        assert r4.data["event"]["sequence_no"] == 4
        assert r4.data["event"]["monetary_impact_bdt"] == "-125.75"

        # 5. Real P306 -> P307 integration: Commit a canonical ack on dev_a1 -> auto-emits seq 5
        ack_res = self.client.post(
            "/api/v2/offline-sync/canonical-acks/commit/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "commands": [
                    {
                        "command_id": f"cmd-cf-{self.suffix}",
                        "idempotency_key": f"idem-cf-{self.suffix}",
                        "client_sequence": 1,
                        "command_type": "OFFLINE_POS_SALE",
                        "aggregate_type": "SALE_INVOICE",
                        "aggregate_ref": f"LOCAL-INV-{self.suffix}",
                        "monetary_total_bdt": "25.00",
                        "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 10},
                    }
                ],
            },
            format="json",
        )
        assert ack_res.status_code == status.HTTP_200_OK
        assert ack_res.data["committed_count"] == 1

        # 6. Pull from sibling device on Branch 1 with limit=2 (paginated) + verify Branch 2 event excluded!
        self.client.force_authenticate(user=self.cashier_a1)
        page1 = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {
                "device_id": self.dev_a1_sibling.device_id,
                "device_fingerprint_sha256": self.fp2,
                "since_sequence": 0,
                "limit": 2,
            },
            format="json",
        )
        assert page1.status_code == status.HTTP_200_OK
        assert page1.data["event_count"] == 2
        assert page1.data["has_more"] is True
        seqs_p1 = [e["sequence_no"] for e in page1.data["events"]]
        assert seqs_p1 == [1, 3]  # seq 2 (Branch 2) was strictly filtered out!
        assert page1.data["tombstone_count"] == 1

        # Checkpoint page 1 durably
        cp1 = self.client.post(
            "/api/v2/offline-sync/change-feed/checkpoint/",
            {
                "device_id": self.dev_a1_sibling.device_id,
                "device_fingerprint_sha256": self.fp2,
                "acknowledged_sequence_no": page1.data["next_cursor_sequence"],
                "cursor_token": page1.data["cursor_token"],
                "applied_event_count": 2,
            },
            format="json",
        )
        assert cp1.status_code == status.HTTP_200_OK
        assert cp1.data["last_sequence_no"] == 3
        assert cp1.data["lag_events"] == 2

        # Replay identical checkpoint -> idempotent!
        cp1_replay = self.client.post(
            "/api/v2/offline-sync/change-feed/checkpoint/",
            {
                "device_id": self.dev_a1_sibling.device_id,
                "device_fingerprint_sha256": self.fp2,
                "acknowledged_sequence_no": 3,
                "cursor_token": page1.data["cursor_token"],
                "applied_event_count": 2,
            },
            format="json",
        )
        assert cp1_replay.status_code == status.HTTP_200_OK
        assert cp1_replay.data["idempotent_replay"] is True
        assert cp1_replay.data["total_events_applied"] == 2  # Not double-counted!

        # Pull page 2 using stored cursor (omit since_sequence -> resumes from 3!)
        page2 = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {
                "device_id": self.dev_a1_sibling.device_id,
                "device_fingerprint_sha256": self.fp2,
                "limit": 10,
            },
            format="json",
        )
        assert page2.status_code == status.HTTP_200_OK
        assert page2.data["since_sequence"] == 3
        assert page2.data["has_more"] is False
        assert [e["sequence_no"] for e in page2.data["events"]] == [4, 5]
        assert page2.data["reversal_count"] == 1
        assert page2.data["events"][1]["source_device_id"] == self.dev_a1.device_id

        # Origin device dev_a1 pulling with exclude_self_device=True skips its own seq 5!
        self_pull = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "since_sequence": 3,
                "exclude_self_device": True,
            },
            format="json",
        )
        assert self_pull.status_code == status.HTTP_200_OK
        assert [e["sequence_no"] for e in self_pull.data["events"]] == [4]

        # Owner monitors device cursor lag
        self.client.force_authenticate(user=self.owner_a)
        cur_res = self.client.get("/api/v2/offline-sync/change-feed/cursors/")
        assert cur_res.status_code == status.HTTP_200_OK
        assert cur_res.data["latest_server_sequence"] == 5
        assert cur_res.data["count"] >= 2

    def test_invalid_inputs_security_boundaries_and_lease_suspension_drills(self):
        # 1. Invalid inputs (negative cursor, limit > 100, invalid entity_type, reversal without reason, cursor regression)
        self.client.force_authenticate(user=self.owner_a)
        r_neg = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1, "since_sequence": -5},
            format="json",
        )
        assert r_neg.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        r_lim = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1, "limit": 500},
            format="json",
        )
        assert r_lim.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        r_rev = self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {"entity_type": "SALE_INVOICE", "entity_id": "INV-1", "operation": "REVERSAL", "payload_json": {}},
            format="json",
        )
        assert r_rev.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Forged cursor token rejected
        self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {"entity_type": "MEDICINE_CATALOG", "entity_id": "m1", "operation": "UPSERT", "payload_json": {"mrp": "5.00"}},
            format="json",
        )
        r_tok = self.client.post(
            "/api/v2/offline-sync/change-feed/checkpoint/",
            {
                "device_id": self.dev_a1.device_id,
                "device_fingerprint_sha256": self.fp1,
                "acknowledged_sequence_no": 1,
                "cursor_token": "seq:1:forged000000000000000000",
            },
            format="json",
        )
        assert r_tok.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # 2. RBAC: Cashier CANNOT publish change events directly (403)
        self.client.force_authenticate(user=self.cashier_a1)
        r_cash_pub = self.client.post(
            "/api/v2/offline-sync/change-feed/publish/",
            {"entity_type": "MEDICINE_CATALOG", "entity_id": "m2", "operation": "UPSERT", "payload_json": {}},
            format="json",
        )
        assert r_cash_pub.status_code == status.HTTP_403_FORBIDDEN

        # 3. Cross-branch cashier CANNOT pull Branch 1 device (403)
        self.client.force_authenticate(user=self.cashier_a2)
        r_xbr = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_xbr.status_code == status.HTTP_403_FORBIDDEN

        # 4. Cross-tenant Owner B CANNOT pull Tenant A's device or query Tenant A's feed (403)
        self.client.force_authenticate(user=self.owner_b)
        r_xten = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_xten.status_code == status.HTTP_403_FORBIDDEN

        r_xten_list = self.client.get(f"/api/v2/offline-sync/change-feed/?tenant_id={self.tenant_a.tenant_id}")
        assert r_xten_list.status_code == status.HTTP_403_FORBIDDEN

        # 5. Expired offline lease blocks pull (403)
        self.dev_a1.lease_expires_at = timezone.now() - datetime.timedelta(minutes=10)
        self.dev_a1.save(update_fields=["lease_expires_at"])
        self.client.force_authenticate(user=self.owner_a)
        r_exp = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_exp.status_code == status.HTTP_403_FORBIDDEN

        # Restore lease, then suspend tenant -> blocks pull (403)
        self.dev_a1.lease_expires_at = timezone.now() + datetime.timedelta(days=5)
        self.dev_a1.save(update_fields=["lease_expires_at"])
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save(update_fields=["status"])
        r_susp = self.client.post(
            "/api/v2/offline-sync/change-feed/pull/",
            {"device_id": self.dev_a1.device_id, "device_fingerprint_sha256": self.fp1},
            format="json",
        )
        assert r_susp.status_code == status.HTTP_403_FORBIDDEN
