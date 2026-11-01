"""
backend/apps/core/platform_access_views.py

Architecture Decision: ADR-0077
REST ViewSet for Platform API Access Policies, MFA Enforcement,
Step-Up Verification, and Access Evaluation.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.models import PlatformApiAccessPolicy
from apps.core.platform_access_service import PlatformAccessPolicyService
from apps.core.serializers import (
    PlatformApiAccessPolicySerializer,
    PlatformPolicyEvaluationRequestSerializer,
    StepUpVerificationRequestSerializer,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class PlatformAccessPolicyViewSet(viewsets.ViewSet):
    """
    Platform Super Admin console for managing API access policies,
    evaluating caller privileges, and performing step-up MFA challenges.
    Mounted under: /api/v2/platform/access-policies/
    """

    permission_classes = [IsAuthenticated]

    def _evaluate_caller_access(self, request, require_step_up: bool = False) -> Optional[Response]:
        """Internal guard evaluating caller against Platform Access Policy Service."""
        headers = {
            "X-MFA-Session-Token": request.headers.get("X-MFA-Session-Token", ""),
            "X-MFA-Verified": request.headers.get("X-MFA-Verified", ""),
            "X-MFA-Code": request.headers.get("X-MFA-Code", ""),
            "X-MFA-Step-Up-Token": request.headers.get("X-MFA-Step-Up-Token", ""),
            "X-Support-Grant-Token": request.headers.get("X-Support-Grant-Token", ""),
        }
        client_ip = request.META.get("REMOTE_ADDR")
        eval_result = PlatformAccessPolicyService.evaluate_access(
            user=request.user,
            path=request.path,
            method=request.method,
            headers=headers,
            client_ip=client_ip,
        )

        if not eval_result["allowed"]:
            error_code = eval_result.get("error_code", "PERMISSION_DENIED")
            msg_en = eval_result.get("message_en", "Access denied.")
            msg_bn = eval_result.get("message_bn", "অ্যাক্সেস অনুমোদিত নয়।")
            return Response(
                {
                    "success": False,
                    "error_code": error_code,
                    "code": error_code,
                    "message_en": msg_en,
                    "message_bn": msg_bn,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def list(self, request):
        """Lists active platform access policies."""
        denied = self._evaluate_caller_access(request)
        if denied:
            return denied

        policies = PlatformApiAccessPolicy.objects.filter(is_active=True).order_by("-created_at")
        serializer = PlatformApiAccessPolicySerializer(policies, many=True)
        return Response(
            {
                "success": True,
                "count": policies.count(),
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific platform access policy."""
        denied = self._evaluate_caller_access(request)
        if denied:
            return denied

        policy = PlatformApiAccessPolicy.objects.filter(policy_id=pk).first()
        if not policy:
            return Response(
                {
                    "error_code": "POLICY_NOT_FOUND",
                    "message_en": f"Platform access policy '{pk}' not found.",
                    "message_bn": f"প্ল্যাটফর্ম পলিসি '{pk}' পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = PlatformApiAccessPolicySerializer(policy)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def create(self, request):
        """Creates a new Platform API Access Policy (SuperAdmin only with step-up MFA)."""
        denied = self._evaluate_caller_access(request, require_step_up=True)
        if denied:
            return denied

        user_role = str(getattr(request.user, "role", "")).upper().strip()
        if user_role not in [str(UserRole.SUPERADMIN), "SUPER_ADMIN"] and not getattr(
            request.user, "is_superuser", False
        ):
            return Response(
                {
                    "success": False,
                    "error_code": "SUPERADMIN_REQUIRED",
                    "code": "SUPERADMIN_REQUIRED",
                    "message_en": "Only Platform Super Administrators can create access policies.",
                    "message_bn": "শুধুমাত্র প্ল্যাটফর্ম সুপার অ্যাডমিনিস্ট্রেটররা নতুন অ্যাক্সেস পলিসি তৈরি করতে পারেন।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = PlatformApiAccessPolicySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "Invalid policy parameters.",
                    "message_bn": "পলিসি প্যারামিটারগুলো সঠিক নয়।",
                    "details": serializer.errors,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            policy = PlatformAccessPolicyService.create_policy(
                name=serializer.validated_data["name"],
                endpoint_pattern=serializer.validated_data["endpoint_pattern"],
                http_method=serializer.validated_data.get("http_method", "*"),
                allowed_roles=serializer.validated_data.get("allowed_roles"),
                require_mfa=serializer.validated_data.get("require_mfa", True),
                require_step_up=serializer.validated_data.get("require_step_up", False),
                enforce_zero_implicit_tenant_access=serializer.validated_data.get(
                    "enforce_zero_implicit_tenant_access", True
                ),
                ip_allowlist=serializer.validated_data.get("ip_allowlist"),
                description=serializer.validated_data.get("description", ""),
                created_by=request.user,
            )
        except DjangoValidationError as e:
            return Response(
                {
                    "error_code": "INVALID_POLICY_CONFIGURATION",
                    "message_en": str(e.message if hasattr(e, "message") else e),
                    "message_bn": "অবৈধ পলিসি কনফিগারেশন।",
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(PlatformApiAccessPolicySerializer(policy).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        """Updates an existing access policy (SuperAdmin only with step-up MFA)."""
        denied = self._evaluate_caller_access(request, require_step_up=True)
        if denied:
            return denied

        user_role = str(getattr(request.user, "role", "")).upper().strip()
        if user_role not in [str(UserRole.SUPERADMIN), "SUPER_ADMIN"] and not getattr(
            request.user, "is_superuser", False
        ):
            return Response(
                {
                    "success": False,
                    "error_code": "SUPERADMIN_REQUIRED",
                    "code": "SUPERADMIN_REQUIRED",
                    "message_en": "Only Platform Super Administrators can modify access policies.",
                    "message_bn": "শুধুমাত্র প্ল্যাটফর্ম সুপার অ্যাডমিনিস্ট্রেটররা অ্যাক্সেস পলিসি পরিবর্তন করতে পারেন।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        policy = PlatformApiAccessPolicy.objects.filter(policy_id=pk).first()
        if not policy:
            return Response(
                {
                    "error_code": "POLICY_NOT_FOUND",
                    "message_en": f"Platform access policy '{pk}' not found.",
                    "message_bn": f"প্ল্যাটফর্ম পলিসি '{pk}' পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = PlatformApiAccessPolicySerializer(policy, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "Invalid policy update payload.",
                    "message_bn": "পলিসি আপডেট ডেটা সঠিক নয়।",
                    "details": serializer.errors,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None):
        """Deactivates a platform access policy."""
        denied = self._evaluate_caller_access(request, require_step_up=True)
        if denied:
            return denied

        policy = PlatformApiAccessPolicy.objects.filter(policy_id=pk).first()
        if not policy:
            return Response(
                {
                    "error_code": "POLICY_NOT_FOUND",
                    "message_en": f"Platform access policy '{pk}' not found.",
                    "message_bn": f"প্ল্যাটফর্ম পলিসি '{pk}' পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        policy.is_active = False
        policy.save(update_fields=["is_active", "updated_at"])
        return Response(
            {
                "success": True,
                "message_en": f"Platform access policy '{pk}' successfully deactivated.",
                "message_bn": f"প্ল্যাটফর্ম পলিসি '{pk}' নিষ্ক্রিয় করা হয়েছে।",
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="evaluate")
    def evaluate(self, request):
        """
        Probes and evaluates access rules against a target path and method.
        Useful for clients checking authorization, MFA, or zero implicit access.
        """
        serializer = PlatformPolicyEvaluationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "Path is required for policy evaluation.",
                    "message_bn": "পলিসি মূল্যায়নের জন্য পাথ আবশ্যক।",
                    "details": serializer.errors,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        path = serializer.validated_data["path"]
        method = serializer.validated_data.get("method", "GET")
        client_ip = serializer.validated_data.get("client_ip") or request.META.get("REMOTE_ADDR")

        headers = {
            "X-MFA-Session-Token": request.headers.get("X-MFA-Session-Token", ""),
            "X-MFA-Verified": request.headers.get("X-MFA-Verified", ""),
            "X-MFA-Code": request.headers.get("X-MFA-Code", ""),
            "X-MFA-Step-Up-Token": request.headers.get("X-MFA-Step-Up-Token", ""),
            "X-Support-Grant-Token": request.headers.get("X-Support-Grant-Token", ""),
        }

        result = PlatformAccessPolicyService.evaluate_access(
            user=request.user,
            path=path,
            method=method,
            headers=headers,
            client_ip=client_ip,
        )
        return Response(result, status=status.HTTP_200_OK if result["allowed"] else status.HTTP_403_FORBIDDEN)

    @action(detail=False, methods=["post"], url_path="verify-step-up")
    def verify_step_up(self, request):
        """
        Verifies TOTP code and issues a short-lived step-up token for high-risk operations.
        """
        serializer = StepUpVerificationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "6-digit TOTP verification code is required.",
                    "message_bn": "৬-সংখ্যার TOTP কোড আবশ্যক।",
                    "details": serializer.errors,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        code = serializer.validated_data["code"]
        try:
            token_data = PlatformAccessPolicyService.issue_step_up_token(request.user, code)
            return Response(
                {
                    "success": True,
                    "step_up_token": token_data["step_up_token"],
                    "expires_in": token_data["expires_in"],
                    "message_en": "Step-up MFA verification successful.",
                    "message_bn": "স্টেপ-আপ MFA যাচাইকরণ সফল হয়েছে।",
                },
                status=status.HTTP_200_OK,
            )
        except DjangoValidationError as e:
            return Response(
                {
                    "error_code": "INVALID_STEP_UP_CODE",
                    "message_en": str(e.message if hasattr(e, "message") else e),
                    "message_bn": "প্রদত্ত স্টেপ-আপ MFA কোডটি সঠিক নয়।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
