"""
apps.file_storage.models
Architecture Decision: ADR-0051
Secure File Record and Private Object Storage Partitioning Model for Bangladesh Pharmacy SaaS.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.core.models import Branch, Tenant
from apps.identity.models import PharmacyUser


class FileCategory(models.TextChoices):
    PRESCRIPTION_IMAGE = "PRESCRIPTION_IMAGE", "Clinical Prescription Image / Scan"
    SALES_INVOICE_PDF = "SALES_INVOICE_PDF", "Sales Tax Invoice Document (PDF)"
    PURCHASE_ORDER = "PURCHASE_ORDER", "Procurement Order / GRN Attachment"
    DGDA_COMPLIANCE = "DGDA_COMPLIANCE", "DGDA Regulatory / Inspection Document"
    GENERAL_ATTACHMENT = "GENERAL_ATTACHMENT", "General Expense / Operational Attachment"


class ScanStatus(models.TextChoices):
    PENDING_SCAN = "PENDING_SCAN", "Pending Antivirus Inspection"
    CLEAN = "CLEAN", "Clean & Approved for Access"
    INFECTED = "INFECTED", "Malware or Malicious Script Detected"
    QUARANTINED = "QUARANTINED", "Quarantined by Policy"
    FAILED = "FAILED", "Scan Engine Failure"


class SecureFileRecord(models.Model):
    """
    Immutable metadata record for private object storage attachments.
    Files are stored in private tenant-isolated storage buckets/paths and served via expiring signed URLs.
    """

    file_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="stored_files", db_index=True)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stored_files",
        db_index=True,
    )
    file_name = models.CharField(max_length=255)
    category = models.CharField(max_length=64, choices=FileCategory.choices, db_index=True)
    mime_type = models.CharField(max_length=128)
    file_size_bytes = models.PositiveIntegerField()
    checksum_sha256 = models.CharField(max_length=64, db_index=True)
    storage_path = models.CharField(max_length=512)

    scan_status = models.CharField(
        max_length=32,
        choices=ScanStatus.choices,
        default=ScanStatus.PENDING_SCAN,
        db_index=True,
    )
    is_quarantined = models.BooleanField(default=False, db_index=True)
    quarantine_reason = models.CharField(max_length=255, blank=True, default="")
    scan_metadata = models.JSONField(default=dict, blank=True)

    uploaded_by = models.ForeignKey(
        PharmacyUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_files",
    )
    is_archived = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Secure File Record"
        verbose_name_plural = "Secure File Records"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "category"]),
            models.Index(fields=["tenant", "is_quarantined"]),
            models.Index(fields=["checksum_sha256"]),
        ]

    def __str__(self) -> str:
        status_tag = "QUARANTINED" if self.is_quarantined else self.scan_status
        return f"[{self.tenant_id}] {self.file_name} ({self.category}) - {status_tag} [{self.file_id}]"
