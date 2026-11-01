"""
backend/apps/core/billing_report_views.py

Architecture Decision: ADR-0098
Phase 05: Basic/Standard/Pro Subscription & Billing (P098)
Requirement Trace: FR-044, FR-064, FR-066, FR-067, FR-074

DRF ViewSets for Platform and Tenant Billing Reports:
- PlatformBillingReportViewSet: SuperAdmin macro reports (MRR, Churn, Collections, Aging, Audit, Snapshots).
- TenantBillingReportViewSet: Pharmacy Owner/Admin tenant-isolated billing reports.
- Enforces strict cross-tenant isolation and role-based access control (RBAC).
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.billing_report_service import PlatformBillingReportService
from apps.core.errors import CrossTenantViolationError
from apps.core.models import PlatformBillingReportSnapshot
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.reports")


class PlatformBillingReportViewSet(viewsets.ViewSet):
    """
    SuperAdmin and Platform Operator Macro Billing Reports ViewSet.
    Accessible exclusively to authenticated platform administrators.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_superadmin(self, request: Any) -> bool:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        role = str(getattr(user, "role", "")).upper()
        return role in [
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]

    def _require_superadmin(self, request: Any) -> None:
        if not self._is_superadmin(request):
            raise PermissionDenied(
                detail={
                    "error_code": "PLATFORM_ACCESS_DENIED",
                    "message_en": "Platform billing reports are strictly restricted to platform administrators.",
                    "message_bn": "প্ল্যাটফর্ম বিলিং রিপোর্ট শুধুমাত্র প্ল্যাটফর্ম অ্যাডমিনিস্ট্রেটরদের জন্য উন্মুক্ত।",
                }
            )

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request: Any) -> Response:
        """Consolidated executive billing dashboard (MRR, Churn, Collections, Outstanding)."""
        self._require_superadmin(request)
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        period = request.query_params.get("period", "MONTHLY")

        try:
            data = PlatformBillingReportService.get_full_summary(
                start_date=start_date,
                end_date=end_date,
                period=period,
                operator_user=request.user,
                save_snapshot=False,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "PLATFORM_BILLING_SUMMARY_RETRIEVED",
                "message_en": "Platform billing summary retrieved successfully.",
                "message_bn": "প্ল্যাটফর্ম সামগ্রিক বিলিং রিপোর্ট সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="mrr")
    def mrr(self, request: Any) -> Response:
        """Detailed MRR, ARR, ARPU, and plan tier breakdown."""
        self._require_superadmin(request)
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            data = PlatformBillingReportService.get_mrr_report(
                start_date=start_date,
                end_date=end_date,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "PLATFORM_MRR_REPORT_RETRIEVED",
                "message_en": "Platform MRR report retrieved successfully.",
                "message_bn": "প্ল্যাটফর্ম এমআরআর রিপোর্ট সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="churn")
    def churn(self, request: Any) -> Response:
        """Detailed churn rates, lost MRR, and attrition reasons."""
        self._require_superadmin(request)
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            data = PlatformBillingReportService.get_churn_report(
                start_date=start_date,
                end_date=end_date,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "PLATFORM_CHURN_REPORT_RETRIEVED",
                "message_en": "Platform churn report retrieved successfully.",
                "message_bn": "প্ল্যাটফর্ম চার্ন রিপোর্ট সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="collections")
    def collections(self, request: Any) -> Response:
        """Verified realized collections by payment rail."""
        self._require_superadmin(request)
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            data = PlatformBillingReportService.get_collections_report(
                start_date=start_date,
                end_date=end_date,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "PLATFORM_COLLECTIONS_REPORT_RETRIEVED",
                "message_en": "Platform collections report retrieved successfully.",
                "message_bn": "প্ল্যাটফর্ম পেমেন্ট কালেকশন রিপোর্ট সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="outstanding")
    def outstanding(self, request: Any) -> Response:
        """Accounts receivable, aging breakdown, and at-risk delinquent accounts."""
        self._require_superadmin(request)
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            data = PlatformBillingReportService.get_outstanding_report(
                start_date=start_date,
                end_date=end_date,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "PLATFORM_OUTSTANDING_REPORT_RETRIEVED",
                "message_en": "Platform outstanding invoices report retrieved successfully.",
                "message_bn": "প্ল্যাটফর্ম বকেয়া ইনভয়েস রিপোর্ট সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="subscription-audit")
    def subscription_audit(self, request: Any) -> Response:
        """Audited event stream of plan transitions, payments, and quota changes."""
        self._require_superadmin(request)
        tenant_id = request.query_params.get("tenant_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        event_type = request.query_params.get("event_type")
        limit = int(request.query_params.get("limit", 50))

        try:
            data = PlatformBillingReportService.get_subscription_audit_report(
                tenant_id=tenant_id,
                start_date=start_date,
                end_date=end_date,
                event_type=event_type,
                limit=limit,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "SUBSCRIPTION_AUDIT_REPORT_RETRIEVED",
                "message_en": "Subscription audit report retrieved successfully.",
                "message_bn": "সাবস্ক্রিপশন অডিট রিপোর্ট সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get", "post"], url_path="snapshots")
    def snapshots(self, request: Any) -> Response:
        """Create or list persistent historical billing report snapshots."""
        self._require_superadmin(request)
        if request.method == "POST":
            start_date = request.data.get("start_date")
            end_date = request.data.get("end_date")
            period = request.data.get("period", "MONTHLY")

            try:
                data = PlatformBillingReportService.get_full_summary(
                    start_date=start_date,
                    end_date=end_date,
                    period=period,
                    operator_user=request.user,
                    save_snapshot=True,
                )
            except DjangoValidationError as err:
                err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
                raise ValidationError(detail=err_dict)

            return Response(
                {
                    "success": True,
                    "code": "PLATFORM_BILLING_SNAPSHOT_CREATED",
                    "message_en": "Platform billing snapshot created and archived successfully.",
                    "message_bn": "প্ল্যাটফর্ম বিলিং রিপোর্ট স্ন্যাপশট সফলভাবে সংরক্ষিত হয়েছে।",
                    "data": data,
                },
                status=status.HTTP_201_CREATED,
            )
        else:
            snapshots = PlatformBillingReportSnapshot.objects.all().order_by("-created_at")[:50]
            items = []
            for s in snapshots:
                items.append(
                    {
                        "report_id": s.report_id,
                        "report_type": s.report_type,
                        "period": s.period,
                        "start_date": str(s.start_date),
                        "end_date": str(s.end_date),
                        "total_mrr_bdt": str(s.total_mrr_bdt),
                        "total_collected_bdt": str(s.total_collected_bdt),
                        "total_outstanding_bdt": str(s.total_outstanding_bdt),
                        "created_at": s.created_at.isoformat(),
                    }
                )
            return Response(
                {
                    "success": True,
                    "code": "PLATFORM_BILLING_SNAPSHOTS_RETRIEVED",
                    "data": {"count": len(items), "snapshots": items},
                },
                status=status.HTTP_200_OK,
            )


class TenantBillingReportViewSet(viewsets.ViewSet):
    """
    Pharmacy Owner and Manager Tenant-Isolated Billing Report ViewSet.
    Accessible only to authenticated owners and managers for their own pharmacy.
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

    def _require_owner_or_admin(self, user: Any) -> None:
        if self._is_platform_admin(user):
            return
        role = str(getattr(user, "role", "")).upper()
        if role not in ["PHARMACY_OWNER", "OWNER", "ADMIN", "PHARMACY_MANAGER"]:
            raise PermissionDenied(
                detail={
                    "error_code": "PERMISSION_DENIED",
                    "message_en": "Only pharmacy owners and administrators can access billing reports.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও অ্যাডমিনিস্ট্রেটরদের বিলিং রিপোর্ট দেখার অনুমতি আছে।",
                }
            )

    def _resolve_tenant_id(self, request: Any, target_tenant_id: str | None = None) -> str:
        user = request.user
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        if target_tenant_id:
            clean_target = str(target_tenant_id).strip()
            if not self._is_platform_admin(user) and user_tid and user_tid != clean_target:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access billing reports of tenant '{clean_target}'."
                )
            return clean_target

        if user_tid:
            return user_tid

        header_tid = str(request.headers.get("X-Tenant-ID", "")).strip()
        if header_tid:
            return header_tid

        raise ValidationError({"tenant_id": "Target tenant_id could not be resolved from authentication context."})

    @action(detail=False, methods=["get"], url_path="tenant")
    def tenant(self, request: Any) -> Response:
        """Retrieves tenant-isolated billing statement and invoice audit for authenticated pharmacy."""
        self._require_owner_or_admin(request.user)
        target_tid = request.query_params.get("tenant_id")
        resolved_tid = self._resolve_tenant_id(request, target_tid)

        try:
            data = PlatformBillingReportService.get_tenant_billing_report(
                tenant_id=resolved_tid,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "TENANT_BILLING_REPORT_RETRIEVED",
                "message_en": "Tenant billing report retrieved successfully.",
                "message_bn": "ফার্মেসির বিলিং স্টেটমেন্ট সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

