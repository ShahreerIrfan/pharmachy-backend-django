"""
Active User Sessions & Instant Online Revocation Service.
Architecture Decision: ADR-0045, ADR-0112 (P112).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Enforces:
1. Multi-device visibility for Owners, Branch Managers, and Staff.
2. Immediate online revocation stopping API access instantly upon revocation.
3. Separation of duties (RBAC):
   - Pharmacy Owner & Superadmin: Tenant-wide session visibility and revocation.
   - Branch Manager: Branch-scoped session visibility and revocation.
   - Cashier / Frontline Staff: Self-only session management.
4. Multi-tenant isolation (ERR_CROSS_TENANT_VIOLATION) and subscription guards.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import BranchStaffMembership, Tenant
from apps.identity.models import PharmacyUser, UserSession

logger = logging.getLogger("pharmacy.identity.sessions")


class ActiveSessionService:
    """Authoritative service for user device sessions, RBAC scoping, and instant revocation."""

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> None:
        """Blocks mutations if tenant subscription is suspended or expired."""
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
            tenant_status = str(getattr(tenant, "status", getattr(tenant, "lifecycle_status", ""))).upper()
            if tenant_status in ["SUSPENDED", "ARCHIVED", "TERMINATED"]:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                        "message_en": "Tenant subscription is suspended or expired. Session mutation blocked.",
                        "message_bn": "টেন্যান্ট সাবস্ক্রিপশন স্থগিত রয়েছে। সেশন পরিবর্তন সম্পন্ন করা সম্ভব নয়।",
                    }
                )
        except Tenant.DoesNotExist:
            pass

    @classmethod
    def _resolve_actor_context(cls, actor_user: Any) -> Dict[str, Any]:
        role = str(getattr(actor_user, "role", "UNKNOWN")).upper()
        user_tid = getattr(actor_user, "tenant_id", None)
        if hasattr(user_tid, "tenant_id"):
            user_tid = user_tid.tenant_id
        user_tid = str(user_tid) if user_tid else ""

        user_bid = getattr(actor_user, "branch_id", None)
        if hasattr(user_bid, "branch_id"):
            user_bid = user_bid.branch_id
        user_bid = str(user_bid) if user_bid else ""

        user_id = str(getattr(actor_user, "user_id", ""))
        user_email = getattr(actor_user, "email", "operator")

        return {
            "role": role,
            "tenant_id": user_tid,
            "branch_id": user_bid,
            "user_id": user_id,
            "email": user_email,
            "is_superadmin": role in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"],
            "is_owner": role in ["PHARMACY_OWNER", "TENANT_OWNER"],
            "is_manager": role in ["BRANCH_MANAGER", "PHARMACY_MANAGER"],
        }

    @classmethod
    def serialize_session(cls, session: UserSession, current_session_id: Optional[str] = None) -> Dict[str, Any]:
        """Formats a UserSession according to active_sessions.schema.json."""
        is_cur = False
        if current_session_id and str(session.session_id) == str(current_session_id):
            is_cur = True

        return {
            "session_id": str(session.session_id),
            "user_id": str(session.user.user_id),
            "user_email": session.user.email,
            "user_full_name": session.user.full_name or "",
            "user_role": str(session.user.role),
            "tenant_id": session.tenant_id,
            "branch_id": session.user.branch_id,
            "device_id": session.device_id,
            "device_name": session.device_name or "",
            "client_platform": session.client_platform,
            "ip_address": session.ip_address,
            "is_active": session.is_active,
            "is_current": is_cur,
            "revocation_reason": session.revocation_reason or None,
            "revoked_at": session.revoked_at.isoformat() if session.revoked_at else None,
            "last_seen_at": session.last_seen_at.isoformat() if session.last_seen_at else None,
            "created_at": session.created_at.isoformat() if session.created_at else None,
        }

    @classmethod
    def list_sessions(
        cls,
        actor_user: Any,
        tenant_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        target_user_id: Optional[str] = None,
        is_active: Optional[bool] = None,
        current_session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists user sessions filtered according to actor permissions:
        - Owner/Superadmin: Tenant-wide sessions with optional branch/user filters.
        - Branch Manager: Sessions strictly within their assigned branch.
        - Cashier/Staff: Self-only sessions.
        """
        ctx = cls._resolve_actor_context(actor_user)

        effective_tenant = tenant_id or ctx["tenant_id"]
        if not ctx["is_superadmin"] and ctx["tenant_id"] and effective_tenant != ctx["tenant_id"]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant access forbidden. User cannot query tenant '{effective_tenant}'.",
                    "message_bn": "অন্য টেন্যান্টের সেশন দেখার অনুমতি নেই।",
                }
            )

        qs = UserSession.objects.filter(tenant_id=effective_tenant).select_related("user")

        if is_active is not None:
            qs = qs.filter(is_active=is_active)

        if ctx["is_superadmin"] or ctx["is_owner"]:
            # Full tenant visibility
            if branch_id:
                qs = qs.filter(user__branch_id=branch_id)
            if target_user_id:
                qs = qs.filter(user__user_id=target_user_id)
        elif ctx["is_manager"]:
            # Branch manager: strictly assigned branch
            manager_bid = ctx["branch_id"]
            if branch_id and branch_id != manager_bid:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                        "message_en": f"Branch Manager cannot access sessions outside assigned branch '{manager_bid}'.",
                        "message_bn": "শাখা ব্যবস্থাপক অন্য শাখার সেশন দেখতে পারেন না।",
                    }
                )
            qs = qs.filter(user__branch_id=manager_bid)
            if target_user_id:
                qs = qs.filter(user__user_id=target_user_id)
        else:
            # Frontline Cashier / Staff: strictly self-only
            if target_user_id and target_user_id != ctx["user_id"]:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Staff members can only view their own active sessions.",
                        "message_bn": "স্টাফ সদস্যরা শুধুমাত্র নিজেদের সক্রিয় সেশন দেখতে পারবেন।",
                    }
                )
            qs = qs.filter(user_id=ctx["user_id"])

        qs = qs.order_by("-is_active", "-last_seen_at")
        return [cls.serialize_session(s, current_session_id=current_session_id) for s in qs]

    @classmethod
    def revoke_session(
        cls,
        actor_user: Any,
        session_id: str,
        reason: str = "MANUAL_REVOCATION",
    ) -> Dict[str, Any]:
        """
        Revokes a single active session.
        Immediately stops API operations online by setting is_active=False.
        """
        ctx = cls._resolve_actor_context(actor_user)

        try:
            val_uuid = uuid.UUID(str(session_id))
        except (ValueError, AttributeError):
            raise ValidationError(
                {"error_code": "INVALID_SESSION_ID", "message": f"Session ID '{session_id}' is not a valid UUID."}
            )

        session = UserSession.objects.filter(session_id=val_uuid).select_related("user").first()
        if not session:
            raise ValidationError(
                {"error_code": "SESSION_NOT_FOUND", "message": f"Session '{session_id}' not found."}
            )

        cls._assert_tenant_active(session.tenant_id)

        # Cross-tenant check
        if not ctx["is_superadmin"] and ctx["tenant_id"] and session.tenant_id != ctx["tenant_id"]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant access forbidden. Cannot revoke session in tenant '{session.tenant_id}'.",
                    "message_bn": "অন্য টেন্যান্টের সেশন বাতিল করা নিষিদ্ধ।",
                }
            )

        # Authority checks
        if not ctx["is_superadmin"] and not ctx["is_owner"]:
            if ctx["is_manager"]:
                # Check if session user is in manager's branch
                target_bid = str(session.user.branch_id or "")
                if target_bid != ctx["branch_id"]:
                    # Check branch staff membership
                    in_branch = BranchStaffMembership.objects.filter(
                        tenant_id=session.tenant_id,
                        branch_id=ctx["branch_id"],
                        staff_member__username=session.user.email,
                        is_active=True,
                    ).exists()
                    if not in_branch:
                        raise PermissionDenied(
                            {
                                "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                                "message_en": f"Branch Manager cannot revoke sessions in branch '{target_bid}'.",
                                "message_bn": "শাখা ব্যবস্থাপক অন্য শাখার কর্মচারীর সেশন বাতিল করতে পারেন না।",
                            }
                        )
            else:
                # Cashier can only revoke their OWN session
                if str(session.user_id) != ctx["user_id"]:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_PERMISSION_DENIED",
                            "message_en": "You are not authorized to revoke another user's session.",
                            "message_bn": "অন্য কর্মচারীর সেশন বাতিল করার অনুমতি নেই।",
                        }
                    )

        with transaction.atomic():
            clean_reason = reason.strip() or "MANUAL_REVOCATION"
            session.revoke(reason=clean_reason)

        logger.info(
            "Session %s [User: %s, Device: %s] REVOKED by %s (Reason: %s)",
            session_id,
            session.user.email,
            session.device_id,
            ctx["email"],
            clean_reason,
        )

        return cls.serialize_session(session)

    @classmethod
    def revoke_all_sessions(
        cls,
        actor_user: Any,
        target_user_id: Optional[str] = None,
        reason: str = "LOGOUT_ALL",
    ) -> Dict[str, Any]:
        """Bulk revokes all active device sessions for a user."""
        ctx = cls._resolve_actor_context(actor_user)
        effective_user_id = target_user_id or ctx["user_id"]

        target_user = PharmacyUser.objects.filter(user_id=effective_user_id).first()
        if not target_user:
            raise ValidationError(
                {"error_code": "USER_NOT_FOUND", "message": f"Target user '{effective_user_id}' not found."}
            )

        cls._assert_tenant_active(target_user.tenant_id)

        # Cross-tenant check
        if not ctx["is_superadmin"] and ctx["tenant_id"] and target_user.tenant_id != ctx["tenant_id"]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": "Cross-tenant bulk revocation denied.",
                    "message_bn": "অন্য টেন্যান্টের সেশন বাতিল করা নিষিদ্ধ।",
                }
            )

        # Authority check
        if not ctx["is_superadmin"] and not ctx["is_owner"]:
            if ctx["is_manager"]:
                if str(target_user.branch_id or "") != ctx["branch_id"]:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                            "message_en": "Branch Manager cannot revoke sessions for staff outside their assigned branch.",
                            "message_bn": "অন্য শাখার কর্মচারীর সেশন বাতিল করার অনুমতি নেই।",
                        }
                    )
            else:
                if effective_user_id != ctx["user_id"]:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_PERMISSION_DENIED",
                            "message_en": "Staff members cannot bulk-revoke other users' sessions.",
                            "message_bn": "অন্য কর্মচারীর সেশন বাতিল করার অনুমতি নেই।",
                        }
                    )

        with transaction.atomic():
            active_sessions = UserSession.objects.filter(
                user=target_user,
                is_active=True,
            )
            count = active_sessions.count()
            active_sessions.update(
                is_active=False,
                revocation_reason=reason.strip() or "LOGOUT_ALL",
                revoked_at=timezone.now(),
            )

        logger.info(
            "Bulk revoked %d session(s) for user %s by %s",
            count,
            target_user.email,
            ctx["email"],
        )

        return {
            "status": "SUCCESS",
            "revoked_count": count,
            "target_user_id": str(target_user.user_id),
            "target_user_email": target_user.email,
            "message_en": f"Successfully revoked {count} active device session(s).",
            "message_bn": f"সফলভাবে {count}টি সক্রিয় ডিভাইস সেশন বাতিল করা হয়েছে।",
        }

