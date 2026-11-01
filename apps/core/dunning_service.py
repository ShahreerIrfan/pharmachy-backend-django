"""
backend/apps/core/dunning_service.py

Architecture Decision: ADR-0099
Phase 05: Basic/Standard/Pro Subscription & Billing (P099)
Requirement Trace: FR-044, FR-066, FR-068, FR-070

Dunning Notification, De-duplication, and Recovery Engine Service.
Manages localized lifecycle alerts for renewals, payment failures, grace periods,
account suspensions, and payment recoveries with deterministic 24-hour de-duplication.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    DunningChannel,
    DunningDeliveryStatus,
    DunningNotification,
    DunningStage,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    Tenant,
)

logger = logging.getLogger("pharmacy.billing.dunning")


class DunningNotificationService:
    """
    Core business logic for generating, de-duplicating, dispatching,
    and acknowledging subscription dunning notifications.
    """

    @classmethod
    def _format_decimal(cls, value: Any) -> str:
        if value is None:
            return "0.00"
        return str(Decimal(str(value)).quantize(Decimal("0.01")))

    @classmethod
    def _build_dedup_key(
        cls,
        tenant_id: str,
        stage: str,
        invoice_id: Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> str:
        d_str = date_str or date.today().isoformat()
        inv_part = str(invoice_id or "current").strip()
        return f"{tenant_id}:{stage}:{inv_part}:{d_str}"

    @classmethod
    def generate_and_dispatch_notification(
        cls,
        tenant_id: str,
        stage: str,
        invoice_id: Optional[str] = None,
        amount_bdt: Optional[Decimal] = None,
        channel: str = DunningChannel.IN_APP,
        force: bool = False,
        operator_user: Any = None,
    ) -> DunningNotification:
        """
        Generates and dispatches a localized dunning notification with 24h de-duplication rules.
        """
        clean_tid = str(tenant_id or "").strip()
        if not clean_tid:
            raise ValidationError({"tenant_id": "tenant_id is required."})

        try:
            tenant = Tenant.objects.get(tenant_id=clean_tid)
        except Tenant.DoesNotExist:
            raise ValidationError({"tenant_id": f"Tenant '{clean_tid}' does not exist."})

        clean_stage = str(stage or "").strip().upper()
        if clean_stage not in DunningStage.values:
            raise ValidationError(
                {"stage": f"Invalid dunning stage '{stage}'. Allowed: {DunningStage.values}"}
            )

        clean_channel = str(channel or DunningChannel.IN_APP).strip().upper()
        if clean_channel not in DunningChannel.values:
            clean_channel = DunningChannel.IN_APP

        # Resolve active subscription and invoice
        sub = SubscriptionRecord.objects.filter(tenant_id=clean_tid).first()
        inv = None
        if invoice_id:
            inv = SubscriptionRenewalInvoice.objects.filter(
                tenant_id=clean_tid, invoice_id=invoice_id
            ).first() or SubscriptionRenewalInvoice.objects.filter(
                tenant_id=clean_tid, invoice_number=invoice_id
            ).first()

        # Resolve amount
        if amount_bdt is not None:
            resolved_amount = Decimal(str(amount_bdt)).quantize(Decimal("0.01"))
        elif inv and getattr(inv, "total_bdt", None) is not None:
            resolved_amount = Decimal(str(inv.total_bdt)).quantize(Decimal("0.01"))
        elif sub and getattr(sub, "amount_bdt", None) is not None:
            resolved_amount = Decimal(str(sub.amount_bdt)).quantize(Decimal("0.01"))
        else:
            resolved_amount = Decimal("0.00")

        # De-duplication check
        today_str = date.today().isoformat()
        dedup_key = cls._build_dedup_key(
            clean_tid, clean_stage, inv.invoice_id if inv else None, today_str
        )

        cutoff = timezone.now() - timedelta(hours=24)
        existing = DunningNotification.objects.filter(
            dedup_key=dedup_key,
            delivery_status__in=[DunningDeliveryStatus.DELIVERED, DunningDeliveryStatus.PENDING],
            created_at__gte=cutoff,
        ).first()

        if existing and not force:
            logger.info(
                f"Suppressed duplicate dunning notification for tenant {clean_tid} ({clean_stage}) under dedup_key {dedup_key}"
            )
            # Record suppressed event record
            suppressed_notif = DunningNotification.objects.create(
                notification_id=f"notif-dunning-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                subscription=sub,
                invoice=inv,
                stage=clean_stage,
                channel=clean_channel,
                delivery_status=DunningDeliveryStatus.SUPPRESSED,
                dedup_key=dedup_key,
                title_en=existing.title_en,
                title_bn=existing.title_bn,
                body_en=f"[Suppressed Duplicate] {existing.body_en}",
                body_bn=f"[সতর্কতা: ডুপ্লিকেট বাতিল] {existing.body_bn}",
                amount_due_bdt=resolved_amount,
                recipient_email=getattr(tenant, "billing_email", "") or "",
                recipient_phone=getattr(tenant, "billing_phone", "") or "",
                is_acknowledged=False,
                last_error_message="Suppressed due to 24h de-duplication rule.",
            )
            return suppressed_notif

        # Localization copy generation
        amt_str = cls._format_decimal(resolved_amount)
        renewal_date_str = str(getattr(sub, "renewal_date", None) or date.today() + timedelta(days=7))
        inv_num_str = getattr(inv, "invoice_number", "INV-RENEWAL")

        if clean_stage == DunningStage.UPCOMING_RENEWAL:
            title_en = "Upcoming Subscription Renewal"
            title_bn = "সাবস্ক্রিপশন নবায়ন নোটিশ"
            body_en = f"Plan renewal for {tenant.name} is scheduled on {renewal_date_str} for ৳{amt_str}."
            body_bn = f"{tenant.name}-এর সাবস্ক্রিপশন প্ল্যান {renewal_date_str} তারিখে ৳{amt_str} মূল্যে নবায়ন হবে।"
        elif clean_stage == DunningStage.PAYMENT_FAILED:
            title_en = "Payment Failed: Renewal Unsuccessful"
            title_bn = "পেমেন্ট ব্যর্থ হয়েছে: নবায়ন সম্পন্ন হয়নি"
            body_en = f"Renewal payment of ৳{amt_str} for {tenant.name} failed. Please settle invoice #{inv_num_str} to avoid service disruption."
            body_bn = f"{tenant.name}-এর ৳{amt_str} রিনিউয়াল পেমেন্ট সম্পন্ন করা যায়নি। সার্ভিস সচল রাখতে অনুগ্রহ করে ইনভয়েস #{inv_num_str} পরিশোধ করুন।"
        elif clean_stage == DunningStage.GRACE_PERIOD_ACTIVE:
            title_en = "Grace Period Warning: Action Required"
            title_bn = "গ্রেস পিরিয়ড সতর্কতা: বিল পরিশোধ প্রয়োজন"
            body_en = f"{tenant.name} has entered a grace period. Sales operations remain active, but full access will be restricted if unpaid."
            body_bn = f"{tenant.name} গ্রেস পিরিয়ডে রয়েছে। বিক্রয় সেবা চালু আছে, তবে বিল পরিশোধ না করলে প্রবেশাধিকার সীমিত করা হবে।"
        elif clean_stage == DunningStage.SUBSCRIPTION_EXPIRED:
            title_en = "Subscription Expired: Terminal Restricted"
            title_bn = "সাবস্ক্রিপশনের মেয়াদ উত্তীর্ণ: বিক্রয় সীমিত"
            body_en = f"The subscription for {tenant.name} has expired. POS terminal is now in read-only/export mode. Please renew to restore full sales."
            body_bn = f"{tenant.name}-এর সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে। বিক্রয় চালনা করতে অনুগ্রহ করে পুনরায় সাবস্ক্রিপশন নবায়ন করুন।"
        elif clean_stage == DunningStage.ACCOUNT_SUSPENDED:
            title_en = "Account Suspended: Immediate Attention Required"
            title_bn = "অ্যাকাউন্ট স্থগিত: অবিলম্বে যোগাযোগ করুন"
            body_en = f"The account for {tenant.name} has been suspended due to overdue billing. Contact platform support or pay outstanding invoices."
            body_bn = f"বকেয়া বিলের কারণে {tenant.name}-এর অ্যাকাউন্ট স্থগিত করা হয়েছে। দ্রুত বকেয়া বিল পরিশোধ করুন।"
        else:  # PAYMENT_RECOVERED
            title_en = "Payment Recovered: Account Fully Restored"
            title_bn = "পেমেন্ট নিশ্চিত: অ্যাকাউন্ট সম্পূর্ণ পুনরুদ্ধার"
            body_en = f"Payment of ৳{amt_str} has been confirmed. Full active capabilities restored for {tenant.name}."
            body_bn = f"৳{amt_str} পেমেন্ট সফলভাবে গৃহীত হয়েছে। {tenant.name}-এর সম্পূর্ণ সার্ভিস পুনরুদ্ধার করা হয়েছে।"

        notif = DunningNotification.objects.create(
            notification_id=f"notif-dunning-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            subscription=sub,
            invoice=inv,
            stage=clean_stage,
            channel=clean_channel,
            delivery_status=DunningDeliveryStatus.DELIVERED,
            dedup_key=dedup_key,
            title_en=title_en,
            title_bn=title_bn,
            body_en=body_en,
            body_bn=body_bn,
            amount_due_bdt=resolved_amount,
            recipient_email=getattr(tenant, "billing_email", "") or "",
            recipient_phone=getattr(tenant, "billing_phone", "") or "",
            is_acknowledged=False,
            metadata_json={
                "operator_user_id": str(getattr(operator_user, "id", None) or "system"),
                "auto_generated": True,
            },
        )

        AuditLogEntry.objects.create(
            action="DUNNING_NOTIFICATION_SENT",
            resource_type="DUNNING_NOTIFICATION",
            resource_id=notif.notification_id,
            tenant_id=clean_tid,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "stage": clean_stage,
                "channel": clean_channel,
                "amount_due_bdt": amt_str,
                "dedup_key": dedup_key,
            },
        )

        return notif

    @classmethod
    def acknowledge_notification(
        cls,
        notification_id: str,
        operator_user: Any = None,
    ) -> DunningNotification:
        """
        Marks a dunning notification as acknowledged/read by pharmacy owner.
        """
        clean_nid = str(notification_id or "").strip()
        try:
            notif = DunningNotification.objects.get(notification_id=clean_nid)
        except DunningNotification.DoesNotExist:
            raise ValidationError({"notification_id": f"Notification '{clean_nid}' not found."})

        notif.is_acknowledged = True
        notif.acknowledged_at = timezone.now()
        notif.acknowledged_by_user_id = str(getattr(operator_user, "user_id", None) or getattr(operator_user, "id", None) or "user")
        notif.save(update_fields=["is_acknowledged", "acknowledged_at", "acknowledged_by_user_id", "updated_at"])

        AuditLogEntry.objects.create(
            action="DUNNING_NOTIFICATION_ACKNOWLEDGED",
            resource_type="DUNNING_NOTIFICATION",
            resource_id=clean_nid,
            tenant_id=notif.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"acknowledged_by": notif.acknowledged_by_user_id},
        )

        return notif

    @classmethod
    def retry_notification(
        cls,
        notification_id: str,
        operator_user: Any = None,
    ) -> DunningNotification:
        """
        Retries delivery of a failed or pending dunning notification with bounded retry rule.
        """
        clean_nid = str(notification_id or "").strip()
        try:
            notif = DunningNotification.objects.get(notification_id=clean_nid)
        except DunningNotification.DoesNotExist:
            raise ValidationError({"notification_id": f"Notification '{clean_nid}' not found."})

        if notif.retry_count >= notif.max_retries:
            notif.last_error_message = f"Maximum retry limit ({notif.max_retries}) reached."
            notif.delivery_status = DunningDeliveryStatus.FAILED
            notif.save(update_fields=["last_error_message", "delivery_status", "updated_at"])
            raise ValidationError({"retry": f"Maximum retry limit of {notif.max_retries} exceeded."})

        notif.retry_count += 1
        notif.delivery_status = DunningDeliveryStatus.DELIVERED
        notif.last_error_message = ""
        notif.save(update_fields=["retry_count", "delivery_status", "last_error_message", "updated_at"])

        AuditLogEntry.objects.create(
            action="DUNNING_NOTIFICATION_RETRIED",
            resource_type="DUNNING_NOTIFICATION",
            resource_id=clean_nid,
            tenant_id=notif.tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"retry_count": notif.retry_count},
        )

        return notif

    @classmethod
    def list_notifications(
        cls,
        tenant_id: str,
        stage: Optional[str] = None,
        unacknowledged_only: bool = False,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        Lists dunning notifications for a specific tenant with unacknowledged counters.
        """
        clean_tid = str(tenant_id or "").strip()
        qs = DunningNotification.objects.filter(tenant_id=clean_tid)
        if stage:
            qs = qs.filter(stage=stage.upper())
        if unacknowledged_only:
            qs = qs.filter(is_acknowledged=False)

        total_count = qs.count()
        unack_count = qs.filter(is_acknowledged=False).count()
        items = qs.order_by("-created_at")[:limit]

        formatted = []
        for n in items:
            formatted.append(
                {
                    "notification_id": n.notification_id,
                    "tenant_id": n.tenant_id,
                    "invoice_id": getattr(n, "invoice_id", None) or (n.invoice.invoice_id if n.invoice else None),
                    "stage": n.stage,
                    "channel": n.channel,
                    "delivery_status": n.delivery_status,
                    "dedup_key": n.dedup_key,
                    "title_en": n.title_en,
                    "title_bn": n.title_bn,
                    "body_en": n.body_en,
                    "body_bn": n.body_bn,
                    "amount_due_bdt": cls._format_decimal(n.amount_due_bdt),
                    "recipient_email": n.recipient_email,
                    "recipient_phone": n.recipient_phone,
                    "is_acknowledged": n.is_acknowledged,
                    "acknowledged_at": n.acknowledged_at.isoformat() if n.acknowledged_at else None,
                    "retry_count": n.retry_count,
                    "max_retries": n.max_retries,
                    "created_at": n.created_at.isoformat() if hasattr(n.created_at, "isoformat") else str(n.created_at),
                }
            )

        return {
            "tenant_id": clean_tid,
            "total_count": total_count,
            "unacknowledged_count": unack_count,
            "notifications": formatted,
        }
