"""
backend/apps/core/payment_method_views.py
Architecture Decision: ADR-0275 (P275).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet governing payment method registry and configurations.
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.payment_method_registry_service import PaymentMethodRegistryService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    return ""


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Payment configuration access blocked.",
                "message_en": "Tenant account subscription is suspended. Payment configuration access blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পেমেন্ট কনফিগারেশন বন্ধ করা হয়েছে।",
            }
        )


class PaymentMethodViewSet(viewsets.ViewSet):
    """
    ViewSet for tenant and branch-level payment method configurations.
    Endpoints:
    - GET   /api/v2/finance/payment-methods/
    - POST  /api/v2/finance/payment-methods/
    - GET   /api/v2/finance/payment-methods/{id}/
    - PATCH /api/v2/finance/payment-methods/{id}/
    - POST  /api/v2/finance/payment-methods/{id}/toggle/
    - POST  /api/v2/finance/payment-methods/{id}/validate-tender/
    - DELETE /api/v2/finance/payment-methods/{id}/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        only_enabled = str(request.query_params.get("only_enabled", "false")).lower() in ("true", "1")

        try:
            methods = PaymentMethodRegistryService.list_payment_methods(
                tenant_id=tenant_id,
                actor_user=request.user,
                branch_id=branch_id,
                only_enabled=only_enabled,
            )
            return Response({"payment_methods": methods, "count": len(methods)}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            created = PaymentMethodRegistryService.configure_payment_method(
                tenant_id=tenant_id,
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            cfg = PaymentMethodRegistryService.get_payment_method(
                tenant_id=tenant_id,
                actor_user=request.user,
                config_id=pk,
            )
            return Response(cfg, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            updated = PaymentMethodRegistryService.update_payment_method(
                tenant_id=tenant_id,
                actor_user=request.user,
                config_id=pk,
                data=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="toggle")
    def toggle(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        is_enabled = bool(request.data.get("is_enabled", True))

        try:
            updated = PaymentMethodRegistryService.toggle_payment_method(
                tenant_id=tenant_id,
                actor_user=request.user,
                config_id=pk,
                is_enabled=is_enabled,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="validate-tender")
    def validate_tender(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        raw_amt = request.data.get("amount_bdt", "0.00")
        reference = str(request.data.get("reference", "")).strip()
        is_offline = bool(request.data.get("is_offline", False))

        try:
            amt = Decimal(str(raw_amt))
        except Exception:
            return Response(
                {"error_code": "ERR_INVALID_AMOUNT", "detail": "Invalid amount format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            res = PaymentMethodRegistryService.validate_tender(
                tenant_id=tenant_id,
                actor_user=request.user,
                config_id=pk,
                amount_bdt=amt,
                reference=reference,
                is_offline=is_offline,
            )
            return Response(res, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            disabled = PaymentMethodRegistryService.toggle_payment_method(
                tenant_id=tenant_id,
                actor_user=request.user,
                config_id=pk,
                is_enabled=False,
            )
            return Response(disabled, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
