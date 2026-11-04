"""
backend/apps/core/multi_level_unit_conversion_views.py
Architecture Decision: ADR-0163 (P163).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API ViewSet for Multi-Level Unit Conversion, Fractional Tablet Guardrails,
and Dosing Unit Validation.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import Tenant, TenantLifecycleStatus, UnitConversionPolicy
from apps.core.multi_level_unit_conversion_service import MultiLevelUnitConversionService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.multi_level_unit_conversion.views")


def serialize_policy(p: UnitConversionPolicy) -> Dict[str, Any]:
    return {
        "policy_id": p.policy_id,
        "tenant_id": p.tenant_id,
        "branch_id": p.branch_id if p.branch else None,
        "product_id": p.product_id,
        "product_name": p.product.brand_name if p.product else "",
        "is_splittable": p.is_splittable,
        "allowed_split_fractions": p.allowed_split_fractions,
        "clinical_dosing_unit": p.clinical_dosing_unit,
        "dosing_conversion_ratio": str(p.dosing_conversion_ratio),
        "prohibited_dosing_units": p.prohibited_dosing_units,
        "notes": p.notes,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


class MultiLevelUnitConversionViewSet(viewsets.ViewSet):
    """
    ViewSet for Multi-Level Unit Conversion and Dosing Unit Validation.
    Enforces tenant boundaries, RBAC permissions, fractional tablet prevention,
    and incompatible dosing-unit guardrails.
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
        """Lists unit conversion policies for the requesting tenant."""
        tenant = self._get_tenant(request)
        qs = UnitConversionPolicy.objects.filter(tenant=tenant, is_active=True)

        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)

        is_splittable = request.query_params.get("is_splittable")
        if is_splittable is not None:
            qs = qs.filter(is_splittable=is_splittable.lower() in ["true", "1"])

        results = [serialize_policy(p) for p in qs]
        return Response({"count": len(results), "policies": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None):
        """Retrieves a single policy by policy_id enforcing tenant boundaries."""
        tenant = self._get_tenant(request)
        policy = UnitConversionPolicy.objects.filter(policy_id=pk, tenant=tenant).first()
        if not policy:
            return Response(
                {"error_code": "ERR_POLICY_NOT_FOUND", "detail": f"Unit conversion policy '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(serialize_policy(policy), status=status.HTTP_200_OK)

    def create(self, request):
        """Creates or upserts a unit conversion policy."""
        try:
            policy = MultiLevelUnitConversionService.create_or_update_policy(
                user=request.user, data=request.data
            )
            return Response(serialize_policy(policy), status=status.HTTP_201_CREATED)
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
        """Updates an existing policy."""
        try:
            policy = MultiLevelUnitConversionService.create_or_update_policy(
                user=request.user, data=request.data, policy_id=pk
            )
            return Response(serialize_policy(policy), status=status.HTTP_200_OK)
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
        """Soft-deactivates an existing policy."""
        tenant = self._get_tenant(request)
        if getattr(request.user, "role", None) in [UserRole.CASHIER]:
            return Response(
                {"error_code": "ERR_ROLE_UNAUTHORIZED", "detail": "Cashier cannot archive unit policies."},
                status=status.HTTP_403_FORBIDDEN,
            )
        policy = UnitConversionPolicy.objects.filter(policy_id=pk, tenant=tenant).first()
        if not policy:
            return Response(
                {"error_code": "ERR_POLICY_NOT_FOUND", "detail": f"Unit conversion policy '{pk}' was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        policy.is_active = False
        policy.save()
        return Response({"status": "ARCHIVED", "policy_id": pk}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="convert")
    def convert(self, request):
        """Converts quantity between packaging tiers with guardrails against inappropriate dosing units."""
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
            result = MultiLevelUnitConversionService.convert_multi_level(
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
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_dispense(self, request):
        """Pre-flight check for dispense or checkout unit and quantity."""
        tenant = self._get_tenant(request)
        product_id = request.data.get("product_id")
        unit = request.data.get("unit")
        raw_qty = request.data.get("quantity")

        if not product_id or not unit or raw_qty is None:
            return Response(
                {"error_code": "ERR_PARAMS_REQUIRED", "detail": "Fields 'product_id', 'unit', and 'quantity' are required."},
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
            result = MultiLevelUnitConversionService.validate_dispense_unit_and_quantity(
                product_id=product_id,
                tenant_id=tenant.tenant_id,
                unit=unit,
                quantity=qty,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["get"], url_path="allowed-units")
    def allowed_units(self, request):
        """Returns allowed packaging units, base units, and prohibited units for a product."""
        tenant = self._get_tenant(request)
        product_id = request.query_params.get("product_id")
        if not product_id:
            return Response(
                {"error_code": "ERR_PRODUCT_REQUIRED", "detail": "Query parameter 'product_id' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = MultiLevelUnitConversionService.get_allowed_units(
                product_id=product_id,
                tenant_id=tenant.tenant_id,
            )
            return Response(result, status=status.HTTP_200_OK)
        except NotFound as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

