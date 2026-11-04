"""
Architecture Decision: ADR-0168 (P168)
DRF ViewSet for Goods Receipt Line to Verified Batches & Cost Lots Binding.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.batch_receipt_linking_service import BatchReceiptLinkingService
from apps.core.models import GoodsReceiptNote, Tenant, TenantLifecycleStatus

logger = logging.getLogger(__name__)


class BatchReceiptLinkingViewSet(viewsets.ViewSet):
    """
    API ViewSet for managing Goods Received Notes (GRN), binding receipt lines
    to verified batches and purchase cost lots, and idempotent inventory posting.
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
        """Lists Goods Received Notes for tenant."""
        try:
            tenant = self._get_tenant(request)
            branch_id = request.query_params.get("branch_id")
            status_filter = request.query_params.get("status")

            qs = GoodsReceiptNote.objects.select_related("supplier").prefetch_related(
                "lines__product", "lines__batch_allocations__batch"
            ).filter(tenant=tenant, is_active=True)

            if branch_id:
                qs = qs.filter(branch_id=branch_id)
            if status_filter:
                qs = qs.filter(status=status_filter)

            results = [BatchReceiptLinkingService.serialize_grn(grn) for grn in qs]
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

    def retrieve(self, request, pk=None) -> Response:
        """Retrieves a single Goods Received Note with all lines and batch allocations."""
        try:
            tenant = self._get_tenant(request)
            grn = (
                GoodsReceiptNote.objects.select_related("supplier")
                .prefetch_related("lines__product", "lines__batch_allocations__batch")
                .filter(grn_id=pk, tenant=tenant)
                .first()
            )
            if not grn:
                raise NotFound({"error_code": "ERR_GRN_NOT_FOUND", "detail": f"GRN {pk} not found."})

            return Response(BatchReceiptLinkingService.serialize_grn(grn), status=status.HTTP_200_OK)
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
        """Creates a new Goods Received Note."""
        try:
            tenant = self._get_tenant(request)
            payload = dict(request.data)
            payload["tenant_id"] = tenant.tenant_id

            grn = BatchReceiptLinkingService.create_grn(user=request.user, data=payload)
            return Response(grn, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="add-line")
    def add_line(self, request, pk=None) -> Response:
        """Appends a line item to the Goods Received Note."""
        try:
            self._get_tenant(request)
            line = BatchReceiptLinkingService.add_grn_line(
                user=request.user,
                grn_id=pk,
                data=request.data,
            )
            return Response(line, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="link-batch")
    def link_batch(self, request, pk=None) -> Response:
        """Binds a verified batch and quantity to a GRN line item."""
        try:
            self._get_tenant(request)
            line_id = request.data.get("line_id")
            if not line_id:
                return Response(
                    {"error_code": "ERR_LINE_REQUIRED", "detail": "line_id is required."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            link = BatchReceiptLinkingService.link_batch_to_line(
                user=request.user,
                grn_id=pk,
                line_id=line_id,
                data=request.data,
            )
            return Response(link, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="post-receipt")
    def post_receipt(self, request, pk=None) -> Response:
        """
        Idempotently posts the GRN to inventory.
        Increments batch stock and creates purchase cost lots exactly once.
        """
        try:
            self._get_tenant(request)
            result = BatchReceiptLinkingService.post_grn(
                user=request.user,
                grn_id=pk,
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

    @action(detail=True, methods=["post"], url_path="reverse-receipt")
    def reverse_receipt(self, request, pk=None) -> Response:
        """
        Reverses a posted GRN, deducting stock from batch and purchase cost lots.
        """
        try:
            self._get_tenant(request)
            reason = request.data.get("reason", "Goods Return / Invoice Cancellation")
            result = BatchReceiptLinkingService.reverse_grn(
                user=request.user,
                grn_id=pk,
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

