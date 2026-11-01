"""
apps.identity.models
Architecture Decision: ADR-0005 & ADR-0042
Canonical Custom User Model for Bangladesh Multi-Tenant Pharmacy SaaS ERP & POS.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def normalize_bangladesh_phone(raw_phone: str) -> str:
    """
    Normalizes Bangladesh mobile numbers to E.164 standard (+8801[3-9]\\d{8}).
    Accepts raw formats like 01712345678, +8801712345678, 8801712345678, 01712-345678.
    """
    if not raw_phone:
        raise ValidationError("Phone number is required.")

    cleaned = re.sub(r"[\s\-\(\)]", "", str(raw_phone).strip())

    if cleaned.startswith("+880"):
        pass
    elif cleaned.startswith("880"):
        cleaned = "+" + cleaned
    elif cleaned.startswith("01"):
        cleaned = "+88" + cleaned
    elif cleaned.startswith("1"):
        cleaned = "+880" + cleaned
    else:
        raise ValidationError(f"Invalid Bangladesh phone number format: '{raw_phone}'. Must match +8801[3-9]XXXXXXXX.")

    pattern = re.compile(r"^\+8801[3-9]\d{8}$")
    if not pattern.match(cleaned):
        raise ValidationError(
            f"Invalid Bangladesh mobile operator prefix or length: '{cleaned}'. "
            f"Valid operators: 013, 014, 015, 016, 017, 018, 019 (11 digits)."
        )

    return cleaned


class UserRole(models.TextChoices):
    SUPERADMIN = "SUPERADMIN", "Platform Super Administrator"
    PLATFORM_OPERATOR = "PLATFORM_OPERATOR", "Platform Operator / DevOps"
    PLATFORM_AUDITOR = "PLATFORM_AUDITOR", "Platform Security & Compliance Auditor"
    PHARMACY_OWNER = "PHARMACY_OWNER", "Pharmacy Owner / Licensee"
    BRANCH_MANAGER = "BRANCH_MANAGER", "Branch Manager"
    STAFF_PHARMACIST = "STAFF_PHARMACIST", "Registered Pharmacist (DGDA/PCB)"
    CASHIER = "CASHIER", "POS Terminal Cashier"
    INVENTORY_CLERK = "INVENTORY_CLERK", "Warehouse & Inventory Clerk"
    FINANCE_OFFICER = "FINANCE_OFFICER", "Finance & Accounts Officer"
    AUDITOR = "AUDITOR", "External Regulatory Auditor"
    INTEGRATION_AGENT = "INTEGRATION_AGENT", "Automated Device / API Service Agent"


class ActivationStatus(models.TextChoices):
    PENDING_VERIFICATION = "PENDING_VERIFICATION", "Pending Email/Phone Verification"
    ACTIVE = "ACTIVE", "Active & Authorized"
    SUSPENDED = "SUSPENDED", "Temporarily Suspended / Security Lockout"
    DEACTIVATED = "DEACTIVATED", "Deactivated / Safely Archived"


class PharmacyUserManager(BaseUserManager):
    """
    Custom user manager with email and phone normalization, safe deletion querysets,
    and role-based creation.
    """

    def create_user(
        self,
        email: str,
        phone_number: str,
        password: Optional[str] = None,
        **extra_fields,
    ) -> PharmacyUser:
        if not email:
            raise ValueError("An email address must be provided.")
        if not phone_number:
            raise ValueError("A Bangladesh phone number must be provided.")

        email = self.normalize_email(email).lower()
        phone_number = normalize_bangladesh_phone(phone_number)

        extra_fields.setdefault("activation_status", ActivationStatus.ACTIVE)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)

        user = self.model(email=email, phone_number=phone_number, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()

        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        email: str,
        phone_number: str,
        password: Optional[str] = None,
        **extra_fields,
    ) -> PharmacyUser:
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", UserRole.SUPERADMIN)
        extra_fields.setdefault("activation_status", ActivationStatus.ACTIVE)
        extra_fields.setdefault("tenant_id", "platform-system")

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, phone_number, password, **extra_fields)

    def active(self):
        """Returns non-archived, actively authorized users."""
        return self.get_queryset().filter(is_archived=False, activation_status=ActivationStatus.ACTIVE)

    def all_with_archived(self):
        """Returns all users including archived records for regulatory audits."""
        return self.get_queryset()


class PharmacyUser(AbstractBaseUser, PermissionsMixin):
    """
    Extensible custom user model conforming to ADR-0042.
    Replaces default auth.User with tenant context, BD mobile policy, and safe deletion.
    """

    user_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.CharField(max_length=64, db_index=True, default="tenant-default")
    branch_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

    email = models.EmailField(unique=True, db_index=True)
    phone_number = models.CharField(max_length=20, unique=True, db_index=True)
    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=32, choices=UserRole.choices, default=UserRole.CASHIER, db_index=True)
    pcb_registration_no = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Pharmacy Council of Bangladesh Grade-A/B Pharmacist Registration Number",
    )

    activation_status = models.CharField(
        max_length=32,
        choices=ActivationStatus.choices,
        default=ActivationStatus.ACTIVE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    # Safe deletion & archival audit trail (Hard DELETE forbidden)
    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.CharField(max_length=100, blank=True, default="")
    archival_reason = models.TextField(blank=True, default="")

    # Authentication security metadata & lockouts
    failed_login_attempts = models.PositiveIntegerField(default=0)
    lockout_until = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    last_login_user_agent = models.TextField(blank=True, default="")
    password_changed_at = models.DateTimeField(default=timezone.now)
    mfa_enabled = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PharmacyUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["phone_number", "full_name"]

    class Meta:
        db_table = "identity_pharmacy_users"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant_id", "role"]),
            models.Index(fields=["phone_number"]),
            models.Index(fields=["is_archived", "activation_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}> [{self.role}] ({self.tenant_id})"

    def clean(self):
        super().clean()
        if self.email:
            self.email = self.email.lower().strip()
        if self.phone_number:
            self.phone_number = normalize_bangladesh_phone(self.phone_number)

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False, force_hard_delete=False):
        """
        Safe deletion rule: Overrides default delete to perform soft-deletion/archival.
        Hard delete is only accessible if force_hard_delete=True is explicitly passed.
        """
        if force_hard_delete:
            return super().delete(using=using, keep_parents=keep_parents)

        self.is_archived = True
        self.is_active = False
        self.activation_status = ActivationStatus.DEACTIVATED
        self.archived_at = timezone.now()
        self.save(
            update_fields=[
                "is_archived",
                "is_active",
                "activation_status",
                "archived_at",
                "updated_at",
            ]
        )

    def archive(self, by_user: str = "SYSTEM", reason: str = "") -> None:
        """Explicit audited archival state transition."""
        self.is_archived = True
        self.is_active = False
        self.activation_status = ActivationStatus.DEACTIVATED
        self.archived_at = timezone.now()
        self.archived_by = by_user
        self.archival_reason = reason
        self.save(
            update_fields=[
                "is_archived",
                "is_active",
                "activation_status",
                "archived_at",
                "archived_by",
                "archival_reason",
                "updated_at",
            ]
        )

    def activate(self) -> None:
        """Explicit activation state transition."""
        self.activation_status = ActivationStatus.ACTIVE
        self.is_active = True
        self.is_archived = False
        self.failed_login_attempts = 0
        self.lockout_until = None
        self.save(
            update_fields=[
                "activation_status",
                "is_active",
                "is_archived",
                "failed_login_attempts",
                "lockout_until",
                "updated_at",
            ]
        )

    def suspend(self, reason: str = "") -> None:
        """Suspends user access due to security violation or administrative review."""
        self.activation_status = ActivationStatus.SUSPENDED
        self.is_active = False
        self.save(update_fields=["activation_status", "is_active", "updated_at"])

    @property
    def is_privileged(self) -> bool:
        """Indicates whether this role has elevated administrative authority requiring mandatory MFA (ADR-0044)."""
        return self.is_superuser or self.role in {
            UserRole.SUPERADMIN,
            UserRole.PLATFORM_OPERATOR,
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.FINANCE_OFFICER,
        }


class UserMfaDevice(models.Model):
    """
    RFC 6238 TOTP device configuration and hashed backup recovery codes.
    Architecture Decision: ADR-0044
    """

    device_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(PharmacyUser, on_delete=models.CASCADE, related_name="mfa_device")
    secret_key = models.CharField(max_length=64, help_text="Base32 TOTP secret key")
    is_confirmed = models.BooleanField(default=False)
    recovery_codes = models.JSONField(default=list, help_text="Hashed single-use recovery codes")
    failed_attempts = models.PositiveIntegerField(default=0)
    lockout_until = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "identity_user_mfa_devices"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"MfaDevice for {self.user.email} (confirmed={self.is_confirmed})"

    def is_locked_out(self) -> bool:
        return bool(self.lockout_until and timezone.now() < self.lockout_until)

    def remaining_recovery_codes_count(self) -> int:
        return sum(1 for c in self.recovery_codes if not c.get("is_used", False))


class MfaChallengeSession(models.Model):
    """
    Short-lived (5-minute) login challenge session for MFA verification.
    Architecture Decision: ADR-0044
    """

    challenge_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(PharmacyUser, on_delete=models.CASCADE, related_name="mfa_challenges")
    challenge_token = models.CharField(max_length=128, unique=True, db_index=True)
    device_id = models.CharField(max_length=64, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    failed_attempts = models.PositiveIntegerField(default=0)
    is_verified = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "identity_mfa_challenge_sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["challenge_token", "is_verified"]),
        ]

    def __str__(self) -> str:
        return f"MfaChallenge-{self.challenge_id} for {self.user.email} (verified={self.is_verified})"

    def is_valid(self) -> bool:
        return not self.is_verified and timezone.now() < self.expires_at and self.failed_attempts < 5

    def is_locked_out(self) -> bool:
        return self.failed_attempts >= 5


class PasswordResetToken(models.Model):
    """
    Time-bound (15-minute) single-use cryptographic token for password recovery.
    Architecture Decision: ADR-0043
    """

    token_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(PharmacyUser, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token = models.CharField(max_length=128, unique=True, db_index=True)
    otp_code = models.CharField(max_length=6, blank=True, default="")
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "identity_password_reset_tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token", "is_used"]),
        ]

    def __str__(self) -> str:
        return f"ResetToken-{self.token_id} for {self.user.email} (used={self.is_used})"

    def is_valid(self) -> bool:
        return not self.is_used and timezone.now() < self.expires_at


class UserSession(models.Model):
    """
    Active and historical client device session with token rotation and replay protection.
    Architecture Decision: ADR-0045
    """

    session_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(PharmacyUser, on_delete=models.CASCADE, related_name="sessions")
    tenant_id = models.CharField(max_length=64, db_index=True)
    device_id = models.CharField(max_length=64, db_index=True, help_text="Client device identifier")
    device_name = models.CharField(max_length=128, blank=True, default="")
    client_platform = models.CharField(
        max_length=32,
        default="DESKTOP_POS",
        choices=[
            ("DESKTOP_POS", "Desktop POS"),
            ("MOBILE_MANAGER", "Mobile Manager"),
            ("WEB_ADMIN", "Web Admin"),
        ],
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    current_refresh_jti = models.CharField(max_length=64, db_index=True, blank=True, default="")
    token_family = models.CharField(max_length=64, db_index=True, blank=True, default="")
    rotated_jtis = models.JSONField(default=list, help_text="History of previously rotated refresh JTIs")
    is_active = models.BooleanField(default=True, db_index=True)
    revocation_reason = models.CharField(max_length=128, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "identity_user_sessions"
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["tenant_id", "device_id"]),
            models.Index(fields=["token_family"]),
        ]

    def __str__(self) -> str:
        return f"Session-{self.session_id} for {self.user.email} on {self.device_id} (active={self.is_active})"

    def revoke(self, reason: str = "LOGOUT") -> None:
        """Revokes the active session and marks revocation reason."""
        self.is_active = False
        self.revocation_reason = reason
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revocation_reason", "revoked_at", "updated_at"])

    def rotate_token(self, old_jti: str, new_jti: str) -> None:
        """Rotates refresh token JTI and appends previous token to rotated list."""
        if self.rotated_jtis is None:
            self.rotated_jtis = []
        if old_jti and old_jti not in self.rotated_jtis:
            self.rotated_jtis.append(old_jti)
        self.current_refresh_jti = new_jti
        self.last_seen_at = timezone.now()
        self.save(update_fields=["rotated_jtis", "current_refresh_jti", "last_seen_at", "updated_at"])

    def is_replayed_token(self, jti: str) -> bool:
        """Detects whether a presented refresh token JTI belongs to historical rotated tokens."""
        return bool(self.rotated_jtis and jti in self.rotated_jtis)
