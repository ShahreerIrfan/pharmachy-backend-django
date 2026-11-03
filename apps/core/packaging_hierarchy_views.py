"""
backend/apps/core/packaging_hierarchy_views.py
Architecture Decision: ADR-0162 (P162).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API ViewSet for Packaging Hierarchy Schema, Tree Resolution,
and Effective-Dated Conversions.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import PackagingHierarchyNode, Tenant, TenantLifecycleStatus
from apps.core.packaging_hierarchy_service import PackagingHierarchyService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.packaging_hierarchy.views")


def serialize_node(node: PackagingHierarchyNode) -> Dict[str, Any]:
    return {
        "node_id": node.node_id,
        "tenant_id": node.tenant_id,
        "branch_id": node.branch_id if node.branch else None,
        "product_id": node.product_id,
        "product_name": node.product.brand_name if node.product else "",
        "parent_unit": node.parent_unit,
        "child_unit": node.child_unit,
        "conversion_factor": node.conversion_factor,
        "tier_level": node.tier_level,
        "is_base_level": node.is_base_level,
        "tier_gtin_barcode": node.tier_gtin_barcode,
        "effective_from": node.effective_from.isoformat(),
        "effective_to": node.effective_to.isoformat() if node.effective_to else None,
        "is_active": node.is_active,
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
    }


class PackagingHierarchyViewSet(viewsets.ViewSet):
    """
    ViewSet for Packaging Hierarchy Trees and Conversion Factors.
    Enforces tenant scoping, RBAC permissions, cycle prevention,
    and effective-dated factor calculations.
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
        """Lists packaging hierarchy nodes for the tenant."""
        tenant = self._get_tenant(request)
        qs = PackagingHierarchyNode.objects.filter(tenant=tenant, is_active=True)

        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)

        parent_unit = request.query_params.get("parent_unit")
        if parent_unit:
            qs = qs.filter(parent_unit=parent_unit.upper())

        child_unit = request.query_params.get("child_unit")
        if child_unit:
            qs = qs.filter(child_unit=child_unit.upper())

        results = [serialize_node(n) for n in qs]
        return Response({"count": len(results), "nodes": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None):
        """Retrieves a single packaging hierarchy node."""
        tenant = self._get_tenant(request)
        node = PackagingHierarchyNode.objects.filter(node_id=pk, tenant=tenant).first()
        if not node:
            return Response(
                {"error_code": "ERR_NODE_NOT_FOUND", "detail": f"Packaging hierarchy node '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(serialize_node(node), status=status.HTTP_200_OK)

    def create(self, request):
        """Creates a new packaging hierarchy node."""
        try:
            node = PackagingHierarchyService.create_or_update_node(
                user=request.user, data=request.data
            )
            return Response(serialize_node(node), status=status.HTTP_201_CREATED)
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
        """Updates an existing node."""
        try:
            node = PackagingHierarchyService.create_or_update_node(
                user=request.user, data=request.data, node_id=pk
            )
            return Response(serialize_node(node), status=status.HTTP_200_OK)
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
        """Soft-deactivates an existing node."""
        tenant = self._get_tenant(request)
        if getattr(request.user, "role", None) in [UserRole.CASHIER]:
            return Response(
                {"error_code": "ERR_ROLE_UNAUTHORIZED", "detail": "Cashier cannot archive packaging nodes."},
                status=status.HTTP_403_FORBIDDEN,
            )
        node = PackagingHierarchyNode.objects.filter(node_id=pk, tenant=tenant).first()
        if not node:
            return Response(
                {"error_code": "ERR_NODE_NOT_FOUND", "detail": f"Packaging hierarchy node '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        node.is_active = False
        node.save()
        return Response({"status": "ARCHIVED", "node_id": pk}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="tree")
    def tree(self, request):
        """Resolves the full packaging hierarchy tree for a product as of a specific date."""
        tenant = self._get_tenant(request)
        product_id = request.query_params.get("product_id")
        if not product_id:
            return Response(
                {"error_code": "ERR_PRODUCT_REQUIRED", "detail": "Query parameter 'product_id' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        as_of_date = request.query_params.get("as_of_date")
        try:
            tree_data = PackagingHierarchyService.resolve_packaging_tree(
                product_id=product_id, tenant_id=tenant.tenant_id, as_of_date=as_of_date
            )
            return Response(tree_data, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="convert")
    def convert(self, request):
        """Converts quantity between packaging hierarchy tiers as of a target date."""
        tenant = self._get_tenant(request)
        product_id = request.data.get("product_id")
        from_unit = request.data.get("from_unit")
        to_unit = request.data.get("to_unit")
        raw_qty = request.data.get("quantity")
        as_of_date = request.data.get("as_of_date")

        if not product_id or not from_unit or not to_unit or raw_qty is None:
            return Response(
                {
                    "error_code": "ERR_PARAMS_REQUIRED",
                    "detail": "Fields 'product_id', 'from_unit', 'to_unit', and 'quantity' are required.",
                },
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
            result = PackagingHierarchyService.convert_packaging_tier(
                product_id=product_id,
                tenant_id=tenant.tenant_id,
                from_unit=from_unit,
                to_unit=to_unit,
                quantity=qty,
                as_of_date=as_of_date,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

