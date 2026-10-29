"""
apps.developer_toolchain.serializers
Architecture Decision: ADR-0036
Serializers for Developer Quickstart API Endpoints and Audit Logs.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.developer_toolchain.models import DeveloperBootstrapAudit


class DeveloperBootstrapAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeveloperBootstrapAudit
        fields = [
            "id",
            "timestamp",
            "operator_role",
            "tenant_id",
            "action",
            "duration_ms",
            "status",
            "report_json",
        ]
        read_only_fields = fields


class QuickstartTriggerActionSerializer(serializers.Serializer):
    dry_run = serializers.BooleanField(default=False)
    tenant_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    action = serializers.ChoiceField(choices=["VERIFY_ALL", "MIGRATE", "SEED", "DRY_RUN"], default="VERIFY_ALL")
