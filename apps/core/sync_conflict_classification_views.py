"""
backend/apps/core/sync_conflict_classification_views.py
Architecture Decision: ADR-0313 (P313).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

DRF ViewSet for Sync Conflict Classification, Review & Resolution:
- Surfaces conflict review queue for pharmacy owners & branch managers.
- Provides explicit classification API for evaluating offline command invariants.
- Enforces strict multi-tenant boundary checks and role-based access control.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import (
    DeviceLocalDbRegistration,
    SyncConflictCategory,
    SyncConflictErrorCode,
    SyncConflictRecord,
    SyncConflictResolutionStatus,
    Tenant,
)
from apps.core.sync_conflict_classification_service import SyncConflictClassificationService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class SyncConflictClassificationViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant_and_device(self, request, tenant_id_param: str = None) -> tuple[Tenant, str]:
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
        tenant = SyncConflictClassificationService.assert_tenant_active(str(tenant_id))
        return tenant

    def _serialize_ticket(self, ticket: SyncConflictRecord) -> dict[str, Any]:
        return {
            "conflict_id": ticket.conflict_id,
            "tenant_id": str(ticket.tenant.tenant_id),
            "branch_id": str(ticket.branch.branch_id),
            "branch_name": ticket.branch.name,
            "device_id": ticket.device_registration.device_id,
            "command_id": ticket.command_id,
            "idempotency_key": ticket.idempotency_key,
            "client_sequence": ticket.client_sequence,
            "conflict_category": ticket.conflict_category,
            "error_code": ticket.error_code,
            "disputed_monetary_bdt": str(ticket.disputed_monetary_bdt),
            "local_payload_json": ticket.local_payload_json,
            "server_state_snapshot": ticket.server_state_snapshot,
            "resolution_status": ticket.resolution_status,
            "resolution_notes": ticket.resolution_notes,
            "resolution_reference_id": ticket.resolution_reference_id,
            "resolved_by_id": str(ticket.resolved_by.user_id) if ticket.resolved_by else None,
            "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
            "created_at": ticket.created_at.isoformat(),
            "updated_at": ticket.updated_at.isoformat(),
        }

    def list(self, request):
        tenant = self._resolve_tenant_and_device(request)
        branch_id = request.query_params.get("branch_id")
        res_status = request.query_params.get("resolution_status")
        cat = request.query_params.get("conflict_category")
        err = request.query_params.get("error_code")

        qs = SyncConflictClassificationService.list_conflicts(
            tenant_id=str(tenant.tenant_id),
            branch_id=branch_id,
            resolution_status=res_status,
            conflict_category=cat,
            error_code=err,
            actor_user=request.user,
        )
        results = [self._serialize_ticket(t) for t in qs[:100]]
        return Response({"count": len(results), "results": results})

    def retrieve(self, request, pk=None):
        tenant = self._resolve_tenant_and_device(request)
        ticket = SyncConflictRecord.objects.filter(tenant=tenant, conflict_id=pk).first()
        if not ticket:
            return Response(
                {"error_code": "ERR_CONFLICT_NOT_FOUND", "detail": f"Conflict ticket '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(self._serialize_ticket(ticket))

    @action(detail=False, methods=["post"], url_path="classify")
    def classify(self, request):
        tenant = self._resolve_tenant_and_device(request)
        device_id = str(request.data.get("device_id") or "").strip()
        command = request.data.get("command")
        auto_record = bool(request.data.get("auto_record", False))

        if not device_id or not command or not isinstance(command, dict):
            return Response(
                {"error_code": "ERR_INVALID_PAYLOAD", "detail": "device_id and command dict are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reg = DeviceLocalDbRegistration.objects.select_related("branch").filter(
            tenant=tenant, device_id=device_id
        ).first()
        if not reg:
            return Response(
                {"error_code": "ERR_DEVICE_NOT_FOUND", "detail": f"Device '{device_id}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        res = SyncConflictClassificationService.classify_offline_command(
            tenant=tenant,
            device_reg=reg,
            command=command,
            actor_user=request.user,
        )

        ticket_data = None
        if res["has_conflict"] and auto_record:
            ticket = SyncConflictClassificationService.record_conflict_ticket(
                tenant_id=str(tenant.tenant_id),
                device_id=device_id,
                command=command,
                classification=res,
                actor_user=request.user,
            )
            ticket_data = self._serialize_ticket(ticket)

        return Response(
            {
                "has_conflict": res["has_conflict"],
                "conflict_category": res["conflict_category"],
                "error_code": res["error_code"],
                "conflict_reason": res["conflict_reason"],
                "disputed_monetary_bdt": str(res["disputed_monetary_bdt"]),
                "server_snapshot": res["server_snapshot"],
                "ticket": ticket_data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="resolve")
    def resolve(self, request, pk=None):
        tenant = self._resolve_tenant_and_device(request)
        resolution_status = request.data.get("resolution_status")
        notes = request.data.get("resolution_notes", "")
        ref_id = request.data.get("resolution_reference_id", "")

        ticket = SyncConflictClassificationService.resolve_conflict_ticket(
            tenant_id=str(tenant.tenant_id),
            conflict_id=pk,
            resolution_status=resolution_status,
            resolution_notes=notes,
            resolution_reference_id=ref_id,
            actor_user=request.user,
        )
        return Response(self._serialize_ticket(ticket), status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        tenant = self._resolve_tenant_and_device(request)
        branch_id = request.query_params.get("branch_id")
        summary_data = SyncConflictClassificationService.get_conflict_summary(
            tenant_id=str(tenant.tenant_id),
            branch_id=branch_id,
            actor_user=request.user,
        )
        return Response(summary_data, status=status.HTTP_200_OK)
