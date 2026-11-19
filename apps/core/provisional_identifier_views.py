"""
backend/apps/core/provisional_identifier_views.py
Architecture Decision: ADR-0315 (P315).
Phase 16: JavaFX SQLite Offline Store and Reliable Cloud Sync.

DRF ViewSet for Local Provisional Identifier Reconciliation:
- Allows POS terminals and Mobile manager clients to reconcile provisional client UUIDs
  to authoritative cloud canonical IDs.
- Provides unified bidirectional lookup (/lookup/) for barcode scanning, receipt reprint,
  and cross-device returns.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.models import (
    DeviceLocalDbRegistration,
    ProvisionalIdentifierMapping,
    Tenant,
)
from apps.core.provisional_identifier_service import ProvisionalIdentifierService

logger = logging.getLogger(__name__)


class ProvisionalIdentifierViewSet(viewsets.ViewSet):
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
        return ProvisionalIdentifierService.assert_tenant_active(str(tenant_id))

    def list(self, request):
        tenant = self._resolve_tenant(request)
        branch_id = request.query_params.get("branch_id")
        entity_type = request.query_params.get("entity_type")

        qs = ProvisionalIdentifierMapping.objects.select_related("branch", "device_registration").filter(tenant=tenant)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if entity_type:
            qs = qs.filter(entity_type=entity_type.upper())

        results = [
            ProvisionalIdentifierService._serialize_mapping(m, is_replay=False)
            for m in qs.order_by("-mapped_at")[:100]
        ]
        return Response({"count": len(results), "results": results})

    @action(detail=False, methods=["post"], url_path="reconcile")
    def reconcile(self, request):
        tenant = self._resolve_tenant(request)
        device_id = str(request.data.get("device_id") or "").strip()
        items = request.data.get("items")

        if not device_id:
            return Response(
                {"error_code": "ERR_DEVICE_ID_REQUIRED", "detail": "device_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not items or not isinstance(items, list):
            # Single item shortcut
            prov_id = request.data.get("provisional_id")
            ent_type = request.data.get("entity_type", "INVOICE")
            can_id = request.data.get("canonical_id")
            idem = request.data.get("idempotency_key")
            seq = request.data.get("client_sequence", 1)
            if not prov_id:
                return Response(
                    {"error_code": "ERR_INVALID_PAYLOAD", "detail": "items list or provisional_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            items = [
                {
                    "entity_type": ent_type,
                    "provisional_id": prov_id,
                    "canonical_id": can_id,
                    "idempotency_key": idem,
                    "client_sequence": seq,
                }
            ]

        res = ProvisionalIdentifierService.batch_reconcile(
            tenant_id=str(tenant.tenant_id),
            device_id=device_id,
            items=items,
            actor_user=request.user,
        )
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="lookup")
    def lookup(self, request):
        tenant = self._resolve_tenant(request)
        search_id = request.query_params.get("search_id") or request.query_params.get("id")
        entity_type = request.query_params.get("entity_type")

        mapping_data = ProvisionalIdentifierService.lookup_by_any_id(
            tenant_id=str(tenant.tenant_id),
            search_id=str(search_id or ""),
            entity_type=entity_type,
        )
        return Response(mapping_data, status=status.HTTP_200_OK)
