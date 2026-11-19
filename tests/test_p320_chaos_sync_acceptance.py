"""
backend/tests/test_p320_chaos_sync_acceptance.py
Architecture Decision: ADR-0320 (P320).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Targeted integration tests for Chaos Sync Acceptance & Data Safety Drills:
1. Authoritative chaos suite spec retrieval (6 drills + data safety invariants).
2. Chaos drill execution recording with all 7 safety gates verified (PASSED).
3. Chaos drill failure tracking when any safety gate fails (FAILED).
4. Cross-tenant isolation: Tenant B cannot execute drill on Tenant A's branch.
5. Inactive/suspended tenant rejection.
6. Non-existent branch rejection.
7. DRF ViewSet REST endpoints: /spec/, /execute/, and list query.
"""

import random
import uuid
import pytest
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.chaos_sync_service import ChaosSyncAcceptanceService
from apps.core.models import (
    Branch,
    ChaosDrillStatus,
    ChaosSyncAcceptanceDrill,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestChaosSyncAcceptanceIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-csa-a-{self.suffix}",
            name=f"Chaos Pharma A {self.suffix}",
            subdomain=f"csa-a-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-csa-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Central Branch",
        )
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@example.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name=f"Owner CSA {self.suffix}",
            password="StrongPassword123!",
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

        # Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-csa-b-{self.suffix}",
            name=f"Chaos Pharma B {self.suffix}",
            subdomain=f"csa-b-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-csa-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Gulshan Branch",
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@example.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name=f"Owner B CSA {self.suffix}",
            password="StrongPassword123!",
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
            role=UserRole.PHARMACY_OWNER,
            is_active=True,
        )

    def test_get_chaos_suite_spec(self):
        spec = ChaosSyncAcceptanceService.get_chaos_suite_spec()
        assert spec["suite_name"] == "PHASE_16_CHAOS_SYNC_ACCEPTANCE_SUITE"
        assert len(spec["required_drills"]) == 6
        assert "DRILL_APP_CRASH" in spec["required_drills"]
        assert "DRILL_MULTI_COUNTER_STOCK_CONFLICT" in spec["required_drills"]
        assert "EXACT_DECIMAL_STOCK_INTEGRITY" in spec["data_safety_invariants"]

    def test_record_chaos_drill_all_passed(self):
        results = {
            "app_crash_recovered": True,
            "network_partition_recovered": True,
            "duplicate_push_idempotent": True,
            "lost_ack_reconciled": True,
            "multi_counter_conflict_quarantined": True,
            "lease_expiry_blocked": True,
            "stock_ledger_precision_intact": True,
        }
        res = ChaosSyncAcceptanceService.record_chaos_drill_execution(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            drill_name="FULL_CHAOS_ACCEPTANCE_RUN",
            results_map=results,
            evidence_payload={"wal_checkpoint": "RECOVERED", "duration_ms": 320},
            actor_user=self.owner_a,
        )
        assert res["status"] == ChaosDrillStatus.PASSED
        assert res["all_passed"] is True
        assert res["criteria_summary"]["multi_counter_conflict_quarantined"] is True

        drill_record = ChaosSyncAcceptanceDrill.objects.get(drill_id=res["drill_id"])
        assert drill_record.status == ChaosDrillStatus.PASSED
        assert drill_record.app_crash_recovered is True
        assert drill_record.stock_ledger_precision_intact is True

        audit = AuditLogEntry.objects.filter(resource_id=res["drill_id"]).first()
        assert audit is not None
        assert audit.action == "CHAOS_SYNC_DRILL_COMPLETED"
        assert audit.status == "SUCCESS"

    def test_record_chaos_drill_partial_failure(self):
        results = {
            "app_crash_recovered": True,
            "network_partition_recovered": True,
            "duplicate_push_idempotent": True,
            "lost_ack_reconciled": True,
            "multi_counter_conflict_quarantined": False,  # Failed gate!
            "lease_expiry_blocked": True,
            "stock_ledger_precision_intact": True,
        }
        res = ChaosSyncAcceptanceService.record_chaos_drill_execution(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
            drill_name="CONFLICT_GATE_FAILURE_DRILL",
            results_map=results,
            actor_user=self.owner_a,
        )
        assert res["status"] == ChaosDrillStatus.FAILED
        assert res["all_passed"] is False

        drill_record = ChaosSyncAcceptanceDrill.objects.get(drill_id=res["drill_id"])
        assert drill_record.status == ChaosDrillStatus.FAILED
        assert drill_record.multi_counter_conflict_quarantined is False

        audit = AuditLogEntry.objects.filter(resource_id=res["drill_id"]).first()
        assert audit is not None
        assert audit.status == "DENIED"

    def test_cross_tenant_branch_isolation(self):
        with pytest.raises(PermissionDenied) as exc:
            ChaosSyncAcceptanceService.record_chaos_drill_execution(
                tenant_id=self.tenant_a.tenant_id,
                branch_id=self.branch_b.branch_id,  # Branch belonging to Tenant B
                drill_name="MALICIOUS_CROSS_TENANT_DRILL",
                results_map={},
                actor_user=self.owner_a,
            )
        assert exc.value.detail["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_nonexistent_branch_rejected(self):
        with pytest.raises(NotFound) as exc:
            ChaosSyncAcceptanceService.record_chaos_drill_execution(
                tenant_id=self.tenant_a.tenant_id,
                branch_id="nonexistent-branch-id",
                drill_name="INVALID_BRANCH_DRILL",
                results_map={},
                actor_user=self.owner_a,
            )
        assert exc.value.detail["error_code"] == "ERR_BRANCH_NOT_FOUND"

    def test_suspended_tenant_rejected(self):
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save(update_fields=["status"])

        with pytest.raises(PermissionDenied) as exc:
            ChaosSyncAcceptanceService.record_chaos_drill_execution(
                tenant_id=self.tenant_a.tenant_id,
                branch_id=self.branch_a.branch_id,
                drill_name="SUSPENDED_TENANT_DRILL",
                results_map={},
                actor_user=self.owner_a,
            )
        assert exc.value.detail["error_code"] == "ERR_TENANT_SUSPENDED"

    def test_drf_endpoints(self):
        self.client.force_authenticate(user=self.owner_a)

        # 1. Spec endpoint
        spec_resp = self.client.get("/api/v2/offline-sync/chaos-drills/spec/")
        assert spec_resp.status_code == status.HTTP_200_OK
        assert spec_resp.data["suite_name"] == "PHASE_16_CHAOS_SYNC_ACCEPTANCE_SUITE"

        # 2. Execute endpoint validation: missing branch_id
        bad_resp = self.client.post("/api/v2/offline-sync/chaos-drills/execute/", {})
        assert bad_resp.status_code == status.HTTP_400_BAD_REQUEST

        # 3. Execute endpoint success
        exec_payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "drill_name": "API_DRF_CHAOS_RUN",
            "results_map": {
                "app_crash_recovered": True,
                "network_partition_recovered": True,
                "duplicate_push_idempotent": True,
                "lost_ack_reconciled": True,
                "multi_counter_conflict_quarantined": True,
                "lease_expiry_blocked": True,
                "stock_ledger_precision_intact": True,
            },
        }
        exec_resp = self.client.post("/api/v2/offline-sync/chaos-drills/execute/", exec_payload, format="json")
        assert exec_resp.status_code == status.HTTP_200_OK
        assert exec_resp.data["status"] == ChaosDrillStatus.PASSED

        # 4. List drills endpoint
        list_resp = self.client.get(
            f"/api/v2/offline-sync/chaos-drills/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a.branch_id}"
        )
        assert list_resp.status_code == status.HTTP_200_OK
        assert list_resp.data["count"] >= 1
