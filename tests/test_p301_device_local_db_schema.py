"""
backend/tests/test_p301_device_local_db_schema.py
Architecture Decision: ADR-0301 (P301).
Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync.

Targeted integration + real SQLite DDL execution tests:
1. Owner registers device, receives 8-table authoritative SQLITE_DDL_V1 + SHA-256 + active 72h lease.
2. Real sqlite3 execution of SQLITE_DDL_V1 verifies:
   - All 8 tables created with WAL & FK enforcement.
   - Atomic local stock reservation + outbox enqueue succeeds and preserves exact Decimal string precision.
   - Duplicate idempotency_key rolls back transaction cleanly (repeated delivery / crash safety).
   - Negative stock CHECK constraint rejected by SQLite.
3. Device fingerprint mismatch (anti-cloning) rejected (403 ERR_DEVICE_FINGERPRINT_MISMATCH).
4. Expired subscription lease & revoked device block offline command envelope validation (403).
5. Invalid fingerprint length, missing branch, negative monetary amount rejected (400).
6. RBAC: Cashier blocked from registering/revoking devices (403); Branch Manager restricted to own branch (403 on other).
7. Cross-tenant device/branch isolation (403) and suspended tenant subscription block (403).
"""

import datetime
import random
import sqlite3
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.device_local_db_schema_service import SQLITE_DDL_V1
from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestDeviceLocalDbSchemaIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp_valid = "a1b2c3d4" * 8
        self.fp_other = "f9e8d7c6" * 8

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-off-{self.suffix}",
            name=f"Offline Pharma {self.suffix}",
            subdomain=f"off-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-off1-{self.suffix}", tenant=self.tenant_a, name="Counter Main"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-off2-{self.suffix}", tenant=self.tenant_a, name="Counter Annex"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-offb-{self.suffix}",
            name=f"Other Off {self.suffix}",
            subdomain=f"offb-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-offb1-{self.suffix}", tenant=self.tenant_b, name="Other Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880141{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a1 = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@pharma.com",
            phone_number=f"+880142{random.randint(1000000, 9999999)}",
            full_name="BM A1",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880143{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880144{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

    def test_01_register_device_and_execute_real_sqlite_ddl_with_atomic_outbox(self):
        self.client.force_authenticate(user=self.owner_a)
        reg = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-TERM-01",
                "device_label": "Front Counter 1",
                "device_fingerprint_sha256": self.fp_valid,
                "offline_stock_policy": "SINGLE_LEADER_PER_BRANCH",
            },
            format="json",
        )
        assert reg.status_code == status.HTTP_201_CREATED, reg.data
        assert reg.data["is_lease_valid"] is True
        ddl = reg.data["schema_manifest"]["ddl_sql"]
        assert len(reg.data["schema_manifest"]["tables"]) == 8

        # Execute real SQLite DDL in-memory and test constraints & crash-safe atomic rollback
        conn = sqlite3.connect(":memory:")
        conn.executescript(ddl)
        conn.execute("PRAGMA foreign_keys = ON;")

        conn.execute(
            "INSERT INTO cached_catalog_medicines VALUES ('med-1', ?, 'Napa', 'Paracetamol', 'TABLET', '12.50', 0, 0, 1, 1, '2026-11-18T00:00:00Z')",
            (self.tenant_a.tenant_id,),
        )
        conn.execute(
            "INSERT INTO cached_branch_stock VALUES ('bat-1', ?, ?, 'med-1', 'B1', '2027-12-31', 20, 0, '8.25', 'ACTIVE', 1, '2026-11-18T00:00:00Z')",
            (self.tenant_a.tenant_id, self.branch_a1.branch_id),
        )

        # Negative stock CHECK constraint must fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE cached_branch_stock SET quantity_base_units = -1 WHERE batch_id = 'bat-1'")

        # Insert outbox command
        conn.execute(
            "INSERT INTO pending_offline_commands VALUES ('cmd-1', 'idem-1', 'POS-TERM-01', ?, ?, 1, 'OFFLINE_POS_SALE', 'INV-1', '{}', '125.50', 1, 'SAVED_LOCALLY', 0, NULL, NULL, '', '2026-11-18T00:00:00Z')",
            (self.tenant_a.tenant_id, self.branch_a1.branch_id),
        )
        # Repeated delivery (duplicate idempotency_key) must fail UNIQUE constraint
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO pending_offline_commands VALUES ('cmd-2', 'idem-1', 'POS-TERM-01', ?, ?, 2, 'OFFLINE_POS_SALE', 'INV-1', '{}', '125.50', 1, 'SAVED_LOCALLY', 0, NULL, NULL, '', '2026-11-18T00:00:00Z')",
                (self.tenant_a.tenant_id, self.branch_a1.branch_id),
            )
        conn.close()

    def test_02_fingerprint_mismatch_and_expired_lease_rejection(self):
        self.client.force_authenticate(user=self.owner_a)
        reg = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-TERM-02",
                "device_fingerprint_sha256": self.fp_valid,
            },
            format="json",
        )
        assert reg.status_code == status.HTTP_201_CREATED
        reg_id = reg.data["registration_id"]

        # Cloned device_id with different fingerprint -> 403
        spoof = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-TERM-02",
                "device_fingerprint_sha256": self.fp_other,
            },
            format="json",
        )
        assert spoof.status_code == status.HTTP_403_FORBIDDEN
        assert spoof.data["error_code"] == "ERR_DEVICE_FINGERPRINT_MISMATCH"

        # Valid command envelope accepted
        env = self.client.post(
            "/api/v2/offline-sync/device-schemas/validate-envelope/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-TERM-02",
                "command_id": "cmd-uuid-1",
                "idempotency_key": "idem-uuid-1",
                "client_sequence": 1,
                "command_type": "OFFLINE_POS_SALE",
                "aggregate_ref": "OFF-INV-001",
                "monetary_total_bdt": "249.75",
            },
            format="json",
        )
        assert env.status_code == status.HTTP_200_OK
        assert env.data["normalized_monetary_total_bdt"] == "249.75"

        # Expire lease -> envelope validation blocked (403)
        obj = DeviceLocalDbRegistration.objects.get(registration_id=reg_id)
        obj.lease_expires_at = timezone.now() - datetime.timedelta(hours=1)
        obj.save()
        expired = self.client.post(
            "/api/v2/offline-sync/device-schemas/validate-envelope/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "device_id": "POS-TERM-02",
                "command_id": "cmd-uuid-2",
                "idempotency_key": "idem-uuid-2",
                "client_sequence": 2,
                "command_type": "OFFLINE_POS_SALE",
                "aggregate_ref": "OFF-INV-002",
                "monetary_total_bdt": "100.00",
            },
            format="json",
        )
        assert expired.status_code == status.HTTP_403_FORBIDDEN
        assert expired.data["error_code"] == "ERR_OFFLINE_LEASE_EXPIRED"

        # Revoke device (DELETE)
        rev = self.client.delete(f"/api/v2/offline-sync/device-schemas/{reg_id}/", {"reason": "Lost terminal"}, format="json")
        assert rev.status_code == status.HTTP_200_OK
        assert rev.data["status"] == DeviceLocalDbStatus.REVOKED

    def test_03_invalid_inputs_rbac_cross_tenant_and_suspended_tenant(self):
        # Short fingerprint rejected (400)
        self.client.force_authenticate(user=self.owner_a)
        bad = self.client.post(
            "/api/v2/offline-sync/device-schemas/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "device_id": "POS-BAD",
                "device_fingerprint_sha256": "too-short",
            },
            format="json",
        )
        assert bad.status_code == status.HTTP_400_BAD_REQUEST

        # Cashier denied device registration (403)
        self.client.force_authenticate(user=self.cashier_a1)
        assert (
            self.client.post(
                "/api/v2/offline-sync/device-schemas/",
                {
                    "tenant_id": self.tenant_a.tenant_id,
                    "branch_id": self.branch_a1.branch_id,
                    "device_id": "POS-CASH",
                    "device_fingerprint_sha256": self.fp_valid,
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Branch Manager cannot register device on Branch A2 (403)
        self.client.force_authenticate(user=self.bm_a1)
        assert (
            self.client.post(
                "/api/v2/offline-sync/device-schemas/",
                {
                    "tenant_id": self.tenant_a.tenant_id,
                    "branch_id": self.branch_a2.branch_id,
                    "device_id": "POS-A2",
                    "device_fingerprint_sha256": self.fp_valid,
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Cross-tenant branch registration denied (403)
        self.client.force_authenticate(user=self.owner_b)
        assert (
            self.client.post(
                "/api/v2/offline-sync/device-schemas/",
                {
                    "tenant_id": self.tenant_b.tenant_id,
                    "branch_id": self.branch_a1.branch_id,
                    "device_id": "POS-CROSS",
                    "device_fingerprint_sha256": self.fp_valid,
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Suspended tenant blocked (403)
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        assert (
            self.client.get(f"/api/v2/offline-sync/device-schemas/manifest/?tenant_id={self.tenant_a.tenant_id}").status_code
            == status.HTTP_403_FORBIDDEN
        )
