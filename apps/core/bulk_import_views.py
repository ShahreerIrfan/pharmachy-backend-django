"""
backend/apps/core/bulk_import_views.py
Architecture Decision: ADR-0132 (P132).
REST API ViewSet for Bulk Medicine Catalog Import Wizard:
File Upload, Field Mapping, Pre-Flight Validation, Atomic Commit, and Single-Click Rollback.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.bulk_import_service import MedicineBulkImportService
from apps.core.models import MedicineImportSession, Tenant, TenantLifecycleStatus
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.bulk_import.views")


class MedicineBulkImportViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for Medicine Catalog Bulk Import Wizard.
    Enforces RBAC (Owner/Manager/Superadmin only), tenant isolation,
    multi-stage preview, mapping, pre-flight validation, and rollback safety.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _require_import_role(self, request):
        """Cashiers and unauthorized roles are denied from catalog bulk import operations."""
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")

        if self._is_platform_operator(user):
            return

        role = str(getattr(user, "role", "")).upper().strip()
        allowed = {
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            "OWNER",
            "MANAGER",
            "PHARMACIST",
        }
        if role not in allowed:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Cashiers cannot perform bulk catalog import operations. Contact store owner.",
                    "message_bn": "ক্যাশিয়াররা বাল্ক মেডিসিন ইমপোর্ট করতে পারবেন না। স্টোর মালিকের সাথে যোগাযোগ করুন।",
                }
            )

    def _check_tenant_status(self, request):
        """Guard against suspended tenants accessing import services."""
        user = getattr(request, "user", None)
        if self._is_platform_operator(user):
            return

        tenant_id = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        if tenant_id:
            tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
            if tenant and tenant.status == TenantLifecycleStatus.SUSPENDED:
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_TENANT_SUSPENDED",
                        "message_en": "Tenant account is suspended. Bulk catalog import access denied.",
                        "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত থাকায় বাল্ক ইমপোর্ট সুবিধা বন্ধ রয়েছে।",
                    }
                )

    def _get_authorized_session(self, request, session_id: str) -> MedicineImportSession:
        """Retrieves import session and enforces cross-tenant boundary."""
        session = MedicineImportSession.objects.filter(session_id=str(session_id).strip()).first()
        if not session:
            raise ValidationError({"session_id": f"Import session '{session_id}' not found."})

        user = getattr(request, "user", None)
        if self._is_platform_operator(user):
            return session

        user_tenant = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        if not user_tenant or str(session.tenant_id).strip() != str(user_tenant).strip():
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                    "message_en": f"Cannot access or mutate import session of foreign tenant '{session.tenant_id}'.",
                    "message_bn": "ভিন্ন ফার্মেসির ইমপোর্ট সেশন অ্যাক্সেস বা পরিবর্তন করা সম্পূর্ণ নিষিদ্ধ।",
                }
            )

        return session

    def list(self, request) -> Response:
        """GET /api/v2/catalog/bulk-import/"""
        self._require_import_role(request)
        self._check_tenant_status(request)

        user = request.user
        if self._is_platform_operator(user):
            qs = MedicineImportSession.objects.all()
        else:
            tenant_id = getattr(user, "tenant_id", None) or "platform"
            qs = MedicineImportSession.objects.filter(tenant_id=tenant_id)

        serialized = [MedicineBulkImportService.serialize_session(s) for s in qs]
        return Response({"count": len(serialized), "results": serialized}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/catalog/bulk-import/{session_id}/"""
        self._require_import_role(request)
        self._check_tenant_status(request)

        session = self._get_authorized_session(request, pk)
        return Response(MedicineBulkImportService.serialize_session(session), status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="upload")
    def upload_csv(self, request) -> Response:
        """
        POST /api/v2/catalog/bulk-import/upload/
        Uploads and parses CSV content, generates preview, and auto-suggests field mappings.
        """
        self._require_import_role(request)
        self._check_tenant_status(request)

        data = request.data or {}
        file_name = data.get("file_name", "upload.csv")
        csv_content = data.get("csv_content", "")
        duplicate_policy = data.get("duplicate_policy", "SKIP")

        user = request.user
        target_tenant = getattr(user, "tenant_id", "platform") if not self._is_platform_operator(user) else "platform"

        try:
            session = MedicineBulkImportService.create_session_from_csv(
                file_name=file_name,
                csv_content=csv_content,
                actor=user,
                target_tenant_id=target_tenant,
                duplicate_policy=duplicate_policy,
            )
            return Response(
                MedicineBulkImportService.serialize_session(session),
                status=status.HTTP_201_CREATED,
            )
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))
        except Exception as exc:
            raise ValidationError(str(exc))

    @action(detail=True, methods=["post"], url_path="map-and-validate")
    def map_and_validate(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/bulk-import/{session_id}/map-and-validate/
        Applies column mapping and performs dry-run pre-flight validation.
        """
        self._require_import_role(request)
        self._check_tenant_status(request)

        session = self._get_authorized_session(request, pk)
        data = request.data or {}
        field_mapping = data.get("field_mapping")
        if not isinstance(field_mapping, dict) or not field_mapping:
            raise ValidationError({"field_mapping": "field_mapping dictionary is required."})

        duplicate_policy = data.get("duplicate_policy")

        try:
            updated_session = MedicineBulkImportService.apply_mapping_and_validate(
                session=session,
                field_mapping=field_mapping,
                duplicate_policy=duplicate_policy,
            )
            return Response(
                MedicineBulkImportService.serialize_session(updated_session),
                status=status.HTTP_200_OK,
            )
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))
        except Exception as exc:
            raise ValidationError(str(exc))

    @action(detail=True, methods=["post"], url_path="commit")
    def commit(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/bulk-import/{session_id}/commit/
        Commits validated records into the medicine catalog in an atomic transaction.
        """
        self._require_import_role(request)
        self._check_tenant_status(request)

        session = self._get_authorized_session(request, pk)
        try:
            committed_summary = MedicineBulkImportService.commit_import(session, request.user)
            return Response(committed_summary, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))
        except Exception as exc:
            raise ValidationError(str(exc))

    @action(detail=True, methods=["post"], url_path="rollback")
    def rollback(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/bulk-import/{session_id}/rollback/
        Reverses the import, cleanly deleting created products.
        """
        self._require_import_role(request)
        self._check_tenant_status(request)

        session = self._get_authorized_session(request, pk)
        try:
            rollback_summary = MedicineBulkImportService.rollback_import(session, request.user)
            return Response(rollback_summary, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise ValidationError(str(exc))
        except Exception as exc:
            raise ValidationError(str(exc))
