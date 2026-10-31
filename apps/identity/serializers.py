"""
apps.identity.serializers
Architecture Decision: ADR-0042 & ADR-0043
DRF Serializers for Custom PharmacyUser Identity, Authentication, and Token Lifecycle.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.identity.models import (
    PharmacyUser,
    UserSession,
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
    device_id = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


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


class MfaSetupResponseSerializer(serializers.Serializer):
    """Serializes MFA enrollment initiation details (ADR-0044)."""

    secret = serializers.CharField()
    otpauth_uri = serializers.CharField()
    recovery_codes = serializers.ListField(child=serializers.CharField())
    issuer = serializers.CharField(default="PharmacySaasBD")


class MfaActivateRequestSerializer(serializers.Serializer):
    """Validates 6-digit TOTP verification code to confirm MFA activation."""

    code = serializers.CharField(min_length=6, max_length=6, required=True)


class MfaChallengeResponseSerializer(serializers.Serializer):
    """Serializes login second-factor challenge conforming to mfa_challenge.schema.json."""

    mfa_required = serializers.BooleanField(default=True)
    challenge_token = serializers.CharField()
    expires_in = serializers.IntegerField(default=300)
    method = serializers.CharField(default="TOTP")
    user_id = serializers.CharField()
    email = serializers.EmailField()
    role = serializers.CharField()
    remaining_attempts = serializers.IntegerField(default=5)


class MfaVerifyRequestSerializer(serializers.Serializer):
    """Validates MFA verification request with TOTP code or backup recovery code."""

    challenge_token = serializers.CharField(required=True, max_length=128)
    code = serializers.CharField(required=True, max_length=32, help_text="6-digit TOTP code or recovery code")
    device_id = serializers.CharField(required=False, default="client-terminal", max_length=64)


class MfaStatusResponseSerializer(serializers.Serializer):
    """Serializes user MFA security posture and recovery code telemetry."""

    mfa_enabled = serializers.BooleanField()
    is_mandatory = serializers.BooleanField()
    is_confirmed = serializers.BooleanField()
    remaining_recovery_codes = serializers.IntegerField()
    enrolled_at = serializers.DateTimeField(allow_null=True)


class MfaDisableRequestSerializer(serializers.Serializer):
    """Validates password confirmation to disable voluntary MFA."""

    current_password = serializers.CharField(write_only=True, required=True)


class UserSessionSerializer(serializers.ModelSerializer):
    """
    Serializes client device sessions conforming to user_session.schema.json (ADR-0045).
    """

    user_id = serializers.UUIDField(source="user.user_id", read_only=True)
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = UserSession
        fields = [
            "session_id",
            "user_id",
            "tenant_id",
            "device_id",
            "device_name",
            "client_platform",
            "ip_address",
            "user_agent",
            "is_active",
            "is_current",
            "last_seen_at",
            "created_at",
        ]
        read_only_fields = fields

    def get_is_current(self, obj) -> bool:
        request = self.context.get("request")
        if not request:
            return False
        token_session_id = getattr(request, "auth_session_id", None)
        if token_session_id:
            return str(obj.session_id) == str(token_session_id)
        current_jti = getattr(request, "auth_token_jti", None)
        if current_jti:
            return obj.current_refresh_jti == current_jti
        return False


class SessionRevokeResponseSerializer(serializers.Serializer):
    """Serializes outcome of a single session termination."""

    session_id = serializers.CharField()
    status = serializers.CharField(default="SUCCESS")
    revoked = serializers.BooleanField(default=True)
    message_en = serializers.CharField()
    message_bn = serializers.CharField()


class LogoutAllResponseSerializer(serializers.Serializer):
    """Serializes outcome of global multi-device logout."""

    status = serializers.CharField(default="SUCCESS")
    revoked_sessions_count = serializers.IntegerField()
    message_en = serializers.CharField()
    message_bn = serializers.CharField()

