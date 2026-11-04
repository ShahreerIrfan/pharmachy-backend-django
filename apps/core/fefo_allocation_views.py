"""
backend/apps/core/fefo_allocation_views.py
Architecture Decision: ADR-0169 (P169).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

DRF ViewSet for FEFO Batch Allocation, Expiry & Quarantine Validation,
and Concurrency Reservation Operations.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.fefo_allocation_service import FefoAllocationService
from apps.core.models import Tenant, TenantLifecycleStatus

logger = logging.getLogger(__name__)


class FefoAllocationViewSet(viewsets.ViewSet):
    """
    API ViewSet for deterministic FEFO batch stock allocation,
    available candidate preview, and concurrency reservations.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant(self, request) -> Tenant:
        user = request.user
        tenant_id = (
            request.query_params.get("tenant_id")
            or request.data.get("tenant_id")
            or getattr(user, "tenant_id", None)
        )
        if not tenant_id:
            raise ValidationError(
                {"error_code": "ERR_TENANT_REQUIRED", "detail": "tenant_id query param or payload field is required."}
            )

        user_tenant_id = getattr(user, "tenant_id", None)
        if user_tenant_id and str(user_tenant_id) != str(tenant_id) and getattr(user, "role", None) != "SUPERADMIN":
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_MISMATCH", "detail": "Cross-tenant access prohibited."}
            )

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": "Tenant not found."})

        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {"error_code": "ERR_TENANT_SUSPENDED", "detail": "Tenant subscription suspended."}
            )

        return tenant

    @action(detail=False, methods=["get"], url_path="batches-preview")
    def batches_preview(self, request) -> Response:
        """Previews eligible FEFO batches for a product in deterministic order."""
        try:
            self._get_tenant(request)
            results = FefoAllocationService.preview_fefo_batches(
                user=request.user,
                params=request.query_params,
            )
            return Response(results, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="allocate")
    def allocate(self, request) -> Response:
        """Runs FEFO allocation and optional atomic reservation for requested medicine units."""
        try:
            self._get_tenant(request)
            result = FefoAllocationService.allocate_fefo(
                user=request.user,
                data=request.data,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="commit-reservation")
    def commit_reservation(self, request) -> Response:
        """Commits an active reservation upon checkout."""
        try:
            self._get_tenant(request)
            reservation_id = request.data.get("reservation_id")
            if not reservation_id:
                return Response(
                    {"error_code": "ERR_RESERVATION_ID_REQUIRED", "detail": "reservation_id is required."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            result = FefoAllocationService.commit_reservation(
                user=request.user,
                reservation_id=reservation_id,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="release-reservation")
    def release_reservation(self, request) -> Response:
        """Releases an active reservation upon checkout cancellation."""
        try:
            self._get_tenant(request)
            reservation_id = request.data.get("reservation_id")
            if not reservation_id:
                return Response(
                    {"error_code": "ERR_RESERVATION_ID_REQUIRED", "detail": "reservation_id is required."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            reason = request.data.get("reason", "Cart Cancelled / Timed Out")
            result = FefoAllocationService.release_reservation(
                user=request.user,
                reservation_id=reservation_id,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

