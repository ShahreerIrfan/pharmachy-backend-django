"""
backend/apps/core/financial_account_views.py
Architecture Decision: ADR-0274 (P274).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet governing cash registers, bank accounts, and payment clearing accounts.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.financial_account_registry_service import FinancialAccountRegistryService
from apps.core.models import TenantLifecycleStatus

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
                "detail": "Tenant account subscription is suspended. Financial operations blocked.",
                "message_en": "Tenant account subscription is suspended. Financial operations blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। আর্থিক লেনদেন ও অ্যাকাউন্ট কনফিগারেশন ব্লক করা হয়েছে।",
            }
        )


class FinancialAccountViewSet(viewsets.ViewSet):
    """
    ViewSet for branch cash registers, bank accounts, and payment clearing accounts.
    Endpoints:
    - GET    /api/v2/finance/accounts/
    - POST   /api/v2/finance/accounts/
    - GET    /api/v2/finance/accounts/{id}/
    - PATCH  /api/v2/finance/accounts/{id}/
    - POST   /api/v2/finance/accounts/{id}/freeze/
    - POST   /api/v2/finance/accounts/{id}/unfreeze/
    - POST   /api/v2/finance/accounts/{id}/close/
    - DELETE /api/v2/finance/accounts/{id}/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id")
        account_type = request.query_params.get("account_type")
        status_filter = request.query_params.get("status")

        try:
            accounts = FinancialAccountRegistryService.list_accounts(
                tenant_id=tenant_id,
                actor_user=request.user,
                branch_id=branch_id,
                account_type=account_type,
                status=status_filter,
            )
            return Response({"accounts": accounts, "count": len(accounts)}, status=status.HTTP_200_OK)
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
            created = FinancialAccountRegistryService.register_account(
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
            account = FinancialAccountRegistryService.get_account(
                tenant_id=tenant_id,
                actor_user=request.user,
                account_id=pk,
            )
            return Response(account, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        try:
            updated = FinancialAccountRegistryService.update_account(
                tenant_id=tenant_id,
                actor_user=request.user,
                account_id=pk,
                data=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="freeze")
    def freeze(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "Administrative freeze pending audit")

        try:
            frozen = FinancialAccountRegistryService.freeze_account(
                tenant_id=tenant_id,
                actor_user=request.user,
                account_id=pk,
                reason=reason,
            )
            return Response(frozen, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="unfreeze")
    def unfreeze(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "Audit cleared, unfreezing account")

        try:
            unfrozen = FinancialAccountRegistryService.unfreeze_account(
                tenant_id=tenant_id,
                actor_user=request.user,
                account_id=pk,
                reason=reason,
            )
            return Response(unfrozen, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, pk=None) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "Account decommissioned")

        try:
            closed = FinancialAccountRegistryService.close_account(
                tenant_id=tenant_id,
                actor_user=request.user,
                account_id=pk,
                reason=reason,
            )
            return Response(closed, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, pk=None) -> Response:
        return self.close(request, pk=pk)
