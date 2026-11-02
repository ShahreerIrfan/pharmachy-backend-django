"""
Cashier Register Assignment & Zero Concurrent Drawer Collision Service.
Architecture Decision: ADR-0117 (P117).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    BranchStaffMembership,
    CashierRegisterAssignment,
    RegisterAssignmentStatus,
    StaffLifecycleStatus,
    StaffMember,
    StaffShiftSchedule,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.core.cashier_register")


class CashierRegisterAssignmentService:
    """Core domain logic for managing cashier register bindings and drawer collision prevention."""

    @classmethod
    def _resolve_actor_context(cls, actor_user: Any, target_tenant_id: str) -> Dict[str, Any]:
        actor_tenant = getattr(actor_user, "tenant_id", None)
        actor_role = getattr(actor_user, "role", None)
        is_super = actor_role in ["SUPERADMIN", "SYSTEM_SUPERADMIN"] or getattr(actor_user, "is_superuser", False)
        is_owner = actor_role in ["PHARMACY_OWNER", "OWNER", "TENANT_OWNER"]
        is_manager = actor_role in ["BRANCH_MANAGER", "MANAGER"]

        if not getattr(actor_user, "is_active", True) or getattr(actor_user, "is_archived", False):
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Staff account is deactivated or archived.",
                    "message_bn": "স্টাফ অ্যাকাউন্টটি নিষ্ক্রিয়।",
                }
            )

        if not is_super and actor_tenant and actor_tenant != target_tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Cross-tenant access forbidden for tenant '{target_tenant_id}'.",
                    "message_bn": "অন্য টেন্যান্টের রেজিস্টার অ্যাসাইনমেন্টে প্রবেশাধিকার নেই।",
                }
            )

        return {
            "actor_tenant": actor_tenant,
            "actor_role": actor_role,
            "is_super": is_super,
            "is_owner": is_owner,
            "is_manager": is_manager,
        }

    @classmethod
    def serialize_assignment(cls, obj: CashierRegisterAssignment) -> Dict[str, Any]:
        """Serializes assignment record conforming to cashier_register_assignment.schema.json."""
        return {
            "assignment_id": obj.assignment_id,
            "tenant_id": obj.tenant.tenant_id,
            "branch_id": obj.branch.branch_id,
            "branch_name": obj.branch.name,
            "register_id": obj.register_id,
            "cashier_id": obj.cashier.staff_id,
            "cashier_name": obj.cashier.full_name,
            "cashier_role": str(obj.cashier.role),
            "shift_schedule_id": obj.shift_schedule.schedule_id if obj.shift_schedule else None,
            "opening_float_amount": f"{obj.opening_float_amount:.2f}",
            "closing_float_amount": f"{obj.closing_float_amount:.2f}" if obj.closing_float_amount is not None else None,
            "status": obj.status,
            "notes": obj.notes or "",
            "assigned_at": obj.assigned_at.isoformat(),
            "closed_at": obj.closed_at.isoformat() if obj.closed_at else None,
        }

    @classmethod
    def list_assignments(
        cls,
        actor_user: Any,
        tenant_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        register_id: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists cashier register assignments with RBAC scoping."""
        effective_tenant = tenant_id or getattr(actor_user, "tenant_id", None)
        ctx = cls._resolve_actor_context(actor_user, effective_tenant)

        tenant = Tenant.objects.filter(tenant_id=effective_tenant).first()
        if tenant and tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": f"Tenant subscription is {tenant.status}. Access blocked.",
                    "message_bn": "টেন্যান্ট অ্যাকাউন্ট স্থগিত থাকায় ডেটা অ্যাক্সেস বন্ধ।",
                }
            )

        qs = CashierRegisterAssignment.objects.filter(tenant=tenant).select_related("tenant", "branch", "cashier", "shift_schedule")

        if ctx["is_super"] or ctx["is_owner"]:
            if branch_id:
                qs = qs.filter(branch__branch_id=branch_id)
        elif ctx["is_manager"]:
            mgr_branch = getattr(actor_user, "branch_id", None)
            if not mgr_branch:
                mem = BranchStaffMembership.objects.filter(staff_member__username=actor_user.email, is_active=True).first()
                if mem and mem.branch:
                    mgr_branch = mem.branch.branch_id
            if branch_id and branch_id != mgr_branch:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                        "message_en": f"Branch manager cannot inspect assignments for foreign branch '{branch_id}'.",
                        "message_bn": "শাখা ব্যবস্থাপক অন্য শাখার রেজিস্টার দেখতে পারেন না।",
                    }
                )
            qs = qs.filter(branch__branch_id=mgr_branch)
        else:
            # Cashier: strictly self-only
            actor_staff = StaffMember.objects.filter(tenant=tenant, username=actor_user.email).first()
            if actor_staff:
                qs = qs.filter(cashier=actor_staff)
            else:
                qs = qs.none()

        if register_id:
            qs = qs.filter(register_id=register_id)
        if status_filter:
            qs = qs.filter(status=status_filter)

        return [cls.serialize_assignment(item) for item in qs]

    @classmethod
    def assign_register(
        cls,
        actor_user: Any,
        branch_id: str,
        register_id: str,
        cashier_id: str,
        opening_float_amount: Decimal = Decimal("0.00"),
        shift_schedule_id: Optional[str] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Binds a cashier to a register terminal, enforcing zero concurrent drawer collision."""
        if not branch_id or not register_id or not cashier_id:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_REGISTER_INPUT",
                    "message_en": "Missing branch_id, register_id, or cashier_id.",
                    "message_bn": "ব্রাঞ্চ আইডি, রেজিস্টার আইডি এবং ক্যাশিয়ার আইডি আবশ্যক।",
                }
            )

        branch = Branch.objects.filter(branch_id=branch_id).select_related("tenant").first()
        if not branch:
            raise ValidationError({"error_code": "BRANCH_NOT_FOUND", "message_en": f"Branch '{branch_id}' not found."})

        ctx = cls._resolve_actor_context(actor_user, branch.tenant.tenant_id)

        # 1. Tenant Subscription Check
        if branch.tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.ARCHIVED]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
                    "message_en": f"Tenant subscription is {branch.tenant.status}. Drawer assignment blocked.",
                    "message_bn": "টেন্যান্ট অ্যাকাউন্ট স্থগিত থাকায় রেজিস্টার অ্যাসাইনমেন্ট বন্ধ।",
                }
            )

        # 2. Staff lookup
        cashier = StaffMember.objects.filter(tenant=branch.tenant, staff_id=cashier_id).first()
        if not cashier:
            raise ValidationError({"error_code": "STAFF_NOT_FOUND", "message_en": f"Cashier '{cashier_id}' not found."})

        if not cashier.is_active or cashier.lifecycle_status != StaffLifecycleStatus.ACTIVE:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Cashier staff account is inactive or terminated.",
                    "message_bn": "ক্যাশিয়ার অ্যাকাউন্টটি নিষ্ক্রিয়।",
                }
            )

        # 3. RBAC Check on who is performing the assignment
        if not (ctx["is_super"] or ctx["is_owner"]):
            if ctx["is_manager"]:
                mgr_branch = getattr(actor_user, "branch_id", None)
                if not mgr_branch:
                    mem = BranchStaffMembership.objects.filter(staff_member__username=actor_user.email, is_active=True).first()
                    if mem and mem.branch:
                        mgr_branch = mem.branch.branch_id
                if mgr_branch != branch_id:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                            "message_en": "Branch managers cannot assign registers in other branches.",
                            "message_bn": "শাখা ব্যবস্থাপক অন্য শাখার রেজিস্টার বরাদ্দ করতে পারেন না।",
                        }
                    )
            else:
                # Cashier can only assign themselves to an open register
                actor_staff = StaffMember.objects.filter(tenant=branch.tenant, username=actor_user.email).first()
                if not actor_staff or actor_staff.staff_id != cashier_id:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_PERMISSION_DENIED",
                            "message_en": "Cashiers cannot assign registers to other staff members.",
                            "message_bn": "ক্যাশিয়ার অন্য স্টাফের জন্য রেজিস্টার বরাদ্দ করতে পারেন না।",
                        }
                    )

        # 4. Zero Concurrent Drawer Invariant: Is this register already assigned?
        existing_reg = CashierRegisterAssignment.objects.filter(
            branch=branch,
            register_id=register_id,
            status=RegisterAssignmentStatus.ACTIVE,
        ).first()
        if existing_reg:
            raise PermissionDenied(
                {
                    "error_code": "ERR_REGISTER_ALREADY_ASSIGNED",
                    "message_en": f"Register '{register_id}' is already actively assigned to {existing_reg.cashier.full_name}.",
                    "message_bn": f"রেজিস্টার '{register_id}' ইতিমধ্যেই সক্রিয়ভাবে বরাদ্দকৃত।",
                }
            )

        # 5. Zero Concurrent Cashier Invariant: Is this cashier already assigned to another register?
        existing_cashier = CashierRegisterAssignment.objects.filter(
            cashier=cashier,
            status=RegisterAssignmentStatus.ACTIVE,
        ).first()
        if existing_cashier:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CASHIER_ALREADY_ASSIGNED",
                    "message_en": f"Cashier '{cashier.full_name}' is already actively operating register '{existing_cashier.register_id}'.",
                    "message_bn": "ক্যাশিয়ার ইতিমধ্যেই অন্য একটি রেজিস্টারে সক্রিয় আছেন।",
                }
            )

        # 6. Shift schedule linkage
        shift_obj = None
        if shift_schedule_id:
            shift_obj = StaffShiftSchedule.objects.filter(schedule_id=shift_schedule_id).first()

        assignment = CashierRegisterAssignment.objects.create(
            assignment_id=f"cra-{uuid.uuid4().hex[:12]}",
            tenant=branch.tenant,
            branch=branch,
            shift_schedule=shift_obj,
            cashier=cashier,
            register_id=register_id,
            opening_float_amount=opening_float_amount,
            status=RegisterAssignmentStatus.ACTIVE,
            notes=notes,
            assigned_at=timezone.now(),
        )

        return cls.serialize_assignment(assignment)

    @classmethod
    def close_assignment(
        cls,
        actor_user: Any,
        assignment_id: str,
        closing_float_amount: Decimal,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Closes an active drawer assignment and records the closing float."""
        assignment = CashierRegisterAssignment.objects.filter(assignment_id=assignment_id).select_related("tenant", "branch", "cashier").first()
        if not assignment:
            raise ValidationError({"error_code": "ASSIGNMENT_NOT_FOUND", "message_en": "Register assignment not found."})

        ctx = cls._resolve_actor_context(actor_user, assignment.tenant.tenant_id)

        # RBAC Check
        if not (ctx["is_super"] or ctx["is_owner"]):
            if ctx["is_manager"]:
                mgr_branch = getattr(actor_user, "branch_id", None)
                if not mgr_branch:
                    mem = BranchStaffMembership.objects.filter(staff_member__username=actor_user.email, is_active=True).first()
                    if mem and mem.branch:
                        mgr_branch = mem.branch.branch_id
                if mgr_branch != assignment.branch.branch_id:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                            "message_en": "Cannot close assignment in foreign branch.",
                            "message_bn": "অন্য শাখার রেজিস্টার বন্ধ করার অনুমতি নেই।",
                        }
                    )
            else:
                actor_staff = StaffMember.objects.filter(tenant=assignment.tenant, username=actor_user.email).first()
                if not actor_staff or actor_staff.staff_id != assignment.cashier.staff_id:
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_PERMISSION_DENIED",
                            "message_en": "Cannot close another cashier's drawer assignment.",
                            "message_bn": "অন্য ক্যাশিয়ারের ড্রয়ার বন্ধ করার অনুমতি নেই।",
                        }
                    )

        assignment.closing_float_amount = closing_float_amount
        assignment.status = RegisterAssignmentStatus.CLOSED
        assignment.closed_at = timezone.now()
        if notes:
            assignment.notes = f"{assignment.notes} | {notes}".strip(" |")
        assignment.save()

        return cls.serialize_assignment(assignment)
