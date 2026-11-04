"""
backend/apps/core/stock_availability_views.py
Architecture Decision: ADR-0171 (P171).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API Endpoints for Branch Stock Availability Matrix,
Real-time POS Pre-Check, and Packaging Breakdowns.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import BranchStockAvailability, InventoryBaseUnitStrategy, TenantLifecycleStatus
from apps.core.stock_availability_service import StockAvailabilityService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class BranchStockAvailabilitySerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.brand_name", read_only=True)
    generic_name = serializers.CharField(source="product.generic_name", read_only=True)
    base_unit = serializers.SerializerMethodField()
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    packaging_summary = serializers.SerializerMethodField()

    class Meta:
        model = BranchStockAvailability
        fields = [
            "availability_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "product_id",
            "product_name",
            "generic_name",
            "base_unit",
            "quantity_on_hand",
            "quantity_reserved",
            "quantity_quarantined",
            "quantity_pending_inward",
            "quantity_physically_available",
            "quantity_server_confirmed_available",
            "reorder_level_base_units",
            "is_out_of_stock",
            "is_below_reorder_level",
            "packaging_summary",
            "version",
            "last_recalculated_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_base_unit(self, obj: BranchStockAvailability) -> str:
        strat = InventoryBaseUnitStrategy.objects.filter(product=obj.product, is_active=True).first()
        return strat.base_unit if strat and strat.base_unit else "PIECE"

    def get_packaging_summary(self, obj: BranchStockAvailability) -> str:
        bd = StockAvailabilityService.format_packaging_breakdown(
            obj.product, obj.quantity_server_confirmed_available
        )
        return bd.get("formatted_summary", "")


class StockAvailabilityViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet providing multi-tier branch stock availability metrics,
    sub-millisecond POS pre-checkout validations, and administrative synchronization.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = BranchStockAvailabilitySerializer
    lookup_field = "availability_id"

    def _check_tenant_and_role(self, request: Any) -> None:
        user = request.user
        role = getattr(user, "role", None)
        if role not in {
            UserRole.CASHIER,
            UserRole.STAFF_PHARMACIST,
            UserRole.BRANCH_MANAGER,
            UserRole.PHARMACY_OWNER,
            UserRole.INVENTORY_CLERK,
            UserRole.AUDITOR,
            UserRole.SUPERADMIN,
            "CASHIER",
            "STAFF_PHARMACIST",
            "BRANCH_MANAGER",
            "PHARMACY_OWNER",
            "INVENTORY_CLERK",
            "AUDITOR",
            "SUPERADMIN",
            "OWNER_ADMIN",
            "ADMIN",
        }:
            raise PermissionDenied(
                {"detail": "Role is not authorized to access inventory availability.", "error_code": "ERR_ROLE_UNAUTHORIZED"}
            )

        tenant = getattr(request, "tenant", None) or getattr(user, "tenant", None)
        if tenant and (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {"detail": "Tenant subscription is suspended.", "error_code": "ERR_TENANT_SUSPENDED"}
            )

    def get_queryset(self):
        self._check_tenant_and_role(self.request)
        user = self.request.user
        tenant_id = self.request.query_params.get("tenant_id") or getattr(user, "tenant_id", None)
        qs = BranchStockAvailability.objects.select_related("product", "branch", "tenant").all()

        if getattr(user, "role", None) != UserRole.SUPERADMIN and tenant_id:
            qs = qs.filter(tenant__tenant_id=tenant_id)
        elif tenant_id:
            qs = qs.filter(tenant__tenant_id=tenant_id)

        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)

        product_id = self.request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product__product_id=product_id)

        out_of_stock = self.request.query_params.get("is_out_of_stock")
        if out_of_stock is not None:
            val = str(out_of_stock).lower() in ("true", "1")
            qs = qs.filter(is_out_of_stock=val)

        below_reorder = self.request.query_params.get("is_below_reorder_level")
        if below_reorder is not None:
            val = str(below_reorder).lower() in ("true", "1")
            qs = qs.filter(is_below_reorder_level=val)

        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(product__brand_name__icontains=search)

        return qs

    @action(detail=False, methods=["get"], url_path="product-details")
    def product_details(self, request):
        """
        GET /api/v2/inventory/availability/product-details/?branch_id=...&product_id=...
        """
        self._check_tenant_and_role(request)
        params = request.query_params.dict()
        if "tenant_id" not in params and getattr(request.user, "tenant_id", None):
            params["tenant_id"] = str(request.user.tenant_id)

        result = StockAvailabilityService.get_availability_details(request.user, params)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="precheck")
    def precheck(self, request):
        """
        POST /api/v2/inventory/availability/precheck/
        Fast pre-checkout availability verification.
        """
        self._check_tenant_and_role(request)
        data = dict(request.data)
        if "tenant_id" not in data and getattr(request.user, "tenant_id", None):
            data["tenant_id"] = str(request.user.tenant_id)

        result = StockAvailabilityService.precheck_checkout_availability(request.user, data)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="refresh")
    def refresh(self, request):
        """
        POST /api/v2/inventory/availability/refresh/
        Forces administrative synchronization of branch availability matrix.
        """
        self._check_tenant_and_role(request)
        data = dict(request.data)
        if "tenant_id" not in data and getattr(request.user, "tenant_id", None):
            data["tenant_id"] = str(request.user.tenant_id)

        result = StockAvailabilityService.refresh_branch_matrix(request.user, data)
        return Response(result, status=status.HTTP_200_OK)

