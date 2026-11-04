"""
Architecture Decision: ADR-0166 (P166)
DRF ViewSet for Stable Medicine Batch Entity, Expiry Lifecycle & Statutory Recall Integration.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.batch_entity_service import BatchEntityService
from apps.core.models import Tenant, TenantLifecycleStatus

logger = logging.getLogger(__name__)


class BatchEntityViewSet(viewsets.ViewSet):
    """
    API endpoints for managing pharmaceutical batch entities,
    expiry constraints, DGDA recall links, and checkout dispensability validation.
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

    def list(self, request) -> Response:
        """
        Lists batch stock lots for the current tenant in FEFO order.
        """
        try:
            tenant = self._get_tenant(request)
            branch_id = request.query_params.get("branch_id")
            product_id = request.query_params.get("product_id")
            status_filter = request.query_params.get("status")
            search = request.query_params.get("search")

            batches = BatchEntityService.list_batches(
                user=request.user,
                tenant_id=tenant.tenant_id,
                branch_id=branch_id,
                product_id=product_id,
                status_filter=status_filter,
                search=search,
            )
            return Response(batches, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """
        Retrieves a single batch with auto-resolved recall status.
        """
        try:
            tenant = self._get_tenant(request)
            batch = BatchEntityService.get_batch(
                user=request.user,
                tenant_id=tenant.tenant_id,
                batch_id=pk,
            )
            return Response(batch, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """
        Creates a new medicine batch with strict manufacturing/expiry checks.
        """
        try:
            tenant = self._get_tenant(request)
            payload = dict(request.data)
            payload["tenant_id"] = tenant.tenant_id

            batch = BatchEntityService.create_batch(
                user=request.user,
                data=payload,
            )
            return Response(batch, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None) -> Response:
        """
        Audited partial update of non-key batch metadata.
        """
        try:
            tenant = self._get_tenant(request)
            batch = BatchEntityService.update_batch(
                user=request.user,
                tenant_id=tenant.tenant_id,
                batch_id=pk,
                data=request.data,
            )
            return Response(batch, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="transition-status")
    def transition_status(self, request, pk=None) -> Response:
        """
        Transitions lifecycle status: ACTIVE -> QUARANTINED, QUARANTINED -> ACTIVE, RECALLED, ARCHIVED.
        """
        try:
            tenant = self._get_tenant(request)
            target_status = request.data.get("target_status")
            reason = request.data.get("reason", "")
            if not target_status:
                return Response(
                    {"error_code": "ERR_STATUS_REQUIRED", "detail": "target_status is required."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            batch = BatchEntityService.transition_status(
                user=request.user,
                tenant_id=tenant.tenant_id,
                batch_id=pk,
                target_status=target_status,
                reason=reason,
            )
            return Response(batch, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="validate-dispense")
    def validate_dispense(self, request, pk=None) -> Response:
        """
        POS checkout pre-check endpoint. Validates dispensability and stock availability.
        """
        try:
            tenant = self._get_tenant(request)
            requested_units = int(request.data.get("requested_units", 1))

            result = BatchEntityService.validate_dispense(
                user=request.user,
                tenant_id=tenant.tenant_id,
                batch_id=pk,
                requested_units=requested_units,
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

