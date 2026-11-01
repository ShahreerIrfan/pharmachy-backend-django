"""
backend/apps/core/announcement_service.py

Architecture Decision: ADR-0076
System Announcements & Delivery/Read Receipt Tracking Service.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    AnnouncementPriority,
    AnnouncementReadReceipt,
    AnnouncementStatus,
    AnnouncementTargetType,
    SystemAnnouncement,
    Tenant,
)

logger = logging.getLogger(__name__)


class SystemAnnouncementService:
    """
    Business logic for broadcast announcements, targeted tenant delivery,
    and immutable read receipt logging.
    """

    @classmethod
    def create_announcement(
        cls,
        title: str,
        message: str,
        priority: str = "INFO",
        target_type: str = "ALL",
        target_tenants: Optional[List[str]] = None,
        target_roles: Optional[List[str]] = None,
        target_branches: Optional[List[str]] = None,
        starts_at: Any = None,
        expires_at: Any = None,
        operator_user: Any = None,
    ) -> SystemAnnouncement:
        clean_title = str(title or "").strip()
        if len(clean_title) < 3:
            raise ValidationError({"title": "Announcement title must be at least 3 characters."})

        clean_message = str(message or "").strip()
        if len(clean_message) < 5:
            raise ValidationError({"message": "Announcement message must be at least 5 characters."})

        clean_priority = str(priority or "INFO").upper().strip()
        valid_priorities = [choice[0] for choice in AnnouncementPriority.choices]
        if clean_priority not in valid_priorities:
            raise ValidationError({"priority": f"Invalid priority '{priority}'. Allowed: {valid_priorities}"})

        clean_target_type = str(target_type or "ALL").upper().strip()
        valid_targets = [choice[0] for choice in AnnouncementTargetType.choices]
        if clean_target_type not in valid_targets:
            raise ValidationError({"target_type": f"Invalid target_type '{target_type}'. Allowed: {valid_targets}"})

        start = starts_at or timezone.now()
        end = expires_at
        if start and end and start > end:
            raise ValidationError({"expires_at": "expires_at must be later than or equal to starts_at."})

        announcement_id = f"ann-{uuid.uuid4().hex[:12]}"

        announcement = SystemAnnouncement.objects.create(
            announcement_id=announcement_id,
            title=clean_title,
            message=clean_message,
            priority=clean_priority,
            target_type=clean_target_type,
            target_tenants=list(target_tenants or []),
            target_roles=list(target_roles or []),
            target_branches=list(target_branches or []),
            starts_at=start,
            expires_at=end,
            status=AnnouncementStatus.DRAFT,
            created_by=operator_user if getattr(operator_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            action="ANNOUNCEMENT_CREATED",
            resource_type="SYSTEM_ANNOUNCEMENT",
            resource_id=announcement.announcement_id,
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "announcement_id": announcement.announcement_id,
                "priority": announcement.priority,
                "target_type": announcement.target_type,
            },
        )
        return announcement

    @classmethod
    def publish_announcement(cls, announcement_id: str, operator_user: Any = None) -> SystemAnnouncement:
        try:
            announcement = SystemAnnouncement.objects.get(announcement_id=announcement_id)
        except SystemAnnouncement.DoesNotExist:
            raise ValidationError({"announcement_id": f"Announcement '{announcement_id}' does not exist."})

        if announcement.status == AnnouncementStatus.ARCHIVED:
            raise ValidationError({"status": "Cannot publish an ARCHIVED announcement."})

        announcement.status = AnnouncementStatus.PUBLISHED
        announcement.published_at = timezone.now()
        announcement.save()

        AuditLogEntry.objects.create(
            action="ANNOUNCEMENT_PUBLISHED",
            resource_type="SYSTEM_ANNOUNCEMENT",
            resource_id=announcement.announcement_id,
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "announcement_id": announcement.announcement_id,
                "published_at": announcement.published_at.isoformat(),
            },
        )
        return announcement

    @classmethod
    def archive_announcement(cls, announcement_id: str, operator_user: Any = None) -> SystemAnnouncement:
        try:
            announcement = SystemAnnouncement.objects.get(announcement_id=announcement_id)
        except SystemAnnouncement.DoesNotExist:
            raise ValidationError({"announcement_id": f"Announcement '{announcement_id}' does not exist."})

        announcement.status = AnnouncementStatus.ARCHIVED
        announcement.save()

        AuditLogEntry.objects.create(
            action="ANNOUNCEMENT_ARCHIVED",
            resource_type="SYSTEM_ANNOUNCEMENT",
            resource_id=announcement.announcement_id,
            tenant_id="platform-global",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"announcement_id": announcement.announcement_id},
        )
        return announcement

    @classmethod
    def get_active_announcements_for_user(cls, user: Any) -> List[Dict[str, Any]]:
        now = timezone.now()
        base_qs = (
            SystemAnnouncement.objects.filter(
                status=AnnouncementStatus.PUBLISHED,
                starts_at__lte=now,
            )
            .filter(models_Q_expires_at_valid(now))
            .order_by("-starts_at")
        )

        user_role = str(getattr(user, "role", "")).upper()
        user_tenant = getattr(user, "tenant", None) or getattr(user, "tenant_id", None)
        user_tenant_id = getattr(user_tenant, "tenant_id", str(user_tenant or ""))
        user_branch = getattr(user, "branch", None) or getattr(user, "branch_id", None)
        user_branch_id = getattr(user_branch, "branch_id", str(user_branch or ""))

        results = []
        for ann in base_qs:
            # Check target matching
            matches = False
            if ann.target_type == AnnouncementTargetType.ALL:
                matches = True
            elif ann.target_type == AnnouncementTargetType.TENANT_LIST:
                matches = user_tenant_id in ann.target_tenants
            elif ann.target_type == AnnouncementTargetType.ROLE_LIST:
                matches = user_role in ann.target_roles
            elif ann.target_type == AnnouncementTargetType.BRANCH_LIST:
                matches = user_branch_id in ann.target_branches

            if not matches:
                continue

            # Ensure delivery receipt exists
            receipt = None
            if user_tenant_id and getattr(user, "is_authenticated", False):
                tenant_obj = (
                    user.tenant
                    if hasattr(user, "tenant") and user.tenant
                    else Tenant.objects.filter(tenant_id=user_tenant_id).first()
                )
                if tenant_obj:
                    receipt, _ = AnnouncementReadReceipt.objects.get_or_create(
                        announcement=ann,
                        user=user,
                        defaults={
                            "tenant": tenant_obj,
                            "receipt_id": f"arr-{uuid.uuid4().hex[:12]}",
                        },
                    )

            results.append(
                {
                    "announcement_id": ann.announcement_id,
                    "title": ann.title,
                    "message": ann.message,
                    "priority": ann.priority,
                    "target_type": ann.target_type,
                    "target_tenants": ann.target_tenants,
                    "target_roles": ann.target_roles,
                    "target_branches": ann.target_branches,
                    "status": ann.status,
                    "starts_at": ann.starts_at.isoformat() if ann.starts_at else None,
                    "expires_at": ann.expires_at.isoformat() if ann.expires_at else None,
                    "is_read": receipt.acknowledged if receipt else False,
                    "read_at": receipt.read_at.isoformat() if receipt and receipt.read_at else None,
                    "created_at": ann.created_at.isoformat() if ann.created_at else None,
                }
            )

        return results

    @classmethod
    def acknowledge_announcement(cls, announcement_id: str, user: Any) -> AnnouncementReadReceipt:
        try:
            announcement = SystemAnnouncement.objects.get(announcement_id=announcement_id)
        except SystemAnnouncement.DoesNotExist:
            raise ValidationError({"announcement_id": f"Announcement '{announcement_id}' does not exist."})

        user_tenant = getattr(user, "tenant", None) or getattr(user, "tenant_id", None)
        user_tenant_id = getattr(user_tenant, "tenant_id", str(user_tenant or ""))
        tenant_obj = (
            user.tenant
            if hasattr(user, "tenant") and user.tenant
            else Tenant.objects.filter(tenant_id=user_tenant_id).first()
        )

        if not tenant_obj:
            raise ValidationError({"tenant": "Authenticated user must be bound to a valid pharmacy tenant."})

        receipt, _ = AnnouncementReadReceipt.objects.get_or_create(
            announcement=announcement,
            user=user,
            defaults={
                "tenant": tenant_obj,
                "receipt_id": f"arr-{uuid.uuid4().hex[:12]}",
            },
        )
        receipt.acknowledged = True
        receipt.read_at = timezone.now()
        receipt.save()

        AuditLogEntry.objects.create(
            action="ANNOUNCEMENT_ACKNOWLEDGED",
            resource_type="SYSTEM_ANNOUNCEMENT",
            resource_id=announcement.announcement_id,
            tenant_id=user_tenant_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "announcement_id": announcement.announcement_id,
                "user_id": str(getattr(user, "user_id", getattr(user, "pk", str(user)))),
                "read_at": receipt.read_at.isoformat(),
            },
        )
        return receipt

    @classmethod
    def get_announcement_stats(cls, announcement_id: str) -> Dict[str, Any]:
        try:
            announcement = SystemAnnouncement.objects.get(announcement_id=announcement_id)
        except SystemAnnouncement.DoesNotExist:
            raise ValidationError({"announcement_id": f"Announcement '{announcement_id}' does not exist."})

        receipts = announcement.receipts.all()
        total_delivered = receipts.count()
        total_read = receipts.filter(acknowledged=True).count()
        rate = "0.00"
        if total_delivered > 0:
            rate = str(
                (Decimal(str(total_read)) / Decimal(str(total_delivered)) * Decimal("100")).quantize(Decimal("0.01"))
            )

        return {
            "announcement_id": announcement.announcement_id,
            "status": announcement.status,
            "total_delivered": total_delivered,
            "total_read": total_read,
            "read_rate_pct": rate,
        }


def models_Q_expires_at_valid(now):
    from django.db.models import Q

    return Q(expires_at__isnull=True) | Q(expires_at__gte=now)
