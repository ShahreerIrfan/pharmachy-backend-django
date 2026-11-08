"""
backend/apps/core/procurement_e2e_qa_views.py
Architecture Decision: ADR-0200 (P200).
Phase 10: Suppliers, Purchase Orders & Supply Chain Capstone.

REST API ViewSet governing Procurement E2E QA & Conflict Verification:
- Automated end-to-end lifecycle verification (PO -> GRN -> Lot -> AP -> Settle).
- Duplicate receipt conflict detection.
- Purchase return and AP debit note reversal audits.
- Verification run inspection and DGDA audit logging.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import ProcurementVerificationRun, TenantLifecycleStatus
from apps.core.procurement_e2e_qa_service import ProcurementE2EQaService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
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
    user = getattr(request, "user", None)
    if user and getattr(user, "branch_id", None):
        return str(user.branch_id)
    header = request.META.get("HTTP_X_BRANCH_ID")
    if header:
        return str(header)
    return ""


def _check_tenant_suspended(request) -> None:
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Procurement QA locked.",
            }
        )


class ProcurementE2EQaViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for Procurement E2E & Conflict QA.
    Routes:
      - GET  /api/v2/procurement/qa/
      - POST /api/v2/procurement/qa/verify-e2e-cycle/
      - POST /api/v2/procurement/qa/verify-duplicate-conflict/
      - POST /api/v2/procurement/qa/verify-return-reversal/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/qa/"""
        _check_tenant_suspended(request)
        ProcurementE2EQaService.assert_procurement_role(request.user)
        tenant_id = _get_request_tenant_id(request)
        if not tenant_id:
            return Response({"error_code": "ERR_TENANT_REQUIRED", "detail": "Tenant ID required."}, status=status.HTTP_400_BAD_REQUEST)

        runs = ProcurementVerificationRun.objects.filter(tenant_id=tenant_id).order_by("-executed_at")[:50]
        results = [
            {
                "run_id": r.run_id,
                "tenant_id": str(r.tenant_id),
                "branch_id": str(r.branch_id) if r.branch_id else None,
                "test_type": r.test_type,
                "po_reference": r.po_reference,
                "grn_reference": r.grn_reference,
                "status": r.status,
                "assertions_count": r.assertions_count,
                "assertions_passed": r.assertions_passed,
                "verification_hash": r.verification_hash,
                "executed_at": r.executed_at.isoformat(),
            }
            for r in runs
        ]
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="verify-e2e-cycle")
    def verify_e2e_cycle(self, request) -> Response:
        """POST /api/v2/procurement/qa/verify-e2e-cycle/"""
        _check_tenant_suspended(request)
        ProcurementE2EQaService.assert_procurement_role(request.user)

        tenant_id = _get_request_tenant_id(request)
        branch_id = request.data.get("branch_id") or _get_request_branch_id(request)
        supplier_id = request.data.get("supplier_id")
        product_id = request.data.get("product_id")
        ordered_qty = int(request.data.get("ordered_quantity", 100))
        unit_cost = Decimal(str(request.data.get("unit_cost", "25.0000")))

        if not supplier_id or not product_id or not branch_id:
            return Response(
                {"error_code": "ERR_INVALID_PARAMETERS", "detail": "supplier_id, product_id, and branch_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = ProcurementE2EQaService.execute_full_procurement_cycle(
            tenant_id=tenant_id,
            branch_id=branch_id,
            supplier_id=supplier_id,
            product_id=product_id,
            ordered_quantity=ordered_qty,
            unit_cost=unit_cost,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="verify-duplicate-conflict")
    def verify_duplicate_conflict(self, request) -> Response:
        """POST /api/v2/procurement/qa/verify-duplicate-conflict/"""
        _check_tenant_suspended(request)
        ProcurementE2EQaService.assert_procurement_role(request.user)

        tenant_id = _get_request_tenant_id(request)
        po_id = request.data.get("po_id")
        if not po_id:
            return Response(
                {"error_code": "ERR_INVALID_PARAMETERS", "detail": "po_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = ProcurementE2EQaService.verify_duplicate_receipt_conflict(
            tenant_id=tenant_id,
            po_id=po_id,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="verify-return-reversal")
    def verify_return_reversal(self, request) -> Response:
        """POST /api/v2/procurement/qa/verify-return-reversal/"""
        _check_tenant_suspended(request)
        ProcurementE2EQaService.assert_procurement_role(request.user)

        tenant_id = _get_request_tenant_id(request)
        branch_id = request.data.get("branch_id") or _get_request_branch_id(request)
        cost_lot_id = request.data.get("cost_lot_id")
        return_qty = int(request.data.get("return_quantity", 10))

        if not cost_lot_id or not branch_id:
            return Response(
                {"error_code": "ERR_INVALID_PARAMETERS", "detail": "cost_lot_id and branch_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = ProcurementE2EQaService.verify_purchase_return_debit_note_reversal(
            tenant_id=tenant_id,
            branch_id=branch_id,
            cost_lot_id=cost_lot_id,
            return_quantity=return_qty,
            actor_user=request.user,
        )
        return Response(result, status=status.HTTP_200_OK)

