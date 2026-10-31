"""
apps.file_storage.views
Architecture Decision: ADR-0051
REST ViewSet for Secure Private File Storage, Antivirus Scanning, and Expiring Download URLs.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from django.http import FileResponse, Http404
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.audit_logging.models import AuditLogEntry
from apps.core.errors import (
    CrossTenantViolationError,
    FileQuarantinedError,
    InvalidFileTypeError,
    PermissionDeniedError,
)
from apps.core.models import Branch, Tenant
from apps.core.responses import StandardResponseMixin
from apps.file_storage.antivirus import AntivirusPolicyEngine
from apps.file_storage.models import FileCategory, SecureFileRecord
from apps.file_storage.serializers import (
    SecureFileRecordSerializer,
    SecureFileUploadSerializer,
    SignedUrlRequestSerializer,
)
from apps.file_storage.storage_engine import PrivateStorageEngine


class IsFileStorageAuthorized(permissions.BasePermission):
    """
    Evaluates category-specific RBAC rules conforming to ADR-0005 & ADR-0051.
    Strictly enforces Zero-PHI protection against Platform SuperAdmins and Cashiers.
    """

    ALLOWED_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "STAFF_PHARMACIST",
        "CASHIER",
        "FINANCE_OFFICER",
        "AUDITOR",
        "SUPERADMIN",
    }

    @classmethod
    def evaluate_category_access(cls, role: str, category: str, is_download_or_url: bool = False) -> bool:
        role = role.upper()

        # Zero PHI Shield: SuperAdmins are strictly blocked from patient prescription attachments
        if category == FileCategory.PRESCRIPTION_IMAGE:
            if role in ("SUPERADMIN", "PLATFORM_OPERATOR"):
                return False
            if role == "CASHIER":
                return False
            if role in ("PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "AUDITOR"):
                return True
            return False

        if category == FileCategory.SALES_INVOICE_PDF:
            if role in ("PHARMACY_OWNER", "BRANCH_MANAGER", "FINANCE_OFFICER", "CASHIER", "AUDITOR"):
                return True
            return False

        if category == FileCategory.DGDA_COMPLIANCE:
            if role in ("PHARMACY_OWNER", "BRANCH_MANAGER", "AUDITOR", "SUPERADMIN"):
                return True
            return False

        if category == FileCategory.PURCHASE_ORDER:
            if role in ("PHARMACY_OWNER", "BRANCH_MANAGER", "FINANCE_OFFICER", "STAFF_PHARMACIST", "AUDITOR"):
                return True
            return False

        # Default general attachments
        return role in ("PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "AUDITOR")

    def has_permission(self, request, view) -> bool:
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()

        if getattr(request.user, "is_superuser", False):
            return True

        return role in self.ALLOWED_ROLES


class SecureFileStorageViewSet(viewsets.ModelViewSet, StandardResponseMixin):
    """
    Private object storage controller with antivirus policy checks,
    tenant partition boundaries, and signed expiring download URLs.
    """

    queryset = SecureFileRecord.objects.all()
    serializer_class = SecureFileRecordSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [permissions.AllowAny]

    def _resolve_caller_role(self, request) -> str:
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()
        if not role and getattr(request.user, "is_superuser", False):
            role = "SUPERADMIN"
        return role or "GUEST"

    def _resolve_tenant_id(self, request) -> str:
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id and hasattr(request.user, "tenant_id"):
            tenant_id = getattr(request.user, "tenant_id")
        if not tenant_id:
            tenant_id = request.META.get("HTTP_X_TENANT_ID")
        return tenant_id or "tenant-default"

    def get_queryset(self):
        qs = super().get_queryset().filter(is_archived=False)
        tenant_id = self._resolve_tenant_id(self.request)

        # Cross-Tenant Boundary
        query_tenant = self.request.query_params.get("tenant_id")
        if query_tenant and query_tenant != tenant_id and not getattr(self.request.user, "is_superuser", False):
            raise CrossTenantViolationError(
                message_en=f"Cross-tenant file inspection denied for tenant '{query_tenant}'.",
                message_bn=f"অন্য ফার্মেসির ফাইলে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ। টেন্যান্ট '{query_tenant}'-এর ফাইল দেখা যাবে না।",
            )

        qs = qs.filter(tenant_id=tenant_id)

        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)

        scan_status = self.request.query_params.get("scan_status")
        if scan_status:
            qs = qs.filter(scan_status=scan_status)

        return qs

    def create(self, request, *args: Any, **kwargs: Any) -> Response:
        """
        Uploads a new file, runs multi-vector antivirus inspection,
        stores clean files in private storage, and isolates infected files.
        """
        serializer = SecureFileUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response(
                code="ERR_VALIDATION_FAILED",
                message_en="File upload validation failed.",
                message_bn="ফাইল আপলোড তথ্যের মান সঠিক নয়।",
                field_errors=serializer.errors,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        uploaded_file = serializer.validated_data["file"]
        category = serializer.validated_data["category"]
        branch_id = serializer.validated_data.get("branch_id")

        role = self._resolve_caller_role(request)
        tenant_id = self._resolve_tenant_id(request)

        # 1. Scoped Category RBAC Check
        if not IsFileStorageAuthorized.evaluate_category_access(role, category):
            raise PermissionDeniedError(
                message_en=f"Role '{role}' is forbidden from uploading or viewing files in category '{category}'.",
                message_bn=f"'{role}' পদবীর জন্য '{category}' ক্যাটাগরির ফাইল আপলোড বা দেখার অনুমতি নেই।",
            )

        file_content = uploaded_file.read()
        checksum_sha256 = hashlib.sha256(file_content).hexdigest()
        file_id = uuid.uuid4()
        original_filename = uploaded_file.name

        # 2. Antivirus & Magic Header Policy Scan
        declared_mime = uploaded_file.content_type or "application/octet-stream"
        scan_result = AntivirusPolicyEngine.scan_file(
            filename=original_filename,
            content=file_content,
            category=category,
            declared_mime=declared_mime,
        )

        # If malicious binary or format violation
        if not scan_result.is_clean and scan_result.scan_status == "QUARANTINED" and "Executable" in scan_result.threat_name:
            raise InvalidFileTypeError(
                message_en=f"Executable binary detected ({scan_result.threat_name}). Upload rejected by antivirus policy.",
                message_bn=f"এক্সিকিউটেবল বাইনারি শনাক্ত হয়েছে ({scan_result.threat_name})। অ্যান্টিভাইরাস পলিসি দ্বারা আপলোড বাতিল করা হলো।",
            )

        # 3. Store file securely
        rel_storage_path = PrivateStorageEngine.store_file(
            tenant_id=tenant_id,
            category=category,
            file_id=str(file_id),
            filename=original_filename,
            content=file_content,
        )

        # 4. Resolve Tenant and Branch Foreign Keys
        tenant_obj, _ = Tenant.objects.get_or_create(
            tenant_id=tenant_id,
            defaults={"name": f"Tenant {tenant_id}", "subdomain": tenant_id, "is_active": True},
        )
        branch_obj = None
        if branch_id:
            branch_obj = Branch.objects.filter(branch_id=branch_id, tenant=tenant_obj).first()

        record = SecureFileRecord.objects.create(
            file_id=file_id,
            tenant=tenant_obj,
            branch=branch_obj,
            file_name=original_filename,
            category=category,
            mime_type=scan_result.detected_mime,
            file_size_bytes=len(file_content),
            checksum_sha256=checksum_sha256,
            storage_path=rel_storage_path,
            scan_status=scan_result.scan_status,
            is_quarantined=not scan_result.is_clean,
            quarantine_reason=scan_result.quarantine_reason,
            scan_metadata={
                "provider": scan_result.scan_provider,
                "threat": scan_result.threat_name,
                "is_clean": scan_result.is_clean,
            },
            uploaded_by=request.user if request.user and request.user.is_authenticated else None,
        )

        # 5. Audit Trail Event
        audit_action = "FILE_MALWARE_QUARANTINED" if record.is_quarantined else "FILE_UPLOAD_COMPLETED"
        AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=str(getattr(request.user, "user_id", request.user.pk)) if request.user and request.user.is_authenticated else "sys-uploader",
            actor_role=role,
            action=audit_action,
            resource_type="SECURE_FILE",
            resource_id=str(record.file_id),
            request_id=request.META.get("HTTP_X_REQUEST_ID", f"req-{uuid.uuid4()}"),
            after_state={
                "file_name": record.file_name,
                "category": record.category,
                "scan_status": record.scan_status,
                "is_quarantined": record.is_quarantined,
            },
        )

        response_data = SecureFileRecordSerializer(record, context={"request": request}).data
        return self.success_response(
            data=response_data,
            code="FILE_UPLOAD_SUCCESS" if not record.is_quarantined else "FILE_UPLOAD_QUARANTINED",
            message_en="File uploaded and scanned successfully." if not record.is_quarantined else "File uploaded but quarantined due to antivirus detection.",
            message_bn="ফাইলটি সফলভাবে আপলোড ও স্ক্যান করা হয়েছে।" if not record.is_quarantined else "ফাইলটি আপলোড হয়েছে তবে অ্যান্টিভাইরাস নীতিমালার কারণে কোয়ারেন্টাইন করা হয়েছে।",
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="signed-url")
    def generate_signed_url(self, request, pk=None):
        """Generates a cryptographically signed expiring download URL."""
        record = self.get_object()
        role = self._resolve_caller_role(request)
        tenant_id = self._resolve_tenant_id(request)

        # Cross Tenant Check
        if record.tenant_id != tenant_id and not getattr(request.user, "is_superuser", False):
            raise CrossTenantViolationError(
                message_en="Cross-tenant signed URL generation forbidden.",
                message_bn="অন্য ফার্মেসির ফাইলের জন্য স্বাক্ষরিত লিঙ্ক তৈরি নিষিদ্ধ।",
            )

        # Category RBAC Check
        if not IsFileStorageAuthorized.evaluate_category_access(role, record.category):
            raise PermissionDeniedError(
                message_en=f"Role '{role}' is forbidden from generating download URLs for '{record.category}'.",
                message_bn=f"'{role}' পদবীর জন্য '{record.category}' ক্যাটাগরির ডাউনলোড লিঙ্ক পাওয়ার অনুমতি নেই।",
            )

        # Quarantined Check
        if record.is_quarantined:
            raise FileQuarantinedError(
                message_en=f"Cannot generate download URL: File '{record.file_name}' is quarantined ({record.quarantine_reason}).",
                message_bn=f"ডাউনলোড লিঙ্ক তৈরি করা সম্ভব নয়: ফাইলটি কোয়ারেন্টাইন করা হয়েছে ({record.quarantine_reason})।",
            )

        serializer = SignedUrlRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ttl = serializer.validated_data.get("ttl_seconds", 900)

        token, expires_at = PrivateStorageEngine.generate_signed_token(
            file_id=str(record.file_id),
            tenant_id=record.tenant_id,
            ttl_seconds=ttl,
        )

        download_url = request.build_absolute_uri(
            f"/api/v2/storage/download/{record.file_id}/?token={token}&tenant_id={record.tenant_id}"
        )

        # Audit Event
        AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            user_id=str(getattr(request.user, "user_id", request.user.pk)) if request.user and request.user.is_authenticated else "sys-token-gen",
            actor_role=role,
            action="FILE_SIGNED_URL_GENERATED",
            resource_type="SECURE_FILE",
            resource_id=str(record.file_id),
            request_id=request.META.get("HTTP_X_REQUEST_ID", f"req-{uuid.uuid4()}"),
            after_state={"expires_at": expires_at.isoformat(), "ttl_seconds": ttl},
        )

        response_payload = {
            "file_id": record.file_id,
            "download_url": download_url,
            "token": token,
            "expires_at": expires_at,
            "ttl_seconds": ttl,
        }
        return self.success_response(
            data=response_payload,
            code="SIGNED_URL_GENERATED",
            message_en="Signed expiring download URL generated successfully.",
            message_bn="স্বাক্ষরিত ডাউনলোডের লিঙ্ক সফলভাবে তৈরি করা হয়েছে।",
        )

    def retrieve(self, request, *args: Any, **kwargs: Any) -> Response:
        """Retrieves file metadata with category RBAC check."""
        record = self.get_object()
        role = self._resolve_caller_role(request)

        if not IsFileStorageAuthorized.evaluate_category_access(role, record.category):
            raise PermissionDeniedError(
                message_en=f"Role '{role}' is forbidden from inspecting '{record.category}'.",
                message_bn=f"'{role}' পদবীর জন্য '{record.category}' ক্যাটাগরির তথ্য দেখার অনুমতি নেই।",
            )

        serializer = self.get_serializer(record)
        return self.success_response(
            data=serializer.data,
            code="FILE_RECORD_RETRIEVED",
            message_en="File metadata retrieved successfully.",
            message_bn="ফাইলের বিবরণ সফলভাবে লোড হয়েছে।",
        )

    def destroy(self, request, *args: Any, **kwargs: Any) -> Response:
        """Soft-archives a file record with audit tracking."""
        record = self.get_object()
        role = self._resolve_caller_role(request)
        tenant_id = self._resolve_tenant_id(request)

        if role not in ("PHARMACY_OWNER", "BRANCH_MANAGER", "SUPERADMIN"):
            raise PermissionDeniedError(
                message_en="Only pharmacy owners or managers may archive stored files.",
                message_bn="শুধুমাত্র ফার্মেসি মালিক বা ম্যানেজার ফাইল আর্কাইভ করতে পারেন।",
            )

        record.is_archived = True
        record.save(update_fields=["is_archived", "updated_at"])

        AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            user_id=str(getattr(request.user, "user_id", request.user.pk)) if request.user and request.user.is_authenticated else "sys-archiver",
            actor_role=role,
            action="FILE_PURGE_ARCHIVED",
            resource_type="SECURE_FILE",
            resource_id=str(record.file_id),
            request_id=request.META.get("HTTP_X_REQUEST_ID", f"req-{uuid.uuid4()}"),
            after_state={"is_archived": True},
        )

        return self.success_response(
            data={"file_id": record.file_id, "is_archived": True},
            code="FILE_ARCHIVED",
            message_en="File record archived successfully.",
            message_bn="ফাইলটি সফলভাবে আর্কাইভ করা হয়েছে।",
        )


class SecureFileDownloadView(viewsets.ViewSet):
    """
    Direct streaming endpoint for cryptographically signed expiring URLs.
    Route: /api/v2/storage/download/{file_id}/
    """

    permission_classes = [permissions.AllowAny]

    def retrieve(self, request, pk=None):
        file_id = pk
        token = request.query_params.get("token")
        tenant_id = request.query_params.get("tenant_id")

        if not token or not tenant_id:
            raise PermissionDeniedError(
                message_en="Missing required signed token or tenant_id parameters.",
                message_bn="স্বাক্ষরিত টোকেন বা টেন্যান্ট আইডি পাওয়া যায়নি।",
            )

        try:
            record = SecureFileRecord.objects.get(file_id=file_id, is_archived=False)
        except (SecureFileRecord.DoesNotExist, ValueError):
            raise Http404("Stored file not found.")

        # 1. Tenant match validation
        if record.tenant_id != tenant_id:
            raise CrossTenantViolationError(
                message_en="Token tenant does not match stored file tenant.",
                message_bn="টোকেনের টেন্যান্ট এবং ফাইলের টেন্যান্টের মধ্যে অমিল রয়েছে।",
            )

        # 2. Cryptographic token and expiry validation
        PrivateStorageEngine.verify_signed_token(
            file_id=str(record.file_id),
            tenant_id=record.tenant_id,
            token=token,
        )

        # 3. Quarantine Guard
        if record.is_quarantined:
            raise FileQuarantinedError(
                message_en=f"File download blocked: Attachment is quarantined ({record.quarantine_reason}).",
                message_bn=f"ফাইল ডাউনলোড ব্লক করা হয়েছে: ফাইলটি কোয়ারেন্টাইন অবস্থায় আছে ({record.quarantine_reason})।",
            )

        abs_path = PrivateStorageEngine.get_absolute_path(record.storage_path)
        if not abs_path.exists():
            raise Http404("Physical file missing from private storage.")

        # 4. Audit Log Event
        AuditLogEntry.objects.create(
            tenant_id=record.tenant_id,
            user_id="signed-token-downloader",
            actor_role="TOKEN_CONSUMER",
            action="FILE_DOWNLOAD_ACCESSED",
            resource_type="SECURE_FILE",
            resource_id=str(record.file_id),
            request_id=request.META.get("HTTP_X_REQUEST_ID", f"req-{uuid.uuid4()}"),
            after_state={"file_name": record.file_name, "file_size_bytes": record.file_size_bytes},
        )

        # 5. Stream response with security headers
        response = FileResponse(open(abs_path, "rb"), content_type=record.mime_type)
        response["Content-Disposition"] = f'inline; filename="{record.file_name}"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
