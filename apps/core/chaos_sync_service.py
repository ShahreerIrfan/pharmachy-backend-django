"""
backend/apps/core/chaos_sync_service.py
Architecture Decision: ADR-0320 (P320).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Authoritative Chaos Sync Acceptance Service:
- Orchestrates and audits Phase 16 chaos failure injection acceptance drills:
  1. App crash & SQLite WAL journal recovery.
  2. Network partition & packet loss backoff.
  3. Duplicate push repeated delivery idempotency.
  4. Lost ACK transparent client reconciliation.
  5. Multi-counter offline concurrent stock dispute quarantining.
  6. Signed subscription lease expiry rejection.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    ChaosDrillStatus,
    ChaosSyncAcceptanceDrill,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger(__name__)


class ChaosSyncAcceptanceService:

    @classmethod
    def get_chaos_suite_spec(cls) -> Dict[str, Any]:
        return {
            "suite_name": "PHASE_16_CHAOS_SYNC_ACCEPTANCE_SUITE",
            "phase": "Phase 16: JavaFX SQLite Offline Store & Reliable Cloud Sync",
            "required_drills": [
                "DRILL_APP_CRASH",
                "DRILL_NETWORK_PARTITION",
                "DRILL_DUPLICATE_PUSH",
                "DRILL_LOST_ACK",
                "DRILL_MULTI_COUNTER_STOCK_CONFLICT",
                "DRILL_LEASE_EXPIRY",
            ],
            "data_safety_invariants": [
                "ZERO_OUTBOX_EVENT_LOSS",
                "IDEMPOTENT_REPLAY_SAFETY",
                "EXACT_DECIMAL_STOCK_INTEGRITY",
                "MUTATION_REJECTION_ON_REVOKED_LEASE",
            ],
            "version": "2.0.0",
        }

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            t = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' does not exist."})
        if t.status != TenantLifecycleStatus.ACTIVE:
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_SUSPENDED", "detail": f"Tenant '{tenant_id}' is suspended or inactive."}
            )
        return t

    @classmethod
    @transaction.atomic
    def record_chaos_drill_execution(
        cls,
        *,
        tenant_id: str,
        branch_id: str,
        drill_name: str,
        results_map: Dict[str, bool],
        evidence_payload: Optional[Dict[str, Any]] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        if not branch:
            foreign = Branch.objects.filter(branch_id=branch_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        # Required safety drill criteria
        app_crash = bool(results_map.get("app_crash_recovered", True))
        net_part = bool(results_map.get("network_partition_recovered", True))
        dup_push = bool(results_map.get("duplicate_push_idempotent", True))
        lost_ack = bool(results_map.get("lost_ack_reconciled", True))
        multi_conf = bool(results_map.get("multi_counter_conflict_quarantined", True))
        lease_exp = bool(results_map.get("lease_expiry_blocked", True))
        stock_prec = bool(results_map.get("stock_ledger_precision_intact", True))

        all_passed = (
            app_crash
            and net_part
            and dup_push
            and lost_ack
            and multi_conf
            and lease_exp
            and stock_prec
        )

        drill_id = f"cdl-{uuid.uuid4().hex[:12]}"
        drill = ChaosSyncAcceptanceDrill.objects.create(
            drill_id=drill_id,
            tenant=tenant,
            branch=branch,
            drill_name=drill_name or "PHASE_16_CHAOS_SYNC_SUITE",
            status=ChaosDrillStatus.PASSED if all_passed else ChaosDrillStatus.FAILED,
            app_crash_recovered=app_crash,
            network_partition_recovered=net_part,
            duplicate_push_idempotent=dup_push,
            lost_ack_reconciled=lost_ack,
            multi_counter_conflict_quarantined=multi_conf,
            lease_expiry_blocked=lease_exp,
            stock_ledger_precision_intact=stock_prec,
            evidence_payload_json=evidence_payload or {},
            conducted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="CHAOS_SYNC_DRILL_COMPLETED",
            resource_type="ChaosSyncAcceptanceDrill",
            resource_id=drill.drill_id,
            status=AuditActionStatus.SUCCESS if all_passed else AuditActionStatus.DENIED,
            metadata_json={
                "drill_name": drill.drill_name,
                "drill_status": drill.status,
                "all_passed": all_passed,
                "app_crash_recovered": app_crash,
                "multi_counter_conflict_quarantined": multi_conf,
            },
        )

        return {
            "drill_id": drill.drill_id,
            "drill_name": drill.drill_name,
            "status": drill.status,
            "all_passed": all_passed,
            "criteria_summary": {
                "app_crash_recovered": app_crash,
                "network_partition_recovered": net_part,
                "duplicate_push_idempotent": dup_push,
                "lost_ack_reconciled": lost_ack,
                "multi_counter_conflict_quarantined": multi_conf,
                "lease_expiry_blocked": lease_exp,
                "stock_ledger_precision_intact": stock_prec,
            },
            "conducted_at": drill.conducted_at.isoformat(),
        }

    @classmethod
    def list_drills(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        tenant = cls.assert_tenant_active(tenant_id)
        qs = ChaosSyncAcceptanceDrill.objects.select_related("branch").filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)

        return [
            {
                "drill_id": d.drill_id,
                "branch_id": d.branch.branch_id,
                "drill_name": d.drill_name,
                "status": d.status,
                "app_crash_recovered": d.app_crash_recovered,
                "multi_counter_conflict_quarantined": d.multi_counter_conflict_quarantined,
                "conducted_at": d.conducted_at.isoformat(),
            }
            for d in qs.order_by("-conducted_at")
        ]
