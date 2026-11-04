"""
backend/apps/core/non_divisible_pack_views.py
Architecture Decision: ADR-0165 (P165).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

REST API ViewSet for Non-Divisible Regulated Pack Restrictions,
Sterile Container Guardrails, and Approved Splitting Protocols.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    NonDivisiblePackPolicy,
    PaymentMethod,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.non_divisible_pack_service import NonDivisiblePackService

logger = logging.getLogger("pharmacy.core.non_divisible_pack.views")


def serialize_non_divisible_policy(p: NonDivisiblePackPolicy) -> Dict[str, Any]:
    return {
        "policy_id": p.policy_id,
        "tenant_id": p.tenant_id,
        "branch_id": p.branch_id if p.branch else None,
        "product_id": p.product_id,
        "product_name": p.product.brand_name if p.product else "",
        "container_type": p.container_type,
        "is_splitting_allowed": p.is_splitting_allowed,
        "require_pharmacist_approval": p.require_pharmacist_approval,
        "approved_break_factor": p.approved_break_factor,
        "sub_unit_label": p.sub_unit_label,
        "prohibition_reason": p.prohibition_reason,
        "exemption_protocols": p.exemption_protocols,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


class NonDivisiblePackViewSet(viewsets.ViewSet):
    """
    ViewSet for Non-Divisible Pack Restrictions and Sterile Container Splitting Policies.
    Enforces packaging integrity, clinical exemption protocols, and pharmacist approval.
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
        """List non-divisible pack policies for authenticated tenant."""
        tenant = self._get_tenant(request)
        qs = NonDivisiblePackPolicy.objects.filter(tenant=tenant)

        product_id = request.query_params.get("product_id")
        if product_id:
            qs = qs.filter(product_id=product_id)

        container_type = request.query_params.get("container_type")
        if container_type:
            qs = qs.filter(container_type=container_type.upper())

        return Response([serialize_non_divisible_policy(p) for p in qs])

    def create(self, request) -> Response:
        """Create or update a non-divisible pack policy."""
        self._get_tenant(request)
        try:
            policy = NonDivisiblePackService.create_or_update_policy(
                user=request.user,
                data=request.data,
            )
            return Response(serialize_non_divisible_policy(policy), status=status.HTTP_201_CREATED)
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
        """Retrieve a policy by ID."""
        tenant = self._get_tenant(request)
        policy = NonDivisiblePackPolicy.objects.filter(policy_id=pk, tenant=tenant).first()
        if not policy:
            raise NotFound({"error_code": "ERR_POLICY_NOT_FOUND", "detail": f"Policy '{pk}' was not found."})
        return Response(serialize_non_divisible_policy(policy))

    def update(self, request, pk=None) -> Response:
        """Update a policy by ID."""
        self._get_tenant(request)
        try:
            policy = NonDivisiblePackService.create_or_update_policy(
                user=request.user,
                data=request.data,
                policy_id=pk,
            )
            return Response(serialize_non_divisible_policy(policy), status=status.HTTP_200_OK)
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

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_dispense(self, request) -> Response:
        """
        Validates dispense or checkout requests against container integrity rules.
        """
        tenant = self._get_tenant(request)
        product_id = request.data.get("product_id")
        if not product_id:
            return Response(
                {"error_code": "ERR_PRODUCT_REQUIRED", "detail": "product_id is required."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        branch_id = request.data.get("branch_id")
        try:
            raw_qty = Decimal(str(request.data.get("requested_quantity", "1.0")))
        except Exception:
            return Response(
                {"error_code": "ERR_INVALID_DECIMAL", "detail": "requested_quantity must be a valid number."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        is_split = bool(request.data.get("is_split_request", False))
        exemption = request.data.get("exemption_protocol")

        try:
            result = NonDivisiblePackService.validate_dispense_request(
                tenant_id=tenant.tenant_id,
                product_id=product_id,
                branch_id=branch_id,
                requested_quantity=raw_qty,
                is_split_request=is_split,
                exemption_protocol=exemption,
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

    @action(detail=False, methods=["post"], url_path="checkout")
    def checkout(self, request) -> Response:
        """
        Atomically checkout non-divisible packs or approved split units.
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

        try:
            qty = Decimal(str(request.data.get("requested_quantity", "1.0")))
        except Exception:
            return Response(
                {"error_code": "ERR_INVALID_DECIMAL", "detail": "requested_quantity must be a valid number."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        is_split = bool(request.data.get("is_split_request", False))
        exemption = request.data.get("exemption_protocol")
        batch_id = request.data.get("batch_id")
        customer_name = request.data.get("customer_name", "Walk-in Customer")
        customer_phone = request.data.get("customer_phone", "")
        payment_method = request.data.get("payment_method", PaymentMethod.CASH)

        try:
            result = NonDivisiblePackService.execute_checkout(
                tenant_id=tenant.tenant_id,
                branch_id=branch_id,
                product_id=product_id,
                requested_quantity=qty,
                is_split_request=is_split,
                exemption_protocol=exemption,
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
        Reverses a non-divisible pack sale invoice and restores inventory.
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
            result = NonDivisiblePackService.reverse_sale(
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
