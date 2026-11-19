"""
backend/tests/test_p318_local_schema_migrations.py
Architecture Decision: ADR-0318 (P318).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Local Device Schema Migrations & Rollback Tracking:
1. Authoritative latest schema version query.
2. Successful device migration reporting & device registration version update.
3. Failed migration reporting with rollback status leaves device version unchanged.
4. Validation constraints on version numbers and statuses.
5. Cross-tenant isolation: Tenant B cannot view or report migrations for Tenant A's device.
6. DRF ViewSet endpoints (/report, /history, /latest-version).
"""

import datetime
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.test import APIClient

from apps.core.device_schema_migration_service import DeviceSchemaMigrationService
from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    DeviceMigrationStatus,
    DeviceSchemaMigrationRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestDeviceSchemaMigrationIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp = "aabbccdd" * 8

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-smr-a-{self.suffix}",
            name=f"Migration Pharma A {self.suffix}",
            subdomain=f"smr-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-smr-a-{self.suffix}", tenant=self.tenant_a, name="Dhanmondi Branch"
        )
        self.device_a = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-smr-a-{self.suffix}",
            device_id=f"POS-SMR-A-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            schema_version=1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-smr-b-{self.suffix}",
            name=f"Migration Pharma B {self.suffix}",
            subdomain=f"smr-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-smr-b-{self.suffix}", tenant=self.tenant_b, name="Banani Branch"
        )
        self.device_b = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-smr-b-{self.suffix}",
            device_id=f"POS-SMR-B-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            schema_version=1,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Users
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_smr_a_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="SMR Owner A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_smr_b_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="SMR Owner B",
            password="Password123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

    def test_01_latest_version_query(self):
        info = DeviceSchemaMigrationService.get_latest_schema_version_info()
        assert info["latest_schema_version"] >= 2
        assert info["min_supported_schema_version"] == 1

    def test_02_successful_migration_report_and_device_version_update(self):
        res = DeviceSchemaMigrationService.record_device_migration(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            from_version=1,
            to_version=2,
            migration_status="SUCCESS",
            outbox_events_preserved=15,
            backup_file_hash_sha256="aabbcc" * 10,
            actor_user=self.owner_a,
        )

        assert res["status"] == DeviceMigrationStatus.SUCCESS
        assert res["from_version"] == 1
        assert res["to_version"] == 2
        assert res["outbox_events_preserved"] == 15

        # Device registration version MUST be updated to 2
        self.device_a.refresh_from_db()
        assert self.device_a.schema_version == 2

    def test_03_failed_rollback_leaves_device_version_unchanged(self):
        res = DeviceSchemaMigrationService.record_device_migration(
            tenant_id=str(self.tenant_a.tenant_id),
            device_id=self.device_a.device_id,
            from_version=1,
            to_version=2,
            migration_status="FAILED_ROLLED_BACK",
            outbox_events_preserved=10,
            error_message="Simulated DDL syntax error, transaction rolled back safely",
            actor_user=self.owner_a,
        )

        assert res["status"] == DeviceMigrationStatus.FAILED_ROLLED_BACK
        self.device_a.refresh_from_db()
        assert self.device_a.schema_version == 1  # Remained at v1

    def test_04_validation_constraints(self):
        # Invalid version <= 0
        with pytest.raises(ValidationError) as exc_ver:
            DeviceSchemaMigrationService.record_device_migration(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                from_version=0,
                to_version=2,
                migration_status="SUCCESS",
                outbox_events_preserved=0,
                actor_user=self.owner_a,
            )
        assert "ERR_INVALID_VERSION_NUMBER" in str(exc_ver.value)

        # Invalid status
        with pytest.raises(ValidationError) as exc_st:
            DeviceSchemaMigrationService.record_device_migration(
                tenant_id=str(self.tenant_a.tenant_id),
                device_id=self.device_a.device_id,
                from_version=1,
                to_version=2,
                migration_status="NON_EXISTENT_STATUS",
                outbox_events_preserved=0,
                actor_user=self.owner_a,
            )
        assert "ERR_INVALID_MIGRATION_STATUS" in str(exc_st.value)

    def test_05_cross_tenant_isolation(self):
        # Tenant B tries to report migration for Tenant A's device
        with pytest.raises((PermissionDenied, NotFound)):
            DeviceSchemaMigrationService.record_device_migration(
                tenant_id=str(self.tenant_b.tenant_id),
                device_id=self.device_a.device_id,
                from_version=1,
                to_version=2,
                migration_status="SUCCESS",
                outbox_events_preserved=5,
                actor_user=self.owner_b,
            )

    def test_06_drf_viewset_endpoints(self):
        self.client.force_authenticate(user=self.owner_a)

        # GET /latest-version
        res_ver = self.client.get("/api/v2/offline-sync/schema-migrations/latest-version/")
        assert res_ver.status_code == status.HTTP_200_OK
        assert res_ver.json()["latest_schema_version"] >= 2

        # POST /report
        payload = {
            "device_id": self.device_a.device_id,
            "from_version": 1,
            "to_version": 2,
            "status": "SUCCESS",
            "outbox_events_preserved": 8,
            "backup_file_hash_sha256": "11223344" * 8,
        }
        res_rep = self.client.post("/api/v2/offline-sync/schema-migrations/report/", data=payload, format="json")
        assert res_rep.status_code == status.HTTP_200_OK
        assert res_rep.json()["status"] == "SUCCESS"

        # GET /history
        res_hist = self.client.get(f"/api/v2/offline-sync/schema-migrations/history/?device_id={self.device_a.device_id}")
        assert res_hist.status_code == status.HTTP_200_OK
        data = res_hist.json()
        assert data["count"] >= 1
        assert data["results"][0]["outbox_events_preserved"] == 8
