"""
apps.audit_logging.serializers
Architecture Decision: ADR-0035 & ADR-0050
Serializers for Immutable Audit Log Entities, Creation, and Query Filters.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.audit_logging.models import AuditLogEntry
from apps.audit_logging.redaction import SensitiveDataRedactor


class AuditLogEntrySerializer(serializers.ModelSerializer):
    """Serializer for immutable audit trail entries conforming to ADR-0050."""

    class Meta:
        model = AuditLogEntry
        fields = [
            "entry_id",
            "timestamp",
            "tenant_id",
            "branch_id",
            "user_id",
            "actor_email",
            "actor_role",
            "device_id",
            "request_id",
            "operation_id",
            "action",
            "resource_type",
            "resource_id",
            "status",
            "before_state",
            "after_state",
            "retention_days",
            "retention_until",
            "is_immutable",
            "ip_address",
            "metadata_json",
        ]
        read_only_fields = fields


class AuditEventCreateSerializer(serializers.Serializer):
    """Validates incoming mutation audit events emitted from clients or internal services."""

    action = serializers.CharField(max_length=64, required=True)
    resource_type = serializers.CharField(max_length=64, required=True)
    resource_id = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    status = serializers.ChoiceField(choices=["SUCCESS", "DENIED", "ERROR"], required=False, default="SUCCESS")
    before_state = serializers.DictField(required=False, default=dict)
    after_state = serializers.DictField(required=False, default=dict)
    metadata_json = serializers.DictField(required=False, default=dict)
    branch_id = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True)
    device_id = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True)
    operation_id = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True)
    retention_days = serializers.IntegerField(required=False, default=730, min_value=365)


class AuditLogQueryFilterSerializer(serializers.Serializer):
    """Validates query parameters for audit trail searches."""

    tenant_id = serializers.CharField(required=False, max_length=64)
    branch_id = serializers.CharField(required=False, max_length=64)
    actor_role = serializers.CharField(required=False, max_length=64)
    user_id = serializers.CharField(required=False, max_length=64)
    action = serializers.CharField(required=False, max_length=64)
    resource_type = serializers.CharField(required=False, max_length=64)
    status = serializers.ChoiceField(choices=["SUCCESS", "DENIED", "ERROR"], required=False)
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)


class SanitizedAuditLogEntrySerializer(serializers.ModelSerializer):
    """
    Architecture Decision: ADR-0078
    Serializer ensuring zero exposure of forbidden payloads in search results.
    """

    before_state = serializers.SerializerMethodField()
    after_state = serializers.SerializerMethodField()
    metadata_json = serializers.SerializerMethodField()

    class Meta:
        model = AuditLogEntry
        fields = [
            "entry_id",
            "timestamp",
            "tenant_id",
            "branch_id",
            "user_id",
            "actor_email",
            "actor_role",
            "device_id",
            "request_id",
            "operation_id",
            "action",
            "resource_type",
            "resource_id",
            "status",
            "before_state",
            "after_state",
            "retention_days",
            "retention_until",
            "is_immutable",
            "ip_address",
            "metadata_json",
        ]
        read_only_fields = fields

    def get_before_state(self, obj):
        if not obj.before_state:
            return {}
        return SensitiveDataRedactor.redact(obj.before_state)

    def get_after_state(self, obj):
        if not obj.after_state:
            return {}
        return SensitiveDataRedactor.redact(obj.after_state)

    def get_metadata_json(self, obj):
        if not obj.metadata_json:
            return {}
        return SensitiveDataRedactor.redact(obj.metadata_json)


class PlatformAuditSearchFilterSerializer(serializers.Serializer):
    """
    Architecture Decision: ADR-0078
    Validates criteria for platform-wide administrative audit searches.
    """

    actor = serializers.CharField(required=False, max_length=255, allow_blank=True, default="")
    tenant = serializers.CharField(required=False, max_length=64, allow_blank=True, default="")
    date_from = serializers.DateTimeField(required=False, allow_null=True, default=None)
    date_to = serializers.DateTimeField(required=False, allow_null=True, default=None)
    entity_type = serializers.CharField(required=False, max_length=64, allow_blank=True, default="")
    entity_id = serializers.CharField(required=False, max_length=128, allow_blank=True, default="")
    reason = serializers.CharField(required=False, max_length=255, allow_blank=True, default="")
    status = serializers.ChoiceField(choices=["SUCCESS", "DENIED", "ERROR", "ALL"], required=False, default="ALL")
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)

    def validate(self, attrs):
        d_from = attrs.get("date_from")
        d_to = attrs.get("date_to")
        if d_from and d_to and d_to < d_from:
            raise serializers.ValidationError({"date_to": ["date_to must be equal to or after date_from."]})
        return attrs
