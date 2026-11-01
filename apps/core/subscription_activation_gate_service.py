"""
backend/apps/core/subscription_activation_gate_service.py

Architecture Decision: ADR-0013, ADR-0015, ADR-0033, ADR-0084 & ADR-0085
Phase 05: Basic/Standard/Pro Subscription & Billing (P085)

Subscription Activation Gate Service:
- Server-side operational ERP write barrier.
- Rejects operational ERP mutations (POST/PUT/PATCH/DELETE) when subscription is unentitled
  (SUSPENDED, CANCELLED, EXPIRED, PENDING).
- Unconditionally permits:
  1. Safe HTTP methods (GET, HEAD, OPTIONS) for regulatory auditing and data retrieval.
  2. Authorized billing endpoints (/api/v2/billing/*) for plan selection, quotes, and payment settlement.
  3. Health, auth, documentation, and metadata routes.
  4. Platform Super Admin emergency reconciliation and maintenance operations.
  5. Policy-approved reconciliation, data privacy, and regulatory export routes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from apps.core.models import (
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.billing.activation_gate")


class SubscriptionActivationGateService:
    """Evaluates whether an operational ERP request is entitled to write data."""

    SAFE_HTTP_METHODS = ("GET", "HEAD", "OPTIONS")

    EXEMPT_PATH_PREFIXES = (
        "/api/v2/billing/",
        "/api/v2/auth/",
        "/api/v2/health",
        "/health/",
        "/api/v2/docs",
        "/api/v2/schema",
        "/api/v2/platform/",
        "/api/v2/support/",
        "/admin/",
        "/api/v2/conventions/",
    )

    POLICY_APPROVED_RECONCILIATION_PATTERNS = (
        r"^/api/v2/tenants/[^/]+/data-export/.*$",
        r"^/api/v2/tenants/[^/]+/privacy/.*$",
        r"^/api/v2/inventory/recall-quarantine/.*$",
    )

    @classmethod
    def is_path_exempt(cls, path: str) -> bool:
        """Evaluates whether a route is an authorized billing, policy-approved, or infrastructure path."""
        if any(path.startswith(prefix) for prefix in cls.EXEMPT_PATH_PREFIXES):
            return True

        for pattern in cls.POLICY_APPROVED_RECONCILIATION_PATTERNS:
            if re.match(pattern, path):
                return True

        return False

    @classmethod
    def is_operational_write_permitted(
        cls,
        tenant_id: str | None,
        path: str,
        method: str,
        user: Any = None,
    ) -> tuple[bool, str, str, dict[str, Any]]:
        """
        Determines if a request is permitted under ADR-0085.
        Returns: (is_permitted, code, reason, details)
        """
        method_upper = (method or "GET").upper().strip()

        # 1. Read-Only requests are always permitted
        if method_upper in cls.SAFE_HTTP_METHODS:
            return True, "READ_ONLY_PERMITTED", "Read-only HTTP method is permitted.", {}

        # 2. Path-based exemptions (Billing, Auth, Reconciliation, Health)
        if cls.is_path_exempt(path):
            return True, "EXEMPT_PATH", f"Path '{path}' is an authorized billing or reconciliation route.", {}

        # 3. Super Admin Exemption
        if user and getattr(user, "is_authenticated", False):
            if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
                return True, "EXEMPT_SUPERADMIN", "Platform Super Admin is exempt from operational write locks.", {}
            role = str(getattr(user, "role", "")).upper()
            if role in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_SUPER_ADMIN", "PLATFORM_OPERATOR"]:
                return True, "EXEMPT_SUPERADMIN", "Platform Super Admin role is exempt from operational write locks.", {}

        # 4. Anonymous or unbound tenant requests
        if not tenant_id or not str(tenant_id).strip():
            return True, "UNBOUND_TENANT", "Unbound tenant request delegated to downstream auth.", {}

        clean_tenant_id = str(tenant_id).strip()

        # 5. Inspect Tenant and Subscription Records
        try:
            tenant = Tenant.objects.get(tenant_id=clean_tenant_id)
        except Tenant.DoesNotExist:
            return True, "TENANT_NOT_FOUND", "Tenant does not exist; delegated to 404 handler.", {}

        # Check Subscription Record
        sub = SubscriptionRecord.objects.filter(tenant=tenant).first()
        sub_status = sub.status if sub else None

        # Check Trial Expiration (ADR-0086)
        from django.utils import timezone
        now = timezone.now()
        is_trial_expired = (
            (sub_status == SubscriptionStatus.TRIAL or tenant.status == TenantLifecycleStatus.TRIAL)
            and tenant.trial_ends_at is not None
            and now >= tenant.trial_ends_at
        )
        if is_trial_expired:
            reason = (
                f"Operational ERP write rejected: Tenant evaluation trial expired on "
                f"{tenant.trial_ends_at.strftime('%Y-%m-%d')}. Data mutations are locked while historical records "
                f"are safely preserved. Please upgrade to a commercial subscription plan."
            )
            return (
                False,
                "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                reason,
                {
                    "tenant_id": clean_tenant_id,
                    "subscription_status": "TRIAL_EXPIRED",
                    "trial_ends_at": tenant.trial_ends_at.isoformat(),
                    "billing_url": f"/api/v2/billing/subscriptions/{clean_tenant_id}/",
                },
            )

        # Unentitled Subscription Statuses
        unentitled_sub_statuses = [
            SubscriptionStatus.SUSPENDED,
            SubscriptionStatus.CANCELLED,
            SubscriptionStatus.EXPIRED,
            SubscriptionStatus.PENDING,
        ]

        if sub and sub_status in unentitled_sub_statuses:
            reason = (
                f"Operational ERP write rejected: Tenant subscription status '{sub_status}' does not permit "
                f"data mutations. Please settle past-due billing or reactivate subscription."
            )
            return (
                False,
                "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                reason,
                {
                    "tenant_id": clean_tenant_id,
                    "subscription_status": sub_status,
                    "billing_url": f"/api/v2/billing/subscriptions/{clean_tenant_id}/",
                },
            )

        # Check Tenant Lifecycle Status if no subscription or subscription is neutral
        unentitled_tenant_statuses = [
            TenantLifecycleStatus.SUSPENDED,
            TenantLifecycleStatus.ARCHIVED,
        ]
        if tenant.status in unentitled_tenant_statuses or not tenant.is_active:
            reason = (
                f"Operational ERP write rejected: Tenant lifecycle status '{tenant.status}' does not permit "
                f"data mutations. Please contact support or reactivate subscription."
            )
            return (
                False,
                "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                reason,
                {
                    "tenant_id": clean_tenant_id,
                    "tenant_status": tenant.status,
                    "billing_url": f"/api/v2/billing/subscriptions/{clean_tenant_id}/",
                },
            )

        return True, "ENTITLED", "Subscription status permits operational ERP mutations.", {"status": sub_status or tenant.status}

