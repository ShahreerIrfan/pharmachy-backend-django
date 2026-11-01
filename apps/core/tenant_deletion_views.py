"""
backend/apps/core/tenant_deletion_views.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0074
Controlled Tenant Deletion Assessment & Purge Administration ViewSets.
Provides:
1. TenantDeletionRequestViewSet: Tenant-scoped submission, hold assessment visibility, and cancellation.
2. PlatformDeletionAssessmentViewSet: Super Admin statutory retention review, rejection, and guarded irreversible purge execution.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import TenantDeletionRequest
from apps.core.pagination import SafeAdaptivePagination
from apps.core.serializers import TenantDeletionRequestSerializer
from apps.core.tenant_deletion_service import TenantDeletionService


class TenantDeletionRequestViewSet(viewsets.ModelViewSet):
    """
    Tenant-Scoped Deletion Request API.
    Restricted to verified Pharmacy Owners.
    Store Cashiers, Dispensary Staff, and Branch Managers are blocked (403).
    """

    serializer_class = TenantDeletionRequestSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = SafeAdaptivePagination
    http_method_names = ["get", "post", "head", "options"]

    def _get_caller_tenant_id(self) -> str:
        user = getattr(self.request, "user", None)
        user_tenant = getattr(user, "tenant_id", None) or getattr(self.request, "tenant_id", None)
        is_platform = getattr(user, "is_superuser", False) or str(getattr(user, "role", "")).upper() in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]

        query_tenant = self.request.query_params.get("tenant_id")
        if is_platform:
            return str(query_tenant or user_tenant or "default-tenant").strip()

        if not user_tenant:
            raise PermissionDenied("User is not associated with an authorized pharmacy tenant.")

        if query_tenant and str(query_tenant).strip() != str(user_tenant).strip():
            raise CrossTenantViolationError(
                message_en=f"Cross-tenant access violation: Cannot inspect deletion requests for foreign tenant '{query_tenant}'.",
                message_bn="ভিন্ন ফার্মেসির ডিলিশন রিকোয়েস্ট অ্যাক্সেস করা সম্পূর্ণ নিষিদ্ধ।",
            )
        return str(user_tenant).strip()

    def _require_owner_or_platform(self):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")

        role = str(getattr(user, "role", "")).upper()
        is_allowed = getattr(user, "is_superuser", False) or role in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
            "PHARMACY_OWNER",
            "OWNER",
        ]
        if not is_allowed:
            raise PermissionDenied(
                detail={
                    "error_code": "TENANT_DELETION_PERMISSION_DENIED",
                    "message_en": (
                        "Tenant deletion requests require Pharmacy Owner or Platform Administrator privileges. "
                        "Store cashiers and dispensary staff cannot submit or cancel deletion requests."
                    ),
                    "message_bn": "টেন্যান্ট ডিলিশন রিকোয়েস্ট শুধুমাত্র ফার্মেসি ওনার বা প্ল্যাটফর্ম অ্যাডমিনের জন্য অনুমোদিত।",
                }
            )

    def get_queryset(self):
        self._require_owner_or_platform()
        tenant_id = self._get_caller_tenant_id()
        return TenantDeletionRequest.objects.filter(tenant_id=tenant_id).order_by("-created_at")

    def create(self, request, *args, **kwargs):
        self._require_owner_or_platform()
        tenant_id = self._get_caller_tenant_id()
        data = request.data or {}
        reason = str(data.get("reason", "")).strip()

        try:
            del_req = TenantDeletionService.submit_deletion_request(
                tenant_id=tenant_id,
                requested_by=request.user,
                reason=reason,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(del_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DELETION_REQUEST_SUBMITTED",
                "message_en": f"Deletion request '{del_req.request_id}' submitted and assessed.",
                "message_bn": f"ডিলিশন রিকোয়েস্ট '{del_req.request_id}' গৃহীত ও যাচাই সম্পন্ন হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        self._require_owner_or_platform()
        data = request.data or {}
        notes = str(data.get("notes", ""))

        try:
            del_req = TenantDeletionService.cancel_deletion_request(
                request_id=pk,
                cancelled_by=request.user,
                notes=notes,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(del_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DELETION_CANCELLED",
                "message_en": f"Deletion request '{pk}' has been withdrawn.",
                "message_bn": f"ডিলিশন রিকোয়েস্ট '{pk}' প্রত্যাহার করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )


class PlatformDeletionAssessmentViewSet(viewsets.ModelViewSet):
    """
    Platform Super Admin Deletion Assessment & Purge Console.
    Inspects active statutory retention holds across tenants, evaluates compliance,
    and executes guarded irreversible purges with multi-step verification.
    """

    queryset = TenantDeletionRequest.objects.all().select_related("tenant", "requested_by", "assessed_by")
    serializer_class = TenantDeletionRequestSerializer
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
                    "message_en": "Only platform administrators can govern tenant deletion assessments.",
                    "message_bn": "শুধুমাত্র প্ল্যাটফর্ম অ্যাডমিন টেন্যান্ট ডিলিশন মূল্যায়ন ও অনুমোদন করতে পারেন।",
                }
            )

    def get_queryset(self):
        self._require_platform_operator(self.request)
        return super().get_queryset().order_by("-created_at")

    @action(detail=True, methods=["post"], url_path="assess")
    def assess(self, request, pk=None):
        self._require_platform_operator(request)
        try:
            del_req = TenantDeletionService.assess_retention_holds(
                request_id=pk,
                assessed_by=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(del_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DELETION_HOLDS_ASSESSED",
                "message_en": f"Retention holds assessed for request '{pk}'. Status: {del_req.status}.",
                "message_bn": f"রিকোয়েস্ট '{pk}'-এর জন্য রিটেনশন হোল্ড যাচাই সম্পন্ন। স্ট্যাটাস: {del_req.status}।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        self._require_platform_operator(request)
        data = request.data or {}
        reason = data.get("reason", "")

        try:
            del_req = TenantDeletionService.reject_deletion_request(
                request_id=pk,
                rejected_by=request.user,
                reason=reason,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(del_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DELETION_REJECTED",
                "message_en": f"Deletion request '{pk}' rejected successfully.",
                "message_bn": f"ডিলিশন রিকোয়েস্ট '{pk}' বাতিল করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="purge")
    def purge(self, request, pk=None):
        self._require_platform_operator(request)
        data = request.data or {}
        code = data.get("confirmation_code", "")
        text = data.get("confirmation_text", "")

        try:
            del_req = TenantDeletionService.execute_purge(
                request_id=pk,
                executed_by=request.user,
                confirmation_code=code,
                confirmation_text=text,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(del_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_PERMANENTLY_PURGED",
                "message_en": f"Tenant deletion request '{pk}' executed and tenant permanently purged.",
                "message_bn": f"টেন্যান্ট ডিলিশন রিকোয়েস্ট '{pk}' কার্যকর এবং টেন্যান্ট স্থায়ীভাবে অপসারিত।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )
