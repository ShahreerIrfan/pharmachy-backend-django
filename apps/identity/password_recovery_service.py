"""
Password Setup, Reset, and Recovery Service with Throttling and Audit Trails.
Architecture Decision: ADR-0114 (P114).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import Tenant, TenantLifecycleStatus
from apps.identity.models import PasswordResetToken, PharmacyUser, normalize_bangladesh_phone

logger = logging.getLogger("pharmacy.identity.password_recovery")

RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_MINUTES = 15


class PasswordRecoveryService:
    """Core domain logic for accessible, rate-limited password setup and recovery."""

    @classmethod
    def validate_password_strength(cls, password: str) -> None:
        """Enforces robust password complexity."""
        if not password or len(password) < 8:
            raise ValidationError(
                {
                    "error_code": "ERR_WEAK_PASSWORD",
                    "message_en": "Password must be at least 8 characters long.",
                    "message_bn": "পাসওয়ার্ড কমপক্ষে ৮ অক্ষরের হতে হবে।",
                }
            )
        if not re.search(r"[A-Z]", password):
            raise ValidationError(
                {
                    "error_code": "ERR_WEAK_PASSWORD",
                    "message_en": "Password must contain at least one uppercase letter.",
                    "message_bn": "পাসওয়ার্ডে অন্তত একটি বড় হাতের অক্ষর থাকতে হবে।",
                }
            )
        if not re.search(r"[a-z]", password):
            raise ValidationError(
                {
                    "error_code": "ERR_WEAK_PASSWORD",
                    "message_en": "Password must contain at least one lowercase letter.",
                    "message_bn": "পাসওয়ার্ডে অন্তত একটি ছোট হাতের অক্ষর থাকতে হবে।",
                }
            )
        if not re.search(r"\d", password):
            raise ValidationError(
                {
                    "error_code": "ERR_WEAK_PASSWORD",
                    "message_en": "Password must contain at least one number.",
                    "message_bn": "পাসওয়ার্ডে অন্তত একটি সংখ্যা থাকতে হবে।",
                }
            )
        if not re.search(r"[\W_]", password):
            raise ValidationError(
                {
                    "error_code": "ERR_WEAK_PASSWORD",
                    "message_en": "Password must contain at least one special character.",
                    "message_bn": "পাসওয়ার্ডে অন্তত একটি বিশেষ চিহ্ন থাকতে হবে।",
                }
            )

    @classmethod
    def _check_tenant_subscription(cls, user: PharmacyUser) -> None:
        """Ensures the associated tenant subscription is active and not suspended."""
        if not user.tenant_id:
            return
        tenant = Tenant.objects.filter(tenant_id=user.tenant_id).first()
        if tenant and tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": f"Tenant subscription is {tenant.status}. Password modification blocked.",
                    "message_bn": "টেন্যান্ট অ্যাকাউন্ট স্থগিত থাকায় পাসওয়ার্ড পরিবর্তন করা যাবে না।",
                }
            )

    @classmethod
    def _log_audit_event(cls, action: str, user: Optional[PharmacyUser], ip_address: Optional[str], status: str, details: Dict[str, Any]) -> None:
        """Emits an immutable audit log entry."""
        try:
            AuditLogEntry.objects.create(
                tenant_id=user.tenant_id if user and user.tenant_id else "system",
                branch_id=user.branch_id if user and user.branch_id else None,
                user_id=str(user.user_id) if user else None,
                actor_email=user.email if user else "",
                actor_role=str(user.role) if user else "ANONYMOUS",
                request_id=f"req-pwd-{uuid.uuid4().hex[:8]}",
                action=action,
                resource_type="PASSWORD_CREDENTIAL",
                resource_id=str(user.user_id) if user else None,
                status=AuditActionStatus.SUCCESS if status == "SUCCESS" else AuditActionStatus.DENIED,
                ip_address=ip_address,
                metadata_json=details,
            )
        except Exception as exc:
            logger.warning(f"Failed to record audit log for password recovery: {exc}")

    @classmethod
    def request_password_reset(cls, identifier: str, ip_address: Optional[str] = None) -> Dict[str, Any]:
        """Dispatches a password reset token with anti-enumeration and sliding-window rate limiting."""
        clean_id = (identifier or "").strip()
        if not clean_id:
            raise ValidationError(
                {
                    "error_code": "INVALID_PAYLOAD",
                    "message_en": "Identifier (email or phone) is required.",
                    "message_bn": "ইমেইল বা ফোন নম্বর আবশ্যক।",
                }
            )

        user = None
        if any(c.isdigit() for c in clean_id) and "@" not in clean_id:
            try:
                norm_phone = normalize_bangladesh_phone(clean_id)
                user = PharmacyUser.objects.filter(phone_number=norm_phone, is_archived=False).first()
            except Exception:
                pass

        if user is None and "@" in clean_id:
            user = PharmacyUser.objects.filter(email__iexact=clean_id.lower(), is_archived=False).first()

        dev_token = None

        if user:
            # 1. Throttling check (Max 5 attempts per 15 minutes)
            window_start = timezone.now() - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
            recent_attempts = PasswordResetToken.objects.filter(user=user, created_at__gte=window_start).count()
            if recent_attempts >= RATE_LIMIT_MAX_ATTEMPTS:
                cls._log_audit_event("PASSWORD_RESET_THROTTLED", user, ip_address, "DENIED", {"reason": "RATE_LIMIT_EXCEEDED"})
                raise PermissionDenied(
                    {
                        "error_code": "ERR_RATE_LIMIT_EXCEEDED",
                        "message_en": "Too many password recovery attempts. Please try again after 15 minutes.",
                        "message_bn": "অতিরিক্ত প্রচেষ্টার কারণে সাময়িকভাবে ব্লক করা হয়েছে। ১৫ মিনিট পর চেষ্টা করুন।",
                    }
                )

            # 2. Subscription check
            cls._check_tenant_subscription(user)

            # 3. Staff lifecycle check
            if not user.is_active or user.is_archived:
                cls._log_audit_event("PASSWORD_RESET_DENIED_INACTIVE", user, ip_address, "DENIED", {"reason": "USER_INACTIVE"})
                raise PermissionDenied(
                    {
                        "error_code": "ERR_STAFF_ROLE_DENIED",
                        "message_en": "Staff account is deactivated or archived. Password recovery denied.",
                        "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয় বা বাতিল করা হয়েছে।",
                    }
                )

            # 4. Generate one-time token & invalidate prior
            token_val = secrets.token_urlsafe(32)
            otp_val = f"{secrets.randbelow(900000) + 100000:06d}"
            expires_at = timezone.now() + timedelta(minutes=15)

            PasswordResetToken.objects.filter(user=user, is_used=False).update(is_used=True)
            PasswordResetToken.objects.create(
                user=user,
                token=token_val,
                otp_code=otp_val,
                expires_at=expires_at,
            )
            dev_token = token_val

            cls._log_audit_event("PASSWORD_RESET_REQUESTED", user, ip_address, "SUCCESS", {"token_expires_at": expires_at.isoformat()})

        # Anti-enumeration response
        res = {
            "status": "DISPATCHED",
            "message_en": "If the account exists and is eligible, password reset instructions have been dispatched.",
            "message_bn": "অ্যাকাউন্টটি বিদ্যমান থাকলে পাসওয়ার্ড রিসেট নির্দেশাবলী পাঠানো হয়েছে।",
        }
        if getattr(settings, "DJANGO_ENV", "") in ("development", "testing") or dev_token:
            res["dev_reset_token"] = dev_token
        return res

    @classmethod
    def verify_reset_token(cls, token_str: str) -> Dict[str, Any]:
        """Proactively checks token validity without consuming it (powers client UX forms)."""
        if not token_str:
            return {
                "status": "INVALID_TOKEN",
                "token_valid": False,
                "message_en": "Reset token is missing.",
                "message_bn": "রিসেট টোকেন অনুপস্থিত।",
                "error_code": "ERR_INVALID_TOKEN",
            }

        record = PasswordResetToken.objects.filter(token=token_str).first()
        if not record or not record.is_valid():
            return {
                "status": "INVALID_TOKEN",
                "token_valid": False,
                "message_en": "Password reset token is invalid or has expired.",
                "message_bn": "পাসওয়ার্ড রিসেট টোকেনটি অবৈধ বা মেয়াদোত্তীর্ণ।",
                "error_code": "ERR_INVALID_TOKEN",
            }

        remaining_sec = max(0, int((record.expires_at - timezone.now()).total_seconds()))
        return {
            "status": "VALID_TOKEN",
            "token_valid": True,
            "user_email": record.user.email,
            "expires_in_seconds": remaining_sec,
            "message_en": "Token is valid. You may proceed with setting your new password.",
            "message_bn": "টোকেনটি বৈধ। নতুন পাসওয়ার্ড সেট করতে পারেন।",
        }

    @classmethod
    def confirm_password_reset(cls, token_str: str, new_password: str, ip_address: Optional[str] = None) -> Dict[str, Any]:
        """Consumes one-time token and updates user password with complexity checks."""
        cls.validate_password_strength(new_password)

        record = PasswordResetToken.objects.filter(token=token_str).first()
        if not record or not record.is_valid():
            raise ValidationError(
                {
                    "error_code": "INVALID_RESET_TOKEN",
                    "message_en": "Password reset token is invalid or has expired.",
                    "message_bn": "পাসওয়ার্ড রিসেট টোকেনটি অবৈধ বা মেয়াদোত্তীর্ণ।",
                }
            )

        user = record.user
        cls._check_tenant_subscription(user)

        if not user.is_active or user.is_archived:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Staff account is deactivated or archived.",
                    "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয়।",
                }
            )

        user.set_password(new_password)
        user.password_changed_at = timezone.now()
        user.failed_login_attempts = 0
        user.lockout_until = None
        user.save(update_fields=["password", "password_changed_at", "failed_login_attempts", "lockout_until", "updated_at"])

        record.is_used = True
        record.save(update_fields=["is_used"])

        cls._log_audit_event("PASSWORD_RESET_COMPLETED", user, ip_address, "SUCCESS", {})

        return {
            "status": "SUCCESS",
            "message_en": "Password has been reset successfully. Please log in with your new credentials.",
            "message_bn": "পাসওয়ার্ড সফলভাবে পরিবর্তিত হয়েছে। অনুগ্রহ করে নতুন পাসওয়ার্ড দিয়ে লগইন করুন।",
        }

    @classmethod
    def setup_initial_password(cls, setup_token: str, new_password: str, ip_address: Optional[str] = None) -> Dict[str, Any]:
        """Accessible initial password setup for newly invited or provisioned pharmacy staff."""
        cls.validate_password_strength(new_password)

        record = PasswordResetToken.objects.filter(token=setup_token).first()
        if not record or not record.is_valid():
            raise ValidationError(
                {
                    "error_code": "INVALID_SETUP_TOKEN",
                    "message_en": "Setup token is invalid or has expired.",
                    "message_bn": "সেটআপ টোকেনটি অবৈধ বা মেয়াদোত্তীর্ণ।",
                }
            )

        user = record.user
        cls._check_tenant_subscription(user)

        user.set_password(new_password)
        user.is_active = True
        user.password_changed_at = timezone.now()
        user.save(update_fields=["password", "is_active", "password_changed_at", "updated_at"])

        record.is_used = True
        record.save(update_fields=["is_used"])

        cls._log_audit_event("PASSWORD_SETUP_COMPLETED", user, ip_address, "SUCCESS", {})

        return {
            "status": "SUCCESS",
            "message_en": "Account password set up successfully. You may now log in.",
            "message_bn": "পাসওয়ার্ড সেটআপ সফল হয়েছে। আপনি এখন লগইন করতে পারেন।",
        }

