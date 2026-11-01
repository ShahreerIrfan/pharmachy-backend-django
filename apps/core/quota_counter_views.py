"""
backend/apps/core/quota_counter_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0082, ADR-0091 & ADR-0095
Phase 05: Basic/Standard/Pro Subscription & Billing (P095)
Requirement Trace: FR-070 / FR-071 / FR-072 (SaaS Plans, Billing & Tenant Lifecycle)

DRF ViewSet for Authoritative Plan Quota Usage Counters & Concurrency Protection:
- Exposes real-time authoritative usage reports for branches, staff seats, and devices.
- Provides atomic reserve-and-consume endpoints preventing concurrency race conditions.
- Provides owner/admin recalibration endpoints ensuring zero drift against database records.
- Strictly enforces multi-tenant boundaries and RBAC permissions.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.quota_counter_service import SubscriptionQuotaCounterService
from apps.core.quota_guard_service import QuotaExceededError
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.quota_counter")


class TenantQuotaUsageViewSet(viewsets.ViewSet):
    """
    Manages authoritative quota usage counters and atomic concurrency reservations.
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
        return role in ["PHARMACY_OWNER", "OWNER", "ADMIN", "PHARMACY_MANAGER"]

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

    def list(self, request: Any) -> Response:
        """
        GET /api/v2/billing/quota-usage/
        Returns real-time authoritative usage report for tenant.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            report = SubscriptionQuotaCounterService.get_quota_usage_report(tenant_id)
            return Response(report, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error retrieving quota usage report: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="check-and-reserve")
    def check_and_reserve(self, request: Any) -> Response:
        """
        POST /api/v2/billing/quota-usage/check-and-reserve/
        Atomically checks and reserves quota with row-level locking.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can allocate plan quota.")

            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            resource_type = request.data.get("resource_type", "")
            amount = request.data.get("amount", 1)

            result = SubscriptionQuotaCounterService.reserve_and_consume(
                tenant_id=tenant_id,
                resource_type=resource_type,
                amount=amount,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except QuotaExceededError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "ERR_QUOTA_EXCEEDED", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error checking and reserving quota: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="recalibrate")
    def recalibrate(self, request: Any) -> Response:
        """
        POST /api/v2/billing/quota-usage/recalibrate/
        Authoritatively scans database records and resets counters inside row lock.
        Requires Owner/Admin privilege.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or administrators can recalibrate quota counters.")

            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            result = SubscriptionQuotaCounterService.recalibrate_counters(
                tenant_id=tenant_id,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
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
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error recalibrating quota counters: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
