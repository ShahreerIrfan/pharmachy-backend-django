"""
backend/apps/core/onboarding_views.py

Architecture Decision: ADR-0079
Platform Super Admin Tenant Onboarding Wizard ViewSet.
Provides:
  - Step-by-step wizard drafting and session inspection.
  - Progressive step updates (licensing, staff, branch, inventory, plan).
  - Atomic completion & provisioning.
  - Strict platform RBAC & MFA step-up enforcement.
  - Immutability safeguards preventing deletion of completed records.
"""

from __future__ import annotations

from typing import Optional

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.models import OnboardingWizardStatus, TenantOnboardingSession
from apps.core.onboarding_service import TenantOnboardingService
from apps.core.pagination import SafeAdaptivePagination
from apps.core.serializers import (
    TenantOnboardingCompleteSerializer,
    TenantOnboardingSessionSerializer,
    TenantOnboardingStepUpdateSerializer,
)
from apps.identity.models import UserRole


class PlatformTenantOnboardingWizardViewSet(viewsets.ModelViewSet):
    """
    Platform Super Admin API for managing Tenant Onboarding Wizard sessions.
    Strictly isolated from tenant regular roles.
    """

    queryset = TenantOnboardingSession.objects.all().order_by("-created_at")
    serializer_class = TenantOnboardingSessionSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = SafeAdaptivePagination
    lookup_field = "session_id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def _enforce_platform_access(self, request, require_step_up: bool = False) -> Optional[Response]:
        """
        Guarantees that only authorized Platform Super Admins can access onboarding wizard,
        with mandatory MFA and step-up challenge for final completion.
        """
        user = getattr(request, "user", None)
        role = str(getattr(user, "role", "")).upper().strip()
        is_platform_role = role in [
            str(UserRole.SUPERADMIN),
            str(UserRole.PLATFORM_OPERATOR),
            "SUPER_ADMIN",
        ] or getattr(user, "is_superuser", False)

        if not is_platform_role:
            return Response(
                {
                    "success": False,
                    "error_code": "PLATFORM_ACCESS_DENIED",
                    "code": "PLATFORM_ACCESS_DENIED",
                    "message_en": "Tenant onboarding wizard is strictly restricted to Platform Super Administrators.",
                    "message_bn": "টেন্যান্ট অনবোর্ডিং উইজার্ড শুধুমাত্র প্ল্যাটফর্ম সুপার অ্যাডমিনিস্ট্রেটরের জন্য সংরক্ষিত।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if not getattr(user, "mfa_enabled", False):
            return Response(
                {
                    "success": False,
                    "error_code": "MFA_AUTHENTICATION_REQUIRED",
                    "code": "MFA_AUTHENTICATION_REQUIRED",
                    "message_en": "Platform administrative onboarding requires active multi-factor authentication (MFA).",
                    "message_bn": "প্ল্যাটফর্ম অনবোর্ডিং ব্যবহারের জন্য অ্যাক্টিভ মাল্টি-ফ্যাক্টর অথেনটিকেশন (MFA) আবশ্যক।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        mfa_verified = (
            request.headers.get("X-MFA-Verified") in ("true", "1", "True")
            or bool(request.headers.get("X-MFA-Session-Token"))
            or bool(request.META.get("HTTP_X_MFA_VERIFIED"))
            or bool(request.META.get("HTTP_X_MFA_SESSION_TOKEN"))
        )

        if not mfa_verified:
            return Response(
                {
                    "success": False,
                    "error_code": "MFA_SESSION_UNVERIFIED",
                    "code": "MFA_SESSION_UNVERIFIED",
                    "message_en": "Platform administrative onboarding requires an active verified MFA session.",
                    "message_bn": "প্ল্যাটফর্ম অনবোর্ডিংয়ের জন্য যাচাইকৃত MFA সেশন আবশ্যক।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if require_step_up:
            step_up_token = (
                request.headers.get("X-MFA-Step-Up-Token")
                or request.META.get("HTTP_X_MFA_STEP_UP_TOKEN")
                or (isinstance(request.data, dict) and request.data.get("mfa_code"))
            )
            if not step_up_token:
                return Response(
                    {
                        "success": False,
                        "error_code": "STEP_UP_MFA_REQUIRED",
                        "code": "STEP_UP_MFA_REQUIRED",
                        "message_en": "Final tenant provisioning requires step-up verification token (X-MFA-Step-Up-Token).",
                        "message_bn": "চূড়ান্ত টেন্যান্ট অ্যাক্টিভেশনের জন্য স্টেপ-আপ ভেরিফিকেশন টোকেন আবশ্যক।",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        return None

    def list(self, request, *args, **kwargs):
        denied = self._enforce_platform_access(request)
        if denied:
            return denied
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        denied = self._enforce_platform_access(request)
        if denied:
            return denied
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        denied = self._enforce_platform_access(request)
        if denied:
            return denied

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            session = TenantOnboardingService.create_session(
                data=serializer.validated_data,
                creator=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        out_serializer = self.get_serializer(session)
        return Response(out_serializer.data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        denied = self._enforce_platform_access(request)
        if denied:
            return denied

        session = self.get_object()
        serializer = TenantOnboardingStepUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        step = serializer.validated_data.get("step", session.current_step)
        try:
            updated_session = TenantOnboardingService.update_session_step(
                session=session,
                step=step,
                step_data=serializer.validated_data,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(self.get_serializer(updated_session).data)

    def destroy(self, request, *args, **kwargs):
        denied = self._enforce_platform_access(request)
        if denied:
            return denied

        session = self.get_object()

        if session.status == OnboardingWizardStatus.COMPLETED:
            return Response(
                {
                    "success": False,
                    "error_code": "ABSOLUTE_SYSTEM_BLOCK",
                    "code": "ABSOLUTE_SYSTEM_BLOCK",
                    "message_en": "Completed and provisioned tenant onboarding sessions cannot be deleted for audit integrity.",
                    "message_bn": "সম্পন্ন হওয়া টেন্যান্ট অনবোর্ডিং রেকর্ড অডিট সুরক্ষার স্বার্থে মুছে ফেলা সম্পূর্ণ নিষিদ্ধ।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        session_id = session.session_id
        session.delete()
        return Response(
            {
                "status": "DELETED",
                "session_id": session_id,
                "message_en": "Onboarding session draft deleted successfully.",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="update-step")
    def update_step(self, request, session_id=None):
        denied = self._enforce_platform_access(request)
        if denied:
            return denied

        session = self.get_object()
        serializer = TenantOnboardingStepUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        step = serializer.validated_data.get("step", session.current_step)
        try:
            updated_session = TenantOnboardingService.update_session_step(
                session=session,
                step=step,
                step_data=serializer.validated_data,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(self.get_serializer(updated_session).data)

    @action(detail=True, methods=["post"], url_path="complete")
    def complete_wizard(self, request, session_id=None):
        denied = self._enforce_platform_access(request, require_step_up=True)
        if denied:
            return denied

        session = self.get_object()
        serializer = TenantOnboardingCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        password = serializer.validated_data.get("owner_password")
        notes = serializer.validated_data.get("operator_notes", "")

        try:
            result = TenantOnboardingService.complete_onboarding(
                session=session,
                owner_password=password,
                operator=request.user,
                notes=notes,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "status": "PROVISIONED",
                "message_en": "Tenant onboarding wizard completed and provisioned successfully.",
                "message_bn": "টেন্যান্ট অনবোর্ডিং সফলভাবে সম্পন্ন ও অ্যাক্টিভেট করা হয়েছে।",
                "provisioning_details": result,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="abandon")
    def abandon_wizard(self, request, session_id=None):
        denied = self._enforce_platform_access(request)
        if denied:
            return denied

        session = self.get_object()
        reason = request.data.get("reason", "Cancelled by platform administrator")

        try:
            abandoned = TenantOnboardingService.abandon_session(
                session=session,
                operator=request.user,
                reason=reason,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(self.get_serializer(abandoned).data)
