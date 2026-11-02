"""
Staff Performance Metrics & Cash Reconciliation Variance Service.
Architecture Decision: ADR-0115 (P115).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.core.models import (
    Branch,
    BranchStaffMembership,
    StaffMember,
    StaffPerformanceMetricSnapshot,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.core.staff_performance")


class StaffPerformanceService:
    """Core domain logic for calculating and retrieving staff sales and reconciliation metrics."""

    @classmethod
    def record_snapshot(
        cls,
        tenant: Tenant,
        staff_member: StaffMember,
        metric_date: timezone.datetime.date,
        total_sales_count: int = 0,
        total_sales_amount: Decimal = Decimal("0.00"),
        total_refunds_count: int = 0,
        total_refunds_amount: Decimal = Decimal("0.00"),
        reconciliation_variance_amount: Decimal = Decimal("0.00"),
        branch: Optional[Branch] = None,
        last_activity_at: Optional[timezone.datetime] = None,
    ) -> StaffPerformanceMetricSnapshot:
        """Records or updates a staff performance snapshot with Decimal monetary precision."""
        effective_branch = branch or staff_member.branch
        net_revenue = total_sales_amount - total_refunds_amount

        status_str = "BALANCED"
        if reconciliation_variance_amount > Decimal("0.00"):
            status_str = "OVERAGE"
        elif reconciliation_variance_amount < Decimal("0.00"):
            status_str = "SHORTAGE"

        now = last_activity_at or timezone.now()
        freshness = max(0, int((timezone.now() - now).total_seconds()))

        snapshot_id = f"spm-{uuid.uuid4().hex[:12]}"
        snapshot, created = StaffPerformanceMetricSnapshot.objects.update_or_create(
            tenant=tenant,
            staff_member=staff_member,
            metric_date=metric_date,
            defaults={
                "snapshot_id": snapshot_id,
                "branch": effective_branch,
                "total_sales_count": total_sales_count,
                "total_sales_amount": total_sales_amount,
                "total_refunds_count": total_refunds_count,
                "total_refunds_amount": total_refunds_amount,
                "net_revenue": net_revenue,
                "reconciliation_variance_amount": reconciliation_variance_amount,
                "reconciliation_status": status_str,
                "reporting_freshness_seconds": freshness,
                "last_activity_at": now,
            },
        )
        return snapshot

    @classmethod
    def list_metrics(
        cls,
        actor_user: Any,
        tenant_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        staff_id: Optional[str] = None,
        metric_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists staff performance metrics enforcing RBAC:
        - Superadmin/Owner: Full tenant visibility across branches and staff.
        - Branch Manager: Scoped strictly to staff members in their assigned branch.
        - Cashier/Staff: Scoped strictly to self-only performance.
        """
        actor_tenant = getattr(actor_user, "tenant_id", None)
        actor_role = getattr(actor_user, "role", None)
        is_superadmin = actor_role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"] or getattr(actor_user, "is_superuser", False)
        is_owner = actor_role in ["PHARMACY_OWNER", "OWNER", "TENANT_OWNER"]
        is_manager = actor_role in ["BRANCH_MANAGER", "MANAGER"]

        effective_tenant = tenant_id or actor_tenant
        if not is_superadmin and actor_tenant and effective_tenant != actor_tenant:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant access forbidden for tenant '{effective_tenant}'.",
                    "message_bn": "অন্য টেন্যান্টের পারফরম্যান্স মেট্রিক দেখার অনুমতি নেই।",
                }
            )

        # 1. Tenant & Subscription Check
        tenant = Tenant.objects.filter(tenant_id=effective_tenant).first()
        if tenant and tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": f"Tenant subscription is {tenant.status}. Access blocked.",
                    "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত থাকায় ডেটা অ্যাক্সেস বন্ধ।",
                }
            )

        # 2. Staff Lifecycle Check on Caller
        if not getattr(actor_user, "is_active", True) or getattr(actor_user, "is_archived", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Staff account is inactive or terminated.",
                    "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয়।",
                }
            )

        qs = StaffPerformanceMetricSnapshot.objects.filter(tenant=tenant).select_related("staff_member", "branch")

        # 3. RBAC Scoping
        if is_superadmin or is_owner:
            if branch_id:
                qs = qs.filter(branch__branch_id=branch_id)
            if staff_id:
                qs = qs.filter(staff_member__staff_id=staff_id)
        elif is_manager:
            # Get manager's assigned branch
            manager_branch_id = getattr(actor_user, "branch_id", None)
            if not manager_branch_id:
                # Check BranchStaffMembership
                mem = BranchStaffMembership.objects.filter(staff_member__username=actor_user.email, is_active=True).first()
                if mem and mem.branch:
                    manager_branch_id = mem.branch.branch_id

            if branch_id and branch_id != manager_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                        "message_en": f"Branch manager cannot access metrics for foreign branch '{branch_id}'.",
                        "message_bn": "শাখা ব্যবস্থাপক অন্য শাখার মেট্রিক দেখতে পারবেন না।",
                    }
                )
            qs = qs.filter(branch__branch_id=manager_branch_id)
            if staff_id:
                qs = qs.filter(staff_member__staff_id=staff_id)
        else:
            # Cashier / Frontline: Strictly self-only
            actor_staff = StaffMember.objects.filter(tenant=tenant, username=actor_user.email).first()
            actor_staff_id = actor_staff.staff_id if actor_staff else None

            if staff_id and staff_id != actor_staff_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Staff members are strictly restricted to their own performance metrics.",
                        "message_bn": "স্টাফ শুধুমাত্র নিজের পারফরম্যান্স মেট্রিক দেখতে পারেন।",
                    }
                )
            if actor_staff_id:
                qs = qs.filter(staff_member__staff_id=actor_staff_id)
            else:
                qs = qs.none()

        if metric_date:
            qs = qs.filter(metric_date=metric_date)

        now = timezone.now()
        results = []
        for snap in qs:
            freshness = max(0, int((now - snap.last_activity_at).total_seconds()))
            results.append(
                {
                    "snapshot_id": snap.snapshot_id,
                    "tenant_id": snap.tenant.tenant_id,
                    "branch_id": snap.branch.branch_id if snap.branch else None,
                    "staff_id": snap.staff_member.staff_id,
                    "staff_name": snap.staff_member.full_name,
                    "staff_role": str(snap.staff_member.role),
                    "metric_date": snap.metric_date.isoformat(),
                    "total_sales_count": snap.total_sales_count,
                    "total_sales_amount": f"{snap.total_sales_amount:.2f}",
                    "total_refunds_count": snap.total_refunds_count,
                    "total_refunds_amount": f"{snap.total_refunds_amount:.2f}",
                    "net_revenue": f"{snap.net_revenue:.2f}",
                    "reconciliation_variance_amount": f"{snap.reconciliation_variance_amount:.2f}",
                    "reconciliation_status": snap.reconciliation_status,
                    "reporting_freshness_seconds": freshness,
                    "last_activity_at": snap.last_activity_at.isoformat(),
                }
            )
        return results

