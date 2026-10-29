"""
apps.secrets_vault.serializers
Architecture Decision: ADR-0037
Serializers for Tenant Secrets, Safe Metadata Previews, and Rotation Invocations.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.secrets_vault.models import SecretRotationAudit, SecretType, TenantSecretRecord


class TenantSecretMetadataSerializer(serializers.ModelSerializer):
    """Safe metadata serializer: strictly returns masked key preview, never exposes plaintext secret."""

    class Meta:
        model = TenantSecretRecord
        fields = [
            "secret_id",
            "tenant_id",
            "secret_type",
            "key_preview",
            "version",
            "is_active",
            "created_at",
            "last_rotated_at",
        ]
        read_only_fields = fields


class TenantSecretCreateSerializer(serializers.Serializer):
    secret_type = serializers.ChoiceField(choices=SecretType.choices)
    raw_secret_value = serializers.CharField(write_only=True, min_length=12, max_length=512)


class SecretRotateRequestSerializer(serializers.Serializer):
    new_secret_value = serializers.CharField(write_only=True, min_length=12, max_length=512)


class SecretRotationAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = SecretRotationAudit
        fields = [
            "audit_id",
            "timestamp",
            "tenant_id",
            "secret_id",
            "operator_role",
            "old_version",
            "new_version",
            "status",
        ]
        read_only_fields = fields

