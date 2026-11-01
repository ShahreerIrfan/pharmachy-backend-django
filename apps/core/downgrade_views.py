"""
backend/apps/core/downgrade_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0083 & ADR-0091
Phase 05: Basic/Standard/Pro Subscription & Billing (P091)

DRF ViewSet for Subscription Downgrades & Over-Quota Handling:
- Assesses downgrade feasibility, identifies locked excess branches, and confirms data retention.
- Executes safe downgrade locking excess branches without deleting historical transactions.
- Enforces multi-tenant boundaries and RBAC (Pharmacy Owner or Platform Admin only).
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.downgrade_service import SubscriptionDowngradeService
from apps.core.errors import CrossTenantViolationError
from apps.core.quota_guard_service import (
    SubscriptionQuotaGuardService,
)
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.downgrades")


class SubscriptionDowngradeViewSet(viewsets.ViewSet):
    """
    Manages subscription downgrades, over-quota evaluations, and mutation gating.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_admin(self, user: Any) -> bool:
        if not user or not user.is_authenticated:
            return False
        return (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR]
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "PLATFORM_OPERATOR", "SUPER_ADMIN"]
        )

    def _is_owner_or_admin(self, user: Any) -> bool:
        if self._is_platform_admin(user):
            return True
        role = str(getattr(user, "role", "")).upper()
        return role in ["PHARMACY_OWNER", "OWNER", "ADMIN"]

    def _resolve_tenant_id(self, request: Any, target_tenant_id: str | None = None) -> str:
        user = request.user
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        if target_tenant_id:
            clean_target = str(target_tenant_id).strip()
            if not self._is_platform_admin(user) and user_tid and user_tid != clean_target:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access tenant '{clean_target}'."
                )
            return clean_target

        if not user_tid:
            raise ValidationError("Tenant context could not be resolved from authenticated user.")
        return user_tid

    def _verify_billing_rbac(self, user: Any) -> None:
        if not self._is_owner_or_admin(user):
            raise PermissionError("Only pharmacy owners or platform administrators can modify subscription plans.")

    @action(detail=False, methods=["post"], url_path="assess")
    def assess(self, request: Any) -> Response:
        """
        POST /api/v2/billing/downgrade/assess/
        Evaluates downgrade feasibility and identifies branches that would be locked.
        """
        try:
            self._verify_billing_rbac(request.user)
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            target_plan_code = request.data.get("target_plan_code")
            if not target_plan_code:
                return Response(
                    {"error": "VALIDATION_ERROR", "detail": "target_plan_code is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            billing_cycle = request.data.get("billing_cycle")
            report = SubscriptionDowngradeService.assess_downgrade(
                tenant_id=tenant_id,
                target_plan_code=target_plan_code,
                billing_cycle=billing_cycle,
            )
            return Response(report, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            logger.warning("Cross-tenant downgrade assessment attempted: %s", err)
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Unexpected error in downgrade assessment: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="apply")
    def apply(self, request: Any) -> Response:
        """
        POST /api/v2/billing/downgrade/apply/
        Executes downgrade, locks excess branches without data deletion, updates entitlements.
        """
        try:
            self._verify_billing_rbac(request.user)
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            target_plan_code = request.data.get("target_plan_code")
            if not target_plan_code:
                return Response(
                    {"error": "VALIDATION_ERROR", "detail": "target_plan_code is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            billing_cycle = request.data.get("billing_cycle", "MONTHLY")
            effective_mode = request.data.get("effective_mode", "IMMEDIATE")
            retained_branch_ids = request.data.get("retained_branch_ids")

            result = SubscriptionDowngradeService.apply_downgrade(
                tenant_id=tenant_id,
                target_plan_code=target_plan_code,
                billing_cycle=billing_cycle,
                effective_mode=effective_mode,
                retained_branch_ids=retained_branch_ids,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            logger.warning("Cross-tenant downgrade execution attempted: %s", err)
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Unexpected error in downgrade execution: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["get"], url_path="quota-status")
    def quota_status(self, request: Any) -> Response:
        """
        GET /api/v2/billing/downgrade/quota-status/
        Returns current active branch/staff/device usage vs quota limits.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            quotas = SubscriptionQuotaGuardService.assess_account_quotas(tenant_id=tenant_id)
            return Response(quotas, status=status.HTTP_200_OK)
        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception as err:
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def list(self, request: Any) -> Response:
        """
        GET /api/v2/billing/downgrade/
        Lists downgrade history for the calling tenant.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            downgrades = SubscriptionDowngradeService.list_downgrades(tenant_id=tenant_id)
            return Response({"results": downgrades, "count": len(downgrades)}, status=status.HTTP_200_OK)
        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception as err:
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """
        GET /api/v2/billing/downgrade/<pk>/
        Retrieves a single downgrade audit record.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            record = SubscriptionDowngradeService.get_downgrade(tenant_id=tenant_id, downgrade_id=pk)
            return Response(record, status=status.HTTP_200_OK)
        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            return Response(
                {"error": "NOT_FOUND", "detail": str(err)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as err:
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
