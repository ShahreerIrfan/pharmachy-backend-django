"""
backend/apps/core/announcement_views.py

Architecture Decision: ADR-0076
REST API ViewSets for Platform Announcements and Tenant Read Receipts.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.announcement_service import SystemAnnouncementService
from apps.core.models import SystemAnnouncement
from apps.core.serializers import (
    AnnouncementReadReceiptSerializer,
    SystemAnnouncementSerializer,
)

logger = logging.getLogger(__name__)


class PlatformAnnouncementViewSet(viewsets.ViewSet):
    """
    Platform Super Admin console for managing broadcast announcements.
    Mounted under: /api/v2/platform/announcements/
    """

    permission_classes = [IsAuthenticated]

    def _is_superadmin(self, request) -> bool:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        return str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]

    def _require_superadmin(self, request):
        if not self._is_superadmin(request):
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_ACCESS_DENIED",
                    "message_en": "Platform announcement management is strictly restricted to platform administrators.",
                    "message_bn": "প্ল্যাটফর্ম ঘোষণা ব্যবস্থাপনা শুধুমাত্র প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটরদের জন্য সংরক্ষিত।",
                }
            )

    def list(self, request):
        self._require_superadmin(request)
        queryset = SystemAnnouncement.objects.all().order_by("-created_at")
        status_param = request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param.upper().strip())
        priority_param = request.query_params.get("priority")
        if priority_param:
            queryset = queryset.filter(priority=priority_param.upper().strip())

        serializer = SystemAnnouncementSerializer(queryset, many=True)
        return Response(
            {
                "success": True,
                "count": queryset.count(),
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request, pk=None):
        self._require_superadmin(request)
        try:
            announcement = SystemAnnouncement.objects.get(announcement_id=pk)
        except SystemAnnouncement.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error_code": "ANNOUNCEMENT_NOT_FOUND",
                    "message_en": f"Announcement '{pk}' was not found.",
                    "message_bn": f"ঘোষণা '{pk}' খুঁজে পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = SystemAnnouncementSerializer(announcement)
        return Response(
            {
                "success": True,
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def create(self, request):
        self._require_superadmin(request)
        payload = request.data or {}

        try:
            announcement = SystemAnnouncementService.create_announcement(
                title=payload.get("title"),
                message=payload.get("message"),
                priority=payload.get("priority", "INFO"),
                target_type=payload.get("target_type", "ALL"),
                target_tenants=payload.get("target_tenants"),
                target_roles=payload.get("target_roles"),
                target_branches=payload.get("target_branches"),
                starts_at=payload.get("starts_at"),
                expires_at=payload.get("expires_at"),
                operator_user=request.user,
            )
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Failed to create announcement.",
                    "message_bn": "সিস্টেম ঘোষণা তৈরিতে ব্যর্থ হয়েছে।",
                }
            )

        serializer = SystemAnnouncementSerializer(announcement)
        return Response(
            {
                "success": True,
                "message_en": "System announcement created successfully.",
                "message_bn": "সিস্টেম ঘোষণা সফলভাবে তৈরি হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="publish")
    def publish(self, request, pk=None):
        self._require_superadmin(request)
        try:
            announcement = SystemAnnouncementService.publish_announcement(
                announcement_id=pk, operator_user=request.user
            )
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Unable to publish announcement.",
                    "message_bn": "ঘোষণা প্রকাশ করতে ব্যর্থ হয়েছে।",
                }
            )

        serializer = SystemAnnouncementSerializer(announcement)
        return Response(
            {
                "success": True,
                "message_en": "System announcement published successfully.",
                "message_bn": "সিস্টেম ঘোষণা সফলভাবে প্রকাশিত হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None):
        self._require_superadmin(request)
        try:
            announcement = SystemAnnouncementService.archive_announcement(
                announcement_id=pk, operator_user=request.user
            )
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Unable to archive announcement.",
                    "message_bn": "ঘোষণা আর্কাইভ করতে ব্যর্থ হয়েছে।",
                }
            )

        serializer = SystemAnnouncementSerializer(announcement)
        return Response(
            {
                "success": True,
                "message_en": "System announcement archived.",
                "message_bn": "সিস্টেম ঘোষণা আর্কাইভ করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, pk=None):
        self._require_superadmin(request)
        try:
            stats_data = SystemAnnouncementService.get_announcement_stats(announcement_id=pk)
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Failed to retrieve announcement statistics.",
                    "message_bn": "ঘোষণার পরিসংখ্যান লোড করতে ব্যর্থ হয়েছে।",
                }
            )

        return Response(
            {
                "success": True,
                "data": stats_data,
            },
            status=status.HTTP_200_OK,
        )


class TenantAnnouncementViewSet(viewsets.ViewSet):
    """
    Tenant-facing endpoint for receiving and acknowledging announcements.
    Mounted under: /api/v2/tenants/announcements/
    """

    permission_classes = [IsAuthenticated]

    def list(self, request):
        announcements = SystemAnnouncementService.get_active_announcements_for_user(request.user)
        return Response(
            {
                "success": True,
                "count": len(announcements),
                "data": announcements,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="acknowledge")
    def acknowledge(self, request, pk=None):
        try:
            receipt = SystemAnnouncementService.acknowledge_announcement(announcement_id=pk, user=request.user)
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Failed to acknowledge announcement.",
                    "message_bn": "ঘোষণা নিশ্চিতকরণে ব্যর্থ হয়েছে।",
                }
            )

        serializer = AnnouncementReadReceiptSerializer(receipt)
        return Response(
            {
                "success": True,
                "message_en": "Announcement acknowledged successfully.",
                "message_bn": "ঘোষণা সফলভাবে নিশ্চিত করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )
