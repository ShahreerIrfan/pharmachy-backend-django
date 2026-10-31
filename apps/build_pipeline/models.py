"""
apps.build_pipeline.models
Architecture Decision: ADR-0040
Data models for Clean-Checkout Automated Build Smoke Pipeline Execution Records.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone


class SmokePipelineStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Execution"
    RUNNING = "RUNNING", "Running Stages"
    SUCCESS = "SUCCESS", "All Stages Passed"
    FAILED = "FAILED", "Pipeline Failed"
    CANCELLED = "CANCELLED", "Pipeline Cancelled"


class SmokeStageName(models.TextChoices):
    WORKSPACE_INSPECTION = "WORKSPACE_INSPECTION", "Workspace Clean Inspection"
    DATABASE_MIGRATION = "DATABASE_MIGRATION", "Database Migration Head Verification"
    CONTRACT_VALIDATION = "CONTRACT_VALIDATION", "JSON Schema & API Contract Validation"
    LINT_AND_FORMAT = "LINT_AND_FORMAT", "Code Quality & Format Linting"
    CLIENT_COMPILATION = "CLIENT_COMPILATION", "Multi-Client Build & Syntax Compilation"
    FAST_TEST_SUITE = "FAST_TEST_SUITE", "Fast Regression Test Marker Execution"


class SmokeStageStatus(models.TextChoices):
    PASSED = "PASSED", "Passed"
    FAILED = "FAILED", "Failed"
    SKIPPED = "SKIPPED", "Skipped"


class BuildSmokeExecutionRecord(models.Model):
    """
    Persisted record of an automated clean-checkout build smoke pipeline run.
    Conforms strictly to shared/contracts/schemas/build_smoke.schema.json.
    """

    pipeline_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=64, db_index=True, default="tenant-default")
    triggered_by = models.CharField(max_length=100, default="CLI_RUNNER")
    status = models.CharField(
        max_length=20,
        choices=SmokePipelineStatus.choices,
        default=SmokePipelineStatus.PENDING,
        db_index=True,
    )
    git_commit_hash = models.CharField(max_length=40, default="0000000")
    git_branch = models.CharField(max_length=100, default="master")
    is_clean_checkout = models.BooleanField(default=True)
    total_duration_seconds = models.DecimalField(max_digits=8, decimal_places=3, default=Decimal("0.000"))
    stages = models.JSONField(
        default=list,
        help_text="Ordered telemetry list of executed and skipped smoke stages.",
    )
    executed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "build_smoke_execution_records"
        ordering = ["-executed_at"]
        indexes = [
            models.Index(fields=["tenant_id", "status"]),
            models.Index(fields=["git_commit_hash"]),
        ]

    def __str__(self) -> str:
        return f"BuildSmoke-{self.pipeline_id} [{self.status}] @ {self.git_commit_hash[:7]}"
