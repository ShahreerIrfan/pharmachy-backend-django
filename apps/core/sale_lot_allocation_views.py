"""
backend/apps/core/sale_lot_allocation_views.py
Architecture Decision: ADR-0170 (P170).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

DRF ViewSet for Sale Item Lot Allocation Records, DGDA Recall Traceability,
Controlled Sales Returns, and Unblended Gross Profit Analytics.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import SaleLotAllocation, Tenant, TenantLifecycleStatus
from apps.core.sale_lot_allocation_service import SaleLotAllocationService

logger = logging.getLogger(__name__)


class SaleLotAllocationViewSet(viewsets.ViewSet):
    """
    API ViewSet for managing sale item lot allocation records, recall patient notifications,
    returns restock, and unblended FIFO profit derivation.
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
        """Lists sale lot allocations for tenant with optional filters."""
        try:
            tenant = self._get_tenant(request)
            qs = SaleLotAllocation.objects.select_related("invoice", "product", "batch", "cost_lot").filter(tenant=tenant)

            branch_id = request.query_params.get("branch_id")
            if branch_id:
                qs = qs.filter(branch_id=branch_id)

            invoice_id = request.query_params.get("invoice_id")
            if invoice_id:
                qs = qs.filter(invoice_id=invoice_id)

            product_id = request.query_params.get("product_id")
            if product_id:
                qs = qs.filter(product_id=product_id)

            batch_id = request.query_params.get("batch_id")
            if batch_id:
                qs = qs.filter(batch_id=batch_id)

            results = [SaleLotAllocationService.serialize_allocation(a) for a in qs[:100]]
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
        """Retrieves a single sale lot allocation record."""
        try:
            tenant = self._get_tenant(request)
            alloc = (
                SaleLotAllocation.objects.select_related("invoice", "product", "batch", "cost_lot")
                .filter(allocation_id=pk, tenant=tenant)
                .first()
            )
            if not alloc:
                raise NotFound({"error_code": "ERR_ALLOCATION_NOT_FOUND", "detail": f"Allocation {pk} not found."})

            return Response(SaleLotAllocationService.serialize_allocation(alloc), status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="record-allocations")
    def record_allocations(self, request) -> Response:
        """Records lot allocations for a sale invoice."""
        try:
            self._get_tenant(request)
            invoice_id = request.data.get("invoice_id")
            if not invoice_id:
                return Response(
                    {"error_code": "ERR_INVOICE_REQUIRED", "detail": "invoice_id is required."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            items_allocations = request.data.get("items_allocations", [])
            if not items_allocations:
                return Response(
                    {"error_code": "ERR_EMPTY_ALLOCATIONS", "detail": "items_allocations cannot be empty."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            results = SaleLotAllocationService.record_sale_lot_allocations(
                user=request.user,
                invoice_id=invoice_id,
                items_allocations=items_allocations,
            )
            return Response(results, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="process-return")
    def process_return(self, request) -> Response:
        """Processes return of units against an exact sale lot allocation."""
        try:
            self._get_tenant(request)
            allocation_id = request.data.get("allocation_id")
            if not allocation_id:
                return Response(
                    {"error_code": "ERR_ALLOCATION_ID_REQUIRED", "detail": "allocation_id is required."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            result = SaleLotAllocationService.process_lot_return(
                user=request.user,
                allocation_id=allocation_id,
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

    @action(detail=False, methods=["get"], url_path="recall-traceability")
    def recall_traceability(self, request) -> Response:
        """Traces all customers and invoices that received units from a recalled batch or lot."""
        try:
            self._get_tenant(request)
            result = SaleLotAllocationService.trace_recall_impact(
                user=request.user,
                params=request.query_params,
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

    @action(detail=False, methods=["get"], url_path="profit-analytics")
    def profit_analytics(self, request) -> Response:
        """Calculates unblended gross profit and margin analytics across batches and products."""
        try:
            self._get_tenant(request)
            result = SaleLotAllocationService.calculate_profit_analytics(
                user=request.user,
                params=request.query_params,
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

