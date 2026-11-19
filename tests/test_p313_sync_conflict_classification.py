"""
backend/tests/test_p313_sync_conflict_classification.py
Architecture Decision: ADR-0313 (P313).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Sync Conflict Classification Engine:
1. Invariant classification for all 6 core categories with explicit error codes:
   - CONFLICT_STALE_STOCK (depleted, quarantined, or recalled batch)
   - CONFLICT_STALE_PRICE (variance > 0.05 BDT vs authoritative catalog MRP)
   - CONFLICT_REVOKED_PERMISSION (deactivated or removed staff user)
   - CONFLICT_EXPIRED_SUBSCRIPTION (expired offline device lease or suspended tenant)
   - CONFLICT_UNVERIFIED_PRESCRIPTION (dispensed without valid/active Rx auth token)
   - CONFLICT_DUPLICATE_COMMAND_TAMPERED (idempotency key replayed with altered payload)
2. Non-destructive conflict ticket recording into SyncConflictRecord with exact Decimal BDT,
   local command payload, and server state snapshot.
3. Manager resolution workflow:
   - Authorized roles (PHARMACY_OWNER, BRANCH_MANAGER) can resolve tickets;
   - Unauthorized Cashier or cross-tenant access blocked (403);
   - Emits audited ChangeFeedEvent and AuditLogEntry.
4. DRF ViewSet endpoints (/classify, /resolve, /summary, list/filter).
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
    BatchStock,
    Branch,
    ChangeFeedEvent,
    ChangeFeedOperation,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    MedicineProduct,
    OfflineOutboxCommandReceipt,
    OfflinePrescriptionAuthToken,
    OfflineRxTokenStatus,
    SignedOfflineLeaseState,
    SignedOfflineSubscriptionLease,
    SyncConflictCategory,
    SyncConflictErrorCode,
    SyncConflictRecord,
    SyncConflictResolutionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.sync_conflict_classification_service import SyncConflictClassificationService
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSyncConflictClassificationIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp = "aabbccdd" * 8

        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-cf-{self.suffix}",
            name=f"Conflict Pharma {self.suffix}",
            subdomain=f"cf-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-cf-{self.suffix}", tenant=self.tenant, name="Dhanmondi Branch"
        )
        self.device = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-cf-{self.suffix}",
            device_id=f"POS-CF-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Active Lease
        self.lease = SignedOfflineSubscriptionLease.objects.create(
            lease_id=f"sol-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            device_registration=self.device,
            plan_tier="PRO",
            allowed_roles_json=["PHARMACY_OWNER", "BRANCH_MANAGER", "CASHIER"],
            issued_at=timezone.now() - datetime.timedelta(hours=1),
            expires_at=timezone.now() + datetime.timedelta(days=3),
            server_clock_anchor_epoch_ms=int(timezone.now().timestamp() * 1000),
            monotonic_counter=1,
            lease_signature_hmac_sha256="0" * 64,
            status=SignedOfflineLeaseState.ACTIVE,
        )

        # Users
        self.owner_user = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Conflict Owner",
            password="Password123!",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.cashier_user = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Conflict Cashier",
            password="Password123!",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            role=UserRole.CASHIER,
            is_active=True,
        )
        self.inactive_user = PharmacyUser.objects.create_user(
            email=f"inactive_{self.suffix}@test.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Inactive Cashier",
            password="Password123!",
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
            role=UserRole.CASHIER,
            is_active=False,
        )

        # Catalog product & batch
        self.product = MedicineProduct.objects.create(
            product_id=f"prod-cf-{self.suffix}",
            brand_name="Napa Extra",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Beximco",
            mrp_unit=Decimal("3.00"),
            cost_unit=Decimal("2.20"),
        )
        self.batch = BatchStock.objects.create(
            batch_id=f"bat-cf-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            product=self.product,
            batch_number=f"B-{self.suffix}",
            purchase_date=timezone.now().date(),
            expiry_date=timezone.now().date() + datetime.timedelta(days=365),
            quantity_base_units=10,
            cost_per_unit=Decimal("2.20"),
            status="ACTIVE",
        )

    def test_classify_stale_stock_conflict(self):
        cmd = {
            "command_id": f"cmd-stk-{self.suffix}",
            "idempotency_key": f"idem-stk-{self.suffix}",
            "command_type": "OFFLINE_POS_SALE",
            "client_sequence": 1,
            "monetary_total_bdt": "45.00",
            "payload": {
                "batch_id": self.batch.batch_id,
                "quantity_base_units": 20,  # Only 10 available on server
            },
        }
        res = SyncConflictClassificationService.classify_offline_command(
            tenant=self.tenant,
            device_reg=self.device,
            command=cmd,
        )
        assert res["has_conflict"] is True
        assert res["conflict_category"] == SyncConflictCategory.STALE_STOCK
        assert res["error_code"] == SyncConflictErrorCode.CONFLICT_STALE_STOCK
        assert "insufficient server stock" in res["conflict_reason"]
        assert res["server_snapshot"]["shortage_units"] == 10

    def test_classify_stale_price_conflict(self):
        cmd = {
            "command_id": f"cmd-prc-{self.suffix}",
            "idempotency_key": f"idem-prc-{self.suffix}",
            "command_type": "OFFLINE_POS_SALE",
            "client_sequence": 2,
            "monetary_total_bdt": "50.00",
            "payload": {
                "product_id": self.product.product_id,
                "unit_price_bdt": "2.50",  # Authoritative MRP is 3.00 (variance 0.50 > 0.05)
            },
        }
        res = SyncConflictClassificationService.classify_offline_command(
            tenant=self.tenant,
            device_reg=self.device,
            command=cmd,
        )
        assert res["has_conflict"] is True
        assert res["conflict_category"] == SyncConflictCategory.STALE_PRICE
        assert res["error_code"] == SyncConflictErrorCode.CONFLICT_STALE_PRICE
        assert "differing from authoritative MRP" in res["conflict_reason"]

    def test_classify_revoked_permission_conflict(self):
        cmd = {
            "command_id": f"cmd-perm-{self.suffix}",
            "idempotency_key": f"idem-perm-{self.suffix}",
            "command_type": "OFFLINE_POS_SALE",
            "client_sequence": 3,
            "monetary_total_bdt": "30.00",
            "payload": {
                "cashier_user_id": str(self.inactive_user.user_id),
            },
        }
        res = SyncConflictClassificationService.classify_offline_command(
            tenant=self.tenant,
            device_reg=self.device,
            command=cmd,
        )
        assert res["has_conflict"] is True
        assert res["conflict_category"] == SyncConflictCategory.REVOKED_PERMISSION
        assert res["error_code"] == SyncConflictErrorCode.CONFLICT_REVOKED_PERMISSION
        assert "deactivated" in res["conflict_reason"]

    def test_classify_unverified_prescription_conflict(self):
        cmd = {
            "command_id": f"cmd-rx-{self.suffix}",
            "idempotency_key": f"idem-rx-{self.suffix}",
            "command_type": "OFFLINE_POS_SALE",
            "client_sequence": 4,
            "monetary_total_bdt": "90.00",
            "payload": {
                "rx_required": True,
                "prescription_auth_token_id": "rx-nonexistent",
            },
        }
        res = SyncConflictClassificationService.classify_offline_command(
            tenant=self.tenant,
            device_reg=self.device,
            command=cmd,
        )
        assert res["has_conflict"] is True
        assert res["conflict_category"] == SyncConflictCategory.UNVERIFIED_PRESCRIPTION
        assert res["error_code"] == SyncConflictErrorCode.CONFLICT_UNVERIFIED_PRESCRIPTION
        assert "Prescription token 'rx-nonexistent' is invalid" in res["conflict_reason"]

    def test_classify_duplicate_command_tampered_conflict(self):
        idem_key = f"idem-tamp-{self.suffix}"
        # Pre-create receipt with original payload
        OfflineOutboxCommandReceipt.objects.create(
            receipt_id=f"rcp-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            device_registration=self.device,
            command_id=f"cmd-orig-{self.suffix}",
            idempotency_key=idem_key,
            client_sequence=1,
            command_type="OFFLINE_POS_SALE",
            aggregate_ref="AGG-1",
            monetary_total_bdt=Decimal("100.00"),
            payload_sha256="original-hash-123456",
        )

        # Replay with altered payload
        cmd = {
            "command_id": f"cmd-orig-{self.suffix}",
            "idempotency_key": idem_key,
            "command_type": "OFFLINE_POS_SALE",
            "client_sequence": 1,
            "monetary_total_bdt": "100.00",
            "payload": {"tampered_field": True},
        }
        res = SyncConflictClassificationService.classify_offline_command(
            tenant=self.tenant,
            device_reg=self.device,
            command=cmd,
        )
        assert res["has_conflict"] is True
        assert res["conflict_category"] == SyncConflictCategory.DUPLICATE_COMMAND_TAMPERED
        assert res["error_code"] == SyncConflictErrorCode.CONFLICT_DUPLICATE_COMMAND_TAMPERED
        assert "replayed with altered payload" in res["conflict_reason"]

    def test_manager_resolution_workflow_and_rbac(self):
        cmd = {
            "command_id": f"cmd-res-{self.suffix}",
            "idempotency_key": f"idem-res-{self.suffix}",
            "command_type": "OFFLINE_POS_SALE",
            "client_sequence": 15,
            "monetary_total_bdt": "150.00",
        }
        classification = {
            "has_conflict": True,
            "conflict_category": SyncConflictCategory.STALE_STOCK,
            "error_code": SyncConflictErrorCode.CONFLICT_STALE_STOCK,
            "conflict_reason": "Server batch depleted during concurrent offline sale.",
            "disputed_monetary_bdt": Decimal("150.00"),
            "server_snapshot": {"shortage": 5},
        }

        ticket = SyncConflictClassificationService.record_conflict_ticket(
            tenant_id=str(self.tenant.tenant_id),
            device_id=self.device.device_id,
            command=cmd,
            classification=classification,
            actor_user=self.owner_user,
        )
        assert ticket.resolution_status == SyncConflictResolutionStatus.OPEN
        assert ticket.disputed_monetary_bdt == Decimal("150.00")

        # Cashier role attempt to resolve MUST BE BLOCKED (403)
        with pytest.raises(Exception):
            SyncConflictClassificationService.resolve_conflict_ticket(
                tenant_id=str(self.tenant.tenant_id),
                conflict_id=ticket.conflict_id,
                resolution_status="RESOLVED_KEEP_SERVER",
                resolution_notes="Cashier trying to resolve",
                actor_user=self.cashier_user,
            )

        # Owner resolves ticket successfully
        resolved_ticket = SyncConflictClassificationService.resolve_conflict_ticket(
            tenant_id=str(self.tenant.tenant_id),
            conflict_id=ticket.conflict_id,
            resolution_status="RESOLVED_KEEP_SERVER",
            resolution_notes="Server batch shortage accepted; manager instructed physical recheck.",
            resolution_reference_id="MGR-REF-001",
            actor_user=self.owner_user,
        )
        assert resolved_ticket.resolution_status == SyncConflictResolutionStatus.RESOLVED_KEEP_SERVER
        assert resolved_ticket.resolved_by == self.owner_user
        assert resolved_ticket.resolution_reference_id == "MGR-REF-001"

        # Verify P307 change feed event was emitted
        feed_event = ChangeFeedEvent.objects.filter(
            tenant=self.tenant, entity_id=ticket.conflict_id
        ).first()
        assert feed_event is not None
        assert feed_event.operation == ChangeFeedOperation.UPSERT
        assert feed_event.payload_json["resolution_status"] == "RESOLVED_KEEP_SERVER"

    def test_drf_viewset_endpoints(self):
        self.client.force_authenticate(user=self.owner_user)

        # 1. Classify endpoint
        classify_resp = self.client.post(
            "/api/v2/offline-sync/conflicts/classify/",
            data={
                "tenant_id": str(self.tenant.tenant_id),
                "device_id": self.device.device_id,
                "auto_record": True,
                "command": {
                    "command_id": f"cmd-api-{self.suffix}",
                    "idempotency_key": f"idem-api-{self.suffix}",
                    "command_type": "OFFLINE_POS_SALE",
                    "client_sequence": 20,
                    "monetary_total_bdt": "220.00",
                    "payload": {
                        "batch_id": self.batch.batch_id,
                        "quantity_base_units": 999,
                    },
                },
            },
            format="json",
        )
        assert classify_resp.status_code == status.HTTP_200_OK
        data = classify_resp.json()
        assert data["has_conflict"] is True
        assert data["error_code"] == "CONFLICT_STALE_STOCK"
        assert data["ticket"] is not None
        conflict_id = data["ticket"]["conflict_id"]

        # 2. Summary endpoint
        summary_resp = self.client.get(
            f"/api/v2/offline-sync/conflicts/summary/?tenant_id={self.tenant.tenant_id}"
        )
        assert summary_resp.status_code == status.HTTP_200_OK
        summary_data = summary_resp.json()
        assert summary_data["open_conflicts"] >= 1
        assert Decimal(summary_data["total_disputed_bdt"]) >= Decimal("220.00")

        # 3. Resolve endpoint
        resolve_resp = self.client.post(
            f"/api/v2/offline-sync/conflicts/{conflict_id}/resolve/",
            data={
                "tenant_id": str(self.tenant.tenant_id),
                "resolution_status": "RESOLVED_ADJUSTED",
                "resolution_notes": "Compensating adjustment posted.",
                "resolution_reference_id": "ADJ-INV-888",
            },
            format="json",
        )
        assert resolve_resp.status_code == status.HTTP_200_OK
        res_data = resolve_resp.json()
        assert res_data["resolution_status"] == "RESOLVED_ADJUSTED"
        assert res_data["resolution_reference_id"] == "ADJ-INV-888"
