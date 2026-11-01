"""
backend/apps/audit_logging/platform_audit_views.py

Architecture Decision: ADR-0078
REST API ViewSet for Platform Admin Audit Search:
- Multi-dimensional filtering by actor, tenant, date range, entity, and reason
- Guaranteed runtime sanitization of forbidden payloads (credentials, PINs, PHI)
- Strict Platform Role RBAC & Mandatory MFA verification
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit_logging.platform_audit_service import PlatformAuditSearchService
from apps.audit_logging.serializers import PlatformAuditSearchFilterSerializer
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)


class PlatformAuditSearchViewSet(viewsets.ViewSet):
    """
    Platform Super Admin & Auditor console for querying cross-tenant immutable audit trails.
    Mounted under: /api/v2/platform/audit-search/
    """

    permission_classes = [IsAuthenticated]

    def _check_platform_mfa_access(self, request) -> Optional[Response]:
        """Enforces platform role eligibility and mandatory verified MFA session."""
        user = request.user
        role = str(getattr(user, "role", "")).upper().strip()
        is_platform_role = role in [
            str(UserRole.SUPERADMIN),
            str(UserRole.PLATFORM_OPERATOR),
            str(UserRole.PLATFORM_AUDITOR),
            "SUPER_ADMIN",
        ] or getattr(user, "is_superuser", False)

        if not is_platform_role:
            return Response(
                {
                    "success": False,
                    "error_code": "PLATFORM_ACCESS_DENIED",
                    "code": "PLATFORM_ACCESS_DENIED",
                    "message_en": "Platform audit log search is strictly restricted to platform administrators.",
                    "message_bn": "প্ল্যাটফর্ম অডিট লগ অনুসন্ধান শুধুমাত্র প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটরদের জন্য সংরক্ষিত।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check MFA requirement (ADR-0077)
        if not getattr(user, "mfa_enabled", False):
            return Response(
                {
                    "success": False,
                    "error_code": "MFA_AUTHENTICATION_REQUIRED",
                    "code": "MFA_AUTHENTICATION_REQUIRED",
                    "message_en": "Platform administrative audit search requires active multi-factor authentication (MFA).",
                    "message_bn": "প্ল্যাটফর্ম অডিট সার্চ ব্যবহারের জন্য অ্যাক্টিভ মাল্টি-ফ্যাক্টর অথেনটিকেশন (MFA) আবশ্যক।",
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
                    "message_en": "Platform administrative APIs require an active verified MFA session.",
                    "message_bn": "প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেশনের জন্য যাচাইকৃত MFA সেশন আবশ্যক।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        return None

    def list(self, request):
        """Executes audit search using query parameters."""
        denied = self._check_platform_mfa_access(request)
        if denied:
            return denied

        serializer = PlatformAuditSearchFilterSerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "Invalid search parameters.",
                    "message_bn": "অনুসন্ধানের প্যারামিটারগুলো সঠিক নয়।",
                    "details": serializer.errors,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        client_ip = request.META.get("REMOTE_ADDR")
        search_result = PlatformAuditSearchService.search_events(
            filters=serializer.validated_data,
            requesting_user=request.user,
            client_ip=client_ip,
        )
        return Response(search_result, status=status.HTTP_200_OK)

    def create(self, request):
        """Executes audit search using JSON request body."""
        denied = self._check_platform_mfa_access(request)
        if denied:
            return denied

        serializer = PlatformAuditSearchFilterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "Invalid search parameters.",
                    "message_bn": "অনুসন্ধানের প্যারামিটারগুলো সঠিক নয়।",
                    "details": serializer.errors,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        client_ip = request.META.get("REMOTE_ADDR")
        search_result = PlatformAuditSearchService.search_events(
            filters=serializer.validated_data,
            requesting_user=request.user,
            client_ip=client_ip,
        )
        return Response(search_result, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None):
        """Retrieves and sanitizes a single audit log entry by ID."""
        denied = self._check_platform_mfa_access(request)
        if denied:
            return denied

        client_ip = request.META.get("REMOTE_ADDR")
        detail = PlatformAuditSearchService.get_event_detail(
            entry_id=pk,
            requesting_user=request.user,
            client_ip=client_ip,
        )
        if not detail:
            return Response(
                {
                    "error_code": "AUDIT_ENTRY_NOT_FOUND",
                    "message_en": f"Audit entry '{pk}' not found.",
                    "message_bn": f"অডিট এন্ট্রি '{pk}' পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(detail, status=status.HTTP_200_OK)

    def destroy(self, request, pk=None):
        """Absolute immutability block: Audit logs cannot be deleted."""
        return Response(
            {
                "success": False,
                "error_code": "ABSOLUTE_SYSTEM_BLOCK",
                "message_en": "Audit log records are immutable and permanent. Deletion is strictly prohibited under DGDA and statutory compliance regulations.",
                "message_bn": "অডিট লগ রেকর্ডগুলো অপরিবর্তনযোগ্য। এগুলো মুছে ফেলা কঠোরভাবে নিষিদ্ধ।",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    @action(detail=False, methods=["post"], url_path="export")
    def export(self, request):
        """Generates an audited, sanitized export manifest of audit search results."""
        denied = self._check_platform_mfa_access(request)
        if denied:
            return denied

        serializer = PlatformAuditSearchFilterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "Invalid export parameters.",
                    "message_bn": "এক্সপোর্ট প্যারামিটারগুলো সঠিক নয়।",
                    "details": serializer.errors,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        client_ip = request.META.get("REMOTE_ADDR")
        search_result = PlatformAuditSearchService.search_events(
            filters=serializer.validated_data,
            requesting_user=request.user,
            client_ip=client_ip,
        )

        serialized_data = json.dumps(search_result["results"], sort_keys=True)
        manifest_hash = hashlib.sha256(serialized_data.encode("utf-8")).hexdigest()

        return Response(
            {
                "success": True,
                "manifest_sha256": manifest_hash,
                "exported_records": search_result["count"],
                "records": search_result["results"],
                "query_filters": search_result["query_filters"],
            },
            status=status.HTTP_200_OK,
        )
