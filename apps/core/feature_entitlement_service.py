"""
apps/core/feature_entitlement_service.py
Architecture Decision: ADR-0015, ADR-0072, ADR-0080, ADR-0081 & ADR-0082
Phase 05: Basic/Standard/Pro Subscription & Billing (P082)

Server-Authoritative Feature Entitlement Engine:
- Evaluates effective capabilities for authenticated tenant/branch/device requests.
- Cardinal Invariant (INV-SAFETY-01): Clinical safety features are NEVER gated, even when suspended.
- Commercial Lockout: Dispensing operations (CHECKOUT_DISPENSING) are locked down upon suspension.
- Dynamic limits and quotas (branches, devices, staff).
- Bounded offline device lease (72 hours).
- Seamless merging with Platform Feature Flags and tenant targeting overrides.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.core.feature_flag_service import FeatureFlagService
from apps.core.models import (
    Branch,
    StaffMember,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.plans import CLINICALLY_PROTECTED_FEATURES

logger = logging.getLogger("pharmacy.billing.entitlement_engine")


class FeatureEntitlementEngine:
    """Evaluates effective capabilities, structural quotas, and offline lease parameters."""

    DEFAULT_OFFLINE_LEASE_HOURS = 72

    @classmethod
    def evaluate_effective_entitlements(
        cls,
        tenant_id: str,
        device_id: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Calculates the complete runtime capabilities, limits, and offline parameters for a tenant/device.
        """
        clean_tenant = str(tenant_id or "").strip()
        if not clean_tenant:
            raise ValueError("Tenant ID is required for entitlement evaluation.")

        try:
            tenant = Tenant.objects.get(tenant_id=clean_tenant)
        except Tenant.DoesNotExist as err:
            raise ValueError(f"Tenant '{clean_tenant}' not found.") from err

        now = timezone.now()

        # 1. Retrieve locked-in active entitlement snapshot
        entitlement_data = PlanCatalogueService.get_active_tenant_entitlement(tenant.tenant_id)
        quotas = entitlement_data.get("quotas", {})
        features_snapshot = entitlement_data.get("features_snapshot", {})

        # 2. Retrieve Subscription Record
        sub = SubscriptionRecord.objects.filter(tenant=tenant).first()
        sub_status = sub.status if sub else tenant.status
        renewal_date = sub.renewal_date.isoformat() if sub and sub.renewal_date else None

        # 3. Compute dynamic usage vs quotas
        used_branches = Branch.objects.filter(tenant=tenant, is_active=True).count()
        used_staff = StaffMember.objects.filter(tenant=tenant, is_active=True).count()
        # Used devices: at least 1 if active, or count of active branch devices
        used_devices = max(1, used_branches)

        max_branches = int(quotas.get("max_branches", 1))
        max_devices_per_branch = int(quotas.get("max_devices_per_branch", 1))
        max_total_devices = int(quotas.get("max_total_devices", 1))
        max_staff_users = int(quotas.get("max_staff_users", 3))

        limits = {
            "branches": {
                "max": max_branches,
                "used": used_branches,
                "remaining": max(0, max_branches - used_branches),
                "is_over_quota": used_branches > max_branches,
            },
            "devices": {
                "max": max_total_devices,
                "max_per_branch": max_devices_per_branch,
                "used": used_devices,
                "remaining": max(0, max_total_devices - used_devices),
                "is_over_quota": used_devices > max_total_devices,
            },
            "staff": {
                "max": max_staff_users,
                "used": used_staff,
                "remaining": max(0, max_staff_users - used_staff),
                "is_over_quota": used_staff > max_staff_users,
            },
        }

        # 4. Evaluate Capabilities (Clinical Invariant + Commercial State Machine + Feature Flags)
        capabilities: dict[str, bool] = {}

        # Commercial baseline from applied entitlement snapshot
        for feat_key, val in features_snapshot.items():
            capabilities[feat_key] = bool(val)

        # Merge central platform feature flags (ADR-0072 overrides)
        flag_evaluations = FeatureFlagService.evaluate_all_flags_for_tenant(tenant.tenant_id)
        for flag_k, flag_v in flag_evaluations.get("flags", {}).items():
            capabilities[flag_k.upper()] = bool(flag_v)

        # Counter dispensing checkout capability
        is_checkout_allowed = tenant.is_checkout_allowed()
        if sub and not sub.is_checkout_allowed():
            is_checkout_allowed = False
        capabilities["CHECKOUT_DISPENSING"] = is_checkout_allowed

        # Mandatory Clinical Safety Invariant (INV-SAFETY-01)
        # Clinical safety controls are ALWAYS True across all tiers and lifecycle states
        for safety_feature in CLINICALLY_PROTECTED_FEATURES:
            capabilities[safety_feature] = True

        # 5. Offline Lease Evaluation (ADR-0082 & ADR-0084: 72 hours bounded validity)
        is_suspended = tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED] or (
            sub
            and sub.status in [SubscriptionStatus.SUSPENDED, SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED]
        )
        if is_suspended:
            lease_status = "SUSPENDED"
            lease_expires_at = None
        else:
            lease_status = "ACTIVE"
            lease_expires_at = (now + timedelta(hours=cls.DEFAULT_OFFLINE_LEASE_HOURS)).isoformat()

        offline_lease = {
            "is_offline_capable": True,
            "lease_duration_hours": cls.DEFAULT_OFFLINE_LEASE_HOURS,
            "lease_status": lease_status,
            "lease_expires_at": lease_expires_at,
        }

        return {
            "tenant_id": tenant.tenant_id,
            "device_id": str(device_id).strip() if device_id else None,
            "branch_id": str(branch_id).strip() if branch_id else None,
            "subscription": {
                "status": sub_status,
                "plan_tier": tenant.plan_tier,
                "plan_code": entitlement_data.get("plan_code", f"PLAN_{tenant.plan_tier}"),
                "billing_cycle": entitlement_data.get("billing_cycle", "MONTHLY"),
                "renewal_date": renewal_date,
                "is_checkout_allowed": is_checkout_allowed,
            },
            "limits": limits,
            "offline_lease": offline_lease,
            "capabilities": capabilities,
            "evaluated_at": now.isoformat(),
        }
