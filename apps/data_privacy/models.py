"""
apps.data_privacy.models
Architecture Decision: ADR-0052
Data Subject Requests (DSR), Statutory Retention Assessments, and Anonymization Records.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.core.models import Branch, Tenant
from apps.identity.models import PharmacyUser


class PrivacyRequestType(models.TextChoices):
    DATA_EXPORT = "DATA_EXPORT", "Export Subject Data Bundle"
    DATA_CORRECTION = "DATA_CORRECTION", "Rectify Personal Demographics"
    DATA_DELETION = "DATA_DELETION", "Data Erasure / Deletion Request"


class PrivacyRequestStatus(models.TextChoices):
    PENDING_ASSESSMENT = "PENDING_ASSESSMENT", "Pending Privacy Assessment"
    UNDER_REVIEW = "UNDER_REVIEW", "Under Compliance Review"
    APPROVED = "APPROVED", "Approved for Execution"
    REJECTED = "REJECTED", "Rejected due to Regulatory Conflict"
    ANONYMIZED = "ANONYMIZED", "Anonymized under Statutory Retention Hold"
    COMPLETED = "COMPLETED", "Fulfilled and Completed"


class DataSubjectType(models.TextChoices):
    PATIENT = "PATIENT", "Patient / Prescription Holder"
    CUSTOMER = "CUSTOMER", "Retail OTC Customer"
    STAFF = "STAFF", "Pharmacy Staff Member"


class PrivacyRequest(models.Model):
    """
    Tracks and audits Data Subject Requests (DSR), statutory retention schedule checks,
    anonymization overrides, and portable export generation under ADR-0052.
    """

    request_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="privacy_requests", db_index=True)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="privacy_requests",
        db_index=True,
    )
    subject_type = models.CharField(
        max_length=32,
        choices=DataSubjectType.choices,
        default=DataSubjectType.PATIENT,
        db_index=True,
    )
    subject_identifier = models.CharField(
        max_length=128,
        db_index=True,
        help_text="Patient token (PT-xxx), customer phone, or user UUID",
    )
    request_type = models.CharField(
        max_length=32,
        choices=PrivacyRequestType.choices,
        db_index=True,
    )
    status = models.CharField(
        max_length=32,
        choices=PrivacyRequestStatus.choices,
        default=PrivacyRequestStatus.PENDING_ASSESSMENT,
        db_index=True,
    )
    reason = models.TextField(blank=True, default="")
    correction_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Demographic corrections requested by data subject",
    )
    assessment_notes = models.TextField(blank=True, default="")
    assessed_by = models.ForeignKey(
        PharmacyUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assessed_privacy_requests",
    )
    assessed_at = models.DateTimeField(null=True, blank=True)

    retention_conflict_detected = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if mandatory DGDA or NBR retention blocks physical erasure",
    )
    retention_horizon_days = models.PositiveIntegerField(
        default=730,
        help_text="Statutory retention horizon in days (e.g. 730d Rx, 1825d controlled)",
    )
    export_artifact_url = models.CharField(max_length=512, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Data Privacy Request"
        verbose_name_plural = "Data Privacy Requests"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["tenant", "request_type", "status"]),
            models.Index(fields=["subject_identifier"]),
        ]

    def __str__(self) -> str:
        return f"[{self.tenant_id}] DSR {self.request_type} for {self.subject_identifier} ({self.status})"
