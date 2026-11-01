"""
backend/apps/core/feature_flag_views.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0072
Server-Authoritative Platform Feature Flag Management & Tenant Evaluation ViewSets.
Provides:
1. PlatformFeatureFlagViewSet: Centralized Super Admin CRUD, rollout adjustments, and emergency kill switches.
2. TenantFeatureFlagEvaluationViewSet: Secure, tenant-scoped evaluation API for client POS/mobile apps.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.feature_flag_service import FeatureFlagService
from apps.core.models import PlatformFeatureFlag
from apps.core.pagination import SafeAdaptivePagination
from apps.core.serializers import (
    PlatformFeatureFlagSerializer,
)


class PlatformFeatureFlagViewSet(viewsets.ModelViewSet):
    """
    Super Admin Feature Flag Management Console & API Boundary.
    Strictly protected: Store personnel (owners, managers, cashiers) receive 403 Forbidden.
    """

    queryset = PlatformFeatureFlag.objects.all().prefetch_related("tenant_overrides")
    serializer_class = PlatformFeatureFlagSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = SafeAdaptivePagination

    def _require_platform_operator(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")

        is_platform = getattr(user, "is_superuser", False) or str(getattr(user, "role", "")).upper() in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]
        if not is_platform:
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_OPERATOR_REQUIRED",
                    "message_en": (
                        "Platform feature flags are centrally governed by platform administrators. "
                        "Pharmacy tenants cannot manage platform feature flags."
                    ),
                    "message_bn": "প্ল্যাটফর্ম ফিচার ফ্ল্যাগ শুধুমাত্র প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটর দ্বারা পরিচালিত হয়।",
                }
            )

    def get_queryset(self):
        self._require_platform_operator(self.request)
        return super().get_queryset()

    def perform_create(self, serializer):
        self._require_platform_operator(self.request)
        serializer.save(updated_by=self.request.user)

    def perform_update(self, serializer):
        self._require_platform_operator(self.request)
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        self._require_platform_operator(self.request)
        super().perform_destroy(instance)

    @action(detail=True, methods=["post"], url_path="kill-switch")
    def kill_switch(self, request, pk=None):
        """Emergency kill switch activation or deactivation."""
        self._require_platform_operator(request)
        data = request.data or {}
        is_active = data.get("is_kill_switch_active", True)
        reason = data.get("reason", "")

        try:
            result = FeatureFlagService.set_kill_switch(
                flag_key=pk,
                is_active=is_active,
                actor=request.user,
                reason=reason,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        msg_en = f"Kill switch {'ACTIVATED' if is_active else 'RESET'} for feature flag '{pk}'."
        msg_bn = f"ফিচার ফ্ল্যাগ '{pk}'-এর কিল সুইচ {'চালু' if is_active else 'রিসেট'} করা হয়েছে।"

        return Response(
            {
                "success": True,
                "code": "FEATURE_FLAG_KILL_SWITCH_UPDATED",
                "message_en": msg_en,
                "message_bn": msg_bn,
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="tenant-override")
    def tenant_override(self, request, pk=None):
        """Sets a targeted tenant override for this feature flag."""
        self._require_platform_operator(request)
        data = request.data or {}
        tenant_id = data.get("tenant_id")
        if not tenant_id:
            raise ValidationError({"tenant_id": "Target tenant_id is required."})

        is_enabled = data.get("is_enabled", True)
        notes = data.get("notes", "")

        try:
            result = FeatureFlagService.upsert_tenant_override(
                flag_key=pk,
                tenant_id=tenant_id,
                is_enabled=is_enabled,
                actor=request.user,
                notes=notes,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "FEATURE_FLAG_OVERRIDE_SAVED",
                "message_en": f"Override for tenant '{tenant_id}' on flag '{pk}' saved successfully.",
                "message_bn": f"টেন্যান্ট '{tenant_id}'-এর জন্য ফিচার ফ্ল্যাগ ওভাররাইড সংরক্ষিত হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["delete"], url_path=r"tenant-override/(?P<tenant_id>[^/.]+)")
    def remove_override(self, request, pk=None, tenant_id=None):
        """Removes a targeted tenant override."""
        self._require_platform_operator(request)
        removed = FeatureFlagService.remove_tenant_override(
            flag_key=pk,
            tenant_id=tenant_id,
            actor=request.user,
        )
        if not removed:
            raise ValidationError({"tenant_id": f"No override found for tenant '{tenant_id}' on flag '{pk}'."})

        return Response(
            {
                "success": True,
                "code": "FEATURE_FLAG_OVERRIDE_REMOVED",
                "message_en": f"Override for tenant '{tenant_id}' on flag '{pk}' removed.",
                "message_bn": f"টেন্যান্ট '{tenant_id}'-এর ওভাররাইড অপসারিত হয়েছে।",
                "data": {"flag_key": pk, "tenant_id": tenant_id},
            },
            status=status.HTTP_200_OK,
        )


class TenantFeatureFlagEvaluationViewSet(viewsets.ViewSet):
    """
    Tenant-Scoped Server-Authoritative Feature Flag Evaluation API.
    Used by JavaFX Desktop POS and Flutter Mobile apps to query active capabilities.
    Enforces strict tenant isolation.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _get_target_tenant_id(self, request) -> str:
        user = getattr(request, "user", None)
        user_tenant = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        is_platform = getattr(user, "is_superuser", False) or str(getattr(user, "role", "")).upper() in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]

        query_tenant = request.query_params.get("tenant_id")
        if is_platform:
            return str(query_tenant or user_tenant or "default-tenant").strip()

        if not user_tenant:
            raise PermissionDenied("User is not associated with an authorized pharmacy tenant.")

        if query_tenant and str(query_tenant).strip() != str(user_tenant).strip():
            raise CrossTenantViolationError(
                message_en=(
                    f"Cross-tenant access violation: Cannot evaluate feature flags for foreign tenant '{query_tenant}'."
                ),
                message_bn="ভিন্ন ফার্মেসির ফিচার ফ্ল্যাগ অ্যাক্সেস করা সম্পূর্ণ নিষিদ্ধ।",
            )
        return str(user_tenant).strip()

    @action(detail=False, methods=["get"], url_path="eval")
    def evaluate_all(self, request):
        """Evaluates all registered feature flags for the caller's tenant."""
        target_tenant = self._get_target_tenant_id(request)
        result = FeatureFlagService.evaluate_all_flags_for_tenant(tenant_id=target_tenant)
        return Response(
            {
                "success": True,
                "code": "FEATURE_FLAGS_EVALUATED",
                "message_en": f"Evaluated {len(result['flags'])} feature flags for tenant '{target_tenant}'.",
                "message_bn": f"টেন্যান্ট '{target_tenant}'-এর জন্য ফিচার ফ্ল্যাগ যাচাই সম্পন্ন হয়েছে।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path=r"eval/(?P<flag_key>[^/.]+)")
    def evaluate_single(self, request, flag_key=None):
        """Evaluates a single feature flag for the caller's tenant."""
        target_tenant = self._get_target_tenant_id(request)
        result = FeatureFlagService.evaluate_flag(flag_key=flag_key, tenant_id=target_tenant)
        return Response(
            {
                "success": True,
                "code": "FEATURE_FLAG_EVALUATED",
                "message_en": f"Flag '{flag_key}' evaluated to {result['is_enabled']}.",
                "message_bn": f"ফ্ল্যাগ '{flag_key}' স্ট্যাটাস: {result['is_enabled']}।",
                "data": result,
            },
            status=status.HTTP_200_OK,
        )
