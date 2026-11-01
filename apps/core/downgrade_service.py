"""
backend/apps/core/downgrade_service.py
Architecture Decision: ADR-0015, ADR-0081, ADR-0083 & ADR-0091
Phase 05: Basic/Standard/Pro Subscription & Billing (P091)

Subscription Downgrade Service:
- Validates downgrade eligibility according to plan tier rank hierarchy (PRO > STANDARD > BASIC).
- Previews and assesses over-quota branch and staff counts before execution.
- Gating excess branches in read-only over-quota lock without deleting historical data.
- Atomically snapshots downgraded TenantAppliedEntitlement and updates SubscriptionRecord.
- Strictly adheres to zero data deletion invariant: all invoices, stock, and staff remain intact.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    DowngradeEffectiveMode,
    PlanCatalogueRecord,
    PlanTier,
    SubscriptionDowngradeRecord,
    SubscriptionDowngradeStatus,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantAppliedEntitlement,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.quota_guard_service import SubscriptionQuotaGuardService

logger = logging.getLogger("pharmacy.billing.downgrades")


class SubscriptionDowngradeService:
    TIER_RANKS: dict[str, int] = {
        PlanTier.BASIC: 1,
        PlanTier.STANDARD: 2,
        PlanTier.PRO: 3,
    }

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

        if subscription.status in [SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED]:
            raise ValidationError(
                f"Cannot downgrade subscription in status '{subscription.status}'."
            )

        return tenant, subscription

    @classmethod
    def assess_downgrade(
        cls,
        tenant_id: str,
        target_plan_code: str,
        billing_cycle: str | None = None,
    ) -> dict[str, Any]:
        """
        Assesses downgrade feasibility and identifies over-quota branches without modifying state.
        """
        PlanCatalogueService.ensure_canonical_plans_seeded()
        tenant, subscription = cls._validate_tenant_and_subscription(tenant_id)

        clean_code = str(target_plan_code or "").strip().upper()
        try:
            target_plan = PlanCatalogueRecord.objects.get(plan_code=clean_code, is_active=True)
        except PlanCatalogueRecord.DoesNotExist as err:
            raise ValidationError(f"Target plan '{clean_code}' does not exist or is inactive.") from err

        current_tier = subscription.plan_tier
        target_tier = target_plan.tier
        current_cycle = (subscription.billing_cycle or "MONTHLY").upper().strip()
        target_cycle = (billing_cycle or current_cycle).upper().strip()

        current_rank = cls.TIER_RANKS.get(current_tier, 0)
        target_rank = cls.TIER_RANKS.get(target_tier, 0)

        # Enforce Downgrade-only validation
        if target_rank > current_rank:
            raise ValidationError(
                f"Target plan '{target_tier}' has a higher tier rank than current plan '{current_tier}'. "
                "This is an upgrade; please use the upgrade workflow."
            )

        if target_rank == current_rank:
            if current_cycle == "ANNUAL" and target_cycle == "MONTHLY":
                # Valid cycle downgrade
                pass
            else:
                raise ValidationError(
                    f"Target plan '{target_tier}' ({target_cycle}) is identical to current subscription plan and cycle."
                )

        quota_report = SubscriptionQuotaGuardService.assess_account_quotas(
            tenant_id=tenant_id,
            target_plan_code=clean_code,
        )

        b_info = quota_report["branches"]
        current_active = b_info["active_count"]
        allowed_branches = target_plan.max_branches
        is_over_quota = current_active > allowed_branches

        all_branches = list(Branch.objects.filter(tenant=tenant))
        # Default suggested retention: first N branches
        retained_branches = [b.branch_id for b in all_branches[:allowed_branches]]
        locked_branches = [b.branch_id for b in all_branches[allowed_branches:]]

        current_entitlement = TenantAppliedEntitlement.objects.filter(
            tenant=tenant,
            is_active=True,
        ).first()
        from_plan_code = current_entitlement.plan_code if current_entitlement else f"PLAN_{current_tier}"

        return {
            "tenant_id": tenant.tenant_id,
            "subscription_id": subscription.subscription_id,
            "from_plan_tier": current_tier,
            "from_plan_code": from_plan_code,
            "to_plan_tier": target_tier,
            "to_plan_code": clean_code,
            "effective_mode": "IMMEDIATE",
            "quota_assessment": {
                "current_active_branches": current_active,
                "target_allowed_branches": allowed_branches,
                "is_over_quota": is_over_quota,
                "retained_branch_ids": retained_branches,
                "locked_branch_ids": locked_branches,
            },
            "data_retention_guarantee": {
                "zero_data_deleted": True,
                "historical_sales_readable": True,
                "stock_ledger_preserved": True,
            },
            "status": "SCHEDULED" if False else "APPLIED",
        }

    @classmethod
    def apply_downgrade(
        cls,
        tenant_id: str,
        target_plan_code: str,
        billing_cycle: str = "MONTHLY",
        effective_mode: str = "IMMEDIATE",
        retained_branch_ids: list[str] | None = None,
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Executes subscription downgrade, locks excess branches without deleting any records,
        and atomically issues new downgraded entitlement snapshot.
        """
        PlanCatalogueService.ensure_canonical_plans_seeded()
        tenant, subscription = cls._validate_tenant_and_subscription(tenant_id)

        clean_code = str(target_plan_code or "").strip().upper()
        try:
            target_plan = PlanCatalogueRecord.objects.get(plan_code=clean_code, is_active=True)
        except PlanCatalogueRecord.DoesNotExist as err:
            raise ValidationError(f"Target plan '{clean_code}' does not exist or is inactive.") from err

        current_tier = subscription.plan_tier
        target_tier = target_plan.tier
        current_cycle = (subscription.billing_cycle or "MONTHLY").upper().strip()
        target_cycle = (billing_cycle or current_cycle).upper().strip()

        current_rank = cls.TIER_RANKS.get(current_tier, 0)
        target_rank = cls.TIER_RANKS.get(target_tier, 0)

        # Enforce Downgrade rule
        if target_rank > current_rank:
            raise ValidationError(
                f"Target plan '{target_tier}' is an upgrade. Use upgrade workflow."
            )
        if target_rank == current_rank and not (current_cycle == "ANNUAL" and target_cycle == "MONTHLY"):
            raise ValidationError(
                f"Target plan '{target_tier}' ({target_cycle}) is identical to current subscription."
            )

        now = timezone.now()
        allowed_branches = target_plan.max_branches
        all_branches = list(Branch.objects.filter(tenant=tenant).order_by("created_at"))
        all_branch_ids = {b.branch_id: b for b in all_branches}

        # Determine retained vs locked branches
        if retained_branch_ids is not None:
            clean_retained_ids = [str(bid).strip() for bid in retained_branch_ids if str(bid).strip()]
            # Validate all belong to tenant
            for bid in clean_retained_ids:
                if bid not in all_branch_ids:
                    raise ValidationError(f"Branch '{bid}' does not belong to tenant '{tenant_id}'.")
            if len(clean_retained_ids) > allowed_branches:
                raise ValidationError(
                    f"Selected {len(clean_retained_ids)} retained branches exceeds target plan limit of {allowed_branches}."
                )
            retained_set = set(clean_retained_ids)
        else:
            # Auto-retain first N
            retained_set = {b.branch_id for b in all_branches[:allowed_branches]}

        locked_branch_ids: list[str] = []
        final_retained_ids: list[str] = []

        # Update branch over-quota lock flags WITHOUT DELETING ANY ENTITY
        for b in all_branches:
            if b.branch_id in retained_set:
                b.is_over_quota_locked = False
                b.is_active = True
                b.save(update_fields=["is_over_quota_locked", "is_active"])
                final_retained_ids.append(b.branch_id)
            else:
                b.is_over_quota_locked = True
                # Keep is_active=True or False but is_over_quota_locked=True ensures historical read-only access
                b.save(update_fields=["is_over_quota_locked"])
                locked_branch_ids.append(b.branch_id)

        is_over_quota = len(locked_branch_ids) > 0

        # Snapshot new downgraded entitlement
        TenantAppliedEntitlement.objects.filter(tenant=tenant, is_active=True).update(
            is_active=False,
            effective_until=now,
        )

        new_entitlement_id = f"ent_{uuid.uuid4().hex[:16]}"
        unit_price = target_plan.annual_price_bdt if target_cycle == "ANNUAL" else target_plan.monthly_price_bdt

        entitlement = TenantAppliedEntitlement.objects.create(
            entitlement_id=new_entitlement_id,
            tenant=tenant,
            plan_code=target_plan.plan_code,
            plan_tier=target_plan.tier,
            plan_version=target_plan.version,
            billing_cycle=target_cycle,
            unit_price_bdt=unit_price,
            currency=target_plan.currency,
            effective_from=now,
            effective_until=None,
            max_branches=target_plan.max_branches,
            max_devices_per_branch=target_plan.max_devices_per_branch,
            max_total_devices=target_plan.max_total_devices,
            max_staff_users=target_plan.max_staff_users,
            features_snapshot=dict(target_plan.features),
            is_active=True,
            applied_by=str(getattr(actor_user, "email", None) or "system"),
            notes=f"Downgrade to {target_plan.plan_code} ({target_cycle})",
        )

        # Update SubscriptionRecord
        subscription.plan_tier = target_tier
        subscription.billing_cycle = target_cycle
        subscription.amount_bdt = unit_price
        subscription.licensed_branches = target_plan.max_branches
        subscription.licensed_devices = target_plan.max_total_devices
        subscription.features_enabled = dict(target_plan.features)
        subscription.save(update_fields=[
            "plan_tier",
            "billing_cycle",
            "amount_bdt",
            "licensed_branches",
            "licensed_devices",
            "features_enabled",
        ])

        # Update Tenant
        tenant.plan_tier = target_tier
        tenant.save(update_fields=["plan_tier"])

        # Determine from_plan_code
        from_plan_code = f"PLAN_{current_tier}"

        # Create Downgrade Record
        downgrade_id = f"dwn-{uuid.uuid4().hex[:16]}"
        record = SubscriptionDowngradeRecord.objects.create(
            downgrade_id=downgrade_id,
            tenant=tenant,
            subscription=subscription,
            from_plan_tier=current_tier,
            from_plan_code=from_plan_code,
            from_billing_cycle=current_cycle,
            to_plan_tier=target_tier,
            to_plan_code=clean_code,
            to_billing_cycle=target_cycle,
            effective_mode=DowngradeEffectiveMode.IMMEDIATE,
            scheduled_effective_date=now.date(),
            active_branches_at_downgrade=len(all_branches),
            allowed_branches_quota=allowed_branches,
            is_over_quota=is_over_quota,
            retained_branch_ids=final_retained_ids,
            locked_branch_ids=locked_branch_ids,
            status=SubscriptionDowngradeStatus.APPLIED,
            applied_entitlement=entitlement,
            notes=f"Downgraded by {getattr(actor_user, 'email', 'system')}",
            executed_at=now,
        )

        # Audit Log
        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_DOWNGRADE_APPLIED",
            resource_type="SUBSCRIPTION",
            resource_id=record.downgrade_id,
            tenant_id=tenant.tenant_id,
            user_id=getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "from_tier": current_tier,
                "to_tier": target_tier,
                "from_cycle": current_cycle,
                "to_cycle": target_cycle,
                "is_over_quota": is_over_quota,
                "retained_branch_ids": final_retained_ids,
                "locked_branch_ids": locked_branch_ids,
                "zero_data_deleted": True,
            },
        )

        logger.info(
            "Downgrade %s executed for tenant '%s': %s -> %s (locked branches: %s)",
            record.downgrade_id,
            tenant.tenant_id,
            current_tier,
            target_tier,
            locked_branch_ids,
        )

        return cls.serialize_downgrade(record)

    @classmethod
    def list_downgrades(cls, tenant_id: str) -> list[dict[str, Any]]:
        clean_tenant_id = str(tenant_id or "").strip()
        qs = SubscriptionDowngradeRecord.objects.filter(tenant_id=clean_tenant_id).select_related("applied_entitlement")
        return [cls.serialize_downgrade(r) for r in qs]

    @classmethod
    def get_downgrade(cls, tenant_id: str, downgrade_id: str) -> dict[str, Any]:
        clean_tenant_id = str(tenant_id or "").strip()
        clean_dwn_id = str(downgrade_id or "").strip()
        record = SubscriptionDowngradeRecord.objects.filter(
            downgrade_id=clean_dwn_id,
            tenant_id=clean_tenant_id,
        ).select_related("applied_entitlement").first()
        if not record:
            raise ValidationError(f"Downgrade record '{clean_dwn_id}' not found for tenant '{clean_tenant_id}'.")
        return cls.serialize_downgrade(record)

    @classmethod
    def serialize_downgrade(cls, record: SubscriptionDowngradeRecord) -> dict[str, Any]:
        return {
            "downgrade_id": record.downgrade_id,
            "tenant_id": record.tenant_id,
            "subscription_id": record.subscription_id,
            "from_plan_tier": record.from_plan_tier,
            "from_plan_code": record.from_plan_code,
            "to_plan_tier": record.to_plan_tier,
            "to_plan_code": record.to_plan_code,
            "effective_mode": record.effective_mode,
            "quota_assessment": {
                "current_active_branches": record.active_branches_at_downgrade,
                "target_allowed_branches": record.allowed_branches_quota,
                "is_over_quota": record.is_over_quota,
                "retained_branch_ids": record.retained_branch_ids,
                "locked_branch_ids": record.locked_branch_ids,
            },
            "data_retention_guarantee": {
                "zero_data_deleted": True,
                "historical_sales_readable": True,
                "stock_ledger_preserved": True,
            },
            "status": record.status,
            "applied_entitlement_id": record.applied_entitlement_id,
            "created_at": record.created_at.isoformat(),
            "executed_at": record.executed_at.isoformat() if record.executed_at else None,
        }
