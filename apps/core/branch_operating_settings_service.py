"""
Branch Operating Settings Service with Precedence Hierarchy & RBAC.
Architecture Decision: ADR-0116 (P116).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    BranchOperatingSettings,
    BranchStaffMembership,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.core.branch_operating_settings")


class BranchOperatingSettingsService:
    """Core domain logic for managing and resolving branch operating settings."""

    @classmethod
    def _resolve_actor(cls, actor_user: Any, branch: Branch) -> None:
        """Verifies actor authorization against tenant and branch boundaries."""
        actor_tenant = getattr(actor_user, "tenant_id", None)
        actor_role = getattr(actor_user, "role", None)
        is_super = actor_role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"] or getattr(actor_user, "is_superuser", False)

        # 1. Inactive staff check
        if not getattr(actor_user, "is_active", True) or getattr(actor_user, "is_archived", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Staff account is deactivated or archived.",
                    "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয়।",
                }
            )

        # 2. Cross-tenant check
        if not is_super and actor_tenant and actor_tenant != branch.tenant.tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant access forbidden for tenant '{branch.tenant.tenant_id}'.",
                    "message_bn": "অন্য টেন্যান্টের ব্রাঞ্চ কনফিগারেশন অ্যাক্সেস করা যাবে না।",
                }
            )

    @classmethod
    def _check_subscription(cls, tenant: Tenant) -> None:
        if tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": f"Tenant subscription is {tenant.status}. Operating settings mutation blocked.",
                    "message_bn": "টেন্যান্ট অ্যাকাউন্ট স্থগিত থাকায় সেটিংস পরিবর্তন বন্ধ।",
                }
            )

    @classmethod
    def get_or_create_settings(cls, branch: Branch) -> BranchOperatingSettings:
        """Ensures a BranchOperatingSettings entity exists with defaults."""
        settings_obj = BranchOperatingSettings.objects.filter(branch=branch).first()
        if not settings_obj:
            settings_obj = BranchOperatingSettings.objects.create(
                settings_id=f"bos-{uuid.uuid4().hex[:12]}",
                tenant=branch.tenant,
                branch=branch,
            )
        return settings_obj

    @classmethod
    def serialize_settings(cls, settings_obj: BranchOperatingSettings) -> Dict[str, Any]:
        """Formats operating settings according to branch_operating_settings.schema.json."""
        return {
            "settings_id": settings_obj.settings_id,
            "tenant_id": settings_obj.tenant.tenant_id,
            "branch_id": settings_obj.branch.branch_id,
            "branch_name": settings_obj.branch.name,
            "receipt_format": settings_obj.receipt_format,
            "operating_hours": settings_obj.operating_hours,
            "storage_zones": settings_obj.storage_zones,
            "low_stock_thresholds": settings_obj.low_stock_thresholds,
            "tax_defaults": settings_obj.tax_defaults,
            "device_policies": settings_obj.device_policies,
            "last_updated_by": settings_obj.last_updated_by or "SYSTEM_DEFAULT",
            "updated_at": settings_obj.updated_at.isoformat(),
        }

    @classmethod
    def get_branch_settings(cls, actor_user: Any, branch_id: str) -> Dict[str, Any]:
        """Retrieves operating settings with precedence resolution."""
        branch = Branch.objects.filter(branch_id=branch_id).select_related("tenant").first()
        if not branch:
            raise ValidationError({"error_code": "BRANCH_NOT_FOUND", "message_en": f"Branch '{branch_id}' not found."})

        cls._resolve_actor(actor_user, branch)
        settings_obj = cls.get_or_create_settings(branch)
        return cls.serialize_settings(settings_obj)

    @classmethod
    def update_branch_settings(cls, actor_user: Any, branch_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Mutates branch operating settings enforcing RBAC (Owner & Manager only)."""
        branch = Branch.objects.filter(branch_id=branch_id).select_related("tenant").first()
        if not branch:
            raise ValidationError({"error_code": "BRANCH_NOT_FOUND", "message_en": f"Branch '{branch_id}' not found."})

        cls._resolve_actor(actor_user, branch)
        cls._check_subscription(branch.tenant)

        actor_role = getattr(actor_user, "role", None)
        is_super = actor_role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"] or getattr(actor_user, "is_superuser", False)
        is_owner = actor_role in ["PHARMACY_OWNER", "OWNER", "TENANT_OWNER"]
        is_manager = actor_role in ["BRANCH_MANAGER", "MANAGER"]

        if not (is_super or is_owner or is_manager):
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Staff members lack permission to modify branch operating settings.",
                    "message_bn": "স্টাফ মেম্বারদের ব্রাঞ্চ অপারেটিং সেটিংস পরিবর্তনের অনুমতি নেই।",
                }
            )

        if is_manager and not (is_super or is_owner):
            mgr_branch = getattr(actor_user, "branch_id", None)
            if not mgr_branch:
                mem = BranchStaffMembership.objects.filter(staff_member__username=actor_user.email, is_active=True).first()
                if mem and mem.branch:
                    mgr_branch = mem.branch.branch_id
            if mgr_branch != branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                        "message_en": f"Branch manager cannot modify operating settings for foreign branch '{branch_id}'.",
                        "message_bn": "শাখা ব্যবস্থাপক অন্য শাখার অপারেটিং সেটিংস পরিবর্তন করতে পারবেন না।",
                    }
                )

        settings_obj = cls.get_or_create_settings(branch)

        fields = [
            "receipt_format",
            "operating_hours",
            "storage_zones",
            "low_stock_thresholds",
            "tax_defaults",
            "device_policies",
        ]
        updated_any = False
        for f in fields:
            if f in updates:
                setattr(settings_obj, f, updates[f])
                updated_any = True

        if updated_any:
            settings_obj.last_updated_by = getattr(actor_user, "email", "SYSTEM")
            settings_obj.updated_at = timezone.now()
            settings_obj.save()

        return cls.serialize_settings(settings_obj)

