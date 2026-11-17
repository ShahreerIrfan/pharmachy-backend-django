"""
backend/apps/core/prescription_upload_service.py

Architecture Decision: ADR-0051 & ADR-0242 (P242).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Authoritative domain service for secure clinical prescription uploads:
- Private-by-default tenant-partitioned object storage
- Content inspection, MIME validation, and antivirus scanning
- Cryptographic SHA-256 integrity verification
- Statutory 5-year DGDA retention policy tracking
- Tamper-proof expiring HMAC-SHA256 signed download URLs
- Pharmacist clinical attachment verification sign-off
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    PrescriptionAttachment,
    PrescriptionRecord,
    StaffMember,
    Tenant,
)
from apps.file_storage.antivirus import AntivirusPolicyEngine
from apps.file_storage.models import FileCategory, ScanStatus, SecureFileRecord
from apps.file_storage.storage_engine import PrivateStorageEngine
from apps.identity.models import PharmacyUser

logger = logging.getLogger(__name__)


class PrescriptionUploadService:
    """
    Manages private, encrypted storage of prescription documents and images.
    Enforces MIME whitelist, virus quarantine, SHA-256 checksums, and signed expiring URLs.
    """

    ALLOWED_MIMES = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
    }
    ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
    MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per DGDA clinical image standard
    MIN_FILE_SIZE_BYTES = 100  # Minimum 100 bytes to prevent zero-byte files
    DEFAULT_RETENTION_DAYS = 1825  # Statutory 5-year retention policy (365 * 5)

    ALLOWED_UPLOAD_ROLES = {
        "STAFF_PHARMACIST",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "CASHIER",
        "SUPERADMIN",
    }

    ALLOWED_VERIFY_ROLES = {
        "STAFF_PHARMACIST",
        "SUPERADMIN",
    }

    @classmethod
    def resolve_staff_member(cls, user: Any, tenant: Tenant) -> Optional[StaffMember]:
        if not hasattr(user, "pk"):
            return None
        uname = getattr(user, "username", "") or getattr(user, "email", "")
        return StaffMember.objects.filter(tenant=tenant, username=uname).first()

    @classmethod
    def upload_attachment(
        cls,
        user: Any,
        *,
        prescription_id: str,
        filename: str,
        content: bytes,
        declared_mime: str,
        notes: str = "",
        retention_days: Optional[int] = None,
    ) -> PrescriptionAttachment:
        """
        Validates, virus scans, and privately stores an uploaded prescription document/image.
        """
        user_role = getattr(user, "role", "")
        if user_role and user_role not in cls.ALLOWED_UPLOAD_ROLES:
            raise PermissionDenied({"user": f"Role '{user_role}' is not authorized to upload prescription attachments."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        if not tenant_id:
            raise ValidationError({"tenant": "Authenticated user is not linked to an active tenant."})

        rx = PrescriptionRecord.objects.select_for_update().filter(
            prescription_id=prescription_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not rx:
            raise ValidationError({"prescription_id": f"Prescription '{prescription_id}' not found in your pharmacy."})

        clean_filename = Path(filename or "prescription_scan.jpg").name.strip()
        if not clean_filename:
            clean_filename = "prescription_scan.jpg"

        ext = Path(clean_filename).suffix.lower()
        if ext not in cls.ALLOWED_EXTENSIONS:
            raise ValidationError(
                {"file": f"File extension '{ext}' is forbidden. Allowed extensions: {', '.join(sorted(cls.ALLOWED_EXTENSIONS))}."}
            )

        file_size = len(content or b"")
        if file_size < cls.MIN_FILE_SIZE_BYTES:
            raise ValidationError({"file": f"Uploaded file is too small or empty ({file_size} bytes, minimum {cls.MIN_FILE_SIZE_BYTES} bytes required)."})

        if file_size > cls.MAX_FILE_SIZE_BYTES:
            raise ValidationError(
                {"file": f"File size exceeds allowable maximum of {cls.MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."}
            )

        clean_mime = str(declared_mime or "application/octet-stream").strip().lower()
        if clean_mime not in cls.ALLOWED_MIMES:
            raise ValidationError(
                {"file": f"MIME type '{clean_mime}' is not permitted. Allowed MIME types: {', '.join(sorted(cls.ALLOWED_MIMES))}."}
            )

        # Antivirus Inspection
        scan_result = AntivirusPolicyEngine.scan_file(
            filename=clean_filename,
            content=content,
            category=FileCategory.PRESCRIPTION_IMAGE,
            declared_mime=clean_mime,
        )

        checksum = hashlib.sha256(content).hexdigest()
        file_id = uuid.uuid4()

        # Persist to private partitioned object storage
        storage_rel_path = PrivateStorageEngine.store_file(
            tenant_id=tenant_id,
            category=FileCategory.PRESCRIPTION_IMAGE,
            file_id=str(file_id),
            filename=clean_filename,
            content=content,
        )

        pharmacy_user = user if isinstance(user, PharmacyUser) else None
        staff = cls.resolve_staff_member(user, rx.tenant)

        # Create SecureFileRecord
        file_rec = SecureFileRecord.objects.create(
            file_id=file_id,
            tenant=rx.tenant,
            branch=rx.branch,
            file_name=clean_filename,
            category=FileCategory.PRESCRIPTION_IMAGE,
            mime_type=scan_result.detected_mime or clean_mime,
            file_size_bytes=file_size,
            checksum_sha256=checksum,
            storage_path=storage_rel_path,
            scan_status=scan_result.scan_status,
            is_quarantined=not scan_result.is_clean,
            quarantine_reason=scan_result.quarantine_reason,
            scan_metadata={
                "threat_name": scan_result.threat_name,
                "provider": scan_result.scan_provider,
            },
            uploaded_by=pharmacy_user,
        )

        ret_days = int(retention_days or cls.DEFAULT_RETENTION_DAYS)
        ret_expiry = timezone.now().date() + timedelta(days=ret_days)

        attachment_id = f"att-rx-{uuid.uuid4().hex[:12]}"
        attachment = PrescriptionAttachment.objects.create(
            attachment_id=attachment_id,
            tenant=rx.tenant,
            branch=rx.branch,
            prescription=rx,
            file_record=file_rec,
            file_name=clean_filename,
            mime_type=scan_result.detected_mime or clean_mime,
            file_size_bytes=file_size,
            checksum_sha256=checksum,
            retention_days=ret_days,
            retention_expires_at=ret_expiry,
            uploaded_by=staff,
            notes=str(notes or "").strip(),
        )

        if not scan_result.is_clean:
            logger.warning(
                "Prescription attachment %s quarantined: %s",
                attachment_id,
                scan_result.quarantine_reason,
            )
            raise ValidationError(
                {"file": f"Security Alert: Uploaded file rejected due to malware policy. {scan_result.quarantine_reason}"}
            )

        logger.info(
            "Prescription attachment %s uploaded for Rx %s (%d bytes, SHA: %s)",
            attachment_id,
            prescription_id,
            file_size,
            checksum[:12],
        )
        return attachment

    @classmethod
    def get_attachment_signed_url(
        cls,
        user: Any,
        *,
        attachment_id: str,
        ttl_seconds: int = 900,
    ) -> Dict[str, Any]:
        """
        Generates a secure, expiring HMAC-SHA256 signed download URL for an attachment.
        Strictly prevents access to quarantined or cross-tenant files.
        """
        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        attachment = PrescriptionAttachment.objects.select_related("file_record", "tenant").filter(
            attachment_id=attachment_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not attachment:
            raise ValidationError({"attachment_id": f"Attachment '{attachment_id}' not found."})

        if attachment.is_quarantined:
            raise PermissionDenied(
                {"file": f"Access Denied: Attachment is quarantined ({attachment.file_record.quarantine_reason})."}
            )

        token, expires_at = PrivateStorageEngine.generate_signed_token(
            file_id=str(attachment.file_record.file_id),
            tenant_id=attachment.tenant.tenant_id,
            ttl_seconds=ttl_seconds,
        )
        signed_url = f"/api/v2/storage/download/?file_id={attachment.file_record.file_id}&token={token}"

        return {
            "attachment_id": attachment.attachment_id,
            "prescription_id": attachment.prescription_id,
            "file_name": attachment.file_name,
            "mime_type": attachment.mime_type,
            "file_size_bytes": attachment.file_size_bytes,
            "checksum_sha256": attachment.checksum_sha256,
            "signed_url": signed_url,
            "signed_token": token,
            "expires_in_seconds": ttl_seconds,
            "retention_expires_at": attachment.retention_expires_at,
            "is_verified_by_pharmacist": attachment.is_verified_by_pharmacist,
        }

    @classmethod
    @transaction.atomic
    def verify_attachment(
        cls,
        user: Any,
        *,
        attachment_id: str,
        pharmacist_pcb_no: str,
        notes: str = "",
    ) -> PrescriptionAttachment:
        """
        Executes clinical pharmacist sign-off on an uploaded prescription document.
        """
        user_role = getattr(user, "role", "")
        if user_role and user_role not in cls.ALLOWED_VERIFY_ROLES:
            raise PermissionDenied({"user": f"Role '{user_role}' is not authorized to clinically verify prescription images."})

        tenant_id = getattr(user, "tenant_id", None) or getattr(getattr(user, "tenant", None), "tenant_id", None)
        attachment = PrescriptionAttachment.objects.select_for_update().filter(
            attachment_id=attachment_id,
            tenant__tenant_id=tenant_id,
        ).first()

        if not attachment:
            raise ValidationError({"attachment_id": f"Attachment '{attachment_id}' not found."})

        clean_pcb = str(pharmacist_pcb_no or "").strip().upper()
        if not clean_pcb:
            raise ValidationError({"pharmacist_pcb_no": "Pharmacist PCB registration number is mandatory."})

        staff = cls.resolve_staff_member(user, attachment.tenant)

        attachment.is_verified_by_pharmacist = True
        attachment.verified_by = staff
        if notes:
            attachment.notes = (attachment.notes + f"\nVerified by PCB {clean_pcb}: {notes}").strip()
        attachment.save(update_fields=["is_verified_by_pharmacist", "verified_by", "notes", "updated_at"])

        logger.info("Attachment %s clinically verified by pharmacist %s", attachment_id, clean_pcb)
        return attachment
