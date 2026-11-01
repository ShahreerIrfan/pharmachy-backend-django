"""
backend/apps/core/platform_tenant_lifecycle_service.py

Architecture Decision: ADR-0080
Platform Tenant Lifecycle End-to-End Orchestration Service.
Coordinates:
  1. Provisioning & Trial evaluation.
  2. Subscription activation (TRIAL -> ACTIVE).
  3. Commercial billing & plan tier upgrades/downgrades with Decimal precision.
  4. Audited suspension (lockout of POS checkouts, preservation of records).
  5. Audited restoration (re-enabling operational checkouts).
  6. Comprehensive lifecycle event timeline querying.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    PlanTier,
    SubscriptionRecord,
    Tenant,
    TenantLifecycleStatus,
)


class PlatformTenantLifecycleService:
    """
    Unified end-to-end orchestration service for SaaS tenant lifecycles.
    """

    PLAN_QUOTAS = {
        PlanTier.BASIC: {
            "branches": 1,
            "devices": 2,
            "amount_bdt": Decimal("2500.00"),
            "features": ["PRESCRIPTION_DISPENSING", "FEFO_BATCH_INVENTORY"],
        },
        PlanTier.STANDARD: {
            "branches": 5,
            "devices": 10,
            "amount_bdt": Decimal("6500.00"),
            "features": [
                "PRESCRIPTION_DISPENSING",
                "FEFO_BATCH_INVENTORY",
                "DGDA_ANTIBIOTIC_REGISTER",
                "OFFLINE_POS_SYNC",
            ],
        },
        PlanTier.PRO: {
            "branches": 25,
            "devices": 50,
            "amount_bdt": Decimal("15000.00"),
            "features": [
                "PRESCRIPTION_DISPENSING",
                "FEFO_BATCH_INVENTORY",
                "DGDA_ANTIBIOTIC_REGISTER",
                "OFFLINE_POS_SYNC",
                "MULTI_BRANCH_TRANSFERS",
                "CENTRALIZED_PLATFORM_ANALYTICS",
            ],
        },
    }

    @classmethod
    def activate_subscription(
        cls,
        tenant_id: str,
        plan_tier: str = PlanTier.STANDARD,
        billing_cycle: str = "MONTHLY",
        payment_reference: str = "",
        operator_id: str = "system",
        operator_email: Optional[str] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Activates tenant subscription (e.g. from TRIAL or past-due to ACTIVE).
        """
        clean_tier = str(plan_tier or PlanTier.STANDARD).upper().strip()
        if clean_tier not in PlanTier.values:
            raise ValidationError(
                {"plan_tier": [f"Invalid plan tier '{clean_tier}'. Must be BASIC, STANDARD, or PRO."]}
            )

        clean_cycle = str(billing_cycle or "MONTHLY").upper().strip()
        if clean_cycle not in ["MONTHLY", "ANNUALLY"]:
            raise ValidationError(
                {"billing_cycle": [f"Invalid billing cycle '{clean_cycle}'. Must be MONTHLY or ANNUALLY."]}
            )

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError({"tenant_id": [f"Tenant '{tenant_id}' does not exist."]}) from err

        now = timezone.now()
        quota_config = cls.PLAN_QUOTAS[clean_tier]
        base_amount = quota_config["amount_bdt"]
        amount_bdt = base_amount if clean_cycle == "MONTHLY" else (base_amount * Decimal("12.00") * Decimal("0.90"))

        with transaction.atomic():
            prev_status = tenant.status
            prev_plan = tenant.plan_tier

            # 1. Update Tenant
            tenant.status = TenantLifecycleStatus.ACTIVE
            tenant.plan_tier = clean_tier
            tenant.is_active = True
            tenant.trial_ends_at = None
            tenant.suspended_at = None
            tenant.suspension_reason = ""
            tenant.save(
                update_fields=[
                    "status",
                    "plan_tier",
                    "is_active",
                    "trial_ends_at",
                    "suspended_at",
                    "suspension_reason",
                    "updated_at",
                ]
            )

            # 2. Update or create SubscriptionRecord
            subscription = SubscriptionRecord.objects.filter(tenant=tenant).order_by("-created_at").first()
            if subscription:
                subscription.plan_tier = clean_tier
                subscription.status = "ACTIVE"
                subscription.amount_bdt = amount_bdt
                subscription.billing_cycle = clean_cycle
                subscription.licensed_branches = quota_config["branches"]
                subscription.licensed_devices = quota_config["devices"]
                subscription.features_enabled = quota_config["features"]
                subscription.renewal_date = (
                    now + datetime.timedelta(days=365 if clean_cycle == "ANNUALLY" else 30)
                ).date()
                subscription.save()
            else:
                subscription = SubscriptionRecord.objects.create(
                    subscription_id=f"sub-{tenant.tenant_id}-{now.strftime('%Y%m%d%H%M%S')}",
                    tenant=tenant,
                    plan_tier=clean_tier,
                    status="ACTIVE",
                    amount_bdt=amount_bdt,
                    billing_cycle=clean_cycle,
                    licensed_branches=quota_config["branches"],
                    licensed_devices=quota_config["devices"],
                    features_enabled=quota_config["features"],
                    start_date=now.date(),
                    renewal_date=(now + datetime.timedelta(days=365 if clean_cycle == "ANNUALLY" else 30)).date(),
                )

            # 3. Record Immutable Audit Log
            AuditLogEntry.objects.create(
                action="TENANT_SUBSCRIPTION_ACTIVATED",
                resource_type="TENANT",
                resource_id=tenant.tenant_id,
                tenant_id=tenant.tenant_id,
                user_id=str(operator_id),
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "event": "LIFECYCLE_SUBSCRIPTION_ACTIVATED",
                    "tenant_id": tenant.tenant_id,
                    "previous_status": prev_status,
                    "current_status": tenant.status,
                    "previous_plan": prev_plan,
                    "plan_tier": clean_tier,
                    "billing_cycle": clean_cycle,
                    "amount_bdt": str(amount_bdt),
                    "licensed_branches": quota_config["branches"],
                    "licensed_devices": quota_config["devices"],
                    "payment_reference": payment_reference,
                    "operator": operator_email or str(operator_id),
                    "notes": notes,
                    "timestamp": now.isoformat(),
                },
            )

        return {
            "tenant_id": tenant.tenant_id,
            "status": tenant.status,
            "plan_tier": tenant.plan_tier,
            "subscription_id": subscription.subscription_id,
            "billing_cycle": subscription.billing_cycle,
            "amount_bdt": str(subscription.amount_bdt),
            "licensed_branches": subscription.licensed_branches,
            "licensed_devices": subscription.licensed_devices,
            "features_enabled": subscription.features_enabled,
            "activated_at": now.isoformat(),
        }

    @classmethod
    def change_plan_tier(
        cls,
        tenant_id: str,
        target_plan_tier: str,
        target_billing_cycle: str = "MONTHLY",
        operator_id: str = "system",
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Executes commercial plan upgrades/downgrades with transactional quota adjustments.
        """
        clean_tier = str(target_plan_tier or "").upper().strip()
        if clean_tier not in PlanTier.values:
            raise ValidationError(
                {"plan_tier": [f"Invalid plan tier '{clean_tier}'. Must be BASIC, STANDARD, or PRO."]}
            )

        if not reason or len(reason.strip()) < 5:
            raise ValidationError({"reason": ["A justification reason of at least 5 characters is required."]})

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError({"tenant_id": [f"Tenant '{tenant_id}' does not exist."]}) from err

        clean_cycle = str(target_billing_cycle or "MONTHLY").upper().strip()
        if clean_cycle not in ["MONTHLY", "ANNUALLY"]:
            clean_cycle = "MONTHLY"

        now = timezone.now()
        quota_config = cls.PLAN_QUOTAS[clean_tier]
        base_amount = quota_config["amount_bdt"]
        amount_bdt = base_amount if clean_cycle == "MONTHLY" else (base_amount * Decimal("12.00") * Decimal("0.90"))

        with transaction.atomic():
            prev_plan = tenant.plan_tier
            tenant.plan_tier = clean_tier
            tenant.save(update_fields=["plan_tier", "updated_at"])

            subscription = SubscriptionRecord.objects.filter(tenant=tenant).order_by("-created_at").first()
            if subscription:
                subscription.plan_tier = clean_tier
                subscription.amount_bdt = amount_bdt
                subscription.billing_cycle = clean_cycle
                subscription.licensed_branches = quota_config["branches"]
                subscription.licensed_devices = quota_config["devices"]
                subscription.features_enabled = quota_config["features"]
                subscription.save()

            AuditLogEntry.objects.create(
                action="TENANT_PLAN_TIER_CHANGED",
                resource_type="TENANT",
                resource_id=tenant.tenant_id,
                tenant_id=tenant.tenant_id,
                user_id=str(operator_id),
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "event": "LIFECYCLE_PLAN_TIER_CHANGED",
                    "tenant_id": tenant.tenant_id,
                    "previous_plan": prev_plan,
                    "target_plan": clean_tier,
                    "billing_cycle": clean_cycle,
                    "amount_bdt": str(amount_bdt),
                    "licensed_branches": quota_config["branches"],
                    "licensed_devices": quota_config["devices"],
                    "reason": reason.strip(),
                    "timestamp": now.isoformat(),
                },
            )

        return {
            "tenant_id": tenant.tenant_id,
            "previous_plan": prev_plan,
            "current_plan": tenant.plan_tier,
            "billing_cycle": clean_cycle,
            "amount_bdt": str(amount_bdt),
            "licensed_branches": quota_config["branches"],
            "licensed_devices": quota_config["devices"],
            "features_enabled": quota_config["features"],
            "updated_at": now.isoformat(),
        }

    @classmethod
    def suspend_tenant(
        cls,
        tenant_id: str,
        reason: str,
        reason_code: str = "ADMIN_MANUAL",
        operator_id: str = "system",
    ) -> Dict[str, Any]:
        """
        Suspends tenant operations, blocking POS checkout while safeguarding data exports.
        """
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError({"reason": ["Suspension reason must be at least 5 characters long."]})

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError({"tenant_id": [f"Tenant '{tenant_id}' does not exist."]}) from err

        now = timezone.now()
        with transaction.atomic():
            prev_status = tenant.status
            tenant.status = TenantLifecycleStatus.SUSPENDED
            tenant.suspended_at = now
            tenant.suspension_reason = clean_reason
            tenant.suspension_reason_code = reason_code
            tenant.save(
                update_fields=[
                    "status",
                    "suspended_at",
                    "suspension_reason",
                    "suspension_reason_code",
                    "updated_at",
                ]
            )

            AuditLogEntry.objects.create(
                action="TENANT_SUSPENDED",
                resource_type="TENANT",
                resource_id=tenant.tenant_id,
                tenant_id=tenant.tenant_id,
                user_id=str(operator_id),
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "event": "LIFECYCLE_TENANT_SUSPENDED",
                    "tenant_id": tenant.tenant_id,
                    "previous_status": prev_status,
                    "current_status": tenant.status,
                    "reason": clean_reason,
                    "reason_code": reason_code,
                    "suspended_at": now.isoformat(),
                },
            )

        return {
            "tenant_id": tenant.tenant_id,
            "status": tenant.status,
            "suspended_at": now.isoformat(),
            "suspension_reason": clean_reason,
            "suspension_reason_code": reason_code,
            "checkout_allowed": tenant.is_checkout_allowed(),
        }

    @classmethod
    def restore_tenant(
        cls,
        tenant_id: str,
        reason: str,
        operator_id: str = "system",
    ) -> Dict[str, Any]:
        """
        Reactivates / restores a suspended tenant, re-enabling POS sales checkouts.
        """
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 5:
            raise ValidationError({"reason": ["Restoration reason must be at least 5 characters long."]})

        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError({"tenant_id": [f"Tenant '{tenant_id}' does not exist."]}) from err

        now = timezone.now()
        with transaction.atomic():
            prev_status = tenant.status
            tenant.status = TenantLifecycleStatus.ACTIVE
            tenant.is_active = True
            tenant.restored_at = now
            tenant.restoration_reason = clean_reason
            tenant.suspended_at = None
            tenant.suspension_reason = ""
            tenant.save(
                update_fields=[
                    "status",
                    "is_active",
                    "restored_at",
                    "restoration_reason",
                    "suspended_at",
                    "suspension_reason",
                    "updated_at",
                ]
            )

            AuditLogEntry.objects.create(
                action="TENANT_RESTORED",
                resource_type="TENANT",
                resource_id=tenant.tenant_id,
                tenant_id=tenant.tenant_id,
                user_id=str(operator_id),
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "event": "LIFECYCLE_TENANT_RESTORED",
                    "tenant_id": tenant.tenant_id,
                    "previous_status": prev_status,
                    "current_status": tenant.status,
                    "reason": clean_reason,
                    "restored_at": now.isoformat(),
                },
            )

        return {
            "tenant_id": tenant.tenant_id,
            "status": tenant.status,
            "restored_at": now.isoformat(),
            "restoration_reason": clean_reason,
            "checkout_allowed": tenant.is_checkout_allowed(),
        }

    @classmethod
    def inspect_lifecycle_timeline(cls, tenant_id: str) -> List[Dict[str, Any]]:
        """
        Returns the ordered immutable timeline of lifecycle events for this tenant.
        """
        actions = [
            "TENANT_PROVISIONED",
            "TENANT_ONBOARDING_COMPLETED",
            "TENANT_SUBSCRIPTION_ACTIVATED",
            "TENANT_PLAN_TIER_CHANGED",
            "TENANT_SUSPENDED",
            "TENANT_RESTORED",
            "TENANT_STATUS_TRANSITIONED",
            "TENANT_DELETION_REQUESTED",
        ]
        logs = AuditLogEntry.objects.filter(
            resource_type="TENANT",
            resource_id=tenant_id,
            action__in=actions,
        ).order_by("-timestamp")

        timeline = []
        for entry in logs:
            timeline.append(
                {
                    "entry_id": str(entry.entry_id),
                    "action": entry.action,
                    "status": entry.status,
                    "user_id": entry.user_id,
                    "metadata": entry.metadata_json,
                    "timestamp": entry.timestamp.isoformat(),
                }
            )
        return timeline
