"""
backend/apps/core/supply_chain_traceability_views.py
Architecture Decision: ADR-0197 (P197).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet for Supply-Chain Traceability:
- Reconstructs full lot ancestry (supplier, PO, shipment, GRN, storage, sales, returns).
- Provides DGDA inspection audit trails with cryptographic hash verification.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from apps.core.supply_chain_traceability_service import SupplyChainTraceabilityService

logger = logging.getLogger(__name__)


class SupplyChainTraceabilityViewSet(viewsets.ViewSet):
    """
    ViewSet for lot traceability, supply-chain lineage reconstruction, and compliance reporting.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant_id(self, request: Request) -> str:
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id and hasattr(request.user, "tenant_id"):
            tenant_id = str(request.user.tenant_id)
        if not tenant_id:
            raise ValidationError(
                {
                    "error_code": "ERR_TENANT_REQUIRED",
                    "detail": "Tenant identification is required for supply chain traceability.",
                }
            )
        return str(tenant_id)

    @action(detail=False, methods=["get"], url_path="trace")
    def trace(self, request: Request) -> Response:
        """
        Dynamically reconstructs full upstream and downstream lineage for a batch/lot.
        Query params: batch_id (or batch_number), branch_id (optional)
        """
        tenant_id = self._get_tenant_id(request)
        batch_id = request.query_params.get("batch_id") or request.query_params.get("batch_number")
        if not batch_id:
            raise ValidationError(
                {
                    "error_code": "ERR_BATCH_REQUIRED",
                    "detail": "Query parameter 'batch_id' or 'batch_number' is required.",
                }
            )

        branch_id = request.query_params.get("branch_id")
        lineage = SupplyChainTraceabilityService.trace_batch_lineage(
            tenant_id=tenant_id,
            actor_user=request.user,
            batch_id_or_number=batch_id,
            branch_id=branch_id,
        )
        return Response(lineage, status=status.HTTP_200_OK)

    def create(self, request: Request) -> Response:
        """
        Generates and saves a formal, audited traceability record with immutable SHA-256 hash.
        Body: batch_id (required), audit_purpose (optional), branch_id (optional), notes (optional)
        """
        tenant_id = self._get_tenant_id(request)
        batch_id = request.data.get("batch_id") or request.data.get("batch_number")
        if not batch_id:
            raise ValidationError(
                {
                    "error_code": "ERR_BATCH_REQUIRED",
                    "detail": "Field 'batch_id' or 'batch_number' is required.",
                }
            )

        audit_purpose = request.data.get("audit_purpose", "INTERNAL_AUDIT")
        branch_id = request.data.get("branch_id")
        notes = request.data.get("notes", "")

        record = SupplyChainTraceabilityService.generate_traceability_record(
            tenant_id=tenant_id,
            actor_user=request.user,
            batch_id_or_number=batch_id,
            audit_purpose=audit_purpose,
            branch_id=branch_id,
            notes=notes,
        )
        return Response(record, status=status.HTTP_201_CREATED)

    def list(self, request: Request) -> Response:
        """
        Lists generated traceability records.
        """
        tenant_id = self._get_tenant_id(request)
        batch_num = request.query_params.get("batch_number")
        audit_purpose = request.query_params.get("audit_purpose")

        records = SupplyChainTraceabilityService.list_traceability_records(
            tenant_id=tenant_id,
            actor_user=request.user,
            batch_number=batch_num,
            audit_purpose=audit_purpose,
        )
        return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)

    def retrieve(self, request: Request, pk: str = None) -> Response:
        """
        Retrieves a single traceability record by ID.
        """
        tenant_id = self._get_tenant_id(request)
        record = SupplyChainTraceabilityService.get_traceability_record(
            tenant_id=tenant_id,
            actor_user=request.user,
            trace_id=pk,
        )
        return Response(record, status=status.HTTP_200_OK)
