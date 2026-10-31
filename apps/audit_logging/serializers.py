"""
apps.audit_logging.serializers
Architecture Decision: ADR-0035 & ADR-0050
Serializers for Immutable Audit Log Entities, Creation, and Query Filters.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.audit_logging.models import AuditLogEntry


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
