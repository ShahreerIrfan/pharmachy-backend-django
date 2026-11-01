"""
backend/apps/core/subscription_state_machine_service.py

Architecture Decision: ADR-0013, ADR-0015, ADR-0033 & ADR-0084
Phase 05: Basic/Standard/Pro Subscription & Billing (P084)

Guarded Subscription Finite State Machine (FSM) Service:
- Supported states: PENDING, TRIAL, ACTIVE, PAST_DUE, GRACE_PERIOD, SUSPENDED, CANCELLED, EXPIRED.
- Guarded state transitions with role-based checks (Owner vs Superadmin vs System).
- Synchronous tenant lifecycle alignment and audit logging.
- Verified subscription payment processing and renewal date extension.
- Preserves Cardinal Invariant (INV-SAFETY-01): clinical safety features never gated.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    PaymentMethod,
    PaymentTransaction,
    PlanTier,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService

logger = logging.getLogger("pharmacy.billing.state_machine")


class SubscriptionStateMachineService:
    """Manages subscription state machine transitions, guards, payment verification, and audit logs."""

    DEFAULT_GRACE_PERIOD_DAYS = 15

    @classmethod
    def get_or_create_subscription(cls, tenant_id: str) -> SubscriptionRecord:
        """Retrieves active subscription or bootstraps a standard subscription record."""
        tenant = Tenant.objects.get(tenant_id=tenant_id)
        sub = SubscriptionRecord.objects.filter(tenant=tenant).first()
        if not sub:
            now = timezone.now().date()
            tier = tenant.plan_tier or PlanTier.STANDARD
            plan_record = PlanCatalogueService.get_plan_by_tier(tier) or PlanCatalogueService.get_plan_by_code(
                "PLAN_STANDARD"
            )
            sub = SubscriptionRecord.objects.create(
                subscription_id=f"sub_{uuid.uuid4().hex[:16]}",
                tenant=tenant,
                plan_tier=tier,
                status=SubscriptionStatus.ACTIVE
                if tenant.status == TenantLifecycleStatus.ACTIVE
                else SubscriptionStatus.TRIAL,
                amount_bdt=plan_record.monthly_price_bdt,
                billing_cycle="MONTHLY",
                licensed_branches=plan_record.max_branches,
                licensed_devices=plan_record.max_total_devices,
                features_enabled=dict(plan_record.features),
                start_date=now,
                renewal_date=now + datetime.timedelta(days=30),
                is_auto_renew=True,
            )
        return sub

    @classmethod
    def get_subscription_details(cls, tenant_id: str) -> dict[str, Any]:
        """Returns structured details of the tenant's subscription and available next transitions."""
        sub = cls.get_or_create_subscription(tenant_id)
        now_date = timezone.now().date()
        days_to_renewal = (sub.renewal_date - now_date).days if sub.renewal_date else 0

        allowed_next = [
            target for target in SubscriptionStatus.values if sub.can_transition_to(target) and target != sub.status
        ]

        return {
            "subscription_id": sub.subscription_id,
            "tenant_id": sub.tenant.tenant_id,
            "tenant_name": sub.tenant.name,
            "plan_tier": sub.plan_tier,
            "status": sub.status,
            "amount_bdt": f"{sub.amount_bdt:.2f}",
            "billing_cycle": sub.billing_cycle,
            "start_date": sub.start_date.isoformat(),
            "renewal_date": sub.renewal_date.isoformat() if sub.renewal_date else None,
            "grace_period_end_date": sub.grace_period_end_date.isoformat() if sub.grace_period_end_date else None,
            "days_to_renewal": days_to_renewal,
            "is_checkout_allowed": sub.is_checkout_allowed(),
            "is_delinquent": sub.is_delinquent(),
            "is_auto_renew": sub.is_auto_renew,
            "allowed_next_transitions": allowed_next,
            "licensed_branches": sub.licensed_branches,
            "licensed_devices": sub.licensed_devices,
        }

    @classmethod
    @transaction.atomic
    def transition_subscription(
        cls,
        tenant_id: str,
        target_status: str,
        operator_id: str,
        operator_role: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Executes a guarded subscription state transition under ADR-0084.
        Enforces state graph constraints, mandatory reasons, role permissions, and tenant sync.
        """
        try:
            tenant = Tenant.objects.select_for_update().get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValueError(f"Tenant '{tenant_id}' does not exist.") from err

        sub = SubscriptionRecord.objects.select_for_update().filter(tenant=tenant).first()
        if not sub:
            sub = cls.get_or_create_subscription(tenant_id)

        target_upper = str(target_status).upper().strip()
        from_status = sub.status

        # 1. Self-transition is a no-op
        if from_status == target_upper:
            return cls.get_subscription_details(tenant_id)

        # 2. Check FSM Graph Validity
        if not sub.can_transition_to(target_upper):
            raise ValidationError(
                f"Invalid subscription transition from '{from_status}' to '{target_upper}'. "
                f"Permitted next states: {[t for t in SubscriptionStatus.values if sub.can_transition_to(t) and t != from_status]}"
            )

        # 3. Role-Based Transition Permissions
        is_superadmin = operator_role in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_SUPER_ADMIN", "PLATFORM_OPERATOR"]
        is_owner = operator_role in ["TENANT_OWNER", "OWNER", "PHARMACY_OWNER"]
        is_system = operator_role in ["SYSTEM", "BILLING_DAEMON", "WEBHOOK"]

        if not (is_superadmin or is_owner or is_system):
            raise PermissionError(f"Role '{operator_role}' is not authorized to transition subscription states.")

        # Owner cannot directly suspend or force past_due (those are administrative / automated)
        if is_owner and not is_superadmin:
            owner_permitted_targets = [
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.CANCELLED,
                SubscriptionStatus.TRIAL,
            ]
            if target_upper not in owner_permitted_targets:
                raise PermissionError(
                    f"Tenant Owner cannot transition subscription directly to '{target_upper}'. "
                    f"Allowed for Owner: {owner_permitted_targets}"
                )

        # 4. Mandatory Reason Guard for Adverse States
        adverse_states = [
            SubscriptionStatus.SUSPENDED,
            SubscriptionStatus.CANCELLED,
            SubscriptionStatus.EXPIRED,
            SubscriptionStatus.PAST_DUE,
        ]
        clean_reason = str(reason or "").strip()
        if target_upper in adverse_states and not clean_reason:
            raise ValidationError(f"A non-empty reason is strictly required when transitioning to '{target_upper}'.")

        # 5. Apply State Transition
        sub.status = target_upper
        now = timezone.now()
        now_date = now.date()

        if target_upper == SubscriptionStatus.ACTIVE:
            tenant.status = TenantLifecycleStatus.ACTIVE
            tenant.is_active = True
            tenant.suspended_at = None
            tenant.suspension_reason = ""
            tenant.suspension_reason_code = ""
            sub.grace_period_end_date = None
            tenant.grace_period_ends_at = None
        elif target_upper == SubscriptionStatus.TRIAL:
            tenant.status = TenantLifecycleStatus.TRIAL
            tenant.is_active = True
            tenant.trial_ends_at = now + datetime.timedelta(days=14)
        elif target_upper == SubscriptionStatus.PAST_DUE:
            tenant.status = TenantLifecycleStatus.PAST_DUE
            tenant.is_active = True
        elif target_upper == SubscriptionStatus.GRACE_PERIOD:
            tenant.status = TenantLifecycleStatus.PAST_DUE
            tenant.is_active = True
            grace_end = now_date + datetime.timedelta(days=cls.DEFAULT_GRACE_PERIOD_DAYS)
            sub.grace_period_end_date = grace_end
            tenant.grace_period_ends_at = now + datetime.timedelta(days=cls.DEFAULT_GRACE_PERIOD_DAYS)
        elif target_upper == SubscriptionStatus.SUSPENDED:
            tenant.status = TenantLifecycleStatus.SUSPENDED
            tenant.is_active = False
            tenant.suspended_at = now
            tenant.suspension_reason = clean_reason
            tenant.suspension_reason_code = "SUBSCRIPTION_SUSPENSION_DELINQUENCY"
        elif target_upper in [SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED]:
            tenant.status = TenantLifecycleStatus.SUSPENDED
            tenant.is_active = False
            tenant.suspended_at = now
            tenant.suspension_reason = clean_reason
            tenant.suspension_reason_code = f"SUBSCRIPTION_{target_upper}"

        sub.save()
        tenant.save()

        # 6. Immutable Audit Trail
        AuditLogEntry.objects.create(
            tenant_id=tenant.tenant_id,
            user_id=operator_id,
            action="SUBSCRIPTION_STATE_TRANSITION",
            resource_type="SUBSCRIPTION_RECORD",
            resource_id=sub.subscription_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "from_status": from_status,
                "to_status": target_upper,
                "operator_role": operator_role,
                "reason": clean_reason,
                "extra": metadata or {},
            },
        )

        logger.info(
            f"Tenant '{tenant_id}' subscription transitioned from '{from_status}' to '{target_upper}' "
            f"by '{operator_id}' ({operator_role}). Reason: {clean_reason}"
        )

        return cls.get_subscription_details(tenant_id)

    @classmethod
    @transaction.atomic
    def verify_and_record_payment(
        cls,
        tenant_id: str,
        amount_bdt: Decimal,
        transaction_ref: str,
        payment_method: str = "BKASH",
        operator_id: str = "SYSTEM",
        operator_role: str = "SYSTEM",
        notes: str = "",
    ) -> dict[str, Any]:
        """
        Verifies subscription payment, records payment transaction, extends renewal date,
        and transitions delinquent or pending subscription back to ACTIVE.
        """
        try:
            tenant = Tenant.objects.select_for_update().get(tenant_id=tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValueError(f"Tenant '{tenant_id}' does not exist.") from err

        sub = cls.get_or_create_subscription(tenant_id)

        clean_ref = str(transaction_ref or "").strip()
        if len(clean_ref) < 4:
            raise ValidationError("Transaction reference must be at least 4 characters long.")

        if amount_bdt <= Decimal("0.00"):
            raise ValidationError("Payment amount must be greater than zero.")

        # Check for duplicate payment transaction ref
        if PaymentTransaction.objects.filter(gateway_reference=clean_ref).exists():
            raise ValidationError(f"Transaction reference '{clean_ref}' has already been processed.")

        primary_branch = Branch.objects.filter(tenant=tenant, is_active=True).first()
        if not primary_branch:
            primary_branch = Branch.objects.create(
                branch_id=f"br_{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                name="Headquarters",
                is_active=True,
            )

        # Record Payment Transaction
        now = timezone.now()
        method_upper = payment_method.upper().strip()
        tender = getattr(PaymentMethod, method_upper, PaymentMethod.BKASH)

        tx = PaymentTransaction.objects.create(
            transaction_id=f"tx_{uuid.uuid4().hex[:16]}",
            tenant=tenant,
            branch=primary_branch,
            tender_type=tender,
            gateway_reference=clean_ref,
            amount_bdt=amount_bdt,
            status="SUCCESS",
        )

        # Extend Renewal Date based on billing cycle
        cycle = (sub.billing_cycle or "MONTHLY").upper()
        extension_days = 365 if cycle == "ANNUAL" else 30
        current_base_date = max(sub.renewal_date, now.date()) if sub.renewal_date else now.date()
        new_renewal_date = current_base_date + datetime.timedelta(days=extension_days)

        sub.renewal_date = new_renewal_date
        sub.amount_bdt = amount_bdt
        sub.grace_period_end_date = None
        sub.save(update_fields=["renewal_date", "amount_bdt", "grace_period_end_date"])

        # Transition to ACTIVE if subscription was pending, trial, past_due, grace, or suspended
        from_status = sub.status
        transition_reason = f"Verified subscription payment ({clean_ref}) of BDT {amount_bdt:.2f} via {payment_method}."
        if from_status != SubscriptionStatus.ACTIVE:
            cls.transition_subscription(
                tenant_id=tenant_id,
                target_status=SubscriptionStatus.ACTIVE,
                operator_id=operator_id,
                operator_role=operator_role,
                reason=transition_reason,
                metadata={"transaction_id": tx.transaction_id, "amount_bdt": str(amount_bdt)},
            )
        else:
            # Audit log renewal
            AuditLogEntry.objects.create(
                tenant_id=tenant.tenant_id,
                user_id=operator_id,
                action="SUBSCRIPTION_PAYMENT_VERIFIED",
                resource_type="SUBSCRIPTION_RECORD",
                resource_id=sub.subscription_id,
                status=AuditActionStatus.SUCCESS,
                metadata_json={
                    "transaction_id": tx.transaction_id,
                    "transaction_ref": clean_ref,
                    "amount_bdt": str(amount_bdt),
                    "new_renewal_date": new_renewal_date.isoformat(),
                    "payment_method": payment_method,
                },
            )

        return {
            "success": True,
            "tenant_id": tenant_id,
            "subscription_id": sub.subscription_id,
            "transaction_id": tx.transaction_id,
            "transaction_ref": clean_ref,
            "amount_bdt": f"{amount_bdt:.2f}",
            "status": SubscriptionStatus.ACTIVE,
            "renewal_date": new_renewal_date.isoformat(),
            "billing_cycle": sub.billing_cycle,
        }

    @classmethod
    def get_subscription_history(cls, tenant_id: str) -> list[dict[str, Any]]:
        """Retrieves audited timeline of subscription state transitions for a tenant."""
        entries = AuditLogEntry.objects.filter(
            tenant_id=tenant_id,
            action__in=["SUBSCRIPTION_STATE_TRANSITION", "SUBSCRIPTION_PAYMENT_VERIFIED", "TENANT_ENTITLEMENT_APPLIED"],
        ).order_by("-timestamp")[:50]

        history = []
        for e in entries:
            meta = e.metadata_json or {}
            history.append(
                {
                    "entry_id": e.entry_id,
                    "action": e.action,
                    "timestamp": e.timestamp.isoformat(),
                    "user_id": e.user_id,
                    "from_status": meta.get("from_status"),
                    "to_status": meta.get("to_status"),
                    "reason": meta.get("reason", ""),
                    "details": meta,
                }
            )
        return history
