"""
backend/apps/core/device_compromise_recovery_views.py
Architecture Decision: ADR-0319 (P319).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

DRF ViewSet for Device Revocation, Forensic Quarantining, and Data Recovery:
- Allows Owner/Admin to revoke stolen or retired devices.
- Retains unsynced data snapshots in recovery dockets.
- Provides owner reconciliation workflow for recovering local transactions.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.device_compromise_recovery_service import DeviceCompromiseRecoveryService
from apps.core.models import Tenant

logger = logging.getLogger(__name__)


class DeviceCompromiseRecoveryViewSet(viewsets.ViewSet):
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
        return DeviceCompromiseRecoveryService.assert_tenant_active(str(tenant_id))

    def list(self, request):
        tenant = self._resolve_tenant(request)
        branch_id = request.query_params.get("branch_id")
        dockets = DeviceCompromiseRecoveryService.list_recovery_dockets(
            tenant_id=str(tenant.tenant_id),
            branch_id=branch_id,
        )
        return Response({"count": len(dockets), "results": dockets}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="revoke")
    def revoke_device(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        compromise_type = str(request.data.get("compromise_type") or "STOLEN").strip()
        revocation_reason = str(request.data.get("revocation_reason") or "").strip()
        unsynced_evidence = request.data.get("unsynced_evidence") or {}

        if not device_id or not revocation_reason:
            return Response(
                {"error_code": "ERR_INVALID_PAYLOAD", "detail": "device_id and revocation_reason are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = DeviceCompromiseRecoveryService.revoke_device_and_quarantine(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            compromise_type=compromise_type,
            revocation_reason=revocation_reason,
            unsynced_evidence=unsynced_evidence,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="reconcile")
    def reconcile_docket(self, request):
        tenant = self._resolve_tenant(request)
        docket_id = str(request.data.get("docket_id") or "").strip()
        resolution_action = str(request.data.get("resolution_action") or "").strip()
        reconciliation_notes = str(request.data.get("reconciliation_notes") or "").strip()

        if not docket_id or not resolution_action:
            return Response(
                {"error_code": "ERR_INVALID_PAYLOAD", "detail": "docket_id and resolution_action are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = DeviceCompromiseRecoveryService.reconcile_recovery_docket(
            tenant_id=str(tenant.tenant_id),
            docket_id=docket_id,
            resolution_action=resolution_action,
            reconciliation_notes=reconciliation_notes,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)
