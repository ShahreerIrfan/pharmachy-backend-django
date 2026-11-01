"""
backend/apps/core/subscription_portal_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0082, ADR-0095, ADR-0096 & ADR-0097
Phase 05: Basic/Standard/Pro Subscription & Billing (P097)
Requirement Trace: FR-070, FR-071, FR-072 (Subscription Customer Portal & Self-Service Billing)

DRF ViewSet for Subscription Customer Portal:
- Provides consolidated overview of active package, renewal schedule, invoices, payment history, and quota counters.
- Supports self-service auto-renew management.
- Strictly enforces tenant privacy boundaries and RBAC permissions.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.subscription_portal_service import SubscriptionCustomerPortalService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.portal")


class SubscriptionCustomerPortalViewSet(viewsets.ViewSet):
    """
    Customer subscription portal viewset for pharmacy owners and managers.
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
                    f"User from tenant '{user_tid}' cannot access portal of tenant '{clean_target}'."
                )
            return clean_target

        if not user_tid:
            raise ValidationError("Tenant context could not be resolved from authenticated user.")
        return user_tid

    def list(self, request: Any) -> Response:
        """
        GET /api/v2/billing/portal/
        Returns comprehensive subscription portal dashboard.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can access the subscription billing portal.")

            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            result = SubscriptionCustomerPortalService.get_portal_overview(tenant_id)
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
            logger.exception("Error loading subscription customer portal: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="toggle-auto-renew")
    def toggle_auto_renew(self, request: Any) -> Response:
        """
        POST /api/v2/billing/portal/toggle-auto-renew/
        Toggles auto-renew state for subscription.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can update subscription renewal settings.")

            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            auto_renew = request.data.get("auto_renew", True)

            result = SubscriptionCustomerPortalService.toggle_auto_renew(
                tenant_id=tenant_id,
                auto_renew=bool(auto_renew),
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
            logger.exception("Error toggling auto-renew: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

