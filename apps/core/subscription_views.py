"""
backend/apps/core/subscription_views.py

Architecture Decision: ADR-0013, ADR-0015, ADR-0033 & ADR-0084
Phase 05: Basic/Standard/Pro Subscription & Billing (P084)

Subscription State Machine ViewSet:
- Guarded subscription lifecycle transitions.
- Verified payment settlement and automatic activation.
- Audited transition history and allowed transition discovery.
- Cross-tenant isolation and strict role verification.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.serializers import (
    SubscriptionPaymentVerificationSerializer,
    SubscriptionTransitionRequestSerializer,
)
from apps.core.subscription_state_machine_service import SubscriptionStateMachineService

logger = logging.getLogger("pharmacy.billing.subscription_views")


def _is_platform_superadmin(user: Any) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    role = str(getattr(user, "role", "")).upper()
    return role in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_SUPER_ADMIN", "PLATFORM_OPERATOR"]


class SubscriptionViewSet(viewsets.ViewSet):
    """
    Guarded Subscription State Machine ViewSet (ADR-0084).
    Route: /api/v2/billing/subscriptions/{tenant_id}/
    """

    permission_classes = [permissions.IsAuthenticated]

    def _verify_tenant_access(self, request, tenant_id: str, require_owner: bool = False):
        user = request.user
        if _is_platform_superadmin(user):
            return

        user_tenant_id = getattr(user, "tenant_id", None)
        if hasattr(user, "tenant") and user.tenant:
            user_tenant_id = getattr(user.tenant, "tenant_id", user_tenant_id)

        if not user_tenant_id or str(user_tenant_id) != str(tenant_id):
            raise CrossTenantViolationError(
                message_en=f"Tenant '{user_tenant_id}' cannot access subscription records for tenant '{tenant_id}'.",
                message_bn="অন্য ফার্মেসির সাবস্ক্রিপশন স্টেট দেখার বা পরিবর্তন করার অনুমতি নেই।",
            )

        if require_owner:
            user_role = str(getattr(user, "role", "")).upper()
            if user_role not in ["TENANT_OWNER", "OWNER", "PHARMACY_OWNER"]:
                raise PermissionDenied(
                    detail={
                        "error_code": "ROLE_PERMISSION_DENIED",
                        "message_en": "Only Tenant Owner or Platform Super Admin can manage subscription transitions.",
                        "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা সুপার অ্যাডমিন সাবস্ক্রিপশন স্টেট পরিবর্তন করতে পারেন।",
                    }
                )

    def retrieve(self, request, pk=None):
        """Retrieves subscription state, quotas, delinquency status, and allowed next transitions."""
        tenant_id = str(pk or "").strip()
        if not tenant_id:
            raise ValidationError(detail={"tenant_id": ["Tenant ID is required."]})

        self._verify_tenant_access(request, tenant_id, require_owner=False)

        try:
            details = SubscriptionStateMachineService.get_subscription_details(tenant_id)
        except ValueError as err:
            raise NotFound(detail=str(err))

        return Response(
            {
                "success": True,
                "data": details,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request, pk=None):
        """
        Executes a guarded subscription state transition under ADR-0084.
        """
        tenant_id = str(pk or "").strip()
        if not tenant_id:
            raise ValidationError(detail={"tenant_id": ["Tenant ID is required."]})

        self._verify_tenant_access(request, tenant_id, require_owner=True)

        serializer = SubscriptionTransitionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator_id = str(getattr(request.user, "user_id", None) or getattr(request.user, "id", "SYSTEM"))
        operator_role = str(getattr(request.user, "role", "TENANT_OWNER")).upper()
        if _is_platform_superadmin(request.user):
            operator_role = "PLATFORM_SUPER_ADMIN"

        try:
            result = SubscriptionStateMachineService.transition_subscription(
                tenant_id=tenant_id,
                target_status=data["target_status"],
                operator_id=operator_id,
                operator_role=operator_role,
                reason=data.get("reason", ""),
                metadata=data.get("metadata", {}),
            )
        except (ValueError, DjangoValidationError) as err:
            raise ValidationError(detail={"error": [str(err)]})
        except PermissionError as perm_err:
            raise PermissionDenied(
                detail={
                    "error_code": "ROLE_PERMISSION_DENIED",
                    "message_en": str(perm_err),
                    "message_bn": "এই সাবস্ক্রিপশন ট্রানজিশন সম্পাদনের অনুমতি আপনার নেই।",
                }
            )

        return Response(
            {
                "success": True,
                "code": "SUBSCRIPTION_TRANSITION_SUCCESS",
                "message_en": f"Subscription transitioned to '{data['target_status']}' successfully.",
                "message_bn": f"সাবস্ক্রিপশন স্টেট সফলভাবে '{data['target_status']}'-এ পরিবর্তিত হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="verify-payment")
    def verify_payment(self, request, pk=None):
        """
        Verifies subscription payment settlement and reactivates/extends subscription.
        """
        tenant_id = str(pk or "").strip()
        if not tenant_id:
            raise ValidationError(detail={"tenant_id": ["Tenant ID is required."]})

        self._verify_tenant_access(request, tenant_id, require_owner=True)

        serializer = SubscriptionPaymentVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        operator_id = str(getattr(request.user, "user_id", None) or getattr(request.user, "id", "SYSTEM"))
        operator_role = str(getattr(request.user, "role", "TENANT_OWNER")).upper()
        if _is_platform_superadmin(request.user):
            operator_role = "PLATFORM_SUPER_ADMIN"

        try:
            result = SubscriptionStateMachineService.verify_and_record_payment(
                tenant_id=tenant_id,
                amount_bdt=Decimal(str(data["amount_bdt"])),
                transaction_ref=data["transaction_ref"],
                payment_method=data.get("payment_method", "BKASH"),
                operator_id=operator_id,
                operator_role=operator_role,
                notes=data.get("notes", ""),
            )
        except (ValueError, DjangoValidationError) as err:
            raise ValidationError(detail={"error": [str(err)]})

        return Response(
            {
                "success": True,
                "code": "SUBSCRIPTION_PAYMENT_VERIFIED",
                "message_en": "Subscription payment verified and subscription activated successfully.",
                "message_bn": "সাবস্ক্রিপশন পেমেন্ট সফলভাবে যাচাইকৃত ও সক্রিয় করা হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        """Retrieves audited timeline of subscription state transitions for the tenant."""
        tenant_id = str(pk or "").strip()
        if not tenant_id:
            raise ValidationError(detail={"tenant_id": ["Tenant ID is required."]})

        self._verify_tenant_access(request, tenant_id, require_owner=False)

        history = SubscriptionStateMachineService.get_subscription_history(tenant_id)
        return Response(
            {
                "success": True,
                "tenant_id": tenant_id,
                "count": len(history),
                "data": history,
            },
            status=status.HTTP_200_OK,
        )
