"""
apps.quality_governance.serializers
Architecture Decision: ADR-0039
Serializers for Quality Issue Tickets, Bug Reproduction, and Definition of Done Audits.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.quality_governance.engine import DefinitionOfDoneEngine
from apps.quality_governance.models import (
    DefinitionOfDoneAudit,
    IssueCategory,
    QualityIssueTicket,
)


class QualityIssueTicketSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(max_length=64, required=False, allow_blank=True)

    class Meta:
        model = QualityIssueTicket
        fields = [
            "issue_id",
            "tenant_id",
            "title",
            "category",
            "severity",
            "client_platform",
            "client_version",
            "environment",
            "connectivity_state",
            "reproduction_steps",
            "expected_behavior",
            "observed_behavior",
            "sanitized_logs",
            "is_resolved",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["issue_id", "created_at", "updated_at"]

    def validate(self, attrs):
        category = attrs.get("category", getattr(self.instance, "category", IssueCategory.BUG))
        if category == IssueCategory.BUG:
            repro = attrs.get("reproduction_steps", getattr(self.instance, "reproduction_steps", ""))
            exp = attrs.get("expected_behavior", getattr(self.instance, "expected_behavior", ""))
            obs = attrs.get("observed_behavior", getattr(self.instance, "observed_behavior", ""))

            if not repro or len(repro.strip()) < 10:
                raise serializers.ValidationError(
                    {"reproduction_steps": "Detailed step-by-step reproduction is mandatory for bug reports."}
                )
            if not exp:
                raise serializers.ValidationError(
                    {"expected_behavior": "Expected behavior is required for bug reports."}
                )
            if not obs:
                raise serializers.ValidationError(
                    {"observed_behavior": "Observed behavior is required for bug reports."}
                )
        return attrs


class DefinitionOfDoneAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = DefinitionOfDoneAudit
        fields = [
            "audit_id",
            "prompt_id",
            "feature_title",
            "tenant_id",
            "status",
            "traceability_pass",
            "security_phi_pass",
            "decimal_money_pass",
            "fefo_stock_pass",
            "migration_safety_pass",
            "multiplatform_ui_pass",
            "test_evidence_pass",
            "has_critical_clinical_safety_flaw",
            "test_command",
            "tests_passed",
            "tests_failed",
            "execution_time_seconds",
            "evidence_log_hash",
            "reviewer_role",
            "evaluated_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["audit_id", "status", "created_at", "updated_at"]


class DoDEvaluationRequestSerializer(serializers.Serializer):
    prompt_id = serializers.CharField(max_length=16)
    feature_title = serializers.CharField(max_length=200)
    tenant_id = serializers.CharField(max_length=64)
    traceability_pass = serializers.BooleanField(default=False)
    security_phi_pass = serializers.BooleanField(default=False)
    decimal_money_pass = serializers.BooleanField(default=False)
    fefo_stock_pass = serializers.BooleanField(default=False)
    migration_safety_pass = serializers.BooleanField(default=False)
    multiplatform_ui_pass = serializers.BooleanField(default=False)
    test_evidence_pass = serializers.BooleanField(default=False)
    has_critical_clinical_safety_flaw = serializers.BooleanField(default=False)
    test_command = serializers.CharField(max_length=300)
    tests_passed = serializers.IntegerField(min_value=0)
    tests_failed = serializers.IntegerField(min_value=0, default=0)
    execution_time_seconds = serializers.DecimalField(max_digits=8, decimal_places=3, default=0.0)
    evidence_log_hash = serializers.CharField(max_length=64, required=False, allow_blank=True)
    reviewer_role = serializers.CharField(max_length=32, default="ADMIN")

    def create(self, validated_data):
        dod_res = DefinitionOfDoneEngine.evaluate(validated_data)
        audit = DefinitionOfDoneAudit.objects.create(
            prompt_id=validated_data["prompt_id"],
            feature_title=validated_data["feature_title"],
            tenant_id=validated_data["tenant_id"],
            status=dod_res.status,
            traceability_pass=validated_data["traceability_pass"],
            security_phi_pass=validated_data["security_phi_pass"],
            decimal_money_pass=validated_data["decimal_money_pass"],
            fefo_stock_pass=validated_data["fefo_stock_pass"],
            migration_safety_pass=validated_data["migration_safety_pass"],
            multiplatform_ui_pass=validated_data["multiplatform_ui_pass"],
            test_evidence_pass=validated_data["test_evidence_pass"],
            has_critical_clinical_safety_flaw=validated_data["has_critical_clinical_safety_flaw"],
            test_command=validated_data["test_command"],
            tests_passed=validated_data["tests_passed"],
            tests_failed=validated_data["tests_failed"],
            execution_time_seconds=validated_data["execution_time_seconds"],
            evidence_log_hash=validated_data.get("evidence_log_hash", ""),
            reviewer_role=validated_data.get("reviewer_role", "ADMIN"),
        )
        return audit
