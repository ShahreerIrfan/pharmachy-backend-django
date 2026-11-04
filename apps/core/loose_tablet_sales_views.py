"""
backend/apps/core/loose_tablet_sales_views.py
Architecture Decision: ADR-0164 (P164).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API ViewSet for Loose Tablet Sales, Partial Strip Dispensing,
Base-Piece Stock Deduction, and Prohibition Guardrails.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.loose_tablet_sales_service import LooseTabletSalesService
from apps.core.models import (
    LooseTabletSalesConfig,
    PaymentMethod,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.core.loose_tablet_sales.views")


def serialize_loose_sales_config(c: LooseTabletSalesConfig) -> Dict[str, Any]:
    return {
        "config_id": c.config_id,
        "tenant_id": c.tenant_id,
        "branch_id": c.branch_id if c.branch else None,
        "product_id": c.product_id,
        "product_name": c.product.brand_name if c.product else "",
        "allow_loose_sales": c.allow_loose_sales,
        "min_loose_dispense_units": c.min_loose_dispense_units,
        "max_loose_dispense_units": c.max_loose_dispense_units,
        "strip_size_pieces": c.strip_size_pieces,
        "strip_mrp": str(c.strip_mrp),
        "loose_unit_mrp": str(c.loose_unit_mrp),
        "apply_loose_unit_surcharge": c.apply_loose_unit_surcharge,
        "loose_surcharge_percentage": str(c.loose_surcharge_percentage),
        "prohibition_reason": c.prohibition_reason,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


class LooseTabletSalesViewSet(viewsets.ViewSet):
    """
    ViewSet for Loose Tablet Sales and Partial Strip Dispensing.
    Enforces multi-tenant boundaries, RBAC permissions, blister integrity,
    and base-piece stock conservation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_tenant(self, request) -> Tenant:
        tenant_id = getattr(request.user, "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"error_code": "ERR_TENANT_REQUIRED", "detail": "Active tenant context required."})
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant or not tenant.is_active or tenant.status in [
            TenantLifecycleStatus.SUSPENDED,
            TenantLifecycleStatus.ARCHIVED,
        ]:
            raise PermissionDenied({
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": f"Tenant '{tenant_id}' is inactive or suspended.",
            })
        return tenant

    def list(self, request) -> Response:
        """List loose tablet sales configs for the authenticated tenant."""
        tenant = self._get_tenant(request)
        qs = LooseTabletSalesConfig.objects.filter(tenant=tenant)

        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)

        branch_id = request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        return Response([serialize_loose_sales_config(c) for c in qs])

    def create(self, request) -> Response:
        """Create or update a loose tablet sales configuration."""
        self._get_tenant(request)
        try:
            config = LooseTabletSalesService.create_or_update_config(
                user=request.user,
                data=request.data,
            )
            return Response(serialize_loose_sales_config(config), status=status.HTTP_201_CREATED)
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
        """Retrieve a specific configuration by ID."""
        tenant = self._get_tenant(request)
        config = LooseTabletSalesConfig.objects.filter(config_id=pk, tenant=tenant).first()
        if not config:
            raise NotFound({"error_code": "ERR_CONFIG_NOT_FOUND", "detail": f"Config '{pk}' was not found."})
        return Response(serialize_loose_sales_config(config))

    def update(self, request, pk=None) -> Response:
        """Update a specific configuration by ID."""
        self._get_tenant(request)
        try:
            config = LooseTabletSalesService.create_or_update_config(
                user=request.user,
                data=request.data,
                config_id=pk,
            )
            return Response(serialize_loose_sales_config(config), status=status.HTTP_200_OK)
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
        return self.update(request, pk=pk)

    @action(detail=False, methods=["post"], url_path="quote")
    def quote(self, request) -> Response:
        """
        Calculate quotation for strip + loose tablet dispensing.
        Validates loose tablet prohibition guardrails.
        """
        tenant = self._get_tenant(request)
        product_id = request.data.get("product_id")
        if not product_id:
            return Response(
                {"error_code": "ERR_PRODUCT_REQUIRED", "detail": "product_id is required."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        branch_id = request.data.get("branch_id")
        strips = int(request.data.get("strips", 0))
        loose_pieces = int(request.data.get("loose_pieces", 0))
        total_pieces = request.data.get("total_pieces")
        if total_pieces is not None:
            total_pieces = int(total_pieces)

        try:
            quote_data = LooseTabletSalesService.calculate_quote(
                tenant_id=tenant.tenant_id,
                product_id=product_id,
                branch_id=branch_id,
                strips=strips,
                loose_pieces=loose_pieces,
                total_pieces=total_pieces,
            )
            return Response(quote_data, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="checkout")
    def checkout(self, request) -> Response:
        """
        Atomically checkout loose tablets and strips.
        Deducts base units from BatchStock and creates SaleInvoice.
        """
        tenant = self._get_tenant(request)
        branch_id = request.data.get("branch_id")
        if not branch_id:
            return Response(
                {"error_code": "ERR_BRANCH_REQUIRED", "detail": "branch_id is required for checkout."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        product_id = request.data.get("product_id")
        if not product_id:
            return Response(
                {"error_code": "ERR_PRODUCT_REQUIRED", "detail": "product_id is required for checkout."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        strips = int(request.data.get("strips", 0))
        loose_pieces = int(request.data.get("loose_pieces", 0))
        total_pieces = request.data.get("total_pieces")
        if total_pieces is not None:
            total_pieces = int(total_pieces)

        batch_id = request.data.get("batch_id")
        customer_name = request.data.get("customer_name", "Walk-in Customer")
        customer_phone = request.data.get("customer_phone", "")
        payment_method = request.data.get("payment_method", PaymentMethod.CASH)

        try:
            result = LooseTabletSalesService.execute_checkout(
                tenant_id=tenant.tenant_id,
                branch_id=branch_id,
                product_id=product_id,
                strips=strips,
                loose_pieces=loose_pieces,
                total_pieces=total_pieces,
                batch_id=batch_id,
                customer_name=customer_name,
                customer_phone=customer_phone,
                payment_method=payment_method,
                user=request.user,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="reverse")
    def reverse(self, request) -> Response:
        """
        Reverses a loose tablet sale invoice and re-credits base units back to BatchStock.
        """
        tenant = self._get_tenant(request)
        invoice_id = request.data.get("invoice_id")
        if not invoice_id:
            return Response(
                {"error_code": "ERR_INVOICE_REQUIRED", "detail": "invoice_id is required for reversal."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        reason = request.data.get("reason", "Customer Return / Void")
        try:
            result = LooseTabletSalesService.reverse_sale(
                tenant_id=tenant.tenant_id,
                invoice_id=invoice_id,
                reason=reason,
                user=request.user,
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
