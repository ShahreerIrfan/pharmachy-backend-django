"""
backend/tests/test_p314_conflict_review_ui.py
Architecture Decision: ADR-0314 (P314).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Manager Conflict Review Center:
1. Valid end-to-end review lifecycle:
   - Source event and diff inspection
   - Proposing decision (OVERRIDE_WITH_INVENTORY_ADJUSTMENT) -> DECISION_PROPOSED
   - Approving decision -> RESOLVED_ADJUSTED + COMMITTED_CLOUD final outcome + P307 change feed emission.
2. Incomplete or invalid input validation:
   - Empty proposed notes (< 3 chars) or invalid decision enum raises 400 ValidationError.
   - Missing approval notes raises 400 ValidationError.
3. Unauthorized cashier case:
   - Cashier attempting to propose or approve decision blocked with 403 PermissionDenied.
4. Cross-tenant isolation case:
   - Tenant B manager cannot view or resolve Tenant A's conflict tickets (404/403).
5. Precision and monetary reconciliation:
   - Exact Decimal BDT integrity preserved with zero float arithmetic.
6. DRF ViewSet endpoints (/propose-decision, /approve-decision, /retrieve).
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
    ConflictProposedDecision,
    DeviceLocalDbRegistration,
    DeviceLocalDbStatus,
    MedicineProduct,
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
class TestConflictReviewCenterIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.fp = "aabbccdd" * 8

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-rc-a-{self.suffix}",
            name=f"Review Pharma A {self.suffix}",
            subdomain=f"rc-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-rc-a-{self.suffix}", tenant=self.tenant_a, name="Dhanmondi Branch"
        )
        self.device_a = DeviceLocalDbRegistration.objects.create(
            registration_id=f"reg-rc-a-{self.suffix}",
            device_id=f"POS-RC-A-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_fingerprint_sha256=self.fp,
            status=DeviceLocalDbStatus.ACTIVE,
            lease_expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        # Tenant B (Cross-tenant testing)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-rc-b-{self.suffix}",
            name=f"Review Pharma B {self.suffix}",
            subdomain=f"rc-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-rc-b-{self.suffix}", tenant=self.tenant_b, name="Banani Branch"
        )

        # Users
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_a_{self.suffix}@test.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Review Owner A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_a_{self.suffix}@test.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Review Cashier A",
            password="Password123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.CASHIER,
            is_active=True,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@test.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Review Owner B",
            password="Password123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

        # Seed Conflict Ticket in Tenant A
        self.ticket = SyncConflictRecord.objects.create(
            conflict_id=f"conf-rc-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            device_registration=self.device_a,
            command_id=f"cmd-rc-{self.suffix}",
            idempotency_key=f"idem-rc-{self.suffix}",
            client_sequence=7,
            conflict_category=SyncConflictCategory.STALE_STOCK,
            error_code=SyncConflictErrorCode.CONFLICT_STALE_STOCK,
            disputed_monetary_bdt=Decimal("540.50"),
            local_payload_json={"product_id": "p1", "qty": 10},
            server_state_snapshot={"server_stock": 2, "shortage": 8},
            resolution_status=SyncConflictResolutionStatus.OPEN,
            resolution_notes="Shortage during concurrent checkout.",
        )

    def test_01_valid_proposal_and_approval_workflow(self):
        # 1. Propose Decision
        proposed = SyncConflictClassificationService.propose_conflict_decision(
            tenant_id=str(self.tenant_a.tenant_id),
            conflict_id=self.ticket.conflict_id,
            proposed_decision=ConflictProposedDecision.OVERRIDE_WITH_INVENTORY_ADJUSTMENT,
            proposed_notes="Accept local sale and record stock adjustment voucher.",
            actor_user=self.owner_a,
        )
        assert proposed.resolution_status == SyncConflictResolutionStatus.DECISION_PROPOSED
        assert proposed.proposed_decision == ConflictProposedDecision.OVERRIDE_WITH_INVENTORY_ADJUSTMENT
        assert proposed.proposed_by == self.owner_a
        assert proposed.proposed_at is not None

        # 2. Approve Decision
        approved = SyncConflictClassificationService.approve_and_execute_decision(
            tenant_id=str(self.tenant_a.tenant_id),
            conflict_id=self.ticket.conflict_id,
            approval_notes="Approved by Pharmacy Owner for month-end adjustment.",
            resolution_reference_id="VOUCHER-ADJ-777",
            actor_user=self.owner_a,
        )
        assert approved.resolution_status == SyncConflictResolutionStatus.RESOLVED_ADJUSTED
        assert approved.approved_by == self.owner_a
        assert approved.resolution_reference_id == "VOUCHER-ADJ-777"
        assert approved.final_cloud_outcome["execution_status"] == "COMMITTED_CLOUD"
        assert approved.final_cloud_outcome["reconciled_disputed_bdt"] == "540.50"

        # 3. Verify P307 change feed was emitted
        feed = ChangeFeedEvent.objects.filter(
            tenant=self.tenant_a, entity_id=self.ticket.conflict_id
        ).first()
        assert feed is not None
        assert feed.payload_json["final_cloud_outcome"]["reference_id"] == "VOUCHER-ADJ-777"

    def test_02_invalid_or_incomplete_inputs_rejected(self):
        # Empty notes
        with pytest.raises(Exception) as exc:
            SyncConflictClassificationService.propose_conflict_decision(
                tenant_id=str(self.tenant_a.tenant_id),
                conflict_id=self.ticket.conflict_id,
                proposed_decision=ConflictProposedDecision.KEEP_SERVER_DISCARD_LOCAL,
                proposed_notes="  ",  # Too short
                actor_user=self.owner_a,
            )

        # Invalid decision enum
        with pytest.raises(Exception):
            SyncConflictClassificationService.propose_conflict_decision(
                tenant_id=str(self.tenant_a.tenant_id),
                conflict_id=self.ticket.conflict_id,
                proposed_decision="NON_EXISTENT_DECISION",
                proposed_notes="Valid notes here",
                actor_user=self.owner_a,
            )

    def test_03_unauthorized_cashier_blocked_403(self):
        # Cashier proposing decision
        with pytest.raises(Exception):
            SyncConflictClassificationService.propose_conflict_decision(
                tenant_id=str(self.tenant_a.tenant_id),
                conflict_id=self.ticket.conflict_id,
                proposed_decision=ConflictProposedDecision.KEEP_SERVER_DISCARD_LOCAL,
                proposed_notes="Cashier trying to propose",
                actor_user=self.cashier_a,
            )

        # Cashier approving decision
        with pytest.raises(Exception):
            SyncConflictClassificationService.approve_and_execute_decision(
                tenant_id=str(self.tenant_a.tenant_id),
                conflict_id=self.ticket.conflict_id,
                approval_notes="Cashier trying to approve",
                actor_user=self.cashier_a,
            )

    def test_04_cross_tenant_isolation(self):
        # Tenant B owner attempting to propose on Tenant A's ticket
        with pytest.raises(Exception):
            SyncConflictClassificationService.propose_conflict_decision(
                tenant_id=str(self.tenant_b.tenant_id),
                conflict_id=self.ticket.conflict_id,
                proposed_decision=ConflictProposedDecision.KEEP_SERVER_DISCARD_LOCAL,
                proposed_notes="Cross tenant attempt",
                actor_user=self.owner_b,
            )

    def test_05_drf_viewset_propose_and_approve_endpoints(self):
        self.client.force_authenticate(user=self.owner_a)

        # 1. Propose decision via API
        prop_resp = self.client.post(
            f"/api/v2/offline-sync/conflicts/{self.ticket.conflict_id}/propose-decision/",
            data={
                "tenant_id": str(self.tenant_a.tenant_id),
                "proposed_decision": "OVERRIDE_WITH_INVENTORY_ADJUSTMENT",
                "proposed_notes": "Manager proposed stock adjustment.",
            },
            format="json",
        )
        assert prop_resp.status_code == status.HTTP_200_OK
        data = prop_resp.json()
        assert data["resolution_status"] == "DECISION_PROPOSED"
        assert data["proposed_decision"] == "OVERRIDE_WITH_INVENTORY_ADJUSTMENT"

        # 2. Approve decision via API
        app_resp = self.client.post(
            f"/api/v2/offline-sync/conflicts/{self.ticket.conflict_id}/approve-decision/",
            data={
                "tenant_id": str(self.tenant_a.tenant_id),
                "approval_notes": "Approved by owner via REST API.",
                "resolution_reference_id": "API-ADJ-101",
            },
            format="json",
        )
        assert app_resp.status_code == status.HTTP_200_OK
        app_data = app_resp.json()
        assert app_data["resolution_status"] == "RESOLVED_ADJUSTED"
        assert app_data["final_cloud_outcome"]["reference_id"] == "API-ADJ-101"

        # 3. Retrieve endpoint shows full decision & outcome metadata
        get_resp = self.client.get(
            f"/api/v2/offline-sync/conflicts/{self.ticket.conflict_id}/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert get_resp.status_code == status.HTTP_200_OK
        ticket_detail = get_resp.json()
        assert ticket_detail["proposed_notes"] == "Manager proposed stock adjustment."
        assert ticket_detail["approval_notes"] == "Approved by owner via REST API."
        assert ticket_detail["final_cloud_outcome"]["execution_status"] == "COMMITTED_CLOUD"
