"""
backend/apps/core/support_access_views.py

Architecture Decision: ADR-0068
Dedicated API ViewSets for Delegated Support Access Governance & Break-Glass Protocol:
1. PlatformSuperAdminSupportAccessViewSet:
   - POST /api/v2/platform/support-access/request/
   - GET  /api/v2/platform/support-access/grants/
   - POST /api/v2/platform/support-access/<grant_id>/revoke/
   - POST /api/v2/platform/support-access/impersonate/ (Strictly BLOCKED with IMPERSONATION_FORBIDDEN)
2. TenantSupportAccessViewSet:
   - GET  /api/v2/tenants/<tenant_id>/support-access/active/
   - POST /api/v2/tenants/<tenant_id>/support-access/<grant_id>/approve/
   - POST /api/v2/tenants/<tenant_id>/support-access/<grant_id>/reject/
   - POST /api/v2/tenants/<tenant_id>/support-access/<grant_id>/revoke/
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import SupportAccessGrant
from apps.core.support_access_service import SupportAccessService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class PlatformSupportAccessViewSet(viewsets.ViewSet):
    """
    Platform Super Admin Support Access Endpoint.
    Role-gated to SUPERADMIN and PLATFORM_OPERATOR.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _require_platform_staff(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")
        role = str(getattr(user, "role", "")).upper().strip()
        if role not in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR] and not getattr(user, "is_superuser", False):
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_ACCESS_DENIED",
                    "message_en": "Platform support governance is restricted to platform operators.",
                    "message_bn": "প্ল্যাটফর্ম সাপোর্ট পরিচালনা শুধুমাত্র প্ল্যাটফর্ম অপারেটরদের জন্য সীমাবদ্ধ।",
                }
            )

    @action(detail=False, methods=["post"], url_path="request")
    def request_access(self, request):
        """Requests delegated support access for a tenant."""
        self._require_platform_staff(request)
        data = request.data or {}

        try:
            grant_payload = SupportAccessService.request_support_access(
                operator=request.user,
                tenant_id=data.get("tenant_id"),
                scope=data.get("scope", "DIAGNOSTIC_READ"),
                reason=data.get("reason", ""),
                ticket_reference=data.get("ticket_reference"),
                duration_minutes=data.get("duration_minutes", 30),
                is_break_glass=bool(data.get("is_break_glass", False)),
                break_glass_justification=data.get("break_glass_justification"),
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        is_bg = grant_payload.get("is_break_glass", False)
        code = "SUPPORT_ACCESS_BREAK_GLASS_ACTIVATED" if is_bg else "SUPPORT_ACCESS_REQUEST_SUBMITTED"
        msg_en = (
            "Emergency break-glass support access activated."
            if is_bg
            else "Support access request submitted. Awaiting tenant owner approval."
        )
        msg_bn = (
            "জরুরি ব্রেক-গ্লাস সাপোর্ট অ্যাক্সেস সক্রিয় করা হয়েছে।"
            if is_bg
            else "সাপোর্ট অ্যাক্সেসের আবেদন জমা হয়েছে। ফার্মেসি মালিকের অনুমোদনের অপেক্ষায়।"
        )

        return Response(
            {
                "success": True,
                "code": code,
                "message_en": msg_en,
                "message_bn": msg_bn,
                "data": grant_payload,
            },
            status=status.HTTP_201_CREATED if not is_bg else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="grants")
    def list_grants(self, request):
        """Lists platform support access requests and grants."""
        self._require_platform_staff(request)
        tenant_id = request.query_params.get("tenant_id")
        status_filter = request.query_params.get("status")

        qs = SupportAccessGrant.objects.select_related("tenant", "requested_by_operator", "approved_by_user").all()
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id.strip())
        if status_filter:
            qs = qs.filter(status=status_filter.upper().strip())

        items = [SupportAccessService.serialize_grant(g) for g in qs[:50]]
        return Response(
            {
                "success": True,
                "code": "SUPPORT_GRANTS_RETRIEVED",
                "message_en": f"Retrieved {len(items)} support grant(s).",
                "message_bn": f"{len(items)}টি সাপোর্ট গ্রান্ট প্রাপ্ত হয়েছে।",
                "data": {"total": len(items), "items": items},
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path=r"(?P<grant_id>[^/.]+)/revoke")
    def revoke_grant(self, request, grant_id=None):
        """Platform Super Admin Kill Switch: instantly revokes an active support grant."""
        self._require_platform_staff(request)
        reason = (request.data or {}).get("reason", "Revoked by platform administrator.")

        try:
            grant_payload = SupportAccessService.revoke_support_access(
                user=request.user, grant_id=grant_id, reason=reason
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_ACCESS_REVOKED",
                "message_en": f"Support access grant '{grant_id}' was immediately revoked.",
                "message_bn": f"সাপোর্ট অ্যাক্সেস গ্রান্ট '{grant_id}' তাৎক্ষণিকভাবে বাতিল করা হয়েছে।",
                "data": grant_payload,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="impersonate")
    def impersonate(self, request):
        """
        Anti-pattern enforcement: Direct user impersonation ('Login As') is strictly forbidden.
        """
        user = getattr(request, "user", None)
        target_user = (request.data or {}).get("target_user_id") or "UNKNOWN"
        target_tenant = (request.data or {}).get("tenant_id") or "UNKNOWN"

        # Log security violation attempt
        AuditLogEntry.objects.create(
            action="UNSAFE_IMPERSONATION_ATTEMPT_BLOCKED",
            resource_type="IMPERSONATION_PREVENTION",
            resource_id=str(target_user),
            tenant_id=str(target_tenant),
            status=AuditActionStatus.DENIED,
            metadata_json={
                "requester": str(getattr(user, "email", None) or user),
                "target_user_id": target_user,
                "target_tenant": target_tenant,
                "policy": "ADR-0068",
            },
        )

        return Response(
            {
                "success": False,
                "error_code": "IMPERSONATION_FORBIDDEN",
                "message_en": (
                    "Direct user impersonation ('Login As') is strictly prohibited under ADR-0068. "
                    "Support access must follow delegated scoped grants with pharmacy owner consent."
                ),
                "message_bn": "সরাসরি ব্যবহারকারী ইমপারসোনেশন ('Login As') কঠোরভাবে নিষিদ্ধ। অনুমোদিত সাপোর্ট গ্রান্ট ব্যবহার করুন।",
            },
            status=status.HTTP_403_FORBIDDEN,
        )


class TenantSupportAccessViewSet(viewsets.ViewSet):
    """
    Tenant-Side Support Access Endpoints.
    Allows Pharmacy Owners to inspect active notices, approve, reject, or revoke support grants.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _require_tenant_access(self, request, tenant_id):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")

        clean_tenant_id = str(tenant_id or "").strip()
        user_tenant_id = str(getattr(user, "tenant_id", "") or "").strip()

        # Allow Super Admin
        if getattr(user, "is_superuser", False) or str(getattr(user, "role", "")).upper() in [
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
        ]:
            return

        # Enforce Tenant Isolation
        if user_tenant_id != clean_tenant_id:
            raise PermissionDenied(
                detail={
                    "error_code": "CROSS_TENANT_VIOLATION",
                    "message_en": f"Access denied: Cannot access support governance for foreign tenant '{clean_tenant_id}'.",
                    "message_bn": f"অন্য ফার্মেসির ('{clean_tenant_id}') সাপোর্ট অ্যাক্সেস কনফিগারেশনে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ।",
                }
            )

    @action(detail=False, methods=["get"], url_path=r"(?P<tenant_id>[^/.]+)/active")
    def active_notice(self, request, tenant_id=None):
        """Returns the active support access notice banner configuration for client UI."""
        self._require_tenant_access(request, tenant_id)
        notice_data = SupportAccessService.get_active_support_notice(tenant_id=tenant_id)
        return Response(
            {
                "success": True,
                "code": "SUPPORT_ACTIVE_NOTICE_RETRIEVED",
                "message_en": "Support access notice retrieved successfully.",
                "message_bn": "সাপোর্ট অ্যাক্সেস নোটিশ সফলভাবে প্রাপ্ত হয়েছে।",
                "data": notice_data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path=r"(?P<tenant_id>[^/.]+)/(?P<grant_id>[^/.]+)/approve")
    def approve_grant(self, request, tenant_id=None, grant_id=None):
        """Pharmacy Owner approves a pending support access grant."""
        self._require_tenant_access(request, tenant_id)
        try:
            grant_payload = SupportAccessService.approve_support_access(user=request.user, grant_id=grant_id)
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_ACCESS_APPROVED",
                "message_en": "Support access request approved. Delegated token active.",
                "message_bn": "সাপোর্ট অ্যাক্সেসের আবেদন অনুমোদিত হয়েছে। ডেলিগেটেড টোকেন সক্রিয়।",
                "data": grant_payload,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path=r"(?P<tenant_id>[^/.]+)/(?P<grant_id>[^/.]+)/reject")
    def reject_grant(self, request, tenant_id=None, grant_id=None):
        """Pharmacy Owner rejects a pending support access grant."""
        self._require_tenant_access(request, tenant_id)
        reason = (request.data or {}).get("reason", "Rejected by pharmacy owner.")
        try:
            grant_payload = SupportAccessService.reject_support_access(
                user=request.user, grant_id=grant_id, reason=reason
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_ACCESS_REJECTED",
                "message_en": "Support access request rejected.",
                "message_bn": "সাপোর্ট অ্যাক্সেসের আবেদন বাতিল করা হয়েছে।",
                "data": grant_payload,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path=r"(?P<tenant_id>[^/.]+)/(?P<grant_id>[^/.]+)/revoke")
    def revoke_grant(self, request, tenant_id=None, grant_id=None):
        """Pharmacy Owner immediately revokes an active support access grant."""
        self._require_tenant_access(request, tenant_id)
        reason = (request.data or {}).get("reason", "Revoked by pharmacy licensee.")
        try:
            grant_payload = SupportAccessService.revoke_support_access(
                user=request.user, grant_id=grant_id, reason=reason
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)
        except DjangoPermissionDenied as err:
            raise PermissionDenied(detail=str(err))

        return Response(
            {
                "success": True,
                "code": "SUPPORT_ACCESS_REVOKED",
                "message_en": "Support access terminated immediately.",
                "message_bn": "সাপোর্ট অ্যাক্সেস তাৎক্ষণিকভাবে বন্ধ করা হয়েছে।",
                "data": grant_payload,
            },
            status=status.HTTP_200_OK,
        )
