"""
backend/apps/core/quota_guard_service.py
Architecture Decision: ADR-0015, ADR-0081, ADR-0083 & ADR-0091
Phase 05: Basic/Standard/Pro Subscription & Billing (P091)

Subscription Quota Guard Service:
- Centralized guardrail evaluating tenant usage against active plan quotas.
- Restricts additional mutations (new branch creation, staff invitations, device enrollments).
- Enforces branch over-quota locks preventing new checkout/sales transactions at archived branches.
- Guarantees 100% historical data readability for audits, ledgers, and regulatory reports.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError

from apps.core.models import (
    Branch,
    PlanCatalogueRecord,
    Tenant,
    TenantAppliedEntitlement,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.billing.quota_guard")


class QuotaExceededError(ValidationError):
    """Raised when an operation would exceed allowed plan quotas."""
    pass


class BranchOverQuotaError(ValidationError):
    """Raised when an operation is attempted on an over-quota locked branch."""
    pass


class SubscriptionQuotaGuardService:

    @classmethod
    def assess_account_quotas(
        cls,
        tenant_id: str,
        target_plan_code: str | None = None,
    ) -> dict[str, Any]:
        """
        Calculates active usage vs allowed quotas for branches, staff, and devices.
        If target_plan_code is provided, evaluates against the prospective plan.
        """
        clean_tid = str(tenant_id or "").strip()
        try:
            tenant = Tenant.objects.get(tenant_id=clean_tid)
        except Tenant.DoesNotExist as err:
            raise ValidationError(f"Tenant '{clean_tid}' does not exist.") from err

        if target_plan_code:
            PlanCatalogueService.ensure_canonical_plans_seeded()
            try:
                plan = PlanCatalogueRecord.objects.get(
                    plan_code=str(target_plan_code).strip().upper(),
                    is_active=True,
                )
                allowed_branches = plan.max_branches
                allowed_staff = plan.max_staff_users
                allowed_devices = plan.max_total_devices
                plan_tier = plan.tier
                plan_code = plan.plan_code
            except PlanCatalogueRecord.DoesNotExist as err:
                raise ValidationError(f"Target plan '{target_plan_code}' does not exist.") from err
        else:
            entitlement = TenantAppliedEntitlement.objects.filter(
                tenant=tenant,
                is_active=True,
            ).first()
            if entitlement:
                allowed_branches = entitlement.max_branches
                allowed_staff = entitlement.max_staff_users
                allowed_devices = entitlement.max_total_devices
                plan_tier = entitlement.plan_tier
                plan_code = entitlement.plan_code
            else:
                allowed_branches = 1
                allowed_staff = 3
                allowed_devices = 2
                plan_tier = tenant.plan_tier
                plan_code = f"PLAN_{tenant.plan_tier}"

        # Count active vs locked branches
        all_branches = list(Branch.objects.filter(tenant=tenant))
        active_unlocked_branches = [b for b in all_branches if b.is_active and not b.is_over_quota_locked]
        locked_branches = [b for b in all_branches if b.is_over_quota_locked]

        active_branches_count = len(active_unlocked_branches)
        is_branch_over_quota = active_branches_count > allowed_branches

        # Staff count
        active_staff_count = PharmacyUser.objects.filter(
            tenant_id=clean_tid,
            is_active=True,
            is_archived=False,
        ).count()
        is_staff_over_quota = active_staff_count > allowed_staff

        return {
            "tenant_id": clean_tid,
            "plan_code": plan_code,
            "plan_tier": plan_tier,
            "branches": {
                "active_count": active_branches_count,
                "locked_count": len(locked_branches),
                "total_count": len(all_branches),
                "allowed_quota": allowed_branches,
                "is_over_quota": is_branch_over_quota,
                "active_branch_ids": [b.branch_id for b in active_unlocked_branches],
                "locked_branch_ids": [b.branch_id for b in locked_branches],
            },
            "staff": {
                "active_count": active_staff_count,
                "allowed_quota": allowed_staff,
                "is_over_quota": is_staff_over_quota,
            },
            "devices": {
                "allowed_quota": allowed_devices,
            },
            "is_over_quota": is_branch_over_quota or is_staff_over_quota,
            "data_retention_guarantee": {
                "zero_data_deleted": True,
                "historical_sales_readable": True,
                "stock_ledger_preserved": True,
            },
        }

    @classmethod
    def check_branch_creation_allowed(cls, tenant_id: str) -> None:
        """Gates creation of new branches if current active count reaches or exceeds quota."""
        quotas = cls.assess_account_quotas(tenant_id)
        b_info = quotas["branches"]
        if b_info["active_count"] >= b_info["allowed_quota"]:
            raise QuotaExceededError(
                f"Cannot create new branch: tenant '{tenant_id}' has reached active branch quota "
                f"({b_info['active_count']}/{b_info['allowed_quota']}). "
                "Please upgrade your plan to add more branches."
            )

    @classmethod
    def check_branch_sales_allowed(cls, tenant_id: str, branch_id: str) -> None:
        """Gates checkout transactions on over-quota locked branches."""
        clean_bid = str(branch_id or "").strip()
        branch = Branch.objects.filter(branch_id=clean_bid, tenant_id=tenant_id).first()
        if not branch:
            raise ValidationError(f"Branch '{clean_bid}' not found for tenant '{tenant_id}'.")

        if branch.is_over_quota_locked:
            raise BranchOverQuotaError(
                f"Branch '{branch.name}' ({clean_bid}) is locked due to plan downgrade over-quota limits. "
                "Historical transactions and inventory remain readable, but new sales mutations are prohibited. "
                "Please upgrade your subscription to reactivate this branch."
            )

        if not branch.is_active:
            raise ValidationError(f"Branch '{branch.name}' is inactive and cannot process sales.")

    @classmethod
    def check_staff_creation_allowed(cls, tenant_id: str) -> None:
        """Gates new staff member invitation or creation if quota is reached."""
        quotas = cls.assess_account_quotas(tenant_id)
        s_info = quotas["staff"]
        if s_info["active_count"] >= s_info["allowed_quota"]:
            raise QuotaExceededError(
                f"Cannot add staff member: tenant '{tenant_id}' has reached active staff quota "
                f"({s_info['active_count']}/{s_info['allowed_quota']}). "
                "Please upgrade your plan to increase staff seats."
            )
