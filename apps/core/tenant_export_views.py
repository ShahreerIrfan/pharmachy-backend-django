"""
backend/apps/core/tenant_export_views.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0073
Controlled Tenant Data Export Administration ViewSets.
Provides:
1. TenantDataExportViewSet: Tenant-scoped request, status inspection, and secure tokenized download.
2. PlatformDataExportAdminViewSet: Super Admin centralized governance, approval, rejection, and revocation.
"""

from __future__ import annotations

import json

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import TenantDataExportRequest, TenantDataExportStatus
from apps.core.pagination import SafeAdaptivePagination
from apps.core.serializers import TenantDataExportRequestSerializer
from apps.core.tenant_export_service import TenantExportService


class TenantDataExportViewSet(viewsets.ModelViewSet):
    """
    Tenant-Scoped Data Export API.
    Authorized for Pharmacy Owners and Platform Operators.
    Cashiers and Pharmacists are strictly restricted.
    """

    serializer_class = TenantDataExportRequestSerializer
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
                message_en=f"Cross-tenant access violation: Cannot inspect exports for foreign tenant '{query_tenant}'.",
                message_bn="ভিন্ন ফার্মেসির ডাটা এক্সপোর্ট অ্যাক্সেস করা সম্পূর্ণ নিষিদ্ধ।",
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
                    "error_code": "TENANT_EXPORT_PERMISSION_DENIED",
                    "message_en": (
                        "Tenant data export requests require Pharmacy Owner or Platform Administrator privileges. "
                        "Store cashiers and dispensary staff cannot request or download tenant exports."
                    ),
                    "message_bn": "টেন্যান্ট ডাটা এক্সপোর্ট শুধুমাত্র ফার্মেসি ওনার বা প্ল্যাটফর্ম অ্যাডমিনের জন্য অনুমোদিত।",
                }
            )

    def get_queryset(self):
        self._require_owner_or_platform()
        tenant_id = self._get_caller_tenant_id()
        return TenantDataExportRequest.objects.filter(tenant_id=tenant_id).order_by("-requested_at")

    def create(self, request, *args, **kwargs):
        self._require_owner_or_platform()
        tenant_id = self._get_caller_tenant_id()
        data = request.data or {}

        include_pii = bool(data.get("include_patient_pii", False))
        include_financials = bool(data.get("include_financials", True))
        include_prescriptions = bool(data.get("include_prescriptions", True))
        include_inventory = bool(data.get("include_inventory", True))
        file_format = str(data.get("file_format", "JSON")).upper()
        notes = str(data.get("notes", ""))

        try:
            export_req = TenantExportService.request_export(
                tenant_id=tenant_id,
                requested_by=request.user,
                include_patient_pii=include_pii,
                include_financials=include_financials,
                include_prescriptions=include_prescriptions,
                include_inventory=include_inventory,
                file_format=file_format,
                notes=notes,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(export_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DATA_EXPORT_REQUESTED",
                "message_en": f"Data export request '{export_req.export_id}' submitted and pending approval.",
                "message_bn": f"ডাটা এক্সপোর্ট রিকোয়েস্ট '{export_req.export_id}' অনুমোদনের জন্য অপেক্ষমাণ।",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, pk=None):
        self._require_owner_or_platform()
        token = request.query_params.get("token")
        if not token:
            raise ValidationError({"token": "Download token is mandatory."})

        try:
            export_req, meta = TenantExportService.verify_and_record_download(
                export_id=pk,
                token=token,
                caller=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        # Build download payload
        tenant = export_req.tenant
        payload = {
            "metadata": {
                "export_id": export_req.export_id,
                "tenant_id": tenant.tenant_id,
                "tenant_name": tenant.name,
                "include_patient_pii": export_req.include_patient_pii,
                "sha256_checksum": export_req.sha256_checksum,
                "download_count": export_req.download_count,
            },
            "status": "VALID_EXPORT_ARTIFACT",
        }
        json_bytes = json.dumps(payload, indent=2).encode("utf-8")

        response = HttpResponse(json_bytes, content_type="application/json; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{export_req.export_id}.json"'
        response["X-Export-SHA256"] = export_req.sha256_checksum
        response["X-Export-Download-Count"] = str(export_req.download_count)
        return response


class PlatformDataExportAdminViewSet(viewsets.ModelViewSet):
    """
    Platform Super Admin Central Governance ViewSet for Data Exports.
    Allows listing all tenant export requests, reviewing, approving, rejecting, and revoking.
    """

    queryset = TenantDataExportRequest.objects.all().select_related("tenant", "requested_by", "approved_by")
    serializer_class = TenantDataExportRequestSerializer
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
                    "message_en": "Only platform administrators can govern tenant data export approvals.",
                    "message_bn": "শুধুমাত্র প্ল্যাটফর্ম অ্যাডমিন ডাটা এক্সপোর্ট অনুমোদন বা বাতিল করতে পারেন।",
                }
            )

    def get_queryset(self):
        self._require_platform_operator(self.request)
        return super().get_queryset().order_by("-requested_at")

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        self._require_platform_operator(request)
        try:
            export_req = TenantExportService.approve_export(
                export_id=pk,
                approved_by=request.user,
                auto_generate=True,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(export_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DATA_EXPORT_APPROVED",
                "message_en": f"Export '{pk}' approved and generated successfully.",
                "message_bn": f"ডাটা এক্সপোর্ট '{pk}' অনুমোদিত ও প্রস্তুত করা হয়েছে।",
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
            export_req = TenantExportService.reject_export(
                export_id=pk,
                rejected_by=request.user,
                reason=reason,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        serializer = self.get_serializer(export_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DATA_EXPORT_REJECTED",
                "message_en": f"Export '{pk}' rejected successfully.",
                "message_bn": f"ডাটা এক্সপোর্ট '{pk}' বাতিল করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        self._require_platform_operator(request)
        export_req = TenantDataExportRequest.objects.filter(export_id=pk).first()
        if not export_req:
            raise ValidationError({"export_id": f"Export request '{pk}' not found."})

        export_req.status = TenantDataExportStatus.EXPIRED
        export_req.download_token = ""
        export_req.save(update_fields=["status", "download_token"])

        serializer = self.get_serializer(export_req)
        return Response(
            {
                "success": True,
                "code": "TENANT_DATA_EXPORT_REVOKED",
                "message_en": f"Export '{pk}' revoked and download token invalidated.",
                "message_bn": f"ডাটা এক্সপোর্ট '{pk}' প্রত্যাহার করা হয়েছে।",
                "data": serializer.data,
            },
            status=status.HTTP_200_OK,
        )
