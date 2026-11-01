"""
backend/apps/core/subscription_support_service.py

Architecture Decision: ADR-0100
Phase 05: Basic/Standard/Pro Subscription & Billing (P100)
Requirement Trace: FR-044, FR-066, FR-070

Subscription Support Override Exception Service:
- Provides platform administrators with time-bound operational relief mechanisms:
  1. GRACE_EXTENSION: Extends grace period during banking holiday or clearance delays.
  2. EMERGENCY_UNBLOCK: Temporarily restores suspended tenant access for critical medicine dispensing.
  3. QUOTA_OVERRIDE: Temporary device/branch capacity extension.
  4. REACTIVATION: Emergency reactivation with full audit trail.
- Enforces mandatory regulatory / operational justification and ticket reference.
- Emits tamper-evident AuditLogEntry records for every action.
- Zero historical data deletion invariant.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    SubscriptionOverrideStatus,
    SubscriptionOverrideType,
    SubscriptionRecord,
    SubscriptionStatus,
    SubscriptionSupportOverride,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.billing.support_override")


class SubscriptionSupportOverrideService:
    """Authoritative service for managing subscription support overrides and emergency exceptions."""

    @classmethod
    def grant_override(
        cls,
        tenant_id: str,
        override_type: str,
        justification: str,
        ticket_reference: str,
        extension_days: int = 7,
        quota_bonus: int = 0,
        subscription_id: str | None = None,
        granted_by_user: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> SubscriptionSupportOverride:
        """
        Grants a time-bound operational support override for a tenant subscription.
        """
        clean_tenant_id = str(tenant_id or "").strip()
        try:
            tenant = Tenant.objects.get(tenant_id=clean_tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError(f"Tenant '{clean_tenant_id}' does not exist.") from err

        if subscription_id:
            try:
                subscription = SubscriptionRecord.objects.get(subscription_id=subscription_id, tenant=tenant)
            except SubscriptionRecord.DoesNotExist as err:
                raise ValidationError(f"Subscription '{subscription_id}' does not exist for tenant '{clean_tenant_id}'.") from err
        else:
            subscription = SubscriptionRecord.objects.filter(tenant=tenant).order_by("-created_at").first()
            if not subscription:
                raise ValidationError(f"Tenant '{clean_tenant_id}' has no subscription record.")

        type_upper = str(override_type or "").upper().strip()
        if type_upper not in SubscriptionOverrideType.values:
            raise ValidationError(
                f"Invalid override_type '{override_type}'. Allowed types: {list(SubscriptionOverrideType.values)}"
            )

        clean_justification = str(justification or "").strip()
        if len(clean_justification) < 5:
            raise ValidationError("Operational justification (minimum 5 characters) is mandatory.")

        clean_ticket = str(ticket_reference or "").strip()
        if len(clean_ticket) < 3:
            raise ValidationError("Support ticket reference (minimum 3 characters) is mandatory.")

        days = int(extension_days or 7)
        if days < 1 or days > 90:
            raise ValidationError("extension_days must be between 1 and 90.")

        bonus = max(0, int(quota_bonus or 0))

        now = timezone.now()
        expires_at = now + datetime.timedelta(days=days)

        actor_id = str(
            getattr(granted_by_user, "user_id", None)
            or getattr(granted_by_user, "id", None)
            or getattr(granted_by_user, "email", None)
            or "support-admin"
        )

        override_id = f"ovr-{uuid.uuid4().hex[:12]}"

        override = SubscriptionSupportOverride.objects.create(
            override_id=override_id,
            tenant=tenant,
            subscription=subscription,
            override_type=type_upper,
            status=SubscriptionOverrideStatus.ACTIVE,
            extension_days=days,
            quota_bonus=bonus,
            justification=clean_justification,
            ticket_reference=clean_ticket,
            granted_by_user_id=actor_id,
            granted_at=now,
            expires_at=expires_at,
            metadata_json=metadata or {},
        )

        # Apply domain side-effects
        if type_upper == SubscriptionOverrideType.GRACE_EXTENSION:
            if subscription.status in [SubscriptionStatus.PAST_DUE, SubscriptionStatus.GRACE_PERIOD]:
                subscription.status = SubscriptionStatus.GRACE_PERIOD
            current_grace_end = subscription.grace_period_end_date
            if not current_grace_end or current_grace_end < now.date():
                subscription.grace_period_end_date = (now + datetime.timedelta(days=days)).date()
            else:
                subscription.grace_period_end_date = current_grace_end + datetime.timedelta(days=days)
            subscription.save(update_fields=["status", "grace_period_end_date"])

        elif type_upper == SubscriptionOverrideType.EMERGENCY_UNBLOCK:
            if tenant.status == TenantLifecycleStatus.SUSPENDED:
                tenant.status = TenantLifecycleStatus.ACTIVE
                tenant.save(update_fields=["status"])
            if subscription.status in [SubscriptionStatus.SUSPENDED, SubscriptionStatus.PAST_DUE]:
                subscription.status = SubscriptionStatus.ACTIVE
                subscription.save(update_fields=["status"])

        elif type_upper == SubscriptionOverrideType.REACTIVATION:
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.renewal_date = (now + datetime.timedelta(days=days)).date()
            subscription.grace_period_end_date = None
            subscription.save(update_fields=["status", "renewal_date", "grace_period_end_date"])
            if tenant.status != TenantLifecycleStatus.ACTIVE:
                tenant.status = TenantLifecycleStatus.ACTIVE
                tenant.save(update_fields=["status"])

        # Audit log creation
        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_SUPPORT_OVERRIDE_GRANTED",
            resource_type="SUBSCRIPTION_OVERRIDE",
            resource_id=override_id,
            tenant_id=tenant.tenant_id,
            user_id=actor_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "override_type": type_upper,
                "ticket_reference": clean_ticket,
                "extension_days": days,
                "quota_bonus": bonus,
                "expires_at": expires_at.isoformat(),
                "justification": clean_justification,
            },
        )

        logger.info(
            "Granted subscription support override %s [%s] for tenant %s (Ticket: %s, Expires: %s)",
            override_id,
            type_upper,
            clean_tenant_id,
            clean_ticket,
            expires_at.isoformat(),
        )
        return override

    @classmethod
    def revoke_override(
        cls,
        override_id: str,
        revocation_reason: str,
        actor_user: Any = None,
    ) -> SubscriptionSupportOverride:
        """
        Revokes an active support override before its scheduled expiration.
        """
        clean_override_id = str(override_id or "").strip()
        try:
            override = SubscriptionSupportOverride.objects.get(override_id=clean_override_id)
        except SubscriptionSupportOverride.DoesNotExist as err:
            raise ValidationError(f"Support override '{clean_override_id}' does not exist.") from err

        if override.status != SubscriptionOverrideStatus.ACTIVE:
            raise ValidationError(
                f"Cannot revoke override '{clean_override_id}' with status '{override.status}'."
            )

        clean_reason = str(revocation_reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError("Revocation reason (minimum 5 characters) is mandatory.")

        now = timezone.now()
        actor_id = str(
            getattr(actor_user, "user_id", None)
            or getattr(actor_user, "id", None)
            or getattr(actor_user, "email", None)
            or "support-admin"
        )

        override.status = SubscriptionOverrideStatus.REVOKED
        override.revoked_at = now
        override.revoked_by_user_id = actor_id
        override.revocation_reason = clean_reason
        override.save(update_fields=["status", "revoked_at", "revoked_by_user_id", "revocation_reason"])

        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_SUPPORT_OVERRIDE_REVOKED",
            resource_type="SUBSCRIPTION_OVERRIDE",
            resource_id=clean_override_id,
            tenant_id=override.tenant_id,
            user_id=actor_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "override_type": override.override_type,
                "revocation_reason": clean_reason,
                "revoked_at": now.isoformat(),
            },
        )

        logger.info(
            "Revoked subscription support override %s for tenant %s (Reason: %s)",
            clean_override_id,
            override.tenant_id,
            clean_reason,
        )
        return override

    @classmethod
    def list_overrides(
        cls,
        tenant_id: str | None = None,
        status_filter: str | None = None,
        override_type: str | None = None,
    ) -> list[SubscriptionSupportOverride]:
        """
        Lists overrides with automatic expiration of stale active records.
        """
        now = timezone.now()
        # Auto-expire active overrides past expiration time
        expired_qs = SubscriptionSupportOverride.objects.filter(
            status=SubscriptionOverrideStatus.ACTIVE,
            expires_at__lt=now,
        )
        if expired_qs.exists():
            expired_qs.update(status=SubscriptionOverrideStatus.EXPIRED)

        qs = SubscriptionSupportOverride.objects.all().select_related("tenant", "subscription")
        if tenant_id:
            qs = qs.filter(tenant_id=str(tenant_id).strip())
        if status_filter:
            qs = qs.filter(status=str(status_filter).strip().upper())
        if override_type:
            qs = qs.filter(override_type=str(override_type).strip().upper())

        return list(qs.order_by("-granted_at"))

    @classmethod
    def serialize_override(cls, override: SubscriptionSupportOverride) -> dict[str, Any]:
        """Serializes a SubscriptionSupportOverride instance to a schema-compliant dictionary."""
        return {
            "override_id": override.override_id,
            "tenant_id": override.tenant_id,
            "subscription_id": override.subscription_id,
            "override_type": override.override_type,
            "status": override.status,
            "extension_days": override.extension_days,
            "quota_bonus": override.quota_bonus,
            "justification": override.justification,
            "ticket_reference": override.ticket_reference,
            "granted_by_user_id": override.granted_by_user_id,
            "granted_at": override.granted_at.isoformat(),
            "expires_at": override.expires_at.isoformat(),
            "revoked_at": override.revoked_at.isoformat() if override.revoked_at else None,
            "revoked_by_user_id": override.revoked_by_user_id or None,
            "revocation_reason": override.revocation_reason or None,
            "metadata": override.metadata_json or {},
        }
