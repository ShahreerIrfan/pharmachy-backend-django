"""
backend/apps/core/event_ordering_service.py
Architecture Decision: ADR-0316 (P316).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

Event Ordering & Transactional Dependency Barrier Engine:
- Topologically sorts causal events (Customer Creation -> Sale, Goods Receipt -> Sale, Sale -> Return).
- Detects circular dependencies (ERR_CIRCULAR_DEPENDENCY_BARRIER) and rejects malformed graphs.
- Enforces transactional ordering barriers: upstream dependencies must either be committed in the same
  topological batch or already verified on the cloud server, preventing orphaned child records.
"""

from __future__ import annotations

from collections import defaultdict, deque
import logging
import uuid
from typing import Any, Dict, List, Optional, Set

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.durable_outbox_service import DurableOutboxService
from apps.core.models import (
    Branch,
    DeviceLocalDbRegistration,
    EventBarrierStatus,
    EventDependencyRule,
    EventOrderingBarrier,
    OfflineOutboxCommandReceipt,
    ProvisionalIdentifierMapping,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger(__name__)


class EventOrderingService:

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
    def topological_sort_commands(cls, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Sorts incoming commands in topological order based on depends_on_command_ids.
        Detects circular dependencies and raises ERR_CIRCULAR_DEPENDENCY_BARRIER.
        """
        if not commands:
            return []

        cmd_map = {str(c.get("command_id")): c for c in commands if c.get("command_id")}
        in_degree: Dict[str, int] = {cid: 0 for cid in cmd_map}
        adj: Dict[str, List[str]] = defaultdict(list)

        for cid, cmd in cmd_map.items():
            deps = cmd.get("depends_on_command_ids") or []
            if isinstance(deps, list):
                for parent_id in deps:
                    parent_str = str(parent_id).strip()
                    if parent_str in cmd_map:
                        adj[parent_str].append(cid)
                        in_degree[cid] += 1

        # Kahn's algorithm
        queue = deque([cid for cid, deg in in_degree.items() if deg == 0])
        sorted_ids: List[str] = []

        while queue:
            curr = queue.popleft()
            sorted_ids.append(curr)
            for neighbor in adj[curr]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(sorted_ids) != len(cmd_map):
            cycle_nodes = [cid for cid, deg in in_degree.items() if deg > 0]
            raise ValidationError(
                {
                    "error_code": "ERR_CIRCULAR_DEPENDENCY_BARRIER",
                    "detail": f"Circular dependency barrier detected involving commands: {cycle_nodes}.",
                }
            )

        return [cmd_map[cid] for cid in sorted_ids]

    @classmethod
    @transaction.atomic
    def process_and_enforce_barriers(
        cls,
        *,
        tenant_id: str,
        device_id: str,
        commands: List[Dict[str, Any]],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Enforces causal dependency barriers (Customer -> Sale, GRN -> Sale).
        Haults execution safely if upstream dependencies are uncommitted.
        """
        tenant = cls.assert_tenant_active(tenant_id)
        device_reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(
            tenant=tenant, device_id=device_id
        ).first()
        if not device_reg:
            raise NotFound({"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device '{device_id}' not found."})

        # 1. Topologically Sort Commands
        sorted_cmds = cls.topological_sort_commands(commands)

        satisfied_in_batch: Set[str] = set()
        barrier_records: List[Dict[str, Any]] = []
        has_blocked_command = False
        blocked_command_id = ""
        blocked_reasons: List[str] = []

        for cmd in sorted_cmds:
            cmd_id = str(cmd.get("command_id") or "").strip()
            cmd_type = str(cmd.get("command_type") or "OFFLINE_POS_SALE").strip().upper()
            deps = [str(d).strip() for d in (cmd.get("depends_on_command_ids") or []) if str(d).strip()]

            # Determine Causal Rule
            rule = EventDependencyRule.STRICT_SEQUENCE
            if cmd_type == "OFFLINE_POS_SALE":
                if any("CUST" in d.upper() for d in deps):
                    rule = EventDependencyRule.CUSTOMER_BEFORE_SALE
                elif any("GRN" in d.upper() or "BATCH" in d.upper() for d in deps):
                    rule = EventDependencyRule.GRN_BEFORE_SALE
                elif any("RX" in d.upper() or "PRES" in d.upper() for d in deps):
                    rule = EventDependencyRule.PRESCRIPTION_BEFORE_SALE
            elif cmd_type in ("POS_RETURN", "SALE_REFUND"):
                rule = EventDependencyRule.SALE_BEFORE_RETURN

            # Check upstream dependencies
            missing_deps: List[str] = []
            for dep in deps:
                if dep in satisfied_in_batch:
                    continue
                # Check if already committed on server
                exists_receipt = OfflineOutboxCommandReceipt.objects.filter(
                    tenant=tenant, command_id=dep
                ).exists()
                exists_mapping = ProvisionalIdentifierMapping.objects.filter(
                    tenant=tenant, provisional_id=dep
                ).exists()
                if not exists_receipt and not exists_mapping:
                    missing_deps.append(dep)

            barrier_id = f"eob-{uuid.uuid4().hex[:12]}"
            if missing_deps:
                has_blocked_command = True
                blocked_command_id = cmd_id
                reasons = [f"Upstream dependency '{m}' not committed." for m in missing_deps]
                blocked_reasons = reasons

                barrier = EventOrderingBarrier.objects.create(
                    barrier_id=barrier_id,
                    tenant=tenant,
                    branch=device_reg.branch,
                    device_registration=device_reg,
                    command_id=cmd_id,
                    command_type=cmd_type,
                    depends_on_command_ids=deps,
                    dependency_rule=rule,
                    status=EventBarrierStatus.BLOCKED_ON_DEPENDENCY,
                    blocking_reasons=reasons,
                )
                barrier_records.append({
                    "barrier_id": barrier.barrier_id,
                    "command_id": cmd_id,
                    "status": barrier.status,
                    "rule": barrier.dependency_rule,
                    "rule_code": barrier.dependency_rule,
                    "blocking_reasons": reasons,
                    "missing_command_ids": missing_deps,
                    "upstream_command_ids": deps,
                })
                # Halt downstream execution
                break
            else:
                barrier = EventOrderingBarrier.objects.create(
                    barrier_id=barrier_id,
                    tenant=tenant,
                    branch=device_reg.branch,
                    device_registration=device_reg,
                    command_id=cmd_id,
                    command_type=cmd_type,
                    depends_on_command_ids=deps,
                    dependency_rule=rule,
                    status=EventBarrierStatus.SATISFIED,
                    blocking_reasons=[],
                )
                satisfied_in_batch.add(cmd_id)
                barrier_records.append({
                    "barrier_id": barrier.barrier_id,
                    "command_id": cmd_id,
                    "status": barrier.status,
                    "rule": barrier.dependency_rule,
                    "rule_code": barrier.dependency_rule,
                    "blocking_reasons": [],
                    "missing_command_ids": [],
                    "upstream_command_ids": deps,
                })

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=str(getattr(actor_user, "user_id", "") or ""),
            actor_email=str(getattr(actor_user, "email", "") or ""),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="EVENT_ORDERING_BARRIER_EVALUATED",
            resource_type="EventOrderingBarrier",
            resource_id=barrier_records[0]["barrier_id"] if barrier_records else "none",
            status=AuditActionStatus.SUCCESS if not has_blocked_command else AuditActionStatus.DENIED,
            metadata_json={
                "has_blocked_command": has_blocked_command,
                "blocked_command_id": blocked_command_id,
                "total_evaluated": len(barrier_records),
            },
        )

        return {
            "all_dependencies_satisfied": not has_blocked_command,
            "blocked_command_id": blocked_command_id,
            "blocked_reasons": blocked_reasons,
            "topological_execution_order": [c["command_id"] for c in sorted_cmds],
            "topological_order": [c["command_id"] for c in sorted_cmds],
            "barriers": barrier_records,
        }
