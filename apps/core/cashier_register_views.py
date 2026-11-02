"""
REST API ViewSet for Cashier Register & Drawer Assignment.
Architecture Decision: ADR-0117 (P117).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.cashier_register_service import CashierRegisterAssignmentService

logger = logging.getLogger("pharmacy.core.cashier_register.views")


class CashierRegisterAssignmentViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for managing cashier register bindings, shift linkages,
    and concurrent drawer assignment collision prevention.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/registers/assignments/"""
        branch_id = request.query_params.get("branch_id")
        register_id = request.query_params.get("register_id")
        status_filter = request.query_params.get("status")
        tenant_id = request.query_params.get("tenant_id")

        try:
            results = CashierRegisterAssignmentService.list_assignments(
                actor_user=request.user,
                tenant_id=tenant_id,
                branch_id=branch_id,
                register_id=register_id,
                status_filter=status_filter,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request) -> Response:
        """POST /api/v2/registers/assignments/"""
        data = request.data
        branch_id = data.get("branch_id")
        register_id = data.get("register_id")
        cashier_id = data.get("cashier_id")
        opening_float = Decimal(str(data.get("opening_float_amount", "0.00")))
        shift_id = data.get("shift_schedule_id")
        notes = data.get("notes", "")

        try:
            res = CashierRegisterAssignmentService.assign_register(
                actor_user=request.user,
                branch_id=branch_id,
                register_id=register_id,
                cashier_id=cashier_id,
                opening_float_amount=opening_float,
                shift_schedule_id=shift_id,
                notes=notes,
            )
            return Response(res, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            code = exc.detail.get("error_code") if isinstance(exc.detail, dict) else ""
            if code in ["ERR_REGISTER_ALREADY_ASSIGNED", "ERR_CASHIER_ALREADY_ASSIGNED"]:
                return Response(exc.detail, status=status.HTTP_409_CONFLICT)
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="close")
    def close_drawer(self, request, pk=None) -> Response:
        """POST /api/v2/registers/assignments/{id}/close/"""
        closing_float = Decimal(str(request.data.get("closing_float_amount", "0.00")))
        notes = request.data.get("notes", "")

        try:
            res = CashierRegisterAssignmentService.close_assignment(
                actor_user=request.user,
                assignment_id=pk,
                closing_float_amount=closing_float,
                notes=notes,
            )
            return Response(res, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
