"""
apps.release_management.serializers
Architecture Decision: ADR-0038
DRF Serializers for Release Manifests, Compatibility Negotiation, and Client Version checks.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.release_management.models import (
    ClientCompatibilityPolicy,
    ClientPlatform,
    CompatibilityStatus,
    ReleaseManifest,
)
from apps.release_management.semver import InvalidSemVerError, SemVer


class ReleaseManifestSerializer(serializers.ModelSerializer):
    """Read serializer for published release records."""

    class Meta:
        model = ReleaseManifest
        fields = [
            "id",
            "platform",
            "version",
            "min_supported_version",
            "release_status",
            "title_en",
            "title_bn",
            "changelog_markdown",
            "changelog_json",
            "download_url",
            "sha256_checksum",
            "is_critical",
            "sunset_date",
            "released_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ReleaseManifestCreateSerializer(serializers.ModelSerializer):
    """Write serializer for publishing or staging new releases."""

    class Meta:
        model = ReleaseManifest
        fields = [
            "platform",
            "version",
            "min_supported_version",
            "release_status",
            "title_en",
            "title_bn",
            "changelog_markdown",
            "changelog_json",
            "download_url",
            "sha256_checksum",
            "is_critical",
            "sunset_date",
            "released_at",
        ]

    def validate_version(self, value: str) -> str:
        try:
            SemVer.parse(value)
        except InvalidSemVerError as err:
            raise serializers.ValidationError(f"Invalid version: {err}")
        return value

    def validate_min_supported_version(self, value: str) -> str:
        try:
            SemVer.parse(value)
        except InvalidSemVerError as err:
            raise serializers.ValidationError(f"Invalid min_supported_version: {err}")
        return value

    def validate(self, attrs):
        v = attrs.get("version")
        min_v = attrs.get("min_supported_version")
        if v and min_v:
            parsed_v = SemVer.parse(v)
            parsed_min = SemVer.parse(min_v)
            if parsed_v < parsed_min:
                raise serializers.ValidationError(
                    {"min_supported_version": "Minimum supported version cannot exceed the release version."}
                )
        return attrs


class ClientCompatibilityCheckSerializer(serializers.Serializer):
    """Incoming request to check client compatibility."""

    platform = serializers.ChoiceField(choices=ClientPlatform.choices)
    client_version = serializers.CharField(max_length=32)

    def validate_client_version(self, value: str) -> str:
        try:
            SemVer.parse(value)
        except InvalidSemVerError as err:
            raise serializers.ValidationError(f"Malformed SemVer client version: {err}")
        return value


class ClientCompatibilityResponseSerializer(serializers.Serializer):
    """Structured response for compatibility checks."""

    platform = serializers.ChoiceField(choices=ClientPlatform.choices)
    client_version = serializers.CharField()
    status = serializers.ChoiceField(choices=CompatibilityStatus.choices)
    target_version = serializers.CharField()
    min_supported_version = serializers.CharField()
    recommended_version = serializers.CharField(allow_blank=True)
    is_upgrade_required = serializers.BooleanField()
    grace_period_days_remaining = serializers.IntegerField(allow_null=True)
    download_url = serializers.CharField(allow_blank=True)
    message_en = serializers.CharField()
    message_bn = serializers.CharField()


class ClientCompatibilityPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientCompatibilityPolicy
        fields = [
            "id",
            "platform",
            "target_version",
            "min_supported_version",
            "recommended_version",
            "upgrade_grace_period_days",
            "download_url",
            "announcement_en",
            "announcement_bn",
            "is_active",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]
