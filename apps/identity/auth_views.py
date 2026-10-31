"""
apps.identity.auth_views
Architecture Decision: ADR-0043
REST ViewSets for Authentication, JWT Token Lifecycle, Profile, and Password Reset.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.identity.auth_backends import EmailOrPhoneModelBackend
from apps.identity.models import (
    PasswordResetToken,
    PharmacyUser,
    normalize_bangladesh_phone,
)
from apps.identity.serializers import (
    LoginRequestSerializer,
    LoginResponseSerializer,
    LogoutRequestSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PharmacyUserSerializer,
    TokenRefreshRequestSerializer,
    UserProfileUpdateSerializer,
)


class AuthenticationViewSet(viewsets.ViewSet):
    """
    Public and authenticated authentication endpoints conforming to ADR-0043.
    """

    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["post"])
    def login(self, request):
        """
        Dual-identifier login accepting email or Bangladesh mobile number.
        Returns JWT access (15m) and refresh (7d) tokens.
        """
        serializer = LoginRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Invalid login parameters.",
                    "message_bn": "ভুল লগইন প্যারামিটার।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        identifier = serializer.validated_data["identifier"]
        password = serializer.validated_data["password"]

        backend = EmailOrPhoneModelBackend()
        user = backend.authenticate(request, username=identifier, password=password)

        if not user or user.is_archived or not user.is_active:
            return Response(
                {
                    "error_code": "INVALID_CREDENTIALS",
                    "message_en": "Invalid email/phone or password.",
                    "message_bn": "ভুল ইমেইল/ফোন অথবা পাসওয়ার্ড।",
                    "details": "Authentication failed. Check credentials or account status.",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Generate JWT tokens with tenant/role claims
        refresh = RefreshToken.for_user(user)
        refresh["tenant_id"] = user.tenant_id
        refresh["branch_id"] = user.branch_id
        refresh["role"] = user.role

        access_token = str(refresh.access_token)
        refresh_token = str(refresh)

        response_payload = {
            "access": access_token,
            "refresh": refresh_token,
            "token_type": "Bearer",
            "expires_in": 900,
            "user": PharmacyUserSerializer(user).data,
        }

        return Response(LoginResponseSerializer(response_payload).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"])
    def logout(self, request):
        """
        Blacklists the refresh token and closes the session.
        """
        serializer = LogoutRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Refresh token is required to logout.",
                    "message_bn": "লগআউট করার জন্য রিফ্রেশ টোকেন আবশ্যক।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        refresh_token = serializer.validated_data["refresh"]
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except Exception:
            # Client-safe: if already blacklisted or expired, acknowledge logout cleanly
            pass

        return Response(
            {
                "detail": "Successfully logged out.",
                "revoked": True,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="token/refresh")
    def token_refresh(self, request):
        """
        Refreshes JWT access token and rotates refresh token.
        Replaying blacklisted tokens is strictly rejected.
        """
        serializer = TokenRefreshRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Refresh token is required.",
                    "message_bn": "রিফ্রেশ টোকেন প্রদান করুন।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_refresh = serializer.validated_data["refresh"]
        try:
            token = RefreshToken(raw_refresh)
            access_str = str(token.access_token)
            data = {
                "access": access_str,
                "token_type": "Bearer",
                "expires_in": 900,
            }

            if api_settings.ROTATE_REFRESH_TOKENS:
                token.blacklist()
                user_id = token.payload.get("user_id")
                user = PharmacyUser.objects.filter(user_id=user_id, is_archived=False).first()
                if user:
                    new_refresh = RefreshToken.for_user(user)
                    new_refresh["tenant_id"] = user.tenant_id
                    new_refresh["branch_id"] = user.branch_id
                    new_refresh["role"] = user.role
                    data["refresh"] = str(new_refresh)

            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {
                    "error_code": "TOKEN_BLACKLISTED_OR_EXPIRED",
                    "message_en": "Refresh token is invalid, expired, or has been blacklisted.",
                    "message_bn": "রিফ্রেশ টোকেনটি মেয়াদোত্তীর্ণ বা ব্ল্যাকলিস্ট করা হয়েছে।",
                    "details": str(e),
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

    @action(
        detail=False,
        methods=["get", "patch"],
        permission_classes=[permissions.IsAuthenticated],
    )
    def profile(self, request):
        """
        Retrieves or updates authenticated user profile with role escalation guard.
        """
        user = request.user
        if request.method == "GET":
            serializer = PharmacyUserSerializer(user)
            return Response(serializer.data, status=status.HTTP_200_OK)

        # PATCH
        serializer = UserProfileUpdateSerializer(user, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "VALIDATION_ERROR",
                    "message_en": "Profile update parameters are invalid.",
                    "message_bn": "প্রোফাইল আপডেট প্যারামিটারগুলো অবৈধ।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer.save()
        return Response(PharmacyUserSerializer(user).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="password-reset/request")
    def password_reset_request(self, request):
        """
        Anti-enumeration password reset request. Returns client-safe generic 200 OK.
        """
        serializer = PasswordResetRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Identifier is required.",
                    "message_bn": "ইমেইল বা ফোন নম্বর প্রদান করুন।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_id = serializer.validated_data["identifier"].strip()
        user = None

        # Look up by phone if digits
        if any(c.isdigit() for c in raw_id) and "@" not in raw_id:
            try:
                norm = normalize_bangladesh_phone(raw_id)
                user = PharmacyUser.objects.filter(phone_number=norm, is_archived=False).first()
            except Exception:
                pass

        if user is None and "@" in raw_id:
            user = PharmacyUser.objects.filter(email__iexact=raw_id.lower(), is_archived=False).first()

        reset_token_str = None
        if user and user.is_active:
            token_val = secrets.token_urlsafe(32)
            otp_val = f"{secrets.randbelow(900000) + 100000:06d}"
            expires_at = timezone.now() + timedelta(minutes=15)

            # Invalidate older tokens
            PasswordResetToken.objects.filter(user=user, is_used=False).update(is_used=True)

            PasswordResetToken.objects.create(
                user=user,
                token=token_val,
                otp_code=otp_val,
                expires_at=expires_at,
            )
            reset_token_str = token_val

        # Anti-enumeration response: always returns 200 OK
        resp_data = {
            "status": "DISPATCHED",
            "message_en": "If the account exists, password reset instructions have been dispatched.",
            "message_bn": "যদি অ্যাকাউন্টটি বিদ্যমান থাকে, পাসওয়ার্ড রিসেট নির্দেশাবলী পাঠানো হয়েছে।",
        }
        # Include token in response for test / dev environment automation
        if getattr(settings, "DJANGO_ENV", "") in ("development", "testing"):
            resp_data["dev_reset_token"] = reset_token_str

        return Response(resp_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="password-reset/confirm")
    def password_reset_confirm(self, request):
        """
        Confirms password reset using time-bound cryptographic token.
        """
        serializer = PasswordResetConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Reset token and new password are required.",
                    "message_bn": "রিসেট টোকেন এবং নতুন পাসওয়ার্ড আবশ্যক।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        token_str = serializer.validated_data["reset_token"]
        new_password = serializer.validated_data["new_password"]

        reset_record = PasswordResetToken.objects.filter(token=token_str).first()
        if not reset_record or not reset_record.is_valid():
            return Response(
                {
                    "error_code": "INVALID_RESET_TOKEN",
                    "message_en": "Password reset token is invalid or has expired.",
                    "message_bn": "পাসওয়ার্ড রিসেট টোকেনটি অবৈধ বা মেয়াদোত্তীর্ণ।",
                    "details": "The token was either already used or the 15-minute window expired.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = reset_record.user
        user.set_password(new_password)
        user.password_changed_at = timezone.now()
        user.failed_login_attempts = 0
        user.lockout_until = None
        user.save(
            update_fields=[
                "password",
                "password_changed_at",
                "failed_login_attempts",
                "lockout_until",
                "updated_at",
            ]
        )

        reset_record.is_used = True
        reset_record.save(update_fields=["is_used"])

        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Password has been reset successfully. Please log in with your new credentials.",
                "message_bn": "পাসওয়ার্ড সফলভাবে পরিবর্তিত হয়েছে। অনুগ্রহ করে নতুন পাসওয়ার্ড দিয়ে লগইন করুন।",
            },
            status=status.HTTP_200_OK,
        )
