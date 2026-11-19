"""
backend/apps/core/device_schema_migration_views.py
Architecture Decision: ADR-0318 (P318).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

DRF ViewSet for Device Local SQLite Schema Migrations & Rollback Tracking:
- Records migration completion, rollback events, and preserved outbox counts.
- Exposes device migration audit history.
- Publishes authoritative server latest schema version for POS workstations.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.device_schema_migration_service import DeviceSchemaMigrationService
from apps.core.models import Tenant

logger = logging.getLogger(__name__)


class DeviceSchemaMigrationViewSet(viewsets.ViewSet):
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
        return DeviceSchemaMigrationService.assert_tenant_active(str(tenant_id))

    @action(detail=False, methods=["get"], url_path="latest-version")
    def latest_version(self, request):
        info = DeviceSchemaMigrationService.get_latest_schema_version_info()
        return Response(info, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="report")
    def report_migration(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        from_version = int(request.data.get("from_version") or 0)
        to_version = int(request.data.get("to_version") or 0)
        migration_status = str(request.data.get("status") or "SUCCESS").strip()
        outbox_preserved = int(request.data.get("outbox_events_preserved") or 0)
        backup_hash = str(request.data.get("backup_file_hash_sha256") or "").strip()
        error_msg = str(request.data.get("error_message") or "").strip()

        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        res = DeviceSchemaMigrationService.record_device_migration(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            from_version=from_version,
            to_version=to_version,
            migration_status=migration_status,
            outbox_events_preserved=outbox_preserved,
            backup_file_hash_sha256=backup_hash,
            error_message=error_msg,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="history")
    def migration_history(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.query_params.get("device_id") or "").strip()

        if not device_id:
            return Response(
                {"error_code": "ERR_MISSING_DEVICE_ID", "detail": "device_id query param is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        history = DeviceSchemaMigrationService.list_migration_history(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
        )
        return Response({"device_id": device_id, "count": len(history), "results": history}, status=status.HTTP_200_OK)
