"""
apps.identity.serializers
Architecture Decision: ADR-0042 & ADR-0043
DRF Serializers for Custom PharmacyUser Identity, Authentication, and Token Lifecycle.
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


class LoginRequestSerializer(serializers.Serializer):
    """
    Validates login credentials supporting email or Bangladesh phone number.
    """

    identifier = serializers.CharField(
        max_length=128, required=True, help_text="Email address or Bangladesh mobile number (+8801...)"
    )
    password = serializers.CharField(write_only=True, required=True)
    device_id = serializers.CharField(max_length=64, required=False, default="client-terminal")


PharmacyUserLoginSerializer = LoginRequestSerializer


class LoginResponseSerializer(serializers.Serializer):
    """
    Serializes authentication response payload conforming to auth_tokens.schema.json.
    """

    access = serializers.CharField()
    refresh = serializers.CharField()
    token_type = serializers.CharField(default="Bearer")
    expires_in = serializers.IntegerField(default=900)
    user = PharmacyUserSerializer()


class LogoutRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=True, help_text="JWT refresh token to blacklist")


class TokenRefreshRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=True)


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PharmacyUser
        fields = [
            "full_name",
            "pcb_registration_no",
        ]


class PasswordResetRequestSerializer(serializers.Serializer):
    identifier = serializers.CharField(
        max_length=128, required=True, help_text="Email address or Bangladesh mobile number"
    )


class PasswordResetConfirmSerializer(serializers.Serializer):
    reset_token = serializers.CharField(required=True, max_length=128)
    new_password = serializers.CharField(write_only=True, min_length=8, required=True)
