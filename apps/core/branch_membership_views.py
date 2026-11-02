"""
Branch-Specific Staff Membership REST API ViewSets (P105 / ADR-0105).
Provides endpoints for assigning, querying, updating, and revoking branch-specific roles
with strict action-specific RBAC, subscription checks, and cross-tenant guardrails.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from django.db import models
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.branch_membership_service import BranchMembershipService
from apps.core.granular_permission_service import GranularActionPermissionService
from apps.core.models import BranchStaffMembership, StaffMember
from apps.core.postgres_service import CrossTenantOperationError
from apps.identity.models import PharmacyUser

logger = logging.getLogger("pharmacy.branch_membership.views")


class BranchStaffMembershipViewSet(viewsets.ViewSet):
    """
    Manages branch-specific staff memberships and role scoping across multiple branches.
    - Owner/Superadmin: full tenant-wide management authority.
    - Branch Manager: view and assign operational staff (Cashier/Pharmacist/Clerk) in own branch only.
    - Frontline staff: access denied.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant_and_role(self, request) -> Tuple[str, str, Optional[str]]:
        user = request.user
        role = str(getattr(user, "role", "")).upper()

        if not user or not user.is_authenticated:
            raise PermissionDenied(
                {
                    "error_code": "ERR_AUTHENTICATION_REQUIRED",
                    "message_en": "Authentication credentials were not provided.",
                    "message_bn": "প্রবেশাধিকারের জন্য অথেনটিকেশন প্রয়োজন।",
                }
            )

        tenant_id = (
            getattr(user, "tenant_id", None)
            or getattr(user, "tenant", None)
            or getattr(request, "tenant_id", None)
        )
        if hasattr(tenant_id, "tenant_id"):
            tenant_id = tenant_id.tenant_id
        tenant_id = str(tenant_id) if tenant_id else None

        req_tenant = request.query_params.get("tenant_id") or (
            request.data.get("tenant_id") if isinstance(request.data, dict) else None
        )
        if req_tenant and tenant_id and req_tenant != tenant_id and role not in [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
        ]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Access denied: cannot access foreign tenant '{req_tenant}'.",
                    "message_bn": f"অননুমোদিত টেন্যান্ট অ্যাক্সেস: '{req_tenant}' দেখার অনুমতি নেই।",
                }
            )

        if not tenant_id and req_tenant:
            tenant_id = req_tenant

        if not tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_CONTEXT_MISSING",
                    "message_en": "Tenant context missing.",
                    "message_bn": "টেন্যান্ট কনটেক্সট অনুপস্থিত।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None

    def _assert_can_view(self, role: str) -> None:
        allowed = [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
            "PHARMACY_OWNER",
            "TENANT_OWNER",
            "BRANCH_MANAGER",
        ]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Role '{role}' is not authorized to view branch memberships.",
                    "message_bn": f"'{role}' রোলের ব্রাঞ্চ মেম্বারশিপ দেখার অনুমতি নেই।",
                }
            )

    def _assert_can_manage(self, role: str) -> None:
        allowed = [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
            "PHARMACY_OWNER",
            "TENANT_OWNER",
            "BRANCH_MANAGER",
        ]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Role '{role}' is not authorized to manage branch memberships.",
                    "message_bn": f"'{role}' রোলের ব্রাঞ্চ মেম্বারশিপ পরিচালনা করার অনুমতি নেই।",
                }
            )

    def list(self, request) -> Response:
        """GET /api/v2/staff/branch-memberships/"""
        tenant_id, role, caller_branch = self._resolve_tenant_and_role(request)
        self._assert_can_view(role)

        req_branch = request.query_params.get("branch_id")
        if role == "BRANCH_MANAGER":
            if req_branch and caller_branch and req_branch != caller_branch:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_ACCESS_DENIED",
                        "message_en": "Branch Manager can only view assignments for their own branch.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার কেবল নিজের ব্রাঞ্চের মেম্বারশিপ দেখতে পারেন।",
                    }
                )
            req_branch = caller_branch

        staff_member_id = request.query_params.get("staff_member_id")
        is_active_raw = request.query_params.get("is_active")
        is_active = is_active_raw.lower() == "true" if is_active_raw is not None else None

        results = BranchMembershipService.list_branch_memberships(
            tenant_id=tenant_id,
            staff_member_id=staff_member_id,
            branch_id=req_branch,
            is_active=is_active,
        )
        return Response(results, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """POST /api/v2/staff/branch-memberships/"""
        tenant_id, role, caller_branch = self._resolve_tenant_and_role(request)
        self._assert_can_manage(role)

        data = request.data if isinstance(request.data, dict) else {}
        branch_id = data.get("branch_id")
        staff_member_id = data.get("staff_member_id")
        assign_role = str(data.get("role", "")).strip().upper()

        # Branch Manager guardrails
        if role == "BRANCH_MANAGER":
            if caller_branch and str(branch_id) != str(caller_branch):
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_ACCESS_DENIED",
                        "message_en": "Branch Manager cannot assign staff to other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য কোনো ব্রাঞ্চে কর্মী নিয়োগ করতে পারবেন না।",
                    }
                )
            if assign_role in ["PHARMACY_OWNER", "BRANCH_MANAGER", "SUPERADMIN"]:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot assign managerial or ownership roles.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য কাউকে ম্যানেজার বা ওনার রোল দিতে পারেন না।",
                    }
                )

        custom_role_code = data.get("custom_role_code", "")
        custom_role_title = data.get("custom_role_title", "")
        is_pri_raw = data.get("is_primary", False)
        if isinstance(is_pri_raw, str):
            is_primary = is_pri_raw.strip().lower() in ["true", "1", "yes"]
        else:
            is_primary = bool(is_pri_raw)

        assigned_by = getattr(request.user, "username", getattr(request.user, "email", "owner"))

        try:
            bsm = BranchMembershipService.assign_branch_role(
                tenant_id=tenant_id,
                staff_member_id=staff_member_id,
                branch_id=branch_id,
                role=assign_role,
                custom_role_code=custom_role_code,
                custom_role_title=custom_role_title,
                is_primary=is_primary,
                assigned_by_username=assigned_by,
            )
        except CrossTenantOperationError as err:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": str(err),
                    "message_bn": "ভুল টেন্যান্ট বা ব্রাঞ্চ রেফারেন্স।",
                }
            )

        return Response(bsm, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/staff/branch-memberships/{id}/"""
        tenant_id, role, caller_branch = self._resolve_tenant_and_role(request)
        self._assert_can_view(role)

        try:
            bsm = BranchStaffMembership.objects.select_related("staff_member", "branch").get(
                membership_id=pk, tenant_id=tenant_id
            )
        except BranchStaffMembership.DoesNotExist:
            return Response(
                {"error_code": "NOT_FOUND", "message": f"Branch membership '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if role == "BRANCH_MANAGER" and caller_branch and bsm.branch.branch_id != caller_branch:
            raise PermissionDenied(
                {
                    "error_code": "ERR_BRANCH_ACCESS_DENIED",
                    "message_en": "Branch Manager cannot view foreign branch memberships.",
                    "message_bn": "অন্য ব্রাঞ্চের মেম্বারশিপ দেখার অনুমতি নেই।",
                }
            )

        return Response(BranchMembershipService._serialize_membership(bsm), status=status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        """PATCH /api/v2/staff/branch-memberships/{id}/"""
        tenant_id, role, caller_branch = self._resolve_tenant_and_role(request)
        self._assert_can_manage(role)

        try:
            bsm = BranchStaffMembership.objects.select_related("staff_member", "branch").get(
                membership_id=pk, tenant_id=tenant_id
            )
        except BranchStaffMembership.DoesNotExist:
            return Response(
                {"error_code": "NOT_FOUND", "message": f"Branch membership '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if role == "BRANCH_MANAGER" and caller_branch and bsm.branch.branch_id != caller_branch:
            raise PermissionDenied(
                {
                    "error_code": "ERR_BRANCH_ACCESS_DENIED",
                    "message_en": "Branch Manager cannot edit foreign branch memberships.",
                    "message_bn": "অন্য ব্রাঞ্চের মেম্বারশিপ পরিবর্তন করার অনুমতি নেই।",
                }
            )

        data = request.data if isinstance(request.data, dict) else {}
        if "role" in data:
            new_role = str(data["role"]).strip().upper()
            if role == "BRANCH_MANAGER" and new_role in ["PHARMACY_OWNER", "BRANCH_MANAGER"]:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Cannot promote to Branch Manager or Owner.",
                        "message_bn": "ম্যানেজার বা ওনার রোলে প্রমোট করার অনুমতি নেই।",
                    }
                )
            bsm.role = new_role

        if "custom_role_title" in data:
            bsm.custom_role_title = str(data["custom_role_title"]).strip()

        if "is_primary" in data:
            is_pri_raw = data["is_primary"]
            is_pri = (
                is_pri_raw.strip().lower() in ["true", "1", "yes"]
                if isinstance(is_pri_raw, str)
                else bool(is_pri_raw)
            )
            if is_pri:
                BranchStaffMembership.objects.filter(
                    tenant_id=tenant_id, staff_member=bsm.staff_member, is_primary=True
                ).update(is_primary=False)
            bsm.is_primary = is_pri

        bsm.save()
        return Response(BranchMembershipService._serialize_membership(bsm), status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None) -> Response:
        """POST /api/v2/staff/branch-memberships/{id}/revoke/"""
        tenant_id, role, caller_branch = self._resolve_tenant_and_role(request)
        self._assert_can_manage(role)

        try:
            bsm = BranchStaffMembership.objects.select_related("branch").get(
                membership_id=pk, tenant_id=tenant_id
            )
        except BranchStaffMembership.DoesNotExist:
            return Response(
                {"error_code": "NOT_FOUND", "message": f"Branch membership '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if role == "BRANCH_MANAGER" and caller_branch and bsm.branch.branch_id != caller_branch:
            raise PermissionDenied(
                {
                    "error_code": "ERR_BRANCH_ACCESS_DENIED",
                    "message_en": "Branch Manager cannot revoke memberships in other branches.",
                    "message_bn": "অন্য ব্রাঞ্চের মেম্বারশিপ বাতিল করার অনুমতি নেই।",
                }
            )

        reason = request.data.get("reason", "Revoked by branch administrator")
        revoked_by = getattr(request.user, "username", getattr(request.user, "email", "manager"))
        result = BranchMembershipService.revoke_branch_membership(
            tenant_id=tenant_id,
            membership_id=pk,
            revoked_by_username=revoked_by,
            reason=reason,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="effective-role")
    def effective_role(self, request) -> Response:
        """
        GET /api/v2/staff/branch-memberships/effective-role/?branch_id=...&staff_id=...
        Returns effective role and action capabilities for a staff member in a branch.
        """
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        branch_id = request.query_params.get("branch_id")
        staff_id = request.query_params.get("staff_id") or getattr(
            request.user, "username", getattr(request.user, "email", "")
        )

        if not branch_id:
            raise ValidationError({"error_code": "MISSING_BRANCH", "message": "branch_id is required."})

        result = BranchMembershipService.get_effective_branch_role(
            tenant_id=tenant_id,
            staff_identifier=staff_id,
            branch_id=branch_id,
        )

        if not result:
            return Response(
                {
                    "error_code": "ERR_BRANCH_MEMBERSHIP_NOT_FOUND",
                    "message_en": f"Staff member '{staff_id}' holds no authorized membership in branch '{branch_id}'.",
                    "message_bn": f"'{branch_id}' ব্রাঞ্চে কর্মীর কোনো মেম্বারশিপ নেই।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        target_user = request.user
        if staff_id:
            sm = (
                StaffMember.objects.filter(tenant_id=tenant_id)
                .filter(models.Q(staff_id=staff_id) | models.Q(username=staff_id))
                .first()
            )
            if sm:
                u = PharmacyUser.objects.filter(tenant_id=tenant_id, email__iexact=sm.username).first()
                target_user = u if u else sm

        effective_actions = GranularActionPermissionService.resolve_effective_actions(
            target_user, tenant_id=tenant_id, branch_id=branch_id
        )

        result["effective_actions"] = effective_actions
        return Response(result, status=status.HTTP_200_OK)
