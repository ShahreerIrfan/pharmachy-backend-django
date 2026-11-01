"""
backend/apps/core/cancellation_grace_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0073, ADR-0084 & ADR-0092
Phase 05: Basic/Standard/Pro Subscription & Billing (P092)

DRF ViewSet for Subscription Cancellation, Grace Period, and Reactivation:
- Manages cancellation (end-of-period or immediate) with clear owner notifications.
- Manages 15-day clinical safety grace period and safe read/export access.
- Restores active status instantly upon payment confirmation.
- Strictly enforces multi-tenant boundaries and RBAC (Pharmacy Owner or Platform Admin only).
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.cancellation_grace_service import SubscriptionCancellationGraceService
from apps.core.errors import CrossTenantViolationError
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.lifecycle")


class SubscriptionLifecycleViewSet(viewsets.ViewSet):
    """
    Manages subscription lifecycle endpoints: cancellation, grace period, and reactivation.
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

    @action(detail=False, methods=["get"], url_path="status")
    def lifecycle_status(self, request: Any) -> Response:
        """
        GET /api/v2/billing/lifecycle/status/
        Returns active lifecycle status, grace period timeline, and owner messaging.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            data = SubscriptionCancellationGraceService.enter_or_evaluate_grace_period(
                tenant_id=tenant_id,
                actor_user=request.user,
            )
            return Response(data, status=status.HTTP_200_OK)
        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception as err:
            logger.exception("Error retrieving subscription lifecycle status: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="cancel")
    def cancel(self, request: Any) -> Response:
        """
        POST /api/v2/billing/lifecycle/cancel/
        Cancels subscription with required reason.
        """
        try:
            self._verify_billing_rbac(request.user)
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            reason = request.data.get("reason", "")
            effective_mode = request.data.get("effective_mode", "END_OF_BILLING_PERIOD")

            result = SubscriptionCancellationGraceService.cancel_subscription(
                tenant_id=tenant_id,
                reason=reason,
                effective_mode=effective_mode,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            logger.warning("Cross-tenant cancellation attempt blocked: %s", err)
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
            logger.exception("Unexpected error in cancellation: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="reactivate")
    def reactivate(self, request: Any) -> Response:
        """
        POST /api/v2/billing/lifecycle/reactivate/
        Reactivates cancelled or expired subscription upon confirmed payment.
        """
        try:
            self._verify_billing_rbac(request.user)
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            payment_method = request.data.get("payment_method", "BKASH")
            payment_reference = request.data.get("payment_reference", "")

            result = SubscriptionCancellationGraceService.reactivate_subscription(
                tenant_id=tenant_id,
                payment_method=payment_method,
                payment_reference=payment_reference,
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
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Unexpected error in reactivation: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
