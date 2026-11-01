"""
backend/apps/core/prorated_upgrade_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0081, ADR-0083, ADR-0087 & ADR-0090
Phase 05: Basic/Standard/Pro Subscription & Billing (P090)

DRF ViewSet for Prorated Subscription Upgrades:
- Calculates quotes with daily rates, unused credits, and net fees.
- Applies instant upgrade with invoice settlement and immediate entitlement unlocking.
- Enforces multi-tenant boundaries, RBAC (Owner/Admin only), and audit traceability.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.prorated_upgrade_service import ProratedUpgradeService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.upgrades")


class ProratedUpgradeViewSet(viewsets.ViewSet):
    """
    Manages prorated subscription upgrades, credit policies, and immediate entitlement updates.
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

    def _verify_upgrade_rbac(self, user: Any) -> None:
        if not self._is_owner_or_admin(user):
            raise PermissionError("Only pharmacy owners or platform administrators can modify subscription plans.")

    @action(detail=False, methods=["post"], url_path="quote")
    def quote(self, request: Any) -> Response:
        """
        POST /api/v2/billing/upgrade/quote/
        Calculates quote and proration breakdown without committing state changes.
        """
        try:
            self._verify_upgrade_rbac(request.user)
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            target_plan_code = request.data.get("target_plan_code")
            if not target_plan_code:
                return Response(
                    {"error": "VALIDATION_ERROR", "detail": "target_plan_code is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            billing_cycle = request.data.get("billing_cycle")
            quote_data = ProratedUpgradeService.quote_upgrade(
                tenant_id=tenant_id,
                target_plan_code=target_plan_code,
                billing_cycle=billing_cycle,
                actor_user=request.user,
            )
            return Response(quote_data, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            logger.warning("Cross-tenant upgrade quote attempted: %s", err)
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
            logger.exception("Unexpected error in upgrade quote: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="apply")
    def apply(self, request: Any) -> Response:
        """
        POST /api/v2/billing/upgrade/apply/
        Executes upgrade, records adjustment invoice, and updates entitlements immediately.
        """
        try:
            self._verify_upgrade_rbac(request.user)
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            upgrade_id = request.data.get("upgrade_id")
            target_plan_code = request.data.get("target_plan_code")
            billing_cycle = request.data.get("billing_cycle")
            payment_method = request.data.get("payment_method", "BKASH")
            payment_reference = request.data.get("payment_reference", "")

            result = ProratedUpgradeService.apply_upgrade(
                tenant_id=tenant_id,
                upgrade_id=upgrade_id,
                target_plan_code=target_plan_code,
                billing_cycle=billing_cycle,
                payment_method=payment_method,
                payment_reference=payment_reference,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            logger.warning("Cross-tenant upgrade execution attempted: %s", err)
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
            logger.exception("Unexpected error in upgrade execution: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def list(self, request: Any) -> Response:
        """
        GET /api/v2/billing/upgrade/
        Lists upgrade history for the calling tenant.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            upgrades = ProratedUpgradeService.list_upgrades(tenant_id=tenant_id)
            return Response({"results": upgrades, "count": len(upgrades)}, status=status.HTTP_200_OK)
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
        GET /api/v2/billing/upgrade/<pk>/
        Retrieves specific upgrade record.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            record = ProratedUpgradeService.get_upgrade(tenant_id=tenant_id, upgrade_id=pk)
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

