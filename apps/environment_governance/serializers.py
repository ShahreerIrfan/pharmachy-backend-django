"""
apps.environment_governance.serializers
Architecture Decision: ADR-0041
DRF Serializers for Modular Settings Profiles, Audits, and Simulation Requests.
"""

from __future__ import annotations

from typing import List

from rest_framework import serializers

from apps.environment_governance.models import EnvironmentProfileAudit, EnvironmentType
from config.settings.production import ProductionSecretsValidator


class EnvironmentProfileSerializer(serializers.Serializer):
    """
    Serializes active environment settings telemetry conforming to modular_settings.schema.json.
    """

    environment = serializers.ChoiceField(choices=EnvironmentType.choices)
    debug = serializers.BooleanField()
    database_engine = serializers.CharField()
    database_name = serializers.CharField()
    is_hardened = serializers.BooleanField()
    ssl_redirect = serializers.BooleanField()
    session_cookie_secure = serializers.BooleanField()
    csrf_cookie_secure = serializers.BooleanField()
    hsts_seconds = serializers.IntegerField()
    allowed_hosts = serializers.ListField(child=serializers.CharField())
    secrets_validated = serializers.BooleanField()
    inspected_at = serializers.DateTimeField()


class EnvironmentProfileAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = EnvironmentProfileAudit
        fields = [
            "audit_id",
            "tenant_id",
            "environment",
            "debug",
            "database_engine",
            "database_name",
            "is_hardened",
            "ssl_redirect",
            "session_cookie_secure",
            "csrf_cookie_secure",
            "secrets_validated",
            "inspected_by",
            "created_at",
        ]
        read_only_fields = ["audit_id", "created_at"]


class EnvironmentSimulationRequestSerializer(serializers.Serializer):
    """
    Validates simulated environment profile switches and enforces production secrets rules.
    """

    target_profile = serializers.ChoiceField(choices=EnvironmentType.choices)
    simulated_secret_key = serializers.CharField(required=False, allow_blank=True)
    simulated_postgres_password = serializers.CharField(required=False, allow_blank=True)
    simulated_allowed_hosts = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    def validate(self, attrs):
        target = attrs.get("target_profile")
        if target == EnvironmentType.PRODUCTION:
            key = attrs.get("simulated_secret_key", "")
            pg_pass = attrs.get("simulated_postgres_password", "")
            hosts: List[str] = attrs.get("simulated_allowed_hosts", [])
            try:
                ProductionSecretsValidator.validate(key, pg_pass, hosts)
            except Exception as e:
                raise serializers.ValidationError({"production_secrets": str(e)})
        return attrs
