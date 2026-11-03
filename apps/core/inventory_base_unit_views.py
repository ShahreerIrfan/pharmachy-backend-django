"""
backend/apps/core/inventory_base_unit_views.py
Architecture Decision: ADR-0161 (P161).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API ViewSet for Inventory Base-Unit Strategy, Conversions,
Indivisibility Rules, and Inventory Valuation.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.inventory_base_unit_service import InventoryBaseUnitService
from apps.core.models import Branch, InventoryBaseUnitStrategy, Tenant, TenantLifecycleStatus
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.inventory_base_unit.views")


def serialize_strategy(s: InventoryBaseUnitStrategy) -> Dict[str, Any]:
    return {
        "strategy_id": s.strategy_id,
        "tenant_id": s.tenant_id,
        "branch_id": s.branch_id if s.branch else None,
        "product_id": s.product_id,
        "product_name": s.product.brand_name if s.product else "",
        "base_unit": s.base_unit,
        "is_indivisible": s.is_indivisible,
        "allow_decimal_quantities": s.allow_decimal_quantities,
        "decimal_places_allowed": s.decimal_places_allowed,
        "minimum_dispense_quantity": str(s.minimum_dispense_quantity),
        "pack_unit_label": s.pack_unit_label,
        "intermediate_unit_label": s.intermediate_unit_label,
        "pack_to_intermediate_multiplier": s.pack_to_intermediate_multiplier,
        "intermediate_to_base_multiplier": s.intermediate_to_base_multiplier,
        "total_base_units_per_pack": s.total_base_units_per_pack,
        "cost_per_base_unit": str(s.cost_per_base_unit),
        "mrp_per_base_unit": str(s.mrp_per_base_unit),
        "notes": s.notes,
        "is_active": s.is_active,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


class InventoryBaseUnitStrategyViewSet(viewsets.ViewSet):
    """
    ViewSet for Inventory Base-Unit Strategy.
    Provides CRUD operations, unit-tier packaging conversions,
    indivisibility validation, and exact financial valuation.
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
                "detail": "Tenant is suspended or inactive.",
            })
        return tenant

    def list(self, request):
        """Lists all inventory base unit strategies scoped to the requesting tenant."""
        tenant = self._get_tenant(request)
        qs = InventoryBaseUnitStrategy.objects.filter(tenant=tenant, is_active=True)

        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)

        base_unit = request.query_params.get("base_unit")
        if base_unit:
            qs = qs.filter(base_unit=base_unit.upper())

        is_indivisible = request.query_params.get("is_indivisible")
        if is_indivisible is not None:
            qs = qs.filter(is_indivisible=is_indivisible.lower() in ["true", "1"])

        results = [serialize_strategy(s) for s in qs]
        return Response({"count": len(results), "strategies": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None):
        """Retrieves a single strategy by strategy_id enforcing tenant boundaries."""
        tenant = self._get_tenant(request)
        strategy = InventoryBaseUnitStrategy.objects.filter(strategy_id=pk, tenant=tenant).first()
        if not strategy:
            return Response(
                {"error_code": "ERR_STRATEGY_NOT_FOUND", "detail": f"Strategy '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(serialize_strategy(strategy), status=status.HTTP_200_OK)

    def create(self, request):
        """Creates or upserts an inventory base unit strategy."""
        try:
            strategy = InventoryBaseUnitService.create_or_update_strategy(
                user=request.user, data=request.data
            )
            return Response(serialize_strategy(strategy), status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None):
        """Updates an existing strategy."""
        try:
            strategy = InventoryBaseUnitService.create_or_update_strategy(
                user=request.user, data=request.data, strategy_id=pk
            )
            return Response(serialize_strategy(strategy), status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, pk=None):
        """Soft-deactivates an existing strategy."""
        tenant = self._get_tenant(request)
        if getattr(request.user, "role", None) in [UserRole.CASHIER]:
            return Response(
                {"error_code": "ERR_ROLE_UNAUTHORIZED", "detail": "Cashier cannot archive strategies."},
                status=status.HTTP_403_FORBIDDEN,
            )
        strategy = InventoryBaseUnitStrategy.objects.filter(strategy_id=pk, tenant=tenant).first()
        if not strategy:
            return Response(
                {"error_code": "ERR_STRATEGY_NOT_FOUND", "detail": f"Strategy '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        strategy.is_active = False
        strategy.save()
        return Response({"status": "ARCHIVED", "strategy_id": pk}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="convert")
    def convert(self, request, pk=None):
        """Converts quantity from specified tier (PACK, INTERMEDIATE, BASE) to base units."""
        tenant = self._get_tenant(request)
        strategy = InventoryBaseUnitStrategy.objects.filter(strategy_id=pk, tenant=tenant).first()
        if not strategy:
            return Response(
                {"error_code": "ERR_STRATEGY_NOT_FOUND", "detail": f"Strategy '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        raw_qty = request.data.get("quantity")
        unit_tier = request.data.get("unit_tier", "BASE")
        if raw_qty is None:
            return Response(
                {"error_code": "ERR_QUANTITY_REQUIRED", "detail": "Field 'quantity' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            qty = Decimal(str(raw_qty))
        except Exception:
            return Response(
                {"error_code": "ERR_INVALID_DECIMAL", "detail": "Quantity must be a valid number."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = InventoryBaseUnitService.convert_to_base_units(
                strategy=strategy, quantity=qty, unit_tier=unit_tier
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @action(detail=True, methods=["post"], url_path="decompose")
    def decompose(self, request, pk=None):
        """Reverses / unpacks base integer units back into Packs, Strips, and Loose Pieces."""
        tenant = self._get_tenant(request)
        strategy = InventoryBaseUnitStrategy.objects.filter(strategy_id=pk, tenant=tenant).first()
        if not strategy:
            return Response(
                {"error_code": "ERR_STRATEGY_NOT_FOUND", "detail": f"Strategy '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        raw_units = request.data.get("total_base_units")
        if raw_units is None:
            return Response(
                {"error_code": "ERR_UNITS_REQUIRED", "detail": "Field 'total_base_units' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            total_units = int(raw_units)
        except Exception:
            return Response(
                {"error_code": "ERR_INVALID_INTEGER", "detail": "Field 'total_base_units' must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = InventoryBaseUnitService.decompose_base_units(
                strategy=strategy, total_base_units=total_units
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="validate-quantity")
    def validate_quantity(self, request, pk=None):
        """Dry-run validation of a requested checkout or adjustment quantity."""
        tenant = self._get_tenant(request)
        strategy = InventoryBaseUnitStrategy.objects.filter(strategy_id=pk, tenant=tenant).first()
        if not strategy:
            return Response(
                {"error_code": "ERR_STRATEGY_NOT_FOUND", "detail": f"Strategy '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        raw_qty = request.data.get("quantity")
        if raw_qty is None:
            return Response(
                {"error_code": "ERR_QUANTITY_REQUIRED", "detail": "Field 'quantity' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            qty = Decimal(str(raw_qty))
        except Exception:
            return Response(
                {"error_code": "ERR_INVALID_DECIMAL", "detail": "Quantity must be a valid number."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = InventoryBaseUnitService.validate_dispense_quantity(
                strategy=strategy, quantity=qty
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @action(detail=False, methods=["get"], url_path="valuation")
    def valuation(self, request):
        """Returns total inventory valuation in BDT for the tenant."""
        tenant = self._get_tenant(request)
        branch_id = request.query_params.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()

        product_id = request.query_params.get("product_id")
        result = InventoryBaseUnitService.calculate_inventory_valuation(
            tenant=tenant, branch=branch, product_id=product_id
        )
        return Response(result, status=status.HTTP_200_OK)

