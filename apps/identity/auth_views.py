"""
apps.identity.auth_views
Architecture Decision: ADR-0043 & ADR-0044
REST ViewSets for Authentication, JWT Token Lifecycle, Multi-Factor Authentication (MFA),
Profile, and Password Reset.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import pyotp
from django.conf import settings
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.identity.auth_backends import EmailOrPhoneModelBackend
from apps.identity.models import (
    MfaChallengeSession,
    PasswordResetToken,
    PharmacyUser,
    UserMfaDevice,
    normalize_bangladesh_phone,
)
from apps.identity.serializers import (
    LoginRequestSerializer,
    LoginResponseSerializer,
    LogoutRequestSerializer,
    MfaActivateRequestSerializer,
    MfaChallengeResponseSerializer,
    MfaDisableRequestSerializer,
    MfaSetupResponseSerializer,
    MfaStatusResponseSerializer,
    MfaVerifyRequestSerializer,
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

        # Check if MFA is required (ADR-0044)
        device = getattr(user, "mfa_device", None)
        requires_mfa = user.mfa_enabled or (user.is_privileged and device is not None and device.is_confirmed)

        if requires_mfa:
            if device and device.is_locked_out():
                return Response(
                    {
                        "error_code": "MFA_LOCKED_OUT",
                        "message_en": "Too many failed MFA attempts. Account MFA verification locked for 15 minutes.",
                        "message_bn": "অতিরিক্ত ভুল MFA চেষ্টার কারণে অ্যাকাউন্ট ১৫ মিনিটের জন্য সাময়িকভাবে লক করা হয়েছে।",
                        "details": {
                            "lockout_until": device.lockout_until.isoformat() if device.lockout_until else None,
                            "failed_attempts": device.failed_attempts,
                        },
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            challenge_token = secrets.token_urlsafe(32)
            expires_at = timezone.now() + timedelta(minutes=5)
            MfaChallengeSession.objects.create(
                user=user,
                challenge_token=challenge_token,
                device_id=serializer.validated_data.get("device_id", "client-terminal"),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
                expires_at=expires_at,
            )

            challenge_payload = {
                "mfa_required": True,
                "challenge_token": challenge_token,
                "expires_in": 300,
                "method": "TOTP",
                "user_id": str(user.user_id),
                "email": user.email,
                "role": user.role,
                "remaining_attempts": 5,
            }
            return Response(MfaChallengeResponseSerializer(challenge_payload).data, status=status.HTTP_200_OK)

        # Generate JWT tokens with tenant/role claims
        refresh = RefreshToken.for_user(user)
        refresh["email"] = user.email
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

    @action(detail=False, methods=["post"], url_path="mfa/setup", permission_classes=[permissions.IsAuthenticated])
    def mfa_setup(self, request):
        """
        Initiates RFC 6238 TOTP enrollment and provisions 8 single-use recovery codes.
        """
        user = request.user
        secret = pyotp.random_base32()
        raw_recovery_codes = [f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}" for _ in range(8)]
        hashed_codes = [
            {
                "code_hash": hashlib.sha256(c.encode("utf-8")).hexdigest(),
                "is_used": False,
                "used_at": None,
            }
            for c in raw_recovery_codes
        ]

        UserMfaDevice.objects.update_or_create(
            user=user,
            defaults={
                "secret_key": secret,
                "is_confirmed": False,
                "recovery_codes": hashed_codes,
                "failed_attempts": 0,
                "lockout_until": None,
            },
        )

        totp = pyotp.TOTP(secret)
        otpauth_uri = totp.provisioning_uri(name=user.email, issuer_name="PharmacySaasBD")

        payload = {
            "secret": secret,
            "otpauth_uri": otpauth_uri,
            "recovery_codes": raw_recovery_codes,
            "issuer": "PharmacySaasBD",
        }
        return Response(MfaSetupResponseSerializer(payload).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="mfa/activate", permission_classes=[permissions.IsAuthenticated])
    def mfa_activate(self, request):
        """
        Validates TOTP code to confirm enrollment and activate MFA for user session.
        """
        serializer = MfaActivateRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "6-digit TOTP code is required.",
                    "message_bn": "৬-সংখ্যার TOTP কোড আবশ্যক।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        device = getattr(user, "mfa_device", None)
        if not device:
            return Response(
                {
                    "error_code": "MFA_NOT_INITIALIZED",
                    "message_en": "MFA setup has not been initiated. Call /mfa/setup/ first.",
                    "message_bn": "MFA সেটআপ শুরু করা হয়নি। প্রথমে /mfa/setup/ কল করুন।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        code = serializer.validated_data["code"]
        totp = pyotp.TOTP(device.secret_key)
        if not totp.verify(code, valid_window=1):
            return Response(
                {
                    "error_code": "INVALID_TOTP_CODE",
                    "message_en": "Invalid TOTP verification code.",
                    "message_bn": "ভুল TOTP ভেরিফিকেশন কোড।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        device.is_confirmed = True
        device.failed_attempts = 0
        device.lockout_until = None
        device.save(update_fields=["is_confirmed", "failed_attempts", "lockout_until", "updated_at"])

        user.mfa_enabled = True
        user.save(update_fields=["mfa_enabled", "updated_at"])

        return Response(
            {
                "status": "SUCCESS",
                "message_en": "Multi-factor authentication activated successfully.",
                "message_bn": "মাল্টি-ফ্যাক্টর অথেনটিকেশন সফলভাবে সক্রিয় করা হয়েছে।",
                "mfa_enabled": True,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="mfa/verify", permission_classes=[permissions.AllowAny])
    def mfa_verify(self, request):
        """
        Verifies login second-factor challenge using TOTP code or single-use recovery code.
        Upon success, issues full JWT tokens conforming to AuthTokensPayload.
        """
        serializer = MfaVerifyRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Challenge token and verification code are required.",
                    "message_bn": "চ্যালেঞ্জ টোকেন এবং ভেরিফিকেশন কোড আবশ্যক।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        challenge_token = serializer.validated_data["challenge_token"]
        raw_code = serializer.validated_data["code"].strip()

        challenge = MfaChallengeSession.objects.filter(challenge_token=challenge_token).first()
        if not challenge:
            return Response(
                {
                    "error_code": "INVALID_CHALLENGE",
                    "message_en": "MFA challenge token is invalid or expired.",
                    "message_bn": "MFA চ্যালেঞ্জ টোকেনটি অবৈধ বা মেয়াদোত্তীর্ণ।",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if challenge.is_verified:
            return Response(
                {
                    "error_code": "CHALLENGE_ALREADY_VERIFIED",
                    "message_en": "MFA challenge token has already been verified.",
                    "message_bn": "এই MFA চ্যালেঞ্জ টোকেনটি ইতোমধ্যে ব্যবহার করা হয়েছে।",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if timezone.now() > challenge.expires_at:
            return Response(
                {
                    "error_code": "CHALLENGE_EXPIRED",
                    "message_en": "MFA challenge token has expired. Please log in again.",
                    "message_bn": "MFA চ্যালেঞ্জের সময় শেষ হয়েছে। দয়া করে পুনরায় লগইন করুন।",
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Cross-tenant security boundary check
        header_tenant = request.headers.get("X-Tenant-Id") or request.META.get("HTTP_X_TENANT_ID")
        if header_tenant and header_tenant != challenge.user.tenant_id:
            return Response(
                {
                    "error_code": "CROSS_TENANT_MFA_FORBIDDEN",
                    "message_en": "Cross-tenant MFA challenge exchange is strictly forbidden.",
                    "message_bn": "ক্রস-টেন্যান্ট MFA ভেরিফিকেশন কঠোরভাবে নিষিদ্ধ।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        user = challenge.user
        device = getattr(user, "mfa_device", None)

        # Lockout check
        if challenge.is_locked_out() or (device and device.is_locked_out()):
            lockout_time = (
                device.lockout_until.isoformat() if device and device.lockout_until else timezone.now().isoformat()
            )
            return Response(
                {
                    "error_code": "MFA_LOCKED_OUT",
                    "message_en": "Too many failed MFA attempts. Account MFA verification locked for 15 minutes.",
                    "message_bn": "অতিরিক্ত ভুল MFA চেষ্টার কারণে অ্যাকাউন্ট ১৫ মিনিটের জন্য সাময়িকভাবে লক করা হয়েছে।",
                    "details": {"lockout_until": lockout_time, "failed_attempts": 5},
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        verified = False

        if device and device.is_confirmed:
            # 1. Try TOTP code
            clean_digits = raw_code.replace(" ", "").replace("-", "")
            if clean_digits.isdigit() and len(clean_digits) == 6:
                totp = pyotp.TOTP(device.secret_key)
                if totp.verify(clean_digits, valid_window=1):
                    verified = True

            # 2. Try single-use recovery code if not verified as TOTP
            if not verified:
                candidate_hash = hashlib.sha256(raw_code.upper().encode("utf-8")).hexdigest()
                codes = list(device.recovery_codes)
                for item in codes:
                    if item.get("code_hash") == candidate_hash and not item.get("is_used", False):
                        item["is_used"] = True
                        item["used_at"] = timezone.now().isoformat()
                        device.recovery_codes = codes
                        device.save(update_fields=["recovery_codes", "updated_at"])
                        verified = True
                        break

        if verified:
            challenge.is_verified = True
            challenge.save(update_fields=["is_verified"])

            if device:
                device.failed_attempts = 0
                device.last_used_at = timezone.now()
                device.save(update_fields=["failed_attempts", "last_used_at", "updated_at"])

            # Issue full JWT tokens
            refresh = RefreshToken.for_user(user)
            refresh["email"] = user.email
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

        # Verification failed -> Increment failure counter
        challenge.failed_attempts += 1
        challenge.save(update_fields=["failed_attempts"])

        if device:
            device.failed_attempts += 1
            if device.failed_attempts >= 5:
                device.lockout_until = timezone.now() + timedelta(minutes=15)
            device.save(update_fields=["failed_attempts", "lockout_until", "updated_at"])

        remaining = max(0, 5 - challenge.failed_attempts)
        if remaining == 0:
            return Response(
                {
                    "error_code": "MFA_LOCKED_OUT",
                    "message_en": "Too many failed MFA attempts. Account MFA verification locked for 15 minutes.",
                    "message_bn": "অতিরিক্ত ভুল MFA চেষ্টার কারণে অ্যাকাউন্ট ১৫ মিনিটের জন্য সাময়িকভাবে লক করা হয়েছে।",
                    "details": {"remaining_attempts": 0, "failed_attempts": 5},
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(
            {
                "error_code": "INVALID_MFA_CODE",
                "message_en": "Invalid MFA verification code.",
                "message_bn": "ভুল MFA ভেরিফিকেশন কোড।",
                "details": {"remaining_attempts": remaining},
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )

    @action(detail=False, methods=["get"], url_path="mfa/status", permission_classes=[permissions.IsAuthenticated])
    def mfa_status(self, request):
        """
        Retrieves MFA security posture, mandatory status, and remaining recovery codes count.
        """
        user = request.user
        device = getattr(user, "mfa_device", None)
        payload = {
            "mfa_enabled": user.mfa_enabled,
            "is_mandatory": user.is_privileged,
            "is_confirmed": bool(device and device.is_confirmed),
            "remaining_recovery_codes": device.remaining_recovery_codes_count() if device else 0,
            "enrolled_at": device.created_at if (device and device.is_confirmed) else None,
        }
        return Response(MfaStatusResponseSerializer(payload).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="mfa/disable", permission_classes=[permissions.IsAuthenticated])
    def mfa_disable(self, request):
        """
        Disables voluntary MFA for unprivileged users. Rejects privileged role opt-out.
        """
        user = request.user
        if user.is_privileged:
            return Response(
                {
                    "error_code": "MFA_MANDATORY_FOR_PRIVILEGED_ROLE",
                    "message_en": "MFA is strictly mandatory for privileged roles and cannot be disabled.",
                    "message_bn": "প্রিভিলেজড ব্যবহারকারীদের জন্য MFA বাধ্যতামূলক এবং এটি নিষ্ক্রিয় করা যাবে না।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = MfaDisableRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Current password is required to disable MFA.",
                    "message_bn": "MFA নিষ্ক্রিয় করতে বর্তমান পাসওয়ার্ড আবশ্যক।",
                    "details": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user.check_password(serializer.validated_data["current_password"]):
            return Response(
                {
                    "error_code": "INVALID_CREDENTIALS",
                    "message_en": "Invalid current password.",
                    "message_bn": "ভুল বর্তমান পাসওয়ার্ড।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.mfa_enabled = False
        user.save(update_fields=["mfa_enabled", "updated_at"])

        if hasattr(user, "mfa_device"):
            user.mfa_device.delete()

        return Response(
            {
                "status": "SUCCESS",
                "message_en": "MFA disabled successfully.",
                "message_bn": "MFA সফলভাবে নিষ্ক্রিয় করা হয়েছে।",
                "mfa_enabled": False,
            },
            status=status.HTTP_200_OK,
        )
