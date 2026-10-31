"""
apps.identity.auth_backends
Architecture Decision: ADR-0042
Authentication Backend supporting dual-identifier authentication (Email or Bangladesh Phone).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from apps.identity.models import ActivationStatus, normalize_bangladesh_phone


class EmailOrPhoneModelBackend(ModelBackend):
    """
    Authenticates against either email or normalized Bangladesh mobile phone number.
    Enforces lockout after 5 consecutive failed attempts.
    """

    def authenticate(self, request, username: Optional[str] = None, password: Optional[str] = None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD) or kwargs.get("phone_number")
        if not username or not password:
            return None

        username = str(username).strip()
        user: Optional[UserModel] = None

        # Attempt lookup by normalized phone first if digits detected
        if any(c.isdigit() for c in username) and "@" not in username:
            try:
                norm_phone = normalize_bangladesh_phone(username)
                user = UserModel.objects.filter(phone_number=norm_phone, is_archived=False).first()
            except Exception:
                pass

        # Attempt lookup by normalized email
        if user is None and "@" in username:
            user = UserModel.objects.filter(email__iexact=username.lower(), is_archived=False).first()

        # Fallback exact lookup
        if user is None:
            user = UserModel.objects.filter(email__iexact=username, is_archived=False).first()

        if user is None:
            return None

        # Check temporary lockout
        if user.lockout_until and timezone.now() < user.lockout_until:
            return None

        # Verify password
        if user.check_password(password) and self.user_can_authenticate(user):
            user.failed_login_attempts = 0
            user.lockout_until = None
            if request:
                user.last_login_ip = request.META.get("REMOTE_ADDR")
                user.last_login_user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
            user.save(
                update_fields=[
                    "failed_login_attempts",
                    "lockout_until",
                    "last_login_ip",
                    "last_login_user_agent",
                    "updated_at",
                ]
            )
            return user
        else:
            # Increment failed attempts and trigger lockout at 5 failures
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.lockout_until = timezone.now() + timedelta(minutes=15)
                user.activation_status = ActivationStatus.SUSPENDED
            user.save(
                update_fields=[
                    "failed_login_attempts",
                    "lockout_until",
                    "activation_status",
                    "updated_at",
                ]
            )
            return None
