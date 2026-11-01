"""
backend/apps/core/subscription_support_views.py

Architecture Decision: ADR-0100
Phase 05: Basic/Standard/Pro Subscription & Billing (P100)
Requirement Trace: FR-044, FR-066, FR-070

DRF ViewSet for Subscription Support Overrides and Operational Exceptions:
- Governs granting, listing, retrieving, and revoking time-bound operational exceptions.
- Superadmin and Platform Operator authorization for mutations (grant/revoke).
- Pharmacy Owner read access for their own tenant overrides.
- Cashier access denied (ERR_PERMISSION_DENIED).
- Cross-tenant isolation enforced (ERR_CROSS_TENANT_VIOLATION).
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import SubscriptionSupportOverride
from apps.core.subscription_support_service import SubscriptionSupportOverrideService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.support_views")


class SubscriptionSupportOverrideViewSet(viewsets.ViewSet):
    """
    API ViewSet for managing subscription support overrides.
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

    def _require_platform_admin(self, user: Any) -> None:
        if not self._is_platform_admin(user):
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Only platform superadmins and operators can grant or revoke support overrides.",
                    "message_bn": "শুধুমাত্র প্ল্যাটফর্ম সুপারঅ্যাডমিন এবং অপারেটরদের সাপোর্ট ওভাররাইড অনুমোদন বা বাতিলের অনুমতি আছে।",
                }
            )

    def _require_owner_or_admin(self, user: Any) -> None:
        if self._is_platform_admin(user):
            return
        role = str(getattr(user, "role", "")).upper()
        if role not in ["PHARMACY_OWNER", "OWNER", "ADMIN", "PHARMACY_MANAGER"]:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Only pharmacy owners and administrators can view support overrides.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও অ্যাডমিনিস্ট্রেটরদের সাপোর্ট ওভাররাইড দেখার অনুমতি আছে।",
                }
            )

    def _resolve_tenant_id(self, request: Any, target_tenant_id: str | None = None) -> str | None:
        user = request.user
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        if target_tenant_id:
            clean_target = str(target_tenant_id).strip()
            if not self._is_platform_admin(user) and user_tid and user_tid != clean_target:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access overrides for tenant '{clean_target}'."
                )
            return clean_target

        if user_tid:
            return user_tid

        if self._is_platform_admin(user):
            return None

        header_tid = str(request.headers.get("X-Tenant-ID", "")).strip()
        if header_tid:
            return header_tid

        raise ValidationError({"tenant_id": "Target tenant_id could not be resolved from authentication context."})

    def list(self, request: Any) -> Response:
        """Lists support overrides filtered by tenant, status, or type."""
        self._require_owner_or_admin(request.user)
        target_tid = request.query_params.get("tenant_id")
        resolved_tid = self._resolve_tenant_id(request, target_tid)

        status_filter = request.query_params.get("status")
        override_type = request.query_params.get("override_type")

        overrides = SubscriptionSupportOverrideService.list_overrides(
            tenant_id=resolved_tid,
            status_filter=status_filter,
            override_type=override_type,
        )

        serialized = [SubscriptionSupportOverrideService.serialize_override(ovr) for ovr in overrides]
        return Response(
            {
                "success": True,
                "code": "SUPPORT_OVERRIDES_RETRIEVED",
                "data": {
                    "total_count": len(serialized),
                    "tenant_id": resolved_tid,
                    "overrides": serialized,
                },
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """Retrieves a single support override by ID."""
        self._require_owner_or_admin(request.user)
        try:
            override = SubscriptionSupportOverride.objects.select_related("tenant", "subscription").get(override_id=pk)
        except SubscriptionSupportOverride.DoesNotExist:
            return Response(
                {"success": False, "code": "OVERRIDE_NOT_FOUND", "message": f"Support override '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not self._is_platform_admin(request.user):
            user_tid = str(getattr(request.user, "tenant_id", "")).strip()
            if user_tid and user_tid != override.tenant_id:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access override '{pk}' for tenant '{override.tenant_id}'."
                )

        data = SubscriptionSupportOverrideService.serialize_override(override)
        return Response(
            {
                "success": True,
                "code": "SUPPORT_OVERRIDE_RETRIEVED",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    def create(self, request: Any) -> Response:
        """Grants a new support override (superadmin only)."""
        self._require_platform_admin(request.user)

        data = request.data or {}
        tenant_id = data.get("tenant_id")
        override_type = data.get("override_type")
        justification = data.get("justification")
        ticket_reference = data.get("ticket_reference")
        extension_days = data.get("extension_days", 7)
        quota_bonus = data.get("quota_bonus", 0)
        subscription_id = data.get("subscription_id")
        metadata = data.get("metadata", {})

        if not tenant_id:
            raise ValidationError({"tenant_id": "tenant_id is required."})
        if not override_type:
            raise ValidationError({"override_type": "override_type is required."})
        if not justification:
            raise ValidationError({"justification": "justification is required."})
        if not ticket_reference:
            raise ValidationError({"ticket_reference": "ticket_reference is required."})

        try:
            override = SubscriptionSupportOverrideService.grant_override(
                tenant_id=tenant_id,
                override_type=override_type,
                justification=justification,
                ticket_reference=ticket_reference,
                extension_days=extension_days,
                quota_bonus=quota_bonus,
                subscription_id=subscription_id,
                granted_by_user=request.user,
                metadata=metadata,
            )
        except DjangoValidationError as err:
            return Response(
                {"success": False, "code": "VALIDATION_ERROR", "message": str(err)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "success": True,
                "code": "SUPPORT_OVERRIDE_GRANTED",
                "data": SubscriptionSupportOverrideService.serialize_override(override),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request: Any, pk: str = None) -> Response:
        """Revokes an active support override (superadmin only)."""
        self._require_platform_admin(request.user)

        data = request.data or {}
        revocation_reason = data.get("revocation_reason")
        if not revocation_reason:
            raise ValidationError({"revocation_reason": "revocation_reason is required."})

        try:
            override = SubscriptionSupportOverrideService.revoke_override(
                override_id=pk,
                revocation_reason=revocation_reason,
                actor_user=request.user,
            )
        except DjangoValidationError as err:
            return Response(
                {"success": False, "code": "VALIDATION_ERROR", "message": str(err)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "success": True,
                "code": "SUPPORT_OVERRIDE_REVOKED",
                "data": SubscriptionSupportOverrideService.serialize_override(override),
            },
            status=status.HTTP_200_OK,
        )
