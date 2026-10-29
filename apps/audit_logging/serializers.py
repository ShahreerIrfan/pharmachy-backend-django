"""
apps.audit_logging.serializers
Architecture Decision: ADR-0035
Serializers for Audit Log Entities and Query Filters.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.audit_logging.models import AuditLogEntry


class AuditLogEntrySerializer(serializers.ModelSerializer):
    """Serializer for immutable audit trail entries."""

    class Meta:
        model = AuditLogEntry
        fields = [
            "entry_id",
            "timestamp",
            "tenant_id",
            "branch_id",
            "user_id",
            "device_id",
            "request_id",
            "operation_id",
            "action",
            "resource_type",
            "resource_id",
            "status",
            "ip_address",
            "metadata_json",
        ]
        read_only_fields = fields


class AuditLogQueryFilterSerializer(serializers.Serializer):
    """Validates query parameters for audit trail searches."""

    tenant_id = serializers.CharField(required=False, max_length=64)
    action = serializers.CharField(required=False, max_length=64)
    resource_type = serializers.CharField(required=False, max_length=64)
    status = serializers.ChoiceField(choices=["SUCCESS", "DENIED", "ERROR"], required=False)
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)
