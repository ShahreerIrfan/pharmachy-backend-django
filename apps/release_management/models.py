"""
apps.release_management.models
Architecture Decision: ADR-0038
Data models for release manifests, client compatibility policies, and changelog governance.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.release_management.semver import InvalidSemVerError, SemVer


class ClientPlatform(models.TextChoices):
    API = "API", "Cloud Authority API"
    DESKTOP = "DESKTOP", "JavaFX Desktop POS"
    MOBILE = "MOBILE", "Flutter Mobile Manager"


class ReleaseStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft / Unreleased"
    ACTIVE = "ACTIVE", "Active / General Availability"
    DEPRECATED = "DEPRECATED", "Deprecated / Sunset Scheduled"
    REVOKED = "REVOKED", "Revoked / Critical Flaw"


class CompatibilityStatus(models.TextChoices):
    SUPPORTED = "SUPPORTED", "Fully Supported"
    DEPRECATED = "DEPRECATED", "Deprecated (Grace Period Active)"
    UPGRADE_REQUIRED = "UPGRADE_REQUIRED", "Upgrade Required (Blocked)"


class ReleaseManifest(models.Model):
    """
    Immutable release record for a specific platform build.
    Adheres to SemVer 2.0.0 and Keep a Changelog specifications.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.CharField(max_length=16, choices=ClientPlatform.choices, db_index=True)
    version = models.CharField(max_length=32, db_index=True, help_text="SemVer 2.0.0 string (e.g. '1.2.0')")
    min_supported_version = models.CharField(
        max_length=32,
        help_text="Minimum client SemVer supported alongside this release (e.g. '1.0.0')",
    )
    release_status = models.CharField(
        max_length=16,
        choices=ReleaseStatus.choices,
        default=ReleaseStatus.ACTIVE,
        db_index=True,
    )
    title_en = models.CharField(max_length=150)
    title_bn = models.CharField(max_length=150)
    changelog_markdown = models.TextField(blank=True, default="")
    changelog_json = models.JSONField(blank=True, default=list)
    download_url = models.URLField(blank=True, default="")
    sha256_checksum = models.CharField(max_length=64, blank=True, default="")
    is_critical = models.BooleanField(
        default=False,
        help_text="If True, indicates an urgent security or clinical data-integrity patch",
    )
    sunset_date = models.DateField(null=True, blank=True)
    released_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_release_manifests"
        verbose_name = "Release Manifest"
        verbose_name_plural = "Release Manifests"
        unique_together = [("platform", "version")]
        ordering = ["-released_at", "-created_at"]

    def __str__(self) -> str:
        return f"[{self.platform}] v{self.version} ({self.release_status})"

    def clean(self):
        super().clean()
        try:
            parsed_v = SemVer.parse(self.version)
        except InvalidSemVerError as err:
            raise ValidationError({"version": f"Invalid SemVer: {err}"})

        try:
            parsed_min = SemVer.parse(self.min_supported_version)
        except InvalidSemVerError as err:
            raise ValidationError({"min_supported_version": f"Invalid SemVer: {err}"})

        if parsed_v < parsed_min:
            raise ValidationError(
                {"min_supported_version": "Minimum supported version cannot exceed the release version itself."}
            )


class ClientCompatibilityPolicy(models.Model):
    """
    Active compatibility policy for a client platform.
    Used by POS clients and mobile apps during startup and periodic heartbeat checks.
    """

    platform = models.CharField(max_length=16, choices=ClientPlatform.choices, unique=True)
    target_version = models.CharField(max_length=32, help_text="Current latest recommended SemVer")
    min_supported_version = models.CharField(max_length=32, help_text="Strict minimum allowable client SemVer")
    recommended_version = models.CharField(max_length=32, blank=True, default="")
    upgrade_grace_period_days = models.PositiveIntegerField(
        default=30,
        help_text="Days before a deprecated version transitions to UPGRADE_REQUIRED",
    )
    download_url = models.URLField(blank=True, default="")
    announcement_en = models.TextField(blank=True, default="")
    announcement_bn = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pharmacy_client_compatibility_policies"
        verbose_name = "Client Compatibility Policy"
        verbose_name_plural = "Client Compatibility Policies"

    def __str__(self) -> str:
        return f"{self.platform} Policy (Target: {self.target_version}, Min: {self.min_supported_version})"

    def clean(self):
        super().clean()
        try:
            target_v = SemVer.parse(self.target_version)
        except InvalidSemVerError as err:
            raise ValidationError({"target_version": f"Invalid SemVer: {err}"})

        try:
            min_v = SemVer.parse(self.min_supported_version)
        except InvalidSemVerError as err:
            raise ValidationError({"min_supported_version": f"Invalid SemVer: {err}"})

        if target_v < min_v:
            raise ValidationError({"min_supported_version": "min_supported_version cannot exceed target_version"})
