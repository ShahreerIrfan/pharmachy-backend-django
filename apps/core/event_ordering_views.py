"""
backend/apps/core/event_ordering_views.py
Architecture Decision: ADR-0316 (P316).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

DRF ViewSet for Event Ordering Barriers & Topological Dependency Evaluation:
- Enforces causal dependencies (Customer -> Sale, GRN -> Sale, Sale -> Return).
- Evaluates topological batches before execution.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.event_ordering_service import EventOrderingService
from apps.core.models import EventOrderingBarrier, Tenant

logger = logging.getLogger(__name__)


class EventOrderingBarrierViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant(self, request, tenant_id_param: str = None) -> Tenant:
        user = request.user
        tenant_id = (
            tenant_id_param
            or request.data.get("tenant_id")
            or request.query_params.get("tenant_id")
            or getattr(user, "tenant_id", None)
            or getattr(getattr(user, "tenant", None), "tenant_id", None)
        )
        if not tenant_id:
            tenant_id = "ten-default"
        return EventOrderingService.assert_tenant_active(str(tenant_id))

    def list(self, request):
        tenant = self._resolve_tenant(request)
        branch_id = request.query_params.get("branch_id")
        barrier_status = request.query_params.get("status")

        qs = EventOrderingBarrier.objects.select_related("branch", "device_registration").filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if barrier_status:
            qs = qs.filter(status=barrier_status.upper())

        results = [
            {
                "barrier_id": b.barrier_id,
                "command_id": b.command_id,
                "command_type": b.command_type,
                "depends_on_command_ids": b.depends_on_command_ids,
                "dependency_rule": b.dependency_rule,
                "status": b.status,
                "blocking_reasons": b.blocking_reasons,
                "created_at": b.created_at.isoformat(),
            }
            for b in qs.order_by("-created_at")[:100]
        ]
        return Response({"count": len(results), "results": results})

    @action(detail=False, methods=["post"], url_path="evaluate")
    def evaluate(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        commands = request.data.get("commands")

        if not device_id or not commands or not isinstance(commands, list):
            return Response(
                {"error_code": "ERR_INVALID_PAYLOAD", "detail": "device_id and commands list are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = EventOrderingService.process_and_enforce_barriers(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            commands=commands,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
