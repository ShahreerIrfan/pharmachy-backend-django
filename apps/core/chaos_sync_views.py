"""
backend/apps/core/chaos_sync_views.py
Architecture Decision: ADR-0320 (P320).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

DRF ViewSet for Chaos Sync Acceptance Drills & Resilience Verification:
- Records and queries multi-failure chaos drills (crash, partition, duplicate push, lost ACK, two-device conflict).
- Publishes authoritative chaos sync test suite specification.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.chaos_sync_service import ChaosSyncAcceptanceService
from apps.core.models import Tenant

logger = logging.getLogger(__name__)


class ChaosSyncAcceptanceViewSet(viewsets.ViewSet):
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
        return ChaosSyncAcceptanceService.assert_tenant_active(str(tenant_id))

    def list(self, request):
        tenant = self._resolve_tenant(request)
        branch_id = request.query_params.get("branch_id")
        drills = ChaosSyncAcceptanceService.list_drills(
            tenant_id=str(tenant.tenant_id),
            branch_id=branch_id,
        )
        return Response({"count": len(drills), "results": drills}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="spec")
    def suite_spec(self, request):
        spec = ChaosSyncAcceptanceService.get_chaos_suite_spec()
        return Response(spec, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="execute")
    def execute_drill(self, request):
        tenant = self._resolve_tenant(request)
        branch_id = str(request.data.get("branch_id") or "").strip()
        drill_name = str(request.data.get("drill_name") or "PHASE_16_CHAOS_SYNC_SUITE").strip()
        results_map = request.data.get("results_map") or {}
        evidence_payload = request.data.get("evidence_payload") or {}

        if not branch_id:
            return Response(
                {"error_code": "ERR_INVALID_PAYLOAD", "detail": "branch_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = ChaosSyncAcceptanceService.record_chaos_drill_execution(
            tenant_id=str(tenant.tenant_id),
            branch_id=branch_id,
            drill_name=drill_name,
            results_map=results_map,
            evidence_payload=evidence_payload,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
