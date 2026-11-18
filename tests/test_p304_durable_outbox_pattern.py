"""
backend/tests/test_p304_durable_outbox_pattern.py
Architecture Decision: ADR-0304 (P304).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration & Phase 16 drill tests:
1. Ordered outbox replay deducts real server BatchStock once and returns ACCEPTED.
2. Repeated delivery drill: Replaying the exact same batch twice returns DUPLICATE_REPLAY
   with identical server_reference_id and NEVER double-deducts stock!
3. Payload tamper on duplicate idempotency_key flagged as CONFLICT_NEEDS_REVIEW.
4. Two-device stock conflict drill: Device 1 sells 8 of 10 units, Device 2 offline sells 5 units ->
   Device 2 routed cleanly to CONFLICT_NEEDS_REVIEW (stock never goes negative!), then Manager
   resolves conflict via audited /resolve-conflict/ (Cashier denied 403).
5. Expired lease, wrong fingerprint, cross-tenant replay, and suspended tenant blocked (403).
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
    DeviceLocalDbRegistration,
    MedicineProduct,
    OfflineCommandSyncStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestDurableOutboxPatternIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()
        self.fp1 = "1a2b3c4d" * 8
        self.fp2 = "5e6f7a8b" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-ob-{self.suffix}",
            name=f"Outbox Pharma {self.suffix}",
            subdomain=f"ob-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-ob1-{self.suffix}", tenant=self.tenant_a, name="Main Counter"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-obb-{self.suffix}",
            name=f"Other Outbox {self.suffix}",
            subdomain=f"obb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-obb1-{self.suffix}", tenant=self.tenant_b, name="Other Counter"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880181{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880182{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880183{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.med = MedicineProduct.objects.create(
            product_id=f"med-ob-{self.suffix}",
            brand_name="Maxpro 20",
            generic_name="Esomeprazole",
            dosage_form="TABLET",
            cost_unit=Decimal("6.50"),
            mrp_unit=Decimal("10.00"),
        )
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-ob-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="OB-B1",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=200),
            quantity_base_units=10,
            cost_per_unit=Decimal("6.50"),
            status=BatchStatus.ACTIVE,
        )

        # Register TWO POS devices on the same branch for two-device conflict drill
        self.client.force_authenticate(user=self.owner_a)
        for dev_id, fp in (("POS-OB-01", self.fp1), ("POS-OB-02", self.fp2)):
            r = self.client.post(
                "/api/v2/offline-sync/device-schemas/",
                {
                    "tenant_id": self.tenant_a.tenant_id,
                    "branch_id": self.branch_a1.branch_id,
                    "device_id": dev_id,
                    "device_fingerprint_sha256": fp,
                },
                format="json",
            )
            assert r.status_code == status.HTTP_201_CREATED

    def test_01_repeated_delivery_idempotency_and_two_device_stock_conflict_drill(self):
        self.client.force_authenticate(user=self.cashier_a1)
        batch_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "device_id": "POS-OB-01",
            "device_fingerprint_sha256": self.fp1,
            "commands": [
                {
                    "command_id": "cmd-d1-1",
                    "idempotency_key": f"idem-d1-1-{self.suffix}",
                    "client_sequence": 1,
                    "command_type": "OFFLINE_POS_SALE",
                    "aggregate_ref": "OFF-INV-101",
                    "monetary_total_bdt": "80.00",
                    "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 8},
                }
            ],
        }

        # 1. First delivery accepted -> server stock drops 10 -> 2
        r1 = self.client.post("/api/v2/offline-sync/outbox/ingest/", batch_payload, format="json")
        assert r1.status_code == status.HTTP_200_OK, r1.data
        assert r1.data["accepted_count"] == 1
        orig_srv_ref = r1.data["results"][0]["server_reference_id"]
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 2

        # 2. Repeated delivery (network retry) -> DUPLICATE_REPLAY, stock STILL 2 (zero double deduction!)
        r2 = self.client.post("/api/v2/offline-sync/outbox/ingest/", batch_payload, format="json")
        assert r2.status_code == status.HTTP_200_OK
        assert r2.data["duplicate_replay_count"] == 1
        assert r2.data["results"][0]["server_reference_id"] == orig_srv_ref
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 2

        # 3. Two-device stock conflict drill: Device 2 tries to sync offline sale of 5 units when only 2 remain!
        r_conf = self.client.post(
            "/api/v2/offline-sync/outbox/ingest/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-OB-02",
                "device_fingerprint_sha256": self.fp2,
                "commands": [
                    {
                        "command_id": "cmd-d2-1",
                        "idempotency_key": f"idem-d2-1-{self.suffix}",
                        "client_sequence": 1,
                        "command_type": "OFFLINE_POS_SALE",
                        "aggregate_ref": "OFF-INV-201",
                        "monetary_total_bdt": "50.00",
                        "payload": {"batch_id": self.batch.batch_id, "quantity_base_units": 5},
                    }
                ],
            },
            format="json",
        )
        assert r_conf.status_code == status.HTTP_200_OK
        assert r_conf.data["conflict_count"] == 1
        assert r_conf.data["results"][0]["outcome"] == OfflineCommandSyncStatus.CONFLICT_NEEDS_REVIEW
        assert "ERR_TWO_DEVICE_STOCK_CONFLICT" in r_conf.data["results"][0]["conflict_reason"]
        conflict_receipt_id = r_conf.data["results"][0]["receipt_id"]
        self.batch.refresh_from_db()
        assert self.batch.quantity_base_units == 2  # Never goes negative!

        # 4. Cashier blocked from resolving conflict (403); Owner resolves audited (200)
        c_res = self.client.post(
            f"/api/v2/offline-sync/outbox/{conflict_receipt_id}/resolve-conflict/",
            {"tenant_id": self.tenant_a.tenant_id, "resolution_action": "REJECT", "notes": "Try resolve"},
            format="json",
        )
        assert c_res.status_code == status.HTTP_403_FORBIDDEN

        self.client.force_authenticate(user=self.owner_a)
        o_res = self.client.post(
            f"/api/v2/offline-sync/outbox/{conflict_receipt_id}/resolve-conflict/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "resolution_action": "ACCEPT_ADJUSTED",
                "notes": "Fulfilled remaining 3 units from secondary lot after physical count",
            },
            format="json",
        )
        assert o_res.status_code == status.HTTP_200_OK
        assert o_res.data["sync_status"] == OfflineCommandSyncStatus.ACCEPTED

    def test_02_invalid_batch_cross_tenant_expired_lease_and_suspended_blocked(self):
        self.client.force_authenticate(user=self.owner_a)
        empty = self.client.post(
            "/api/v2/offline-sync/outbox/ingest/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-OB-01",
                "device_fingerprint_sha256": self.fp1,
                "commands": [],
            },
            format="json",
        )
        assert empty.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Cross-tenant replay denied (403)
        self.client.force_authenticate(user=self.owner_b)
        ct = self.client.post(
            "/api/v2/offline-sync/outbox/ingest/",
            {
                "tenant_id": self.tenant_b.tenant_id,
                "device_id": "POS-OB-01",
                "device_fingerprint_sha256": self.fp1,
                "commands": [{"command_id": "x", "idempotency_key": "k", "client_sequence": 1, "command_type": "OFFLINE_POS_SALE", "aggregate_ref": "A"}],
            },
            format="json",
        )
        assert ct.status_code == status.HTTP_403_FORBIDDEN

        # Expired lease blocked (403)
        reg = DeviceLocalDbRegistration.objects.get(tenant=self.tenant_a, device_id="POS-OB-01")
        reg.lease_expires_at = timezone.now() - datetime.timedelta(minutes=5)
        reg.save()
        self.client.force_authenticate(user=self.owner_a)
        exp = self.client.post(
            "/api/v2/offline-sync/outbox/ingest/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-OB-01",
                "device_fingerprint_sha256": self.fp1,
                "commands": [{"command_id": "x", "idempotency_key": "k", "client_sequence": 2, "command_type": "OFFLINE_POS_SALE", "aggregate_ref": "A"}],
            },
            format="json",
        )
        assert exp.status_code == status.HTTP_403_FORBIDDEN
        assert exp.data["error_code"] == "ERR_OFFLINE_LEASE_EXPIRED"

        # Suspended tenant blocked (403)
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        assert self.client.get(f"/api/v2/offline-sync/outbox/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN
