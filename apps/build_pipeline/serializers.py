"""
apps.build_pipeline.serializers
Architecture Decision: ADR-0040
DRF Serializers for Build Smoke Pipeline Execution Records and Trigger Requests.
"""

from __future__ import annotations

import re

from rest_framework import serializers

from apps.build_pipeline.models import (
    BuildSmokeExecutionRecord,
    SmokeStageName,
)


class BuildSmokeStageItemSerializer(serializers.Serializer):
    stage_name = serializers.ChoiceField(choices=SmokeStageName.choices)
    status = serializers.ChoiceField(choices=["PASSED", "FAILED", "SKIPPED"])
    duration_ms = serializers.IntegerField(min_value=0)
    error_message = serializers.CharField(allow_null=True, required=False)


class BuildSmokeExecutionRecordSerializer(serializers.ModelSerializer):
    """
    Serializes BuildSmokeExecutionRecord conforming to build_smoke.schema.json.
    """

    stages = BuildSmokeStageItemSerializer(many=True, read_only=True)
    total_duration_seconds = serializers.FloatField()

    class Meta:
        model = BuildSmokeExecutionRecord
        fields = [
            "pipeline_id",
            "triggered_by",
            "tenant_id",
            "status",
            "git_commit_hash",
            "git_branch",
            "is_clean_checkout",
            "total_duration_seconds",
            "stages",
            "executed_at",
        ]
        read_only_fields = [
            "pipeline_id",
            "status",
            "total_duration_seconds",
            "stages",
            "executed_at",
        ]


class BuildSmokeTriggerRequestSerializer(serializers.Serializer):
    """
    Validates input parameters for triggering an automated build smoke run.
    """

    tenant_id = serializers.CharField(max_length=64, required=False, default="tenant-default")
    triggered_by = serializers.CharField(max_length=100, required=False, default="CLI_RUNNER")
    git_commit_hash = serializers.CharField(max_length=40, required=False, allow_blank=True)
    git_branch = serializers.CharField(max_length=100, required=False, default="master")
    fail_at_stage = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def validate_git_commit_hash(self, value: str) -> str:
        if value:
            pattern = re.compile(r"^[0-9a-fA-F]{7,40}$")
            if not pattern.match(value):
                raise serializers.ValidationError(
                    "git_commit_hash must be a valid hex string of length between 7 and 40."
                )
        return value

    def validate_fail_at_stage(self, value: str | None) -> str | None:
        if value and value not in SmokeStageName.values:
            valid_stages = ", ".join(SmokeStageName.values)
            raise serializers.ValidationError(f"Invalid stage name '{value}'. Must be one of: {valid_stages}.")
        return value
