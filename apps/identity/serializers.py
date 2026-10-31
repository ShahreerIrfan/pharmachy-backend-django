"""
apps.identity.serializers
Architecture Decision: ADR-0042
DRF Serializers for Custom PharmacyUser Identity, Registration, and Safe Deletion.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.identity.models import (
    PharmacyUser,
    normalize_bangladesh_phone,
)


class PharmacyUserSerializer(serializers.ModelSerializer):
    """
    Serializes PharmacyUser conforming to user_identity.schema.json.
    """

    class Meta:
        model = PharmacyUser
        fields = [
            "user_id",
            "email",
            "phone_number",
            "full_name",
            "tenant_id",
            "branch_id",
            "role",
            "pcb_registration_no",
            "activation_status",
            "is_active",
            "is_staff",
            "is_archived",
            "archived_at",
            "archived_by",
            "failed_login_attempts",
            "created_at",
        ]
        read_only_fields = [
            "user_id",
            "is_active",
            "is_staff",
            "is_archived",
            "archived_at",
            "archived_by",
            "failed_login_attempts",
            "created_at",
        ]


class PharmacyUserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = PharmacyUser
        fields = [
            "user_id",
            "email",
            "phone_number",
            "password",
            "full_name",
            "tenant_id",
            "branch_id",
            "role",
            "pcb_registration_no",
        ]
        read_only_fields = ["user_id"]

    def validate_phone_number(self, value: str) -> str:
        return normalize_bangladesh_phone(value)

    def validate_email(self, value: str) -> str:
        return value.lower().strip()

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = PharmacyUser.objects.create_user(password=password, **validated_data)
        return user


class PharmacyUserArchiveRequestSerializer(serializers.Serializer):
    archival_reason = serializers.CharField(max_length=500, required=True)


class PharmacyUserLoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=128, required=True, help_text="Email or Bangladesh mobile phone")
    password = serializers.CharField(write_only=True, required=True)
