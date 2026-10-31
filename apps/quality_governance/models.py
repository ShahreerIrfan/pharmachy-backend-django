"""
apps.quality_governance.models
Architecture Decision: ADR-0039
Data models for Quality Issue Tickets, Bug Reproduction Fields, and Definition of Done Audits.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class IssueCategory(models.TextChoices):
    BUG = "BUG", "Bug / Defect"
    FEATURE = "FEATURE", "Feature Request"
    ARCHITECTURE_REVIEW = "ARCHITECTURE_REVIEW", "Architecture Review"
    REGULATORY_COMPLIANCE = "REGULATORY_COMPLIANCE", "DGDA / Clinical Compliance"


class IssueSeverity(models.TextChoices):
    LOW = "LOW", "Low / Cosmetic"
    MEDIUM = "MEDIUM", "Medium / Workaround Available"
    HIGH = "HIGH", "High / Financial or Stock Risk"
    CRITICAL_CLINICAL_SAFETY = "CRITICAL_CLINICAL_SAFETY", "Critical Clinical Safety Risk"
    BLOCKER = "BLOCKER", "System Blocker"


class DoDComplianceStatus(models.TextChoices):
    PASSED = "PASSED", "Definition of Done Passed"
    DEFICIENT = "DEFICIENT", "DoD Incomplete / Pillars Missing"
    BLOCKED = "BLOCKED", "Blocked by Test Failure or Clinical Hazard"


class QualityIssueTicket(models.Model):
    """
    Structured Issue Ticket with mandatory bug reproduction fields and clinical safety levels.
    """

    issue_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=32, choices=IssueCategory.choices, default=IssueCategory.BUG)
    severity = models.CharField(max_length=32, choices=IssueSeverity.choices, default=IssueSeverity.MEDIUM)
    client_platform = models.CharField(max_length=16, default="DESKTOP")
    client_version = models.CharField(max_length=32, default="1.2.0")
    environment = models.CharField(max_length=16, default="DEVELOPMENT")
    connectivity_state = models.CharField(max_length=16, default="ONLINE")
    reproduction_steps = models.TextField(blank=True, default="")
    expected_behavior = models.TextField(blank=True, default="")
    observed_behavior = models.TextField(blank=True, default="")
    sanitized_logs = models.TextField(blank=True, default="")
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_quality_issue_tickets"
        verbose_name = "Quality Issue Ticket"
        verbose_name_plural = "Quality Issue Tickets"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title} ({self.tenant_id})"

    def clean(self):
        super().clean()
        if self.category == IssueCategory.BUG:
            if not self.reproduction_steps or len(self.reproduction_steps.strip()) < 10:
                raise ValidationError({"reproduction_steps": "Bug reports must include detailed reproduction steps."})
            if not self.expected_behavior or not self.observed_behavior:
                raise ValidationError(
                    {
                        "expected_behavior": "Both expected and observed behaviors are required for bugs.",
                        "observed_behavior": "Both expected and observed behaviors are required for bugs.",
                    }
                )


class DefinitionOfDoneAudit(models.Model):
    """
    Audited Definition of Done (DoD) assessment for a development prompt or release.
    """

    audit_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    prompt_id = models.CharField(max_length=16, db_index=True, help_text="e.g. P039")
    feature_title = models.CharField(max_length=200)
    tenant_id = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=DoDComplianceStatus.choices,
        default=DoDComplianceStatus.PASSED,
        db_index=True,
    )
    # The 7 Pillars
    traceability_pass = models.BooleanField(default=False)
    security_phi_pass = models.BooleanField(default=False)
    decimal_money_pass = models.BooleanField(default=False)
    fefo_stock_pass = models.BooleanField(default=False)
    migration_safety_pass = models.BooleanField(default=False)
    multiplatform_ui_pass = models.BooleanField(default=False)
    test_evidence_pass = models.BooleanField(default=False)

    has_critical_clinical_safety_flaw = models.BooleanField(
        default=False,
        help_text="If True, strictly blocks DoD sign-off until verified with regression proof",
    )
    test_command = models.CharField(max_length=300)
    tests_passed = models.PositiveIntegerField(default=0)
    tests_failed = models.PositiveIntegerField(default=0)
    execution_time_seconds = models.DecimalField(max_digits=8, decimal_places=3, default=Decimal("0.000"))
    evidence_log_hash = models.CharField(max_length=64, blank=True, default="")
    reviewer_role = models.CharField(max_length=32, default="ADMIN")
    evaluated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_definition_of_done_audits"
        verbose_name = "Definition of Done Audit"
        verbose_name_plural = "Definition of Done Audits"
        ordering = ["-evaluated_at"]

    def __str__(self) -> str:
        return f"[{self.status}] {self.prompt_id} - {self.feature_title} ({self.tenant_id})"
