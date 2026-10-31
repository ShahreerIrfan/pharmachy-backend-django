"""
apps.data_privacy.serializers
Architecture Decision: ADR-0052
DRF Serializers for Data Subject Requests, Assessments, and Portability Exports.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.data_privacy.models import (
    DataSubjectType,
    PrivacyRequest,
    PrivacyRequestType,
)


class PrivacyRequestCreateSerializer(serializers.Serializer):
    """Input serializer for submitting Data Subject Requests."""

    subject_type = serializers.ChoiceField(
        choices=DataSubjectType.choices,
        default=DataSubjectType.PATIENT,
        required=False,
    )
    subject_identifier = serializers.CharField(
        max_length=128,
        required=True,
        help_text="Patient token, phone number, or user identifier",
    )
    request_type = serializers.ChoiceField(
        choices=PrivacyRequestType.choices,
        required=True,
    )
    branch_id = serializers.CharField(
        max_length=64,
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )
    correction_payload = serializers.JSONField(
        required=False,
        default=dict,
    )

    def validate_subject_identifier(self, value: str) -> str:
        clean = value.strip()
        if len(clean) < 3:
            raise serializers.ValidationError("Subject identifier must contain at least 3 characters.")
        return clean


class PrivacyRequestSerializer(serializers.ModelSerializer):
    """Output serializer for privacy request lifecycle details."""

    tenant_id = serializers.CharField(source="tenant.tenant_id", read_only=True)
    branch_id = serializers.CharField(source="branch.branch_id", read_only=True, allow_null=True)
    assessed_by_email = serializers.CharField(source="assessed_by.email", read_only=True, default="")

    class Meta:
        model = PrivacyRequest
        fields = [
            "request_id",
            "tenant_id",
            "branch_id",
            "subject_type",
            "subject_identifier",
            "request_type",
            "status",
            "reason",
            "correction_payload",
            "assessment_notes",
            "retention_conflict_detected",
            "retention_horizon_days",
            "export_artifact_url",
            "assessed_by_email",
            "assessed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "request_id",
            "status",
            "retention_conflict_detected",
            "retention_horizon_days",
            "export_artifact_url",
            "assessed_at",
            "created_at",
            "updated_at",
        ]


class PrivacyAssessmentActionSerializer(serializers.Serializer):
    """Input serializer for formal assessment decision by Owner or Compliance Officer."""

    action = serializers.ChoiceField(
        choices=["APPROVE", "REJECT", "ANONYMIZE"],
        required=True,
        help_text="Decision: APPROVE, REJECT, or ANONYMIZE under statutory hold",
    )
    assessment_notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Compliance or legal notes regarding retention schedule assessment",
    )
