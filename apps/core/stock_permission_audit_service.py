"""
backend/apps/core/stock_permission_audit_service.py
Architecture Decision: ADR-0219 (P219).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Authoritative RBAC, branch-boundary enforcement, and audit engine governing:
1. STOCK_COUNTING
2. STOCK_ADJUSTMENT
3. QUARANTINE_RELEASE
4. INTERBRANCH_TRANSFER
5. VALUATION_DATA
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Set

from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    PermissionDecision,
    StockActionScope,
    StockPermissionAuditLog,
    StockPermissionPolicy,
    Tenant,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

# Roles authorized tenant-wide (cross-branch access permitted within same tenant)
TENANT_WIDE_ROLES: Set[str] = {
    UserRole.SUPERADMIN,
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.AUDITOR,
    "SUPERADMIN",
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "AUDITOR",
}

# Granular Permission Matrix across the 5 core stock actions
ACTION_PERMISSION_MATRIX: Dict[str, Set[str]] = {
    StockActionScope.STOCK_COUNTING: {
        UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST, UserRole.INVENTORY_CLERK,
        "SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "INVENTORY_CLERK",
    },
    StockActionScope.STOCK_ADJUSTMENT: {
        UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        "SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST",
    },
    StockActionScope.QUARANTINE_RELEASE: {
        UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        "SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST",
    },
    StockActionScope.INTERBRANCH_TRANSFER: {
        UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST,
        "SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST",
    },
    StockActionScope.VALUATION_DATA: {
        UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER,
        UserRole.STAFF_PHARMACIST, UserRole.FINANCE_OFFICER, UserRole.AUDITOR,
        "SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER", "STAFF_PHARMACIST", "FINANCE_OFFICER", "AUDITOR",
    },
}


class StockPermissionAuditService:
    """
    Central evaluator and audit recorder for branch-scoped stock permissions.
    """

    @classmethod
    def get_or_default_policy(cls, tenant: Tenant, branch: Branch) -> StockPermissionPolicy:
        policy = StockPermissionPolicy.objects.filter(tenant=tenant, branch=branch).first()
        if not policy:
            policy = StockPermissionPolicy(
                policy_id=f"stpol-default-{branch.branch_id}",
                tenant=tenant,
                branch=branch,
                strict_branch_isolation=True,
                require_pharmacist_for_quarantine_release=True,
                restrict_cost_data_from_clerks=True,
            )
        return policy

    @classmethod
    def get_role_matrix_summary(cls) -> Dict[str, List[str]]:
        return {
            scope: sorted(list(roles))
            for scope, roles in ACTION_PERMISSION_MATRIX.items()
        }

    @classmethod
    def evaluate_and_audit(
        cls,
        user: Any,
        branch: Branch,
        action_scope: str,
        resource_id: str = "",
        raise_on_deny: bool = True,
    ) -> StockPermissionAuditLog:
        """
        Evaluates user permission against tenant, branch, and role boundaries,
        records an immutable StockPermissionAuditLog entry, and optionally raises exceptions.
        """
        if action_scope not in StockActionScope.values:
            raise ValidationError({"action_scope": f"Invalid stock action scope '{action_scope}'."})

        user_role = getattr(user, "role", "UNKNOWN")
        user_tenant_id = getattr(user, "tenant_id", None)
        user_branch_id = getattr(user, "branch_id", None) or ""
        target_tenant = branch.tenant

        # 1. Multi-Tenant Isolation Check
        if user_role != UserRole.SUPERADMIN and user_tenant_id != target_tenant.tenant_id:
            audit = StockPermissionAuditLog.objects.create(
                audit_id=f"paudit-{uuid.uuid4().hex[:12]}",
                tenant=target_tenant,
                branch=branch,
                actor=user if hasattr(user, "pk") and user.is_authenticated else None,
                actor_role=str(user_role),
                actor_assigned_branch_id=str(user_branch_id),
                action_scope=action_scope,
                resource_id=resource_id,
                decision=PermissionDecision.DENIED,
                error_code="ERR_CROSS_TENANT_ACCESS_DENIED",
                reason_en="Cross-tenant stock action blocked.",
                reason_bn="অন্য টেন্যান্টের শাখায় স্টক অপারেশন অনুমোদিত নয়।",
            )
            if raise_on_deny:
                raise NotFound({"detail": "Target branch not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})
            return audit

        policy = cls.get_or_default_policy(target_tenant, branch)

        # 2. Branch-Boundary Check (Strict Branch Isolation)
        if policy.strict_branch_isolation and user_role not in TENANT_WIDE_ROLES:
            if str(user_branch_id) != str(branch.branch_id):
                reason_en = (
                    f"Cross-branch boundary violation: {user_role} assigned to '{user_branch_id}' "
                    f"cannot execute {action_scope} in branch '{branch.branch_id}'."
                )
                reason_bn = (
                    f"শাখার সীমানা লঙ্ঘন: '{user_branch_id}' শাখার {user_role} ব্যবহারকারী "
                    f"'{branch.branch_id}' শাখায় {action_scope} সম্পন্ন করতে পারবেন না।"
                )
                audit = StockPermissionAuditLog.objects.create(
                    audit_id=f"paudit-{uuid.uuid4().hex[:12]}",
                    tenant=target_tenant,
                    branch=branch,
                    actor=user if hasattr(user, "pk") and user.is_authenticated else None,
                    actor_role=str(user_role),
                    actor_assigned_branch_id=str(user_branch_id),
                    action_scope=action_scope,
                    resource_id=resource_id,
                    decision=PermissionDecision.DENIED,
                    error_code="ERR_CROSS_BRANCH_ACCESS_DENIED",
                    reason_en=reason_en,
                    reason_bn=reason_bn,
                )
                if raise_on_deny:
                    raise PermissionDenied({"detail": reason_en, "reason_bn": reason_bn, "error_code": "ERR_CROSS_BRANCH_ACCESS_DENIED"})
                return audit

        # 3. Role-Based Authorization Matrix Check
        allowed_roles = ACTION_PERMISSION_MATRIX.get(action_scope, set())
        if user_role not in allowed_roles:
            reason_en = f"Role '{user_role}' is forbidden from performing '{action_scope}'."
            reason_bn = f"'{user_role}' রোল-এর জন্য '{action_scope}' অ্যাকশন সম্পূর্ণ নিষিদ্ধ।"
            audit = StockPermissionAuditLog.objects.create(
                audit_id=f"paudit-{uuid.uuid4().hex[:12]}",
                tenant=target_tenant,
                branch=branch,
                actor=user if hasattr(user, "pk") and user.is_authenticated else None,
                actor_role=str(user_role),
                actor_assigned_branch_id=str(user_branch_id),
                action_scope=action_scope,
                resource_id=resource_id,
                decision=PermissionDecision.DENIED,
                error_code="ERR_ROLE_NOT_AUTHORIZED",
                reason_en=reason_en,
                reason_bn=reason_bn,
            )
            if raise_on_deny:
                raise PermissionDenied({"detail": reason_en, "reason_bn": reason_bn, "error_code": "ERR_ROLE_NOT_AUTHORIZED"})
            return audit

        # 4. Authorized! Record ALLOWED audit log
        reason_en = f"Authorized: {user_role} permitted to execute {action_scope} in {branch.name}."
        reason_bn = f"অনুমোদিত: {branch.name} শাখায় {user_role}-এর জন্য {action_scope} অনুমোদিত।"
        audit = StockPermissionAuditLog.objects.create(
            audit_id=f"paudit-{uuid.uuid4().hex[:12]}",
            tenant=target_tenant,
            branch=branch,
            actor=user if hasattr(user, "pk") and user.is_authenticated else None,
            actor_role=str(user_role),
            actor_assigned_branch_id=str(user_branch_id),
            action_scope=action_scope,
            resource_id=resource_id,
            decision=PermissionDecision.ALLOWED,
            error_code="",
            reason_en=reason_en,
            reason_bn=reason_bn,
        )
        return audit
