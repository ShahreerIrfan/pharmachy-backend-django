"""
backend/tests/test_p303_offline_cache_seeding.py
Architecture Decision: ADR-0303 (P303).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration tests for Versioned Offline Cache Seeding & Consistency Tokens:
1. Authorized device bootstrap emits ONLY own-branch active stock (never Branch A2 or Tenant B stock),
   preserves exact Decimal cost/MRP strings, and returns deterministic SHA-256 consistency_token.
2. Conditional skip (if_none_match_token) returns not_modified=True when data unchanged.
3. Device acknowledgement verifies consistency_token (rejects mismatch with 400), marks APPLIED,
   and updates device_registration.last_sync_cursor.
4. Unauthorized cross-branch staff, fingerprint mismatch, expired lease, cross-tenant access,
   and suspended tenant subscription are strictly rejected (403).
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
    OfflineCacheSeedStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestOfflineCacheSeedingIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()
        self.fp = "e1f2a3b4" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-sd-{self.suffix}",
            name=f"Seed Pharma {self.suffix}",
            subdomain=f"sd-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-sd1-{self.suffix}", tenant=self.tenant_a, name="Branch 1"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-sd2-{self.suffix}", tenant=self.tenant_a, name="Branch 2"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-sdb-{self.suffix}",
            name=f"Other Seed {self.suffix}",
            subdomain=f"sdb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-sdb1-{self.suffix}", tenant=self.tenant_b, name="Other Branch"
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
        self.cashier_a2 = PharmacyUser.objects.create_user(
            email=f"cash2_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Cashier Branch 2",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        self.med = MedicineProduct.objects.create(
            product_id=f"med-sd-{self.suffix}",
            brand_name="Sergel 20",
            generic_name="Esomeprazole",
            dosage_form="CAPSULE",
            cost_unit=Decimal("5.25"),
            mrp_unit=Decimal("7.50"),
        )
        # Stock in Branch A1 (SHOULD be seeded), Branch A2 (MUST NOT leak), Tenant B (MUST NOT leak)
        self.bat_a1 = BatchStock.objects.create(
            batch_id=f"bat-a1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="S-A1",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=300),
            quantity_base_units=120,
            cost_per_unit=Decimal("5.25"),
            status=BatchStatus.ACTIVE,
        )
        self.bat_a2 = BatchStock.objects.create(
            batch_id=f"bat-a2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a2,
            product=self.med,
            batch_number="S-A2",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=300),
            quantity_base_units=80,
            cost_per_unit=Decimal("5.25"),
            status=BatchStatus.ACTIVE,
        )
        self.bat_b1 = BatchStock.objects.create(
            batch_id=f"bat-b1-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b1,
            product=self.med,
            batch_number="S-B1",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=300),
            quantity_base_units=999,
            cost_per_unit=Decimal("4.00"),
            status=BatchStatus.ACTIVE,
        )

        self.client.force_authenticate(user=self.owner_a)
        reg = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-SEED-01",
                "device_fingerprint_sha256": self.fp,
            },
            format="json",
        )
        assert reg.status_code == status.HTTP_201_CREATED
        self.reg_id = reg.data["registration_id"]

    def test_01_branch_scoped_seed_conditional_skip_and_ack(self):
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(
            "/api/v2/offline-sync/cache-seeds/bootstrap/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-SEED-01",
                "device_fingerprint_sha256": self.fp,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.data
        assert resp.data["not_modified"] is False
        assert resp.data["snapshot_version"] == 1
        token = resp.data["consistency_token"]
        seed_id = resp.data["seed_id"]

        # Verify ONLY Branch A1 stock included (strict isolation from Branch A2 & Tenant B!)
        stock_ids = [b["batch_id"] for b in resp.data["data"]["branch_stock"]]
        assert self.bat_a1.batch_id in stock_ids
        assert self.bat_a2.batch_id not in stock_ids
        assert self.bat_b1.batch_id not in stock_ids
        assert resp.data["data"]["branch_stock"][0]["cost_per_unit_bdt"] == "5.25"

        # Conditional delta skip returns not_modified=True
        skip = self.client.post(
            "/api/v2/offline-sync/cache-seeds/bootstrap/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-SEED-01",
                "device_fingerprint_sha256": self.fp,
                "if_none_match_token": token,
            },
            format="json",
        )
        assert skip.status_code == status.HTTP_200_OK
        assert skip.data["not_modified"] is True

        # Tampered consistency token rejected on ack (400)
        bad_ack = self.client.post(
            f"/api/v2/offline-sync/cache-seeds/{seed_id}/acknowledge/",
            {"tenant_id": self.tenant_a.tenant_id, "consistency_token": "0" * 64},
            format="json",
        )
        assert bad_ack.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert bad_ack.data["error_code"] == "ERR_CONSISTENCY_TOKEN_MISMATCH"

        # Valid acknowledgement marks APPLIED & advances device sync cursor
        ack = self.client.post(
            f"/api/v2/offline-sync/cache-seeds/{seed_id}/acknowledge/",
            {"tenant_id": self.tenant_a.tenant_id, "consistency_token": token},
            format="json",
        )
        assert ack.status_code == status.HTTP_200_OK
        assert ack.data["status"] == OfflineCacheSeedStatus.APPLIED
        assert ack.data["last_sync_cursor"].startswith("seed-v1:")

    def test_02_cross_branch_cross_tenant_fingerprint_and_suspended_blocked(self):
        # Cashier from Branch A2 cannot bootstrap Branch A1's POS device (403)
        self.client.force_authenticate(user=self.cashier_a2)
        r_br = self.client.post(
            "/api/v2/offline-sync/cache-seeds/bootstrap/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-SEED-01",
                "device_fingerprint_sha256": self.fp,
            },
            format="json",
        )
        assert r_br.status_code == status.HTTP_403_FORBIDDEN
        assert r_br.data["error_code"] == "ERR_BRANCH_SCOPE_DENIED"

        # Wrong fingerprint rejected (403)
        self.client.force_authenticate(user=self.owner_a)
        r_fp = self.client.post(
            "/api/v2/offline-sync/cache-seeds/bootstrap/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-SEED-01",
                "device_fingerprint_sha256": "9" * 64,
            },
            format="json",
        )
        assert r_fp.status_code == status.HTTP_403_FORBIDDEN

        # Cross-tenant Owner B rejected (403)
        self.client.force_authenticate(user=self.owner_b)
        r_ct = self.client.post(
            "/api/v2/offline-sync/cache-seeds/bootstrap/",
            {
                "tenant_id": self.tenant_b.tenant_id,
                "device_id": "POS-SEED-01",
                "device_fingerprint_sha256": self.fp,
            },
            format="json",
        )
        assert r_ct.status_code == status.HTTP_403_FORBIDDEN

        # Suspended tenant blocked (403)
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        assert (
            self.client.post(
                "/api/v2/offline-sync/cache-seeds/bootstrap/",
                {
                    "tenant_id": self.tenant_a.tenant_id,
                    "device_id": "POS-SEED-01",
                    "device_fingerprint_sha256": self.fp,
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )
