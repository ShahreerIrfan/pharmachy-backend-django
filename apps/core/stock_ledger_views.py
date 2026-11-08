"""
backend/apps/core/stock_ledger_views.py
Architecture Decision: ADR-0201 (P201).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

REST API ViewSet for Append-Only Stock Movement Ledger:
- Read-only historical ledger queries with multi-tenant filtering.
- Batch audit trail retrieval for regulatory inspection.
- Automated audit reconciliation with cryptographic SHA-256 integrity verification.
- Strictly guards immutability: creation via direct API and mutations/deletions are blocked.
- Role-based authorization: Cashiers are denied access; Managers/Owners/Pharmacists allowed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import StockLedgerEntry, TenantLifecycleStatus
from apps.core.stock_ledger_service import StockLedgerService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    """Extracts tenant ID from authenticated user, request tenant, or header."""
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    return ""


def _get_request_branch_id(request) -> str:
    """Extracts branch ID from authenticated user or header."""
    user = getattr(request, "user", None)
    if user and getattr(user, "branch_id", None):
        return str(user.branch_id)
    header = request.META.get("HTTP_X_BRANCH_ID")
    if header:
        return str(header)
    return ""


def _check_tenant_suspended(request) -> None:
    """Blocks suspended tenants from querying ledger resources."""
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Stock ledger operations locked.",
            }
        )


def _serialize_ledger_entry(entry: StockLedgerEntry) -> Dict[str, Any]:
    """Helper serializer for StockLedgerEntry."""
    p_name = ""
    if entry.product:
        p_name = getattr(entry.product, "brand_name", "") or getattr(entry.product, "name", "")
    return {
        "entry_id": entry.entry_id,
        "tenant_id": str(entry.tenant_id),
        "branch_id": str(entry.branch_id),
        "branch_name": entry.branch.name if entry.branch else "",
        "product_id": str(entry.product_id),
        "product_name": p_name,
        "product_generic_name": getattr(entry.product, "generic_name", "") if entry.product else "",
        "batch_id": str(entry.batch_id) if entry.batch_id else None,
        "batch_number": entry.batch_number,
        "cost_lot_id": str(entry.cost_lot_id) if entry.cost_lot_id else None,
        "movement_type": entry.movement_type,
        "movement_type_display": entry.get_movement_type_display(),
        "quantity_delta": entry.quantity_delta,
        "balance_before": entry.balance_before,
        "balance_after": entry.balance_after,
        "unit_cost": str(entry.unit_cost),
        "total_cost": str(entry.total_cost),
        "unit_price": str(entry.unit_price),
        "source_entity_type": entry.source_entity_type,
        "source_entity_id": entry.source_entity_id,
        "source_reference": entry.source_reference,
        "is_cold_chain": entry.is_cold_chain,
        "storage_location": entry.storage_location,
        "notes": entry.notes,
        "immutable_verification_hash": entry.immutable_verification_hash,
        "posted_by": entry.posted_by.email if (entry.posted_by and hasattr(entry.posted_by, "email")) else None,
        "posted_at": entry.posted_at.isoformat() if entry.posted_at else None,
    }


class StockLedgerViewSet(viewsets.ViewSet):
    """
    REST API ViewSet governing Append-Only Stock Movement Ledger.
    Routes:
      - GET  /api/v2/inventory/stock-ledger/
      - GET  /api/v2/inventory/stock-ledger/{entry_id}/
      - GET  /api/v2/inventory/stock-ledger/batch-trail/?batch_number=...
      - POST /api/v2/inventory/stock-ledger/reconcile/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/inventory/stock-ledger/"""
        _check_tenant_suspended(request)
        StockLedgerService.assert_inventory_role(request.user)

        tenant_id = _get_request_tenant_id(request)
        if not tenant_id:
            return Response({"error_code": "ERR_TENANT_REQUIRED", "detail": "Tenant identification required."}, status=status.HTTP_400_BAD_REQUEST)

        branch_id = request.query_params.get("branch_id") or _get_request_branch_id(request)
        product_id = request.query_params.get("product_id")
        batch_number = request.query_params.get("batch_number")
        movement_type = request.query_params.get("movement_type")
        source_reference = request.query_params.get("source_reference")
        is_cold_chain = request.query_params.get("is_cold_chain")

        qs = StockLedgerEntry.objects.filter(tenant_id=tenant_id).select_related(
            "product", "branch", "batch", "cost_lot", "posted_by"
        )
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if product_id:
            qs = qs.filter(product_id=product_id)
        if batch_number:
            qs = qs.filter(batch_number=batch_number)
        if movement_type:
            qs = qs.filter(movement_type=movement_type)
        if source_reference:
            qs = qs.filter(source_reference__icontains=source_reference)
        if is_cold_chain is not None and is_cold_chain != "":
            cold_bool = is_cold_chain.lower() in ("true", "1", "yes")
            qs = qs.filter(is_cold_chain=cold_bool)

        from_date = request.query_params.get("from_date")
        if from_date:
            qs = qs.filter(posted_at__date__gte=from_date)
        to_date = request.query_params.get("to_date")
        if to_date:
            qs = qs.filter(posted_at__date__lte=to_date)

        entries = list(qs.order_by("-posted_at", "-entry_id")[:200])
        serialized = [_serialize_ledger_entry(e) for e in entries]
        return Response({"count": len(serialized), "results": serialized}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/inventory/stock-ledger/{entry_id}/"""
        _check_tenant_suspended(request)
        StockLedgerService.assert_inventory_role(request.user)

        tenant_id = _get_request_tenant_id(request)
        entry = (
            StockLedgerEntry.objects.filter(tenant_id=tenant_id, entry_id=pk)
            .select_related("product", "branch", "batch", "cost_lot", "posted_by")
            .first()
        )
        if not entry:
            return Response(
                {"error_code": "ERR_RESOURCE_NOT_FOUND", "detail": f"Stock ledger entry '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(_serialize_ledger_entry(entry), status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="batch-trail")
    def batch_trail(self, request) -> Response:
        """GET /api/v2/inventory/stock-ledger/batch-trail/?batch_number=..."""
        _check_tenant_suspended(request)
        StockLedgerService.assert_inventory_role(request.user)

        tenant_id = _get_request_tenant_id(request)
        batch_number = request.query_params.get("batch_number")
        if not batch_number:
            return Response(
                {"error_code": "ERR_INVALID_PARAMETERS", "detail": "Query parameter 'batch_number' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        branch_id = request.query_params.get("branch_id") or _get_request_branch_id(request)
        entries = StockLedgerService.get_batch_ledger(
            tenant_id=tenant_id,
            batch_number_or_id=batch_number,
            branch_id=branch_id or None,
        )
        serialized = [_serialize_ledger_entry(e) for e in entries]
        return Response(
            {"batch_number": batch_number, "count": len(serialized), "results": serialized},
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="reconcile")
    def reconcile(self, request) -> Response:
        """POST /api/v2/inventory/stock-ledger/reconcile/"""
        _check_tenant_suspended(request)
        StockLedgerService.assert_inventory_role(request.user)

        tenant_id = _get_request_tenant_id(request)
        data = request.data or {}
        batch_number = data.get("batch_number")
        if not batch_number:
            return Response(
                {"error_code": "ERR_INVALID_PARAMETERS", "detail": "Field 'batch_number' is required for reconciliation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        branch_id = data.get("branch_id") or _get_request_branch_id(request)
        result = StockLedgerService.reconcile_batch_stock(
            tenant_id=tenant_id,
            batch_number_or_id=batch_number,
            branch_id=branch_id or None,
        )
        return Response(result, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """Enforces immutability: Direct creation of stock ledger entries via API is disallowed."""
        return Response(
            {
                "error_code": "ERR_IMMUTABLE_RESOURCE",
                "detail": "Direct creation of stock ledger entries via API is disallowed. Entries are generated automatically via transactional stock operations.",
            },
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def update(self, request, pk=None) -> Response:
        return Response(
            {"error_code": "ERR_IMMUTABLE_RESOURCE", "detail": "Stock ledger entries are immutable and cannot be updated."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def partial_update(self, request, pk=None) -> Response:
        return Response(
            {"error_code": "ERR_IMMUTABLE_RESOURCE", "detail": "Stock ledger entries are immutable and cannot be modified."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def destroy(self, request, pk=None) -> Response:
        return Response(
            {"error_code": "ERR_IMMUTABLE_RESOURCE", "detail": "Stock ledger entries are append-only and cannot be deleted."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )
