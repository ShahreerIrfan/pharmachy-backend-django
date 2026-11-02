"""
Role Migration & Grants Versioning Service.
Architecture Decision: ADR-0119 (P119).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Set

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    CustomRoleDefinition,
    PlanTier,
    RoleGrantMigrationReason,
    RoleGrantMigrationRecord,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.core.role_migration")


class RoleMigrationGrantsService:
    """Core domain logic for versioning role grants and pruning invalid entitlements."""

    FORBIDDEN_PERMISSIONS: Set[str] = {
        "system:hard_delete",
        "stock:dispense_expired",
        "auth:bypass_tenant_isolation",
    }

    # Entitlement mapping per subscription plan tier
    BASIC_ENTITLED_PERMISSIONS: Set[str] = {
        "sales:checkout",
        "sales:view_reports",
        "cash:shift_close",
        "inventory:view_stock",
        "finance:due_repayment",
    }

    STANDARD_ENTITLED_PERMISSIONS: Set[str] = BASIC_ENTITLED_PERMISSIONS | {
        "dispense:rx_antibiotics",
        "rx:verify",
        "rx:view_attachments",
        "sales:void",
        "sales:discount_override",
        "inventory:receive_po",
        "inventory:adjust_writeoff",
        "finance:journal_entries",
        "staff:invite",
        "staff:manage_memberships",
        "roles:manage_custom",
    }

    PRO_ENTITLED_PERMISSIONS: Set[str] = STANDARD_ENTITLED_PERMISSIONS | {
        "inventory:transfer",
        "finance:bad_debt_writeoff",
        "finance:executive_dashboard",
        "devices:enroll",
        "subscription:manage",
        "audit:export",
    }

    @classmethod
    def get_tier_entitled_permissions(cls, tier: str) -> Set[str]:
        t = str(tier).upper()
        if t in [PlanTier.PRO, "ENTERPRISE"]:
            return cls.PRO_ENTITLED_PERMISSIONS
        if t == PlanTier.STANDARD:
            return cls.STANDARD_ENTITLED_PERMISSIONS
        return cls.BASIC_ENTITLED_PERMISSIONS

    @classmethod
    def _validate_actor(cls, actor_user: Any, target_tenant_id: str, is_write: bool = True) -> Dict[str, Any]:
        role = str(getattr(actor_user, "role", "")).upper()
        actor_tenant = getattr(actor_user, "tenant_id", None)
        is_super = getattr(actor_user, "is_superuser", False) or role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"]
        is_owner = role in ["PHARMACY_OWNER", "OWNER", "TENANT_OWNER"]

        if not getattr(actor_user, "is_active", True) or getattr(actor_user, "is_archived", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Staff user is inactive or deactivated.",
                    "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয়।",
                }
            )

        if not is_super and not is_owner:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": f"Role '{role}' is not authorized to execute or preview role migrations.",
                    "message_bn": "রোল মাইগ্রেশন সম্পাদন করার অনুমতি এই রোলের নেই।",
                }
            )

        if not is_super and actor_tenant and actor_tenant != target_tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant role migration forbidden for tenant '{target_tenant_id}'.",
                    "message_bn": "অন্য টেন্যান্টের রোল মাইগ্রেশন করা যাবে না।",
                }
            )

        tenant = Tenant.objects.filter(tenant_id=target_tenant_id).first()
        if not tenant:
            raise ValidationError(
                {
                    "error_code": "ERR_TENANT_NOT_FOUND",
                    "message_en": f"Tenant '{target_tenant_id}' does not exist.",
                    "message_bn": "টেন্যান্ট পাওয়া যায়নি।",
                }
            )

        if is_write and tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": f"Tenant subscription is {tenant.status}. Role grant mutations are blocked.",
                    "message_bn": "টেন্যান্ট অ্যাকাউন্ট স্থগিত থাকায় রোল পরিবর্তন বন্ধ।",
                }
            )

        return {"tenant": tenant, "is_super": is_super, "is_owner": is_owner}

    @classmethod
    def reconcile_role_grants(
        cls,
        actor_user: Any,
        role_id: str,
        reason: str = RoleGrantMigrationReason.PLAN_DOWNGRADE,
        is_dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Reconciles grants for a specific custom role, versioning and pruning invalid entitlements."""
        role = CustomRoleDefinition.objects.filter(role_id=role_id).select_related("tenant").first()
        if not role:
            raise ValidationError(
                {
                    "error_code": "ERR_ROLE_NOT_FOUND",
                    "message_en": f"Custom role '{role_id}' not found.",
                    "message_bn": "কাস্টম রোল পাওয়া যায়নি।",
                }
            )

        cls._validate_actor(actor_user, role.tenant.tenant_id, is_write=not is_dry_run)

        # 1. Determine active tier entitlements
        allowed_permissions = cls.get_tier_entitled_permissions(role.tenant.plan_tier)

        current_permissions = list(role.permission_keys or [])
        retained: List[str] = []
        removed: List[str] = []
        forbidden_blocked: List[str] = []

        for p in current_permissions:
            if p in cls.FORBIDDEN_PERMISSIONS:
                forbidden_blocked.append(p)
                removed.append(p)
            elif p in allowed_permissions:
                retained.append(p)
            else:
                removed.append(p)

        prev_version = role.grant_version
        new_version = prev_version + 1

        migration_id = f"rmig-{uuid.uuid4().hex[:12]}"
        applied_at = timezone.now()

        if not is_dry_run:
            role.permission_keys = retained
            role.grant_version = new_version
            role.save(update_fields=["permission_keys", "grant_version", "updated_at"])

            RoleGrantMigrationRecord.objects.create(
                migration_id=migration_id,
                tenant=role.tenant,
                role=role,
                previous_version=prev_version,
                new_version=new_version,
                reason=reason,
                previous_permissions=current_permissions,
                retained_permissions=retained,
                removed_permissions=removed,
                forbidden_grants_blocked=forbidden_blocked,
                is_dry_run=False,
                applied_by=getattr(actor_user, "email", "SYSTEM"),
            )

        return {
            "migration_id": migration_id,
            "tenant_id": role.tenant.tenant_id,
            "role_id": role.role_id,
            "role_code": role.role_code,
            "previous_version": prev_version,
            "new_version": new_version,
            "reason": reason,
            "previous_permissions": current_permissions,
            "retained_permissions": retained,
            "removed_permissions": removed,
            "forbidden_grants_blocked": forbidden_blocked,
            "is_dry_run": is_dry_run,
            "applied_by": getattr(actor_user, "email", "SYSTEM"),
            "applied_at": applied_at.isoformat(),
        }

    @classmethod
    def list_migration_history(
        cls,
        actor_user: Any,
        tenant_id: Optional[str] = None,
        role_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists role migration audit history for a tenant or role."""
        target_tenant = tenant_id or getattr(actor_user, "tenant_id", None)
        cls._validate_actor(actor_user, target_tenant, is_write=False)

        qs = RoleGrantMigrationRecord.objects.filter(tenant_id=target_tenant)
        if role_id:
            qs = qs.filter(role_id=role_id)

        results = []
        for r in qs.order_by("-created_at")[:50]:
            results.append(
                {
                    "migration_id": r.migration_id,
                    "tenant_id": r.tenant_id,
                    "role_id": r.role_id,
                    "role_code": r.role.role_code if r.role else "",
                    "previous_version": r.previous_version,
                    "new_version": r.new_version,
                    "reason": r.reason,
                    "previous_permissions": r.previous_permissions,
                    "retained_permissions": r.retained_permissions,
                    "removed_permissions": r.removed_permissions,
                    "forbidden_grants_blocked": r.forbidden_grants_blocked,
                    "is_dry_run": r.is_dry_run,
                    "applied_by": r.applied_by,
                    "applied_at": r.created_at.isoformat(),
                }
            )
        return results
