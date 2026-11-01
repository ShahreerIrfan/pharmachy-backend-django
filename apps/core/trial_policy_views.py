"""
backend/apps/core/trial_policy_views.py
Architecture Decision: ADR-0062, ADR-0085 & ADR-0086
Phase 05: Basic/Standard/Pro Subscription & Billing (P086)

REST API Endpoints for Trial and Evaluation Policy:
- GET /api/v2/billing/trial/status/ (or ?tenant_id=... / <pk>/status/)
- POST /api/v2/billing/trial/extend/
- POST /api/v2/billing/trial/evaluate-eligibility/
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.trial_policy_service import TrialPolicyService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.trial_views")


class TrialPolicyViewSet(viewsets.ViewSet):
    """
    Endpoints for trial status inspection, anti-abuse checks, and SuperAdmin extensions.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _resolve_target_tenant_id(self, request: Any, pk: str | None = None) -> str:
        """Enforces multi-tenant ownership: Owner can only inspect self; SuperAdmin can inspect any."""
        user = request.user
        is_superadmin = (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) == UserRole.SUPERADMIN
            or str(getattr(user, "role", "")).upper() in [
                "SUPERADMIN",
                "SUPER_ADMIN",
                "PLATFORM_SUPER_ADMIN",
                "PLATFORM_OPERATOR",
            ]
        )
        target_tid = pk or request.query_params.get("tenant_id") or getattr(request, "tenant_id", None) or getattr(user, "tenant_id", None)
        if not target_tid:
            raise ValidationError("Tenant ID is required.")

        target_tid = str(target_tid).strip()
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        if not is_superadmin and user_tid and user_tid != target_tid:
            from apps.core.errors import CrossTenantViolationError

            raise CrossTenantViolationError(
                f"Cross-tenant access denied: You cannot inspect trial policy for tenant '{target_tid}'."
            )

        return target_tid

    @action(detail=False, methods=["GET"], url_path="status")
    def status_root(self, request):
        """GET /api/v2/billing/trial/status/"""
        tenant_id = self._resolve_target_tenant_id(request)
        trial_info = TrialPolicyService.get_trial_status(tenant_id)
        return Response(
            {
                "success": True,
                "code": "TRIAL_STATUS_RETRIEVED",
                "message_en": "Trial policy and expiration terms retrieved successfully.",
                "message_bn": "ট্রায়াল পলিসি এবং মেয়াদের তথ্য সফলভাবে প্রাপ্ত হয়েছে।",
                "data": trial_info,
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request, pk=None):
        """GET /api/v2/billing/trial/<tenant_id>/"""
        tenant_id = self._resolve_target_tenant_id(request, pk=pk)
        trial_info = TrialPolicyService.get_trial_status(tenant_id)
        return Response(
            {
                "success": True,
                "code": "TRIAL_STATUS_RETRIEVED",
                "message_en": "Trial policy and expiration terms retrieved successfully.",
                "message_bn": "ট্রায়াল পলিসি এবং মেয়াদের তথ্য সফলভাবে প্রাপ্ত হয়েছে।",
                "data": trial_info,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["POST"], url_path="extend")
    def extend_trial(self, request):
        """POST /api/v2/billing/trial/extend/"""
        data = request.data or {}
        tenant_id = data.get("tenant_id")
        if not tenant_id:
            return Response(
                {"success": False, "error_code": "ERR_TENANT_REQUIRED", "message": "tenant_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        additional_days = int(data.get("additional_days", 7))
        reason = str(data.get("reason", "")).strip()

        try:
            extended_info = TrialPolicyService.extend_trial(
                tenant_id=tenant_id,
                operator_user=request.user,
                additional_days=additional_days,
                reason=reason,
            )
            return Response(
                {
                    "success": True,
                    "code": "TRIAL_PERIOD_EXTENDED",
                    "message_en": f"Trial extended by {additional_days} day(s) successfully.",
                    "message_bn": f"ট্রায়ালের মেয়াদ সফলভাবে {additional_days} দিন বাড়ানো হয়েছে।",
                    "data": extended_info,
                },
                status=status.HTTP_200_OK,
            )
        except PermissionDenied as e:
            err_data = getattr(e, "detail", {}) if hasattr(e, "detail") and isinstance(e.detail, dict) else {}
            return Response(
                {
                    "success": False,
                    "error_code": err_data.get("error_code", "ROLE_PERMISSION_DENIED"),
                    "code": err_data.get("error_code", "ROLE_PERMISSION_DENIED"),
                    "message": str(e),
                    "message_en": err_data.get("message_en", str(e)),
                    "message_bn": err_data.get("message_bn", "অনুমতি প্রত্যাখ্যাত।"),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {
                    "success": False,
                    "error_code": "ERR_TRIAL_EXTENSION_INVALID",
                    "code": "ERR_TRIAL_EXTENSION_INVALID",
                    "message": msg,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    @action(detail=False, methods=["POST"], url_path="evaluate-eligibility", permission_classes=[permissions.AllowAny])
    def evaluate_eligibility(self, request):
        """POST /api/v2/billing/trial/evaluate-eligibility/"""
        data = request.data or {}
        license_number = data.get("license_number")
        phone = data.get("phone")
        email = data.get("email")

        is_eligible, code, reason = TrialPolicyService.validate_trial_eligibility(
            license_number=license_number,
            phone=phone,
            email=email,
        )

        http_code = status.HTTP_200_OK if is_eligible else status.HTTP_409_CONFLICT
        return Response(
            {
                "success": is_eligible,
                "code": code,
                "error_code": None if is_eligible else code,
                "message": reason,
                "data": {
                    "is_eligible": is_eligible,
                    "abuse_detected": not is_eligible,
                    "license_number": license_number,
                    "phone": phone,
                    "email": email,
                },
            },
            status=http_code,
        )
