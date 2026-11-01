"""
backend/apps/core/cross_tenant_metrics_views.py

Architecture Decision: ADR-0075
REST API ViewSets for Privacy-Safe Cross-Tenant Metrics Aggregation and Tenant Benchmarks.
Platform Super Admin console controls macro aggregation; tenants receive strictly anonymized comparative benchmarks.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.cross_tenant_metrics_service import CrossTenantMetricsService
from apps.core.models import CrossTenantMetricsSnapshot
from apps.core.serializers import CrossTenantMetricsSnapshotSerializer

logger = logging.getLogger(__name__)


class PlatformCrossTenantMetricsViewSet(viewsets.ViewSet):
    """
    Super Admin endpoint for generating, reviewing, publishing, and archiving
    cross-tenant aggregated macro-metrics with k-anonymity privacy guardrails.
    Mounted under: /api/v2/platform/cross-tenant-metrics/
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
                    "message_en": "Cross-tenant platform metrics administration is strictly restricted to platform administrators.",
                    "message_bn": "প্ল্যাটফর্ম ক্রস-টেন্যান্ট মেট্রিক্স প্রশাসন শুধুমাত্র প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটরদের জন্য সংরক্ষিত।",
                }
            )

    def list(self, request):
        self._require_superadmin(request)
        queryset = CrossTenantMetricsSnapshot.objects.all().order_by("-created_at")
        status_param = request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param.upper().strip())

        period_param = request.query_params.get("metric_period")
        if period_param:
            queryset = queryset.filter(metric_period=period_param.upper().strip())

        serializer = CrossTenantMetricsSnapshotSerializer(queryset, many=True)
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
            snapshot = CrossTenantMetricsSnapshot.objects.get(snapshot_id=pk)
        except CrossTenantMetricsSnapshot.DoesNotExist:
            return Response(
                {
                    "success": False,
                    "error_code": "SNAPSHOT_NOT_FOUND",
                    "message_en": f"Metrics snapshot '{pk}' was not found.",
                    "message_bn": f"মেট্রিক্স স্ন্যাপশট '{pk}' খুঁজে পাওয়া যায়নি।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = CrossTenantMetricsSnapshotSerializer(snapshot)
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

        metric_period = payload.get("metric_period", "MONTHLY")
        start_date = payload.get("start_date")
        end_date = payload.get("end_date")
        min_cohort = payload.get("min_cohort_threshold", 3)
        notes = payload.get("notes", "")

        try:
            snapshot = CrossTenantMetricsService.generate_snapshot(
                metric_period=metric_period,
                start_date=start_date,
                end_date=end_date,
                min_cohort_threshold=min_cohort,
                operator_user=request.user,
                notes=notes,
            )
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Failed to aggregate cross-tenant metrics due to invalid inputs.",
                    "message_bn": "ভুল ইনপুটের কারণে ক্রস-টেন্যান্ট মেট্রিক্স গণনা ব্যর্থ হয়েছে।",
                }
            )

        serializer = CrossTenantMetricsSnapshotSerializer(snapshot)
        return Response(
            {
                "success": True,
                "message_en": "Cross-tenant metrics snapshot generated successfully.",
                "message_bn": "ক্রস-টেন্যান্ট মেট্রিক্স স্ন্যাপশট সফলভাবে তৈরি হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="publish")
    def publish(self, request, pk=None):
        self._require_superadmin(request)
        try:
            snapshot = CrossTenantMetricsService.publish_snapshot(snapshot_id=pk, operator_user=request.user)
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Unable to publish metrics snapshot.",
                    "message_bn": "মেট্রিক্স স্ন্যাপশট প্রকাশ করতে ব্যর্থ হয়েছে।",
                }
            )

        serializer = CrossTenantMetricsSnapshotSerializer(snapshot)
        return Response(
            {
                "success": True,
                "message_en": "Cross-tenant metrics snapshot published successfully.",
                "message_bn": "ক্রস-টেন্যান্ট মেট্রিক্স স্ন্যাপশট সফলভাবে প্রকাশিত হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None):
        self._require_superadmin(request)
        try:
            snapshot = CrossTenantMetricsService.archive_snapshot(snapshot_id=pk, operator_user=request.user)
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Unable to archive metrics snapshot.",
                    "message_bn": "মেট্রিক্স স্ন্যাপশট আর্কাইভ করতে ব্যর্থ হয়েছে।",
                }
            )

        serializer = CrossTenantMetricsSnapshotSerializer(snapshot)
        return Response(
            {
                "success": True,
                "message_en": "Cross-tenant metrics snapshot archived.",
                "message_bn": "ক্রস-টেন্যান্ট মেট্রিক্স স্ন্যাপশট আর্কাইভ করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )


class TenantPlatformBenchmarksViewSet(viewsets.ViewSet):
    """
    Tenant endpoint for accessing privacy-safe comparative benchmarks.
    Pharmacy Owners / Managers can compare their performance against platform averages.
    No competitor or patient microdata is ever accessible.
    Mounted under: /api/v2/analytics/platform-benchmarks/
    """

    permission_classes = [IsAuthenticated]

    def list(self, request):
        user = request.user
        role = str(getattr(user, "role", "")).upper()
        is_super = getattr(user, "is_superuser", False) or role in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]

        if not is_super and role not in ["PHARMACY_OWNER", "STORE_MANAGER", "ADMIN"]:
            raise PermissionDenied(
                detail={
                    "error_code": "BENCHMARK_ACCESS_DENIED",
                    "message_en": "Only Pharmacy Owners and Managers are permitted to view platform performance benchmarks.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও ম্যানেজাররা প্ল্যাটফর্ম পারফরম্যান্স বেঞ্চমার্ক দেখতে পারবেন।",
                }
            )

        user_tenant = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        user_tenant_id = getattr(user_tenant, "tenant_id", str(user_tenant or ""))

        requested_tenant_id = request.query_params.get("tenant_id") or user_tenant_id
        if not is_super and requested_tenant_id != user_tenant_id:
            raise PermissionDenied(
                detail={
                    "error_code": "CROSS_TENANT_ACCESS_FORBIDDEN",
                    "message_en": "Cross-tenant isolation violation. You cannot view benchmarks for another pharmacy.",
                    "message_bn": "ক্রস-টেন্যান্ট বিধিনিষেধ: আপনি অন্য ফার্মেসির বেঞ্চমার্ক তথ্য দেখতে পারবেন না।",
                }
            )

        try:
            benchmark_data = CrossTenantMetricsService.get_tenant_benchmark(
                tenant_id=requested_tenant_id,
                requesting_user=user,
            )
        except DjangoValidationError as exc:
            field_errors = getattr(exc, "message_dict", {"error": exc.messages})
            raise ValidationError(
                detail={
                    "error_code": "ERR_VALIDATION_FAILED",
                    "field_errors": field_errors,
                    "message_en": "Failed to retrieve platform benchmarks.",
                    "message_bn": "প্ল্যাটফর্ম বেঞ্চমার্ক তথ্য লোড করতে ব্যর্থ হয়েছে।",
                }
            )

        return Response(
            {
                "success": True,
                "data": benchmark_data,
            },
            status=status.HTTP_200_OK,
        )
