"""
backend/apps/core/cancellation_grace_service.py
Architecture Decision: ADR-0015, ADR-0033, ADR-0073, ADR-0084, ADR-0087 & ADR-0092
Phase 05: Basic/Standard/Pro Subscription & Billing (P092)

Subscription Cancellation & Grace Period Service:
- Manages cancellation (immediate vs end-of-billing-period) with mandatory reason.
- Enforces statutory 15-day grace period with continuous clinical dispensing protection.
- Handles expiration after grace lapse while guaranteeing 100% historical data read & export access.
- Reactivates lapsed or cancelled accounts immediately upon payment confirmation.
- Provides bilingual owner messaging (EN/BN) and zero data deletion guarantees.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    CancellationEffectiveMode,
    SubscriptionCancellationRecord,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService

logger = logging.getLogger("pharmacy.billing.lifecycle")


class SubscriptionCancellationGraceService:
    DEFAULT_GRACE_PERIOD_DAYS = 15

    @classmethod
    def _validate_tenant_and_subscription(cls, tenant_id: str) -> tuple[Tenant, SubscriptionRecord]:
        clean_tenant_id = str(tenant_id or "").strip()
        try:
            tenant = Tenant.objects.get(tenant_id=clean_tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError(f"Tenant '{clean_tenant_id}' does not exist.") from err

        subscription = (
            SubscriptionRecord.objects.filter(tenant=tenant)
            .order_by("-created_at")
            .first()
        )
        if not subscription:
            raise ValidationError(f"Tenant '{clean_tenant_id}' does not have an active subscription.")

        return tenant, subscription

    @classmethod
    def cancel_subscription(
        cls,
        tenant_id: str,
        reason: str,
        effective_mode: str = "END_OF_BILLING_PERIOD",
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Executes subscription cancellation with mandatory reason.
        END_OF_BILLING_PERIOD: retains access until renewal_date with is_auto_renew=False.
        IMMEDIATE: sets status to CANCELLED and suspends mutations.
        Historical data and export capabilities remain 100% accessible.
        """
        tenant, subscription = cls._validate_tenant_and_subscription(tenant_id)

        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError("Cancellation reason (minimum 5 characters) is mandatory.")

        mode_upper = str(effective_mode or "END_OF_BILLING_PERIOD").upper().strip()
        if mode_upper not in [CancellationEffectiveMode.END_OF_BILLING_PERIOD, CancellationEffectiveMode.IMMEDIATE]:
            raise ValidationError(
                f"Invalid effective_mode '{effective_mode}'. Allowed: ['END_OF_BILLING_PERIOD', 'IMMEDIATE']"
            )

        now = timezone.now()
        today = now.date()
        actor_id = str(getattr(actor_user, "email", None) or getattr(actor_user, "user_id", None) or "system")

        if mode_upper == CancellationEffectiveMode.IMMEDIATE:
            subscription.status = SubscriptionStatus.CANCELLED
            subscription.is_auto_renew = False
            subscription.cancel_at_period_end = False
            subscription.cancellation_reason = clean_reason
            subscription.cancelled_at = now
            effective_date = today
            if tenant.status == TenantLifecycleStatus.ACTIVE:
                tenant.status = TenantLifecycleStatus.SUSPENDED
                tenant.save(update_fields=["status"])
        else:
            subscription.is_auto_renew = False
            subscription.cancel_at_period_end = True
            subscription.cancellation_reason = clean_reason
            subscription.cancelled_at = now
            effective_date = subscription.renewal_date

        subscription.save(update_fields=[
            "status",
            "is_auto_renew",
            "cancel_at_period_end",
            "cancellation_reason",
            "cancelled_at",
        ])

        # Record cancellation event
        cancellation_id = f"cnc-{uuid.uuid4().hex[:16]}"
        SubscriptionCancellationRecord.objects.create(
            cancellation_id=cancellation_id,
            tenant=tenant,
            subscription=subscription,
            requested_by=actor_id,
            reason=clean_reason,
            effective_mode=mode_upper,
            effective_date=effective_date,
            grace_period_end_date=subscription.grace_period_end_date,
        )

        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_CANCELLED",
            resource_type="SUBSCRIPTION",
            resource_id=subscription.subscription_id,
            tenant_id=tenant.tenant_id,
            user_id=getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "effective_mode": mode_upper,
                "reason": clean_reason,
                "effective_date": effective_date.isoformat(),
                "zero_data_deleted": True,
            },
        )

        logger.info(
            "Subscription for tenant '%s' cancelled (%s): %s",
            tenant.tenant_id,
            mode_upper,
            clean_reason,
        )

        return cls.get_lifecycle_status(tenant_id)

    @classmethod
    def enter_or_evaluate_grace_period(cls, tenant_id: str, actor_user: Any = None) -> dict[str, Any]:
        """
        Evaluates whether a tenant has lapsed into the 15-day grace period or if grace has expired.
        """
        tenant, subscription = cls._validate_tenant_and_subscription(tenant_id)
        now = timezone.now()
        today = now.date()

        # If active and past renewal date -> enter grace period
        if subscription.status == SubscriptionStatus.ACTIVE and today >= subscription.renewal_date:
            subscription.status = SubscriptionStatus.GRACE_PERIOD
            subscription.grace_period_end_date = subscription.renewal_date + timedelta(days=cls.DEFAULT_GRACE_PERIOD_DAYS)
            subscription.save(update_fields=["status", "grace_period_end_date"])

            AuditLogEntry.objects.create(
                action="SUBSCRIPTION_ENTERED_GRACE_PERIOD",
                resource_type="SUBSCRIPTION",
                resource_id=subscription.subscription_id,
                tenant_id=tenant.tenant_id,
                user_id=getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None),
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "grace_period_end_date": subscription.grace_period_end_date.isoformat(),
                    "zero_data_deleted": True,
                },
            )

        # If in grace period and past grace end date -> expire
        elif subscription.status == SubscriptionStatus.GRACE_PERIOD and subscription.grace_period_end_date:
            if today > subscription.grace_period_end_date:
                subscription.status = SubscriptionStatus.EXPIRED
                subscription.save(update_fields=["status"])

                AuditLogEntry.objects.create(
                    action="SUBSCRIPTION_EXPIRED",
                    resource_type="SUBSCRIPTION",
                    resource_id=subscription.subscription_id,
                    tenant_id=tenant.tenant_id,
                    user_id=getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None),
                    status=AuditActionStatus.SUCCESS,
                    metadata_json={
                        "expired_after_grace": True,
                        "zero_data_deleted": True,
                    },
                )

        return cls.get_lifecycle_status(tenant_id)

    @classmethod
    def expire_subscription(cls, tenant_id: str, actor_user: Any = None) -> dict[str, Any]:
        """Explicitly transitions subscription to EXPIRED status."""
        tenant, subscription = cls._validate_tenant_and_subscription(tenant_id)
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.save(update_fields=["status"])

        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_EXPIRED",
            resource_type="SUBSCRIPTION",
            resource_id=subscription.subscription_id,
            tenant_id=tenant.tenant_id,
            user_id=getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None),
            status=AuditActionStatus.SUCCESS,
            metadata_json={"zero_data_deleted": True},
        )
        return cls.get_lifecycle_status(tenant_id)

    @classmethod
    def reactivate_subscription(
        cls,
        tenant_id: str,
        payment_method: str = "BKASH",
        payment_reference: str = "",
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Reactivates cancelled, expired, or suspended subscription upon verified renewal confirmation.
        Restores full sales dispensing instantly without re-onboarding.
        """
        PlanCatalogueService.ensure_canonical_plans_seeded()
        tenant, subscription = cls._validate_tenant_and_subscription(tenant_id)

        clean_ref = str(payment_reference or "").strip()
        if len(clean_ref) < 4:
            raise ValidationError("Valid payment reference (minimum 4 characters) is required for reactivation.")

        now = timezone.now()
        today = now.date()

        # Extend renewal date
        if subscription.billing_cycle == "ANNUAL":
            new_renewal = today + timedelta(days=365)
        else:
            new_renewal = today + timedelta(days=30)

        subscription.status = SubscriptionStatus.ACTIVE
        subscription.start_date = today
        subscription.renewal_date = new_renewal
        subscription.grace_period_end_date = None
        subscription.is_auto_renew = True
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=[
            "status",
            "start_date",
            "renewal_date",
            "grace_period_end_date",
            "is_auto_renew",
            "cancel_at_period_end",
        ])

        # Re-activate applied entitlement
        TenantAppliedEntitlement.objects.filter(tenant=tenant).update(is_active=True)

        # Re-activate tenant status
        if tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.TRIAL]:
            tenant.status = TenantLifecycleStatus.ACTIVE
            tenant.save(update_fields=["status"])

        # Mark latest cancellation as reactivated
        latest_cancellation = (
            SubscriptionCancellationRecord.objects.filter(tenant=tenant, is_reactivated=False)
            .order_by("-created_at")
            .first()
        )
        actor_id = str(getattr(actor_user, "email", None) or getattr(actor_user, "user_id", None) or "system")
        if latest_cancellation:
            latest_cancellation.is_reactivated = True
            latest_cancellation.reactivated_at = now
            latest_cancellation.reactivated_by = actor_id
            latest_cancellation.save(update_fields=["is_reactivated", "reactivated_at", "reactivated_by"])

        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_REACTIVATED",
            resource_type="SUBSCRIPTION",
            resource_id=subscription.subscription_id,
            tenant_id=tenant.tenant_id,
            user_id=getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "payment_method": str(payment_method).upper(),
                "payment_reference": clean_ref,
                "renewal_date": new_renewal.isoformat(),
            },
        )

        logger.info(
            "Subscription for tenant '%s' reactivated successfully by %s (renewal: %s)",
            tenant.tenant_id,
            actor_id,
            new_renewal,
        )

        return cls.get_lifecycle_status(tenant_id)

    @classmethod
    def get_lifecycle_status(cls, tenant_id: str) -> dict[str, Any]:
        """
        Returns full subscription lifecycle status, days remaining in grace,
        bilingual owner messaging, and safe read/export availability.
        """
        tenant, subscription = cls._validate_tenant_and_subscription(tenant_id)
        now = timezone.now()
        today = now.date()

        st = subscription.status
        in_grace = (st == SubscriptionStatus.GRACE_PERIOD)
        days_remaining = None
        if in_grace and subscription.grace_period_end_date:
            days_remaining = max(0, (subscription.grace_period_end_date - today).days)

        can_dispense = st in [
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIAL,
            SubscriptionStatus.GRACE_PERIOD,
        ]

        # Generate Contextual Owner Messaging
        if st == SubscriptionStatus.ACTIVE:
            if subscription.cancel_at_period_end:
                hl_en = "Subscription Set to Cancel"
                hl_bn = "বিলিং সাইকেল শেষে সাবস্ক্রিপশন বাতিল নির্ধারিত"
                dt_en = f"Access active until {subscription.renewal_date}. Auto-renewal disabled."
                dt_bn = f"{subscription.renewal_date} পর্যন্ত ব্যবহারের সুযোগ থাকবে। অটো-রিনিউয়াল বন্ধ রয়েছে।"
                act = "RESUME_AUTO_RENEW"
            else:
                hl_en = "Subscription In Good Standing"
                hl_bn = "সাবস্ক্রিপশন সচল ও সক্রিয় রয়েছে"
                dt_en = f"Next renewal on {subscription.renewal_date} ({subscription.billing_cycle})."
                dt_bn = f"পরবর্তী রিনিউয়াল তারিখ: {subscription.renewal_date} ({subscription.billing_cycle})।"
                act = "NONE"
        elif st == SubscriptionStatus.GRACE_PERIOD:
            hl_en = f"Grace Period Active ({days_remaining} Days Remaining)"
            hl_bn = f"গ্রেস পিরিয়ড কার্যকর রয়েছে (অবশিষ্ট {days_remaining} দিন)"
            dt_en = "Billing lapsed. Operational sales continue to protect patient care. Please settle invoice."
            dt_bn = "সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে। রোগীর সেবা নিশ্চিত করতে বিক্রয় সচল রয়েছে। বকেয়া পরিশোধ করুন।"
            act = "PAY_RENEWAL_INVOICE"
        elif st in [SubscriptionStatus.EXPIRED, SubscriptionStatus.SUSPENDED]:
            hl_en = "Subscription Expired"
            hl_bn = "সাবস্ক্রিপশনের মেয়াদ উত্তীর্ণ হয়েছে"
            dt_en = "New sales suspended. Historical transactions and data export remain fully readable."
            dt_bn = "নতুন বিক্রয় সাময়িক স্থগিত। পূর্ববর্তী সকল হিস্ট্রি ও ডাটা এক্সপোর্ট সম্পূর্ণ নিরাপদ।"
            act = "REACTIVATE"
        elif st == SubscriptionStatus.CANCELLED:
            hl_en = "Subscription Cancelled"
            hl_bn = "সাবস্ক্রিপশন বাতিল করা হয়েছে"
            dt_en = "Account cancelled. All historical records are safely retained and downloadable."
            dt_bn = "সাবস্ক্রিপশন বাতিল। অতীতের সকল ডাটা ও রিপোর্ট সম্পূর্ণ অক্ষত এবং ডাউনলোডযোগ্য।"
            act = "REACTIVATE"
        else:
            hl_en = f"Subscription Status: {st}"
            hl_bn = f"সাবস্ক্রিপশন স্ট্যাটাস: {st}"
            dt_en = "Standard subscription lifecycle."
            dt_bn = "স্ট্যান্ডার্ড সাবস্ক্রিপশন লাইফসাইকেল।"
            act = "NONE"

        return {
            "tenant_id": tenant.tenant_id,
            "subscription_id": subscription.subscription_id,
            "plan_tier": subscription.plan_tier,
            "status": subscription.status,
            "lifecycle_details": {
                "renewal_date": subscription.renewal_date.isoformat(),
                "in_grace_period": in_grace,
                "grace_period_days_remaining": days_remaining,
                "grace_period_end_date": subscription.grace_period_end_date.isoformat() if subscription.grace_period_end_date else None,
                "is_auto_renew": subscription.is_auto_renew,
                "cancel_at_period_end": subscription.cancel_at_period_end,
                "cancellation_reason": subscription.cancellation_reason or None,
            },
            "owner_messaging": {
                "headline_en": hl_en,
                "headline_bn": hl_bn,
                "details_en": dt_en,
                "details_bn": dt_bn,
                "action_required": act,
            },
            "permissions": {
                "can_dispense_sales": can_dispense,
                "can_read_historical_data": True,
                "can_export_tenant_data": True,
                "zero_data_deleted": True,
            },
        }
