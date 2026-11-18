"""
backend/tests/test_p306_canonical_acknowledgment.py
Architecture Decision: ADR-0306 (P306).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Transactional Commit & Replay-Safe Canonical Acknowledgments:
1. Valid command commits transactionally, deducts real BatchStock, and returns stable canonical_entity_id + SHA-256 ack signature.
2. Dropped-response replay drill: Re-sending identical idempotency_key returns EXACT same ack_id, canonical_entity_id,
   and ack_signature_sha256 with disposition=REPLAY_IDEMPOTENT, increments replay_count, and NEVER double-deducts stock.
3. Crash-recovery /lookup/ retrieves committed canonical acks by idempotency_keys[] and reports missing keys;
   /confirm-applied/ stamps acknowledged_by_client_at.
4. Two-device stock overrun returns TERMINAL_CONFLICT with ERR_SYNC_CONFLICT while preserving prior committed commands.
5. Incomplete command payload (422), cross-branch / cross-tenant / expired lease / suspended tenant (403).
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
    CanonicalAckDisposition,
    CanonicalCommandAcknowledgment,
    DeviceLocalDbRegistration,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestCanonicalAcknowledgmentIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()
        self.fp = "f1e2d3c4" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-ca-{self.suffix}",
            name=f"Ack Pharma {self.suffix}",
            subdomain=f"ca-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-ca1-{self.suffix}", tenant=self.tenant_a, name="Ack Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-ca2-{self.suffix}", tenant=self.tenant_a, name="Ack Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-cab-{self.suffix}",
            name=f"Other Ack {self.suffix}",
            subdomain=f"cab-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-cab1-{self.suffix}", tenant=self.tenant_b, name="Other Ack Branch"
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
            product_id=f"med-ca-{self.suffix}",
            brand_name="Pantonix 20",
            generic_name="Pantoprazole",
            dosage_form="TABLET",
            cost_unit=Decimal("5.00"),
            mrp_unit=Decimal("8.00"),
        )
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-ca-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="CA-01",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=300),
            quantity_base_units=10,
            cost_per_unit=Decimal("5.00"),
            status=BatchStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.owner_a)
        reg = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-ACK-01",
                "device_fingerprint_sha256": self.fp,
            },
            format="json",
        )
        assert reg.status_code == status.HTTP_201_CREATED
        self.reg_id = reg.data["registration_id"]

    def test_01_transactional_commit_replay_safe_ack_lookup_and_conflict(self):
        self.client.force_authenticate(user=self.cashier_a1)
        idem1 = f"idem-ca-1-{self.suffix}"
        idem2 = f"idem-ca-2-{self.suffix}"

        # 1. Commit valid sale of 7 units (stock 10 -> 3) + conflicting sale of 8 units (exceeds remaining 3!)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "device_id": "POS-ACK-01",
            "device_fingerprint_sha256": self.fp,
            "commands": [
                {
                    "command_id": "cmd-ca-1",
                    "idempotency_key": idem1,
                    "client_sequence": 1,
                    "command_type": "OFFLINE_POS_SALE",
                    "aggregate_type": "SALE_INVOICE",
                    "aggregate_ref": "OFF-INV-CA1",
                    "monetary_total_bdt": "56.00",
                    "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 7},
                },
                {
                    "command_id": "cmd-ca-2",
                    "idempotency_key": idem2,
                    "client_sequence": 2,
                    "command_type": "OFFLINE_POS_SALE",
                    "aggregate_type": "SALE_INVOICE",
                    "aggregate_ref": "OFF-INV-CA2",
                    "monetary_total_bdt": "64.00",
                    "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 8},
                },
            ],
        }
        r1 = self.client.post("/api/v2/offline-sync/canonical-acks/commit/", payload, format="json")
        assert r1.status_code == status.HTTP_200_OK, r1.data
        assert r1.data["committed_count"] == 1
        assert r1.data["conflict_count"] == 1
        ack1 = r1.data["acknowledgments"][0]
        ack2 = r1.data["acknowledgments"][1]
        assert ack1["disposition"] == CanonicalAckDisposition.COMMITTED
        assert ack1["canonical_entity_id"] != ""
        assert len(ack1["ack_signature_sha256"]) == 64
        assert ack2["disposition"] == CanonicalAckDisposition.TERMINAL_CONFLICT
        assert ack2["error_code"] == "ERR_SYNC_CONFLICT"

        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 3

        # 2. Replay identical first command (simulating dropped HTTP 200) -> exact same canonical ID, replay_count=1, stock still 3!
        r_rep = self.client.post(
            "/api/v2/offline-sync/canonical-acks/commit/",
            {**payload, "commands": [payload["commands"][0]]},
            format="json",
        )
        assert r_rep.status_code == status.HTTP_200_OK
        assert r_rep.data["replay_idempotent_count"] == 1
        rep_ack = r_rep.data["acknowledgments"][0]
        assert rep_ack["disposition"] == CanonicalAckDisposition.REPLAY_IDEMPOTENT
        assert rep_ack["ack_id"] == ack1["ack_id"]
        assert rep_ack["canonical_entity_id"] == ack1["canonical_entity_id"]
        assert rep_ack["ack_signature_sha256"] == ack1["ack_signature_sha256"]
        assert rep_ack["replay_count"] == 1
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 3

        # 3. Crash-recovery lookup & client confirmation
        lk = self.client.post(
            "/api/v2/offline-sync/canonical-acks/lookup/",
            {"tenant_id": self.tenant_a.tenant_id, "idempotency_keys": [idem1, "missing-key-999"]},
            format="json",
        )
        assert lk.status_code == status.HTTP_200_OK
        assert lk.data["found_count"] == 1
        assert lk.data["missing_keys"] == ["missing-key-999"]

        conf = self.client.post(
            "/api/v2/offline-sync/canonical-acks/confirm-applied/",
            {"tenant_id": self.tenant_a.tenant_id, "ack_ids": [ack1["ack_id"]]},
            format="json",
        )
        assert conf.status_code == status.HTTP_200_OK
        assert conf.data["confirmed_count"] == 1
        assert CanonicalCommandAcknowledgment.objects.get(ack_id=ack1["ack_id"]).acknowledged_by_client_at is not None

    def test_02_invalid_incomplete_cross_branch_cross_tenant_and_suspended_blocked(self):
        self.client.force_authenticate(user=self.cashier_a1)
        bad = self.client.post(
            "/api/v2/offline-sync/canonical-acks/commit/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-ACK-01",
                "device_fingerprint_sha256": self.fp,
                "commands": [{"command_id": ""}],
            },
            format="json",
        )
        assert bad.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Cross-branch cashier denied (403)
        self.client.force_authenticate(user=self.cashier_a2)
        assert (
            self.client.post(
                "/api/v2/offline-sync/canonical-acks/commit/",
                {
                    "tenant_id": self.tenant_a.tenant_id,
                    "device_id": "POS-ACK-01",
                    "device_fingerprint_sha256": self.fp,
                    "commands": [{"command_id": "c", "idempotency_key": "k", "aggregate_ref": "A"}],
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Cross-tenant Owner B denied (403)
        self.client.force_authenticate(user=self.owner_b)
        assert (
            self.client.post(
                "/api/v2/offline-sync/canonical-acks/commit/",
                {
                    "tenant_id": self.tenant_b.tenant_id,
                    "device_id": "POS-ACK-01",
                    "device_fingerprint_sha256": self.fp,
                    "commands": [{"command_id": "c", "idempotency_key": "k", "aggregate_ref": "A"}],
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Expired lease & suspended tenant blocked (403)
        reg = DeviceLocalDbRegistration.objects.get(registration_id=self.reg_id)
        reg.lease_expires_at = timezone.now() - datetime.timedelta(minutes=5)
        reg.save()
        self.client.force_authenticate(user=self.cashier_a1)
        assert (
            self.client.post(
                "/api/v2/offline-sync/canonical-acks/commit/",
                {
                    "tenant_id": self.tenant_a.tenant_id,
                    "device_id": "POS-ACK-01",
                    "device_fingerprint_sha256": self.fp,
                    "commands": [{"command_id": "c", "idempotency_key": "k", "aggregate_ref": "A"}],
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        assert self.client.get(f"/api/v2/offline-sync/canonical-acks/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN
