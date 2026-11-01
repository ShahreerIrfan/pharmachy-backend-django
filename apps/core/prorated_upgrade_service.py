"""
backend/apps/core/prorated_upgrade_service.py
Architecture Decision: ADR-0015, ADR-0081, ADR-0083, ADR-0087 & ADR-0090
Phase 05: Basic/Standard/Pro Subscription & Billing (P090)

Prorated Subscription Upgrade Service:
- Calculates exact day-based prorated upgrades with Decimal BDT precision.
- Enforces upgrade-only transition hierarchy (BASIC < STANDARD < PRO).
- Computes daily rates, unused credits, prorated target gross, and net payable fee.
- Deducts unused credit directly on renewal invoice adjustments.
- Immediately applies new feature entitlements and quotas upon execution.
- Immutably records transition timestamps and audit logs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    PlanCatalogueRecord,
    PlanTier,
    RenewalInvoiceStatus,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    SubscriptionStatus,
    SubscriptionUpgradeRecord,
    SubscriptionUpgradeStatus,
    Tenant,
    TenantAppliedEntitlement,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService

logger = logging.getLogger("pharmacy.billing.upgrades")


class ProratedUpgradeService:
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
                f"Cannot upgrade subscription in status '{subscription.status}'. "
                "Please renew or reactivate your subscription first."
            )

        return tenant, subscription

    @classmethod
    def quote_upgrade(
        cls,
        tenant_id: str,
        target_plan_code: str,
        billing_cycle: str | None = None,
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Calculates quote, daily rates, remaining days, unused credit and net payable.
        Does NOT alter active subscription or entitlements.
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

        if target_cycle not in target_plan.supported_billing_cycles:
            raise ValidationError(
                f"Target cycle '{target_cycle}' is not supported by plan '{clean_code}'. "
                f"Supported: {target_plan.supported_billing_cycles}"
            )

        current_rank = cls.TIER_RANKS.get(current_tier, 0)
        target_rank = cls.TIER_RANKS.get(target_tier, 0)

        # Enforce Upgrade-only rule
        if target_rank < current_rank:
            raise ValidationError(
                f"Target plan '{target_tier}' has a lower tier than current plan '{current_tier}'. "
                "This is a downgrade; please use the downgrade workflow."
            )

        if target_rank == current_rank:
            if current_cycle == "MONTHLY" and target_cycle == "ANNUAL":
                # Valid cycle upgrade
                pass
            else:
                raise ValidationError(
                    f"Target plan '{target_tier}' ({target_cycle}) is identical to current subscription plan and cycle."
                )

        now = timezone.now()
        today = now.date()
        start_date = subscription.start_date or (today - timedelta(days=1))
        renewal_date = subscription.renewal_date or (today + timedelta(days=29))

        # Total cycle days
        raw_total_days = (renewal_date - start_date).days
        total_cycle_days = max(1, raw_total_days if raw_total_days > 0 else (365 if current_cycle == "ANNUAL" else 30))

        # Used and remaining days
        raw_used_days = (today - start_date).days
        used_days = max(0, min(total_cycle_days, raw_used_days))
        remaining_days = max(0, total_cycle_days - used_days)

        current_plan_amount = subscription.amount_bdt or Decimal("0.00")
        if total_cycle_days > 0 and current_plan_amount > Decimal("0.00"):
            current_daily_rate = (current_plan_amount / Decimal(str(total_cycle_days))).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            unused_credit = (
                current_plan_amount * Decimal(str(remaining_days)) / Decimal(str(total_cycle_days))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            current_daily_rate = Decimal("0.0000")
            unused_credit = Decimal("0.00")

        # Target plan pricing
        if target_cycle == "ANNUAL":
            target_plan_amount = target_plan.annual_price_bdt
        else:
            target_plan_amount = target_plan.monthly_price_bdt

        # Gross prorated charge calculation
        is_cycle_upgrade = (current_cycle != target_cycle)
        if is_cycle_upgrade or target_cycle == "ANNUAL":
            # Full annual reset with credit deduction
            target_daily_rate = (target_plan_amount / Decimal("365")).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            prorated_charge = target_plan_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            # Co-termed remaining days in current monthly cycle
            target_daily_rate = (target_plan_amount / Decimal(str(total_cycle_days))).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            prorated_charge = (
                target_plan_amount * Decimal(str(remaining_days)) / Decimal(str(total_cycle_days))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        net_payable = max(Decimal("0.00"), (prorated_charge - unused_credit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        # Look up current plan code
        current_entitlement = (
            TenantAppliedEntitlement.objects.filter(tenant=tenant, is_active=True).first()
        )
        from_plan_code = current_entitlement.plan_code if current_entitlement else f"PLAN_{current_tier}"

        upgrade_id = f"upg-{uuid.uuid4().hex[:16]}"
        record = SubscriptionUpgradeRecord.objects.create(
            upgrade_id=upgrade_id,
            tenant=tenant,
            subscription=subscription,
            from_plan_tier=current_tier,
            from_plan_code=from_plan_code,
            from_billing_cycle=current_cycle,
            to_plan_tier=target_tier,
            to_plan_code=clean_code,
            to_billing_cycle=target_cycle,
            transition_at=now,
            total_cycle_days=total_cycle_days,
            used_days=used_days,
            remaining_days=remaining_days,
            current_plan_amount_bdt=current_plan_amount,
            current_plan_daily_rate_bdt=current_daily_rate,
            unused_credit_bdt=unused_credit,
            target_plan_amount_bdt=target_plan_amount,
            target_plan_daily_rate_bdt=target_daily_rate,
            prorated_charge_bdt=prorated_charge,
            net_payable_bdt=net_payable,
            status=SubscriptionUpgradeStatus.QUOTED,
            metadata_json={
                "is_cycle_upgrade": is_cycle_upgrade,
                "actor": str(getattr(actor_user, "email", None) or "system"),
            },
        )

        return cls.serialize_upgrade(record)

    @classmethod
    def apply_upgrade(
        cls,
        tenant_id: str,
        upgrade_id: str | None = None,
        target_plan_code: str | None = None,
        billing_cycle: str | None = None,
        payment_method: str = "BKASH",
        payment_reference: str = "",
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """
        Confirms upgrade, creates and settles adjusted invoice, and immediately grants new entitlements.
        """
        PlanCatalogueService.ensure_canonical_plans_seeded()
        clean_tenant_id = str(tenant_id or "").strip()

        if upgrade_id:
            clean_upg_id = str(upgrade_id).strip()
            record = SubscriptionUpgradeRecord.objects.filter(
                upgrade_id=clean_upg_id,
                tenant_id=clean_tenant_id,
            ).select_related("tenant", "subscription").first()
            if not record:
                raise ValidationError(f"Upgrade quote '{clean_upg_id}' not found for tenant '{clean_tenant_id}'.")
            if record.status == SubscriptionUpgradeStatus.COMPLETED:
                return cls.serialize_upgrade(record)
        else:
            if not target_plan_code:
                raise ValidationError("Either upgrade_id or target_plan_code must be provided.")
            quote_dict = cls.quote_upgrade(
                tenant_id=clean_tenant_id,
                target_plan_code=target_plan_code,
                billing_cycle=billing_cycle,
                actor_user=actor_user,
            )
            record = SubscriptionUpgradeRecord.objects.get(upgrade_id=quote_dict["upgrade_id"])

        clean_method = str(payment_method or "BKASH").upper().strip()
        clean_ref = str(payment_reference or "").strip()

        # If net payable > 0, payment reference is required
        if record.net_payable_bdt > Decimal("0.00") and len(clean_ref) < 4:
            raise ValidationError("Payment reference (minimum 4 characters) is required to execute paid upgrade.")

        now = timezone.now()
        target_plan = PlanCatalogueRecord.objects.get(plan_code=record.to_plan_code, is_active=True)

        # 1. Generate Adjusted Invoice
        invoice_num = f"INV-UPG-{now.strftime('%Y%m')}-{clean_tenant_id[:6].upper()}-{uuid.uuid4().hex[:6].upper()}"
        invoice_status = RenewalInvoiceStatus.PAID if record.net_payable_bdt > Decimal("0.00") or record.net_payable_bdt == Decimal("0.00") else RenewalInvoiceStatus.PENDING

        inv_period_start = now.date()
        if record.to_billing_cycle == "ANNUAL":
            inv_period_end = (now + timedelta(days=365)).date()
        else:
            inv_period_end = record.subscription.renewal_date or (now + timedelta(days=record.remaining_days)).date()

        invoice = SubscriptionRenewalInvoice.objects.create(
            invoice_id=f"ren-inv-{uuid.uuid4().hex[:12]}",
            invoice_number=invoice_num,
            tenant=record.tenant,
            subscription=record.subscription,
            plan_tier=record.to_plan_tier,
            billing_cycle=record.to_billing_cycle,
            period_start=inv_period_start,
            period_end=inv_period_end,
            subtotal_bdt=record.prorated_charge_bdt,
            discount_bdt=record.unused_credit_bdt,
            tax_rate_percent=Decimal("0.00"),
            tax_amount_bdt=Decimal("0.00"),
            total_bdt=record.net_payable_bdt,
            status=invoice_status,
            payment_method=clean_method,
            payment_reference=clean_ref or "CREDIT_OFFSET",
            paid_at=now,
            status_history_json=[
                {
                    "from_status": "NONE",
                    "to_status": invoice_status,
                    "timestamp": now.isoformat(),
                    "actor": str(getattr(actor_user, "email", None) or "system"),
                    "note": f"Upgrade adjustment from {record.from_plan_tier} to {record.to_plan_tier}",
                }
            ],
        )

        # 2. Deactivate Older Entitlements
        TenantAppliedEntitlement.objects.filter(tenant=record.tenant, is_active=True).update(
            is_active=False,
            effective_until=now,
        )

        # 3. Create Immediate New Entitlement Snapshot
        new_entitlement_id = f"ent_{uuid.uuid4().hex[:16]}"
        entitlement = TenantAppliedEntitlement.objects.create(
            entitlement_id=new_entitlement_id,
            tenant=record.tenant,
            plan_code=target_plan.plan_code,
            plan_tier=target_plan.tier,
            plan_version=target_plan.version,
            billing_cycle=record.to_billing_cycle,
            unit_price_bdt=record.target_plan_amount_bdt,
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
            notes=f"Prorated upgrade from {record.from_plan_tier} ({record.upgrade_id})",
        )

        # 4. Atomically Update Subscription
        sub = record.subscription
        sub.plan_tier = record.to_plan_tier
        sub.billing_cycle = record.to_billing_cycle
        sub.amount_bdt = record.target_plan_amount_bdt
        sub.licensed_branches = target_plan.max_branches
        sub.licensed_devices = target_plan.max_total_devices
        sub.features_enabled = dict(target_plan.features)
        if record.to_billing_cycle == "ANNUAL":
            sub.renewal_date = (now + timedelta(days=365)).date()
        if sub.status in [SubscriptionStatus.TRIAL, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.PAST_DUE]:
            sub.status = SubscriptionStatus.ACTIVE
        sub.save(update_fields=[
            "plan_tier",
            "billing_cycle",
            "amount_bdt",
            "licensed_branches",
            "licensed_devices",
            "features_enabled",
            "renewal_date",
            "status",
        ])

        # 5. Atomically Update Tenant
        tenant = record.tenant
        tenant.plan_tier = record.to_plan_tier
        if tenant.status in [TenantLifecycleStatus.TRIAL, TenantLifecycleStatus.SUSPENDED]:
            tenant.status = TenantLifecycleStatus.ACTIVE
        tenant.save(update_fields=["plan_tier", "status"])

        # 6. Complete Upgrade Record
        record.invoice = invoice
        record.status = SubscriptionUpgradeStatus.COMPLETED
        record.payment_method = clean_method
        record.payment_reference = clean_ref or "CREDIT_OFFSET"
        record.applied_entitlement = entitlement
        record.executed_at = now
        record.save(update_fields=[
            "invoice",
            "status",
            "payment_method",
            "payment_reference",
            "applied_entitlement",
            "executed_at",
        ])

        # 7. Audit Log
        AuditLogEntry.objects.create(
            action="SUBSCRIPTION_UPGRADE_EXECUTED",
            resource_type="SUBSCRIPTION",
            resource_id=record.upgrade_id,
            tenant_id=clean_tenant_id,
            user_id=getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "from_tier": record.from_plan_tier,
                "to_tier": record.to_plan_tier,
                "from_cycle": record.from_billing_cycle,
                "to_cycle": record.to_billing_cycle,
                "net_payable_bdt": str(record.net_payable_bdt),
                "unused_credit_bdt": str(record.unused_credit_bdt),
                "invoice_number": invoice.invoice_number,
                "entitlement_id": entitlement.entitlement_id,
            },
        )

        logger.info(
            "Upgrade %s successfully executed for tenant '%s': %s -> %s (net BDT %s)",
            record.upgrade_id,
            clean_tenant_id,
            record.from_plan_tier,
            record.to_plan_tier,
            record.net_payable_bdt,
        )

        return cls.serialize_upgrade(record)

    @classmethod
    def list_upgrades(cls, tenant_id: str) -> list[dict[str, Any]]:
        clean_tenant_id = str(tenant_id or "").strip()
        qs = SubscriptionUpgradeRecord.objects.filter(tenant_id=clean_tenant_id).select_related("invoice", "applied_entitlement")
        return [cls.serialize_upgrade(r) for r in qs]

    @classmethod
    def get_upgrade(cls, tenant_id: str, upgrade_id: str) -> dict[str, Any]:
        clean_tenant_id = str(tenant_id or "").strip()
        clean_upg_id = str(upgrade_id or "").strip()
        record = SubscriptionUpgradeRecord.objects.filter(
            upgrade_id=clean_upg_id,
            tenant_id=clean_tenant_id,
        ).select_related("invoice", "applied_entitlement").first()
        if not record:
            raise ValidationError(f"Upgrade '{clean_upg_id}' not found for tenant '{clean_tenant_id}'.")
        return cls.serialize_upgrade(record)

    @classmethod
    def serialize_upgrade(cls, record: SubscriptionUpgradeRecord) -> dict[str, Any]:
        return {
            "upgrade_id": record.upgrade_id,
            "tenant_id": record.tenant_id,
            "subscription_id": record.subscription_id,
            "from_plan_tier": record.from_plan_tier,
            "from_plan_code": record.from_plan_code,
            "from_billing_cycle": record.from_billing_cycle,
            "to_plan_tier": record.to_plan_tier,
            "to_plan_code": record.to_plan_code,
            "to_billing_cycle": record.to_billing_cycle,
            "transition_timestamp": record.transition_at.isoformat(),
            "proration_days": {
                "total_days": record.total_cycle_days,
                "used_days": record.used_days,
                "remaining_days": record.remaining_days,
            },
            "financials": {
                "current_plan_amount_bdt": float(record.current_plan_amount_bdt),
                "current_daily_rate_bdt": float(record.current_plan_daily_rate_bdt),
                "unused_credit_bdt": float(record.unused_credit_bdt),
                "target_plan_amount_bdt": float(record.target_plan_amount_bdt),
                "target_daily_rate_bdt": float(record.target_plan_daily_rate_bdt),
                "prorated_target_gross_bdt": float(record.prorated_charge_bdt),
                "tax_amount_bdt": 0.0,
                "net_payable_bdt": float(record.net_payable_bdt),
            },
            "status": record.status,
            "payment_method": record.payment_method,
            "payment_reference": record.payment_reference,
            "invoice_number": record.invoice.invoice_number if record.invoice else None,
            "invoice_id": record.invoice.invoice_id if record.invoice else None,
            "immediate_entitlement_granted": record.status == SubscriptionUpgradeStatus.COMPLETED,
            "applied_entitlement_id": record.applied_entitlement_id,
            "created_at": record.created_at.isoformat(),
            "executed_at": record.executed_at.isoformat() if record.executed_at else None,
        }
