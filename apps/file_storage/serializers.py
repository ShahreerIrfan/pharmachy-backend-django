"""
apps.file_storage.serializers
Architecture Decision: ADR-0051
DRF Serializers for Secure File Upload, Inspection Metadata, and Signed Expiring URLs.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.file_storage.models import FileCategory, SecureFileRecord


class SecureFileUploadSerializer(serializers.Serializer):
    """Input serializer for client-initiated private file uploads."""

    file = serializers.FileField(required=True, help_text="Binary file attachment")
    category = serializers.ChoiceField(choices=FileCategory.choices, required=True)
    branch_id = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True)

    def validate_file(self, value):
        if value.size <= 0:
            raise serializers.ValidationError("Cannot upload empty file.")
        if value.size > 15 * 1024 * 1024:
            raise serializers.ValidationError("File exceeds maximum allowed size of 15MB.")
        return value


class SecureFileRecordSerializer(serializers.ModelSerializer):
    """Output serializer for stored file metadata and inspection status."""

    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)
    branch_id = serializers.CharField(source="branch.branch_id", read_only=True, allow_null=True)
    uploaded_by_email = serializers.CharField(source="uploaded_by.email", read_only=True, default="")
    signed_download_url = serializers.SerializerMethodField()
    expires_at = serializers.SerializerMethodField()

    class Meta:
        model = SecureFileRecord
        fields = [
            "file_id",
            "tenant_id",
            "branch_id",
            "file_name",
            "category",
            "mime_type",
            "file_size_bytes",
            "checksum_sha256",
            "scan_status",
            "is_quarantined",
            "quarantine_reason",
            "scan_metadata",
            "uploaded_by_email",
            "signed_download_url",
            "expires_at",
            "is_archived",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "file_id",
            "checksum_sha256",
            "scan_status",
            "is_quarantined",
            "quarantine_reason",
            "created_at",
            "updated_at",
        ]

    def get_signed_download_url(self, obj: SecureFileRecord) -> str | None:
        request = self.context.get("request")
        if not request:
            return None
        # Only provide signed URL if not quarantined
        if obj.is_quarantined:
            return None
        from apps.file_storage.storage_engine import PrivateStorageEngine

        token, _ = PrivateStorageEngine.generate_signed_token(str(obj.file_id), obj.tenant_id, ttl_seconds=900)
        return request.build_absolute_uri(
            f"/api/v2/storage/download/{obj.file_id}/?token={token}&tenant_id={obj.tenant_id}"
        )

    def get_expires_at(self, obj: SecureFileRecord) -> str | None:
        if obj.is_quarantined:
            return None
        from apps.file_storage.storage_engine import PrivateStorageEngine

        _, expires_at = PrivateStorageEngine.generate_signed_token(
            str(obj.file_id), obj.tenant_id, ttl_seconds=900
        )
        return expires_at.isoformat()


class SignedUrlRequestSerializer(serializers.Serializer):
    """Payload for generating on-demand signed expiring download URL."""

    ttl_seconds = serializers.IntegerField(
        required=False,
        default=900,
        min_value=60,
        max_value=3600,
        help_text="Time to live in seconds (60 to 3600)",
    )


class SignedUrlResponseSerializer(serializers.Serializer):
    """Response containing time-bound download URL and token."""

    file_id = serializers.UUIDField()
    download_url = serializers.CharField()
    token = serializers.CharField()
    expires_at = serializers.DateTimeField()
    ttl_seconds = serializers.IntegerField()
