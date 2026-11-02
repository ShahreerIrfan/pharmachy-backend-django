"""
Staff Audit Timeline Service.
Architecture Decision: ADR-0118 (P118).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BranchStaffMembership,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.audit_logging.timeline")


class StaffAuditTimelineService:
    """Aggregates and formats staff audit timelines across roles, denials, refunds, approvals, and offline events."""

    ALLOWED_VIEWER_ROLES = {
        "PHARMACY_OWNER",
        "OWNER",
        "TENANT_OWNER",
        "BRANCH_MANAGER",
        "MANAGER",
        "AUDITOR",
        "COMPLIANCE_OFFICER",
        "SUPERADMIN",
        "SYSTEM_SUPERADMIN",
    }

    VALID_EVENT_CATEGORIES = {
        "ROLE_CHANGE",
        "PERMISSION_DENIAL",
        "DATA_EXPORT",
        "REFUND",
        "APPROVAL",
        "OFFLINE_EVENT",
        "SYSTEM_AUDIT",
    }

    @classmethod
    def record_staff_audit_event(
        cls,
        tenant_id: str,
        event_category: str,
        action: str,
        actor_id: str,
        actor_role: str,
        actor_email: str = "",
        branch_id: Optional[str] = None,
        device_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        target_staff_id: Optional[str] = None,
        target_staff_name: Optional[str] = None,
        status: str = "SUCCESS",
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        summary_en: Optional[str] = None,
        summary_bn: Optional[str] = None,
    ) -> AuditLogEntry:
        """Records a new audit entry formatted for staff timeline visibility."""
        meta = metadata.copy() if metadata else {}
        meta["event_category"] = event_category
        meta["target_staff_id"] = target_staff_id
        meta["target_staff_name"] = target_staff_name
        meta["summary_en"] = summary_en or f"{actor_role} performed {action}"
        meta["summary_bn"] = summary_bn or f"{actor_role} দ্বারা {action} সম্পন্ন হয়েছে"

        status_enum = AuditActionStatus.SUCCESS
        if status.upper() == "DENIED":
            status_enum = AuditActionStatus.DENIED
        elif status.upper() == "ERROR":
            status_enum = AuditActionStatus.ERROR

        entry = AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=actor_id,
            actor_email=actor_email,
            actor_role=actor_role,
            device_id=device_id,
            request_id=f"req-tl-{uuid.uuid4().hex[:8]}",
            action=action,
            resource_type=f"STAFF_TIMELINE_{event_category}",
            resource_id=target_staff_id or actor_id,
            status=status_enum,
            ip_address=ip_address,
            before_state=before_state or {},
            after_state=after_state or {},
            metadata_json=meta,
            is_immutable=True,
        )
        return entry

    @classmethod
    def get_staff_timeline(
        cls,
        actor_user: Any,
        target_tenant_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        staff_id: Optional[str] = None,
        event_category: Optional[str] = None,
        device_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Queries and returns timeline events respecting RBAC, branch, and tenant boundaries."""
        actor_role = str(getattr(actor_user, "role", "")).upper()
        actor_tenant = getattr(actor_user, "tenant_id", None)
        is_super = getattr(actor_user, "is_superuser", False) or actor_role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"]

        # 1. Staff Lifecycle & Authorization Check
        if not getattr(actor_user, "is_active", True) or getattr(actor_user, "is_archived", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Staff user is inactive or deactivated.",
                    "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয়।",
                }
            )

        if not is_super and actor_role not in cls.ALLOWED_VIEWER_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": f"Role '{actor_role}' is not authorized to inspect staff audit timelines.",
                    "message_bn": "স্টাফ অডিট টাইমলাইন দেখার অনুমতি এই রোলের নেই।",
                }
            )

        effective_tenant_id = target_tenant_id or actor_tenant
        if not effective_tenant_id:
            raise ValidationError(
                {
                    "error_code": "ERR_TENANT_ID_REQUIRED",
                    "message_en": "tenant_id parameter is required.",
                    "message_bn": "টেন্যান্ট আইডি আবশ্যক।",
                }
            )

        # 2. Cross-tenant check
        if not is_super and actor_tenant and actor_tenant != effective_tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant access forbidden for tenant '{effective_tenant_id}'.",
                    "message_bn": "অন্য টেন্যান্টের অডিট টাইমলাইন দেখতে পারবেন না।",
                }
            )

        tenant = Tenant.objects.filter(tenant_id=effective_tenant_id).first()
        if not tenant:
            raise ValidationError(
                {
                    "error_code": "ERR_TENANT_NOT_FOUND",
                    "message_en": f"Tenant '{effective_tenant_id}' does not exist.",
                    "message_bn": "টেন্যান্ট পাওয়া যায়নি।",
                }
            )

        # 3. Subscription Status Check
        if tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_READ_BLOCKED",
                    "message_en": f"Tenant subscription is {tenant.status}. Timeline read access is blocked.",
                    "message_bn": "টেন্যান্ট অ্যাকাউন্ট স্থগিত থাকায় অডিট টাইমলাইন দেখা যাবে না।",
                }
            )

        # 4. Branch Manager Scoping
        is_owner = actor_role in ["PHARMACY_OWNER", "OWNER", "TENANT_OWNER"]
        is_manager = actor_role in ["BRANCH_MANAGER", "MANAGER"]
        mgr_branch: Optional[str] = None

        if is_manager and not is_super and not is_owner:
            mgr_branch = getattr(actor_user, "branch_id", None)
            if not mgr_branch:
                mem = BranchStaffMembership.objects.filter(staff_member__username=actor_user.email, is_active=True).first()
                if mem and mem.branch:
                    mgr_branch = mem.branch.branch_id

            if branch_id and branch_id != mgr_branch:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                        "message_en": f"Branch manager cannot inspect timeline for foreign branch '{branch_id}'.",
                        "message_bn": "শাখা ব্যবস্থাপক অন্য শাখার অডিট টাইমলাইন দেখতে পারেন না।",
                    }
                )
            effective_branch_id = mgr_branch
        else:
            effective_branch_id = branch_id

        # 5. Query Audit Entries
        qs = AuditLogEntry.objects.filter(tenant_id=effective_tenant_id)
        if effective_branch_id:
            qs = qs.filter(branch_id=effective_branch_id)
        if device_id:
            qs = qs.filter(device_id=device_id)

        if event_category:
            if event_category not in cls.VALID_EVENT_CATEGORIES:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_EVENT_CATEGORY",
                        "message_en": f"Invalid event category '{event_category}'. Valid choices: {sorted(cls.VALID_EVENT_CATEGORIES)}",
                        "message_bn": "অবৈধ অডিট ইভেন্ট ক্যাটাগরি।",
                    }
                )
            qs = qs.filter(metadata_json__event_category=event_category)

        if staff_id:
            # Matches actor user_id or target_staff_id
            qs = qs.filter(
                models_Q_helper(staff_id)
            )

        if start_date:
            dt_start = parse_datetime(start_date)
            if dt_start:
                qs = qs.filter(timestamp__gte=dt_start)
        if end_date:
            dt_end = parse_datetime(end_date)
            if dt_end:
                qs = qs.filter(timestamp__lte=dt_end)

        qs = qs.order_by("-timestamp")[:limit]

        results = [cls._serialize_entry(e) for e in qs]
        return {
            "count": len(results),
            "results": results,
        }

    @classmethod
    def _serialize_entry(cls, entry: AuditLogEntry) -> Dict[str, Any]:
        meta = entry.metadata_json or {}
        category = meta.get("event_category") or cls._infer_category(entry.action, entry.resource_type)
        summary_en = meta.get("summary_en") or f"{entry.actor_role or 'Staff'} performed {entry.action}"
        summary_bn = meta.get("summary_bn") or f"{entry.actor_role or 'স্টাফ'} দ্বারা {entry.action} সম্পন্ন হয়েছে"
        target_staff_id = meta.get("target_staff_id")
        target_staff_name = meta.get("target_staff_name")

        return {
            "timeline_id": str(entry.entry_id),
            "timestamp": entry.timestamp.isoformat(),
            "tenant_id": entry.tenant_id,
            "branch_id": entry.branch_id,
            "event_category": category,
            "action": entry.action,
            "status": str(entry.status),
            "actor_id": entry.user_id or "system",
            "actor_email": entry.actor_email or "",
            "actor_role": entry.actor_role or "STAFF",
            "device_id": entry.device_id,
            "ip_address": entry.ip_address,
            "target_staff_id": target_staff_id,
            "target_staff_name": target_staff_name,
            "summary_en": summary_en,
            "summary_bn": summary_bn,
            "before_state": entry.before_state or {},
            "after_state": entry.after_state or {},
            "metadata": meta,
        }

    @classmethod
    def _infer_category(cls, action: str, resource_type: str) -> str:
        act = action.upper()
        res = resource_type.upper()
        if "ROLE" in act or "ROLE" in res or "PERMISSION" in act:
            return "ROLE_CHANGE"
        if "DENIED" in act or "DENIED" in res or "FORBIDDEN" in act:
            return "PERMISSION_DENIAL"
        if "EXPORT" in act or "EXPORT" in res:
            return "DATA_EXPORT"
        if "REFUND" in act or "REFUND" in res:
            return "REFUND"
        if "APPROV" in act or "APPROV" in res:
            return "APPROVAL"
        if "OFFLINE" in act or "SYNC" in act or "OFFLINE" in res:
            return "OFFLINE_EVENT"
        return "SYSTEM_AUDIT"


def models_Q_helper(staff_id: str):
    from django.db.models import Q
    return Q(user_id=staff_id) | Q(metadata_json__target_staff_id=staff_id)
