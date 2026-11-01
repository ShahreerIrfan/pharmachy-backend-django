"""
backend/apps/core/trial_policy_service.py
Architecture Decision: ADR-0013, ADR-0062, ADR-0084, ADR-0085 & ADR-0086
Phase 05: Basic/Standard/Pro Subscription & Billing (P086)

Trial and Evaluation Policy Service:
- Enforces limited, auditable evaluation terms (14 days, 1 branch, 2 devices, 24h offline lease).
- Enforces anti-abuse deduplication by DGDA drug license, phone number, and tenant identity.
- Generates clear, localized expiration notices (ACTIVE_TRIAL, EXPIRING_SOON, EXPIRED).
- Guarantees non-destructive data preservation on expiration (zero record purges/truncations).
- Governs SuperAdmin-guarded trial extensions (max 1 extension, max 7 days, mandatory audit).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole, normalize_bangladesh_phone

logger = logging.getLogger("pharmacy.billing.trial_policy")


class TrialPolicyService:
    DEFAULT_TRIAL_DAYS = 14
    MAX_EXTENSIONS = 1
    MAX_EXTENSION_DAYS = 7
    TRIAL_BRANCH_QUOTA = 1
    TRIAL_DEVICE_QUOTA = 2
    TRIAL_OFFLINE_LEASE_HOURS = 24

    @classmethod
    def validate_trial_eligibility(
        cls,
        license_number: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        exclude_tenant_id: str | None = None,
    ) -> tuple[bool, str, str]:
        """
        Anti-Abuse Engine (ADR-0086):
        Verifies that the prospective pharmacy license, phone, or email has not previously consumed a trial.
        Returns: (is_eligible, code, reason)
        """
        clean_license = str(license_number or "").strip()
        clean_phone = str(phone or "").strip()
        clean_email = str(email or "").strip().lower()

        # 1. DGDA Drug License Deduplication
        if clean_license and clean_license.upper() not in ["", "DGDA-PENDING", "PENDING"]:
            existing_branch = (
                Branch.objects.filter(license_number__iexact=clean_license)
                .exclude(tenant_id=exclude_tenant_id)
                .select_related("tenant")
                .first()
            )
            if existing_branch and existing_branch.tenant:
                t = existing_branch.tenant
                # Check if that tenant was ever a trial or is currently trial
                if t.status == TenantLifecycleStatus.TRIAL or t.trial_ends_at is not None:
                    reason = (
                        f"Anti-abuse violation: Pharmacy drug license '{clean_license}' has already utilized "
                        f"an evaluation trial on tenant '{t.name}' ({t.tenant_id}). Commercial subscription required."
                    )
                    return False, "ERR_TRIAL_ABUSE_DETECTED", reason

        # 2. Owner Phone Deduplication
        if clean_phone:
            try:
                norm_phone = normalize_bangladesh_phone(clean_phone)
            except Exception:
                norm_phone = clean_phone

            existing_user = (
                PharmacyUser.objects.filter(phone_number=norm_phone, role=UserRole.PHARMACY_OWNER)
                .exclude(tenant_id=exclude_tenant_id)
                .first()
            )
            if existing_user and existing_user.tenant_id:
                t = Tenant.objects.filter(tenant_id=existing_user.tenant_id).first()
                if t and (t.status == TenantLifecycleStatus.TRIAL or t.trial_ends_at is not None):
                    reason = (
                        f"Anti-abuse violation: Owner phone number '{norm_phone}' has already utilized "
                        f"an evaluation trial on tenant '{t.name}' ({t.tenant_id}). Commercial subscription required."
                    )
                    return False, "ERR_TRIAL_ABUSE_DETECTED", reason

        # 3. Owner Email Deduplication
        if clean_email and "@" in clean_email:
            existing_user = (
                PharmacyUser.objects.filter(email=clean_email)
                .exclude(tenant_id=exclude_tenant_id)
                .first()
            )
            if existing_user and existing_user.tenant_id:
                t = Tenant.objects.filter(tenant_id=existing_user.tenant_id).first()
                if t and (t.status == TenantLifecycleStatus.TRIAL or t.trial_ends_at is not None):
                    reason = (
                        f"Anti-abuse violation: Owner email '{clean_email}' has already utilized "
                        f"an evaluation trial on tenant '{t.name}' ({t.tenant_id}). Commercial subscription required."
                    )
                    return False, "ERR_TRIAL_ABUSE_DETECTED", reason

        return True, "ELIGIBLE", "Candidate is eligible for evaluation trial."

    @classmethod
    def get_extension_count(cls, tenant_id: str) -> int:
        """Counts prior audited trial extensions for tenant."""
        return AuditLogEntry.objects.filter(
            tenant_id=tenant_id,
            action="TRIAL_PERIOD_EXTENDED",
            status=AuditActionStatus.SUCCESS,
        ).count()

    @classmethod
    def get_trial_status(cls, tenant_id: str) -> dict[str, Any]:
        """
        Computes real-time trial evaluation terms, days remaining, expiration notices,
        write permission gating, and non-destructive data guarantee.
        """
        clean_tid = str(tenant_id).strip()
        tenant = Tenant.objects.filter(tenant_id=clean_tid).first()
        if not tenant:
            raise ValidationError(f"Tenant '{clean_tid}' not found.")

        sub = SubscriptionRecord.objects.filter(tenant=tenant).first()
        now = timezone.now()

        is_trial = (
            tenant.status == TenantLifecycleStatus.TRIAL
            or (sub and sub.status == SubscriptionStatus.TRIAL)
            or (tenant.trial_ends_at is not None and tenant.status not in [TenantLifecycleStatus.ACTIVE, TenantLifecycleStatus.ARCHIVED])
        )

        if not is_trial:
            return {
                "tenant_id": clean_tid,
                "is_trial": False,
                "trial_status": "NON_TRIAL",
                "trial_start_date": None,
                "trial_end_date": None,
                "days_remaining": 0,
                "is_expired": False,
                "is_write_permitted": True,
                "notice_en": "Tenant is operating under commercial subscription plan.",
                "notice_bn": "ফার্মেসি বাণিজ্যিক সাবস্ক্রিপশন প্ল্যানে পরিচালিত হচ্ছে।",
                "quotas": {
                    "max_branches": sub.licensed_branches if sub else 1,
                    "max_devices": sub.licensed_devices if sub else 2,
                    "offline_lease_hours": 72,
                },
                "anti_abuse": {
                    "is_eligible": False,
                    "abuse_detected": False,
                    "reason": "Commercial tenant.",
                },
                "extension_info": {
                    "extension_count": 0,
                    "max_extensions_allowed": 0,
                    "can_extend": False,
                },
                "non_destructive_guarantee": True,
            }

        trial_ends_at = tenant.trial_ends_at or (tenant.created_at + timedelta(days=cls.DEFAULT_TRIAL_DAYS))
        time_diff = trial_ends_at - now
        days_remaining = max(0, time_diff.days if time_diff.total_seconds() > 0 else 0)
        is_expired = now >= trial_ends_at

        extension_count = cls.get_extension_count(clean_tid)
        can_extend = extension_count < cls.MAX_EXTENSIONS

        if is_expired:
            trial_status = "EXPIRED"
            is_write_permitted = False
            notice_en = (
                f"Your evaluation trial expired on {trial_ends_at.strftime('%Y-%m-%d')}. "
                "Operational mutations (sales checkout, inventory stock-in) are locked. "
                "Your historical sales, customers, and medicine records are safely preserved without data loss. "
                "Upgrade to Basic, Standard, or Pro to immediately resume full operations."
            )
            notice_bn = (
                f"আপনার ট্রায়ালের মেয়াদ {trial_ends_at.strftime('%Y-%m-%d')} তারিখে শেষ হয়েছে। "
                "নতুন বিক্রয় ও স্টক ইন সাময়িকভাবে স্থগিত রয়েছে। পূর্ববর্তী সকল ইনভয়েস ও ড্রাগ রেজিস্ট্রি নিরাপদ রয়েছে। "
                "বাণিজ্যিক প্ল্যানে আপগ্রেড করে পুনরায় কার্যক্রম চালু করুন।"
            )
        elif days_remaining <= 3:
            trial_status = "EXPIRING_SOON"
            is_write_permitted = True
            notice_en = (
                f"Urgent: Your evaluation trial expires in {days_remaining} day(s) on {trial_ends_at.strftime('%Y-%m-%d')}. "
                "Select a commercial plan now to ensure uninterrupted counter operations."
            )
            notice_bn = (
                f"জরুরি নোটিশ: আপনার ট্রায়ালের মেয়াদ আর মাত্র {days_remaining} দিন বাকি আছে ({trial_ends_at.strftime('%Y-%m-%d')})। "
                "কাউন্টার বিক্রয় সচল রাখতে এখনই বাণিজ্যিক প্ল্যান নির্বাচন করুন।"
            )
        else:
            trial_status = "ACTIVE_TRIAL"
            is_write_permitted = True
            notice_en = (
                f"Your evaluation trial is active with {days_remaining} day(s) remaining. "
                f"All standard features are enabled with 1 branch and 2 POS devices."
            )
            notice_bn = (
                f"আপনার মূল্যায়ন ট্রায়াল সক্রিয় রয়েছে। বাকি আছে {days_remaining} দিন। "
                "১টি শাখা ও ২টি ডিভাইসে সব স্ট্যান্ডার্ড ফিচার ব্যবহারের সুবিধা চালু রয়েছে।"
            )

        return {
            "tenant_id": clean_tid,
            "is_trial": True,
            "trial_status": trial_status,
            "trial_start_date": tenant.created_at.isoformat(),
            "trial_end_date": trial_ends_at.isoformat(),
            "days_remaining": days_remaining,
            "is_expired": is_expired,
            "is_write_permitted": is_write_permitted,
            "notice_en": notice_en,
            "notice_bn": notice_bn,
            "quotas": {
                "max_branches": cls.TRIAL_BRANCH_QUOTA,
                "max_devices": cls.TRIAL_DEVICE_QUOTA,
                "offline_lease_hours": cls.TRIAL_OFFLINE_LEASE_HOURS,
            },
            "anti_abuse": {
                "is_eligible": True,
                "abuse_detected": False,
                "reason": "Single evaluation trial active.",
            },
            "extension_info": {
                "extension_count": extension_count,
                "max_extensions_allowed": cls.MAX_EXTENSIONS,
                "can_extend": can_extend,
            },
            "non_destructive_guarantee": True,
        }

    @classmethod
    def extend_trial(
        cls,
        tenant_id: str,
        operator_user: Any,
        additional_days: int = 7,
        reason: str = "",
    ) -> dict[str, Any]:
        """
        Audited Trial Extension (ADR-0086):
        Extends an evaluation period under strict SuperAdmin authorization and anti-abuse caps.
        """
        clean_tid = str(tenant_id).strip()
        tenant = Tenant.objects.filter(tenant_id=clean_tid).first()
        if not tenant:
            raise ValidationError(f"Tenant '{clean_tid}' not found.")

        # 1. Authorization: Only Platform Super Admin
        if not operator_user or not getattr(operator_user, "is_authenticated", False):
            raise PermissionDenied("Authentication credentials were not provided.")

        user_role = getattr(operator_user, "role", None)
        is_superadmin = (
            getattr(operator_user, "is_superuser", False)
            or user_role == UserRole.SUPERADMIN
            or str(user_role).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )
        if not is_superadmin:
            raise PermissionDenied(
                detail={
                    "error_code": "ROLE_PERMISSION_DENIED",
                    "message_en": "Only Platform Super Administrators are authorized to grant trial extensions.",
                    "message_bn": "শুধুমাত্র প্ল্যাটফর্ম সুপার অ্যাডমিনিস্ট্রেটর ট্রায়ালের মেয়াদ বৃদ্ধির অনুমতি দিতে পারেন।",
                }
            )

        # 2. Additional Days Bounds Check (1 to 7 days)
        if additional_days < 1 or additional_days > cls.MAX_EXTENSION_DAYS:
            raise ValidationError(
                f"Invalid extension days '{additional_days}'. Extension must be between 1 and {cls.MAX_EXTENSION_DAYS} days."
            )

        # 3. Mandatory Audited Business Reason (>= 10 chars)
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 10:
            raise ValidationError("A detailed business reason of at least 10 characters is required for trial extension.")

        # 4. Anti-Abuse Max Extension Cap (Max 1 extension lifetime)
        current_extensions = cls.get_extension_count(clean_tid)
        if current_extensions >= cls.MAX_EXTENSIONS:
            raise ValidationError(
                "ERR_TRIAL_EXTENSION_LIMIT_EXCEEDED: Tenant has already reached maximum allowed trial extensions (1)."
            )

        # 5. Apply Extension
        now = timezone.now()
        base_time = tenant.trial_ends_at if (tenant.trial_ends_at and tenant.trial_ends_at > now) else now
        previous_end = tenant.trial_ends_at
        new_end = base_time + timedelta(days=additional_days)

        tenant.trial_ends_at = new_end
        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            tenant.status = TenantLifecycleStatus.TRIAL
        tenant.save(update_fields=["trial_ends_at", "status"])

        # Update Subscription status if expired
        sub = SubscriptionRecord.objects.filter(tenant=tenant).first()
        if sub and sub.status in [SubscriptionStatus.EXPIRED, SubscriptionStatus.SUSPENDED]:
            sub.status = SubscriptionStatus.TRIAL
            sub.save(update_fields=["status"])

        # 6. Immutable Audit Log
        AuditLogEntry.objects.create(
            action="TRIAL_PERIOD_EXTENDED",
            resource_type="TENANT",
            resource_id=tenant.tenant_id,
            tenant_id=tenant.tenant_id,
            user_id=str(operator_user.user_id),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "previous_trial_ends_at": previous_end.isoformat() if previous_end else None,
                "new_trial_ends_at": new_end.isoformat(),
                "additional_days": additional_days,
                "reason": clean_reason,
                "extension_number": current_extensions + 1,
                "operator_email": operator_user.email,
            },
        )

        logger.info(
            f"Tenant '{clean_tid}' trial extended by {additional_days} days to {new_end.isoformat()} "
            f"by '{operator_user.email}'. Reason: {clean_reason}"
        )

        return cls.get_trial_status(clean_tid)
