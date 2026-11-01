"""
Staff Invitation & Membership REST API ViewSets (P102 / ADR-0102).
Implements role-gated endpoints for owner invitations, branch manager delegation,
resend, revocation, public rate-throttled acceptance, and branch scope management.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import StaffInvitation
from apps.core.postgres_service import CrossTenantOperationError
from apps.core.quota_guard_service import QuotaExceededError
from apps.core.staff_membership_service import StaffMembershipService
from apps.core.throttling import PublicInvitationRateThrottle

logger = logging.getLogger("pharmacy.staff_membership.views")


class BaseStaffAccessViewSet(viewsets.ViewSet):
    """Base helper providing role authorization and tenant/branch resolution."""

    def _resolve_tenant_and_role(self, request) -> Tuple[str, str, Optional[str]]:
        user = request.user
        role = str(getattr(user, "role", "")).upper()

        # If user is anonymous or unauthenticated
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

        req_tenant = request.query_params.get("tenant_id") or request.data.get("tenant_id")
        if req_tenant and tenant_id and req_tenant != tenant_id and role not in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]:
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
                    "message_en": "Tenant context is missing from user session.",
                    "message_bn": "ইউজার সেশনে টেন্যান্ট কনটেক্সট অনুপস্থিত।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None


class StaffInvitationViewSet(BaseStaffAccessViewSet):
    """
    Manages tenant staff invitations.
    - Owner/Superadmin: full tenant-wide invitation dispatch, resend, revocation.
    - Branch Manager: restricted to operational staff (Cashier, Pharmacist, Clerk) in own branch.
    - Frontline roles: 403 Forbidden.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _assert_can_manage_invitations(self, role: str) -> None:
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
                    "message_en": f"Role '{role}' is not authorized to manage staff invitations.",
                    "message_bn": f"আপনার রোল ('{role}') এর স্টাফ ইনভাইটেশন পরিচালনা করার অনুমতি নেই।",
                }
            )

    def list(self, request) -> Response:
        """Lists invitations for the current tenant subject to role branch scoping."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_manage_invitations(role)

        query_branch = request.query_params.get("branch_id")
        # Branch manager can ONLY inspect their own branch
        if role == "BRANCH_MANAGER":
            if not user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_SCOPE_REQUIRED",
                        "message_en": "Branch Manager must have an assigned branch.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজারের জন্য নির্ধারিত ব্রাঞ্চ আইডি থাকা আবশ্যক।",
                    }
                )
            if query_branch and query_branch != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": f"Branch Manager cannot access branch '{query_branch}'.",
                        "message_bn": f"ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের ('{query_branch}') ইনভাইটেশন দেখতে পারেন না।",
                    }
                )
            query_branch = user_branch_id

        status_param = request.query_params.get("status")
        role_param = request.query_params.get("role")

        invitations = StaffMembershipService.list_invitations(
            tenant_id=tenant_id,
            branch_id=query_branch,
            status=status_param,
            role=role_param,
        )
        return Response({"count": len(invitations), "results": invitations}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """Dispatches a new staff invitation."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_manage_invitations(role)

        data = request.data
        target_role = str(data.get("role", "")).upper()
        target_branch = data.get("branch_id")

        # Branch Manager restrictions:
        if role == "BRANCH_MANAGER":
            if not user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager lacks assigned branch scope.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজারের নির্ধারিত ব্রাঞ্চ স্কোপ নেই।",
                    }
                )
            # Cannot invite another Branch Manager or Tenant Owner
            forbidden_target_roles = ["BRANCH_MANAGER", "PHARMACY_OWNER", "TENANT_OWNER", "SUPERADMIN"]
            if target_role in forbidden_target_roles:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": f"Branch Manager cannot invite role '{target_role}'.",
                        "message_bn": f"ব্রাঞ্চ ম্যানেজার '{target_role}' রোলে কাউকে ইনভাইট করতে পারেন না।",
                    }
                )
            # Branch must match Branch Manager's assigned branch
            if target_branch and target_branch != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": f"Branch Manager cannot invite staff to branch '{target_branch}'.",
                        "message_bn": f"ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চে ('{target_branch}') কর্মী ইনভাইট করতে পারেন না।",
                    }
                )
            target_branch = user_branch_id

        try:
            inv = StaffMembershipService.create_invitation(
                tenant_id=tenant_id,
                email=data.get("email"),
                full_name=data.get("full_name"),
                role=target_role,
                branch_id=target_branch,
                custom_role_title=data.get("custom_role_title", ""),
                pcb_registration_no=data.get("pcb_registration_no", ""),
                phone_number=data.get("phone_number", ""),
                invited_by_username=getattr(request.user, "username", ""),
            )
            return Response(inv, status=status.HTTP_201_CREATED)
        except (CrossTenantOperationError, PermissionDenied) as exc:
            raise PermissionDenied(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc), "message_bn": "টেন্যান্ট বা ব্রাঞ্চ অ্যাক্সেস ডিনায়েড।"}
            )
        except QuotaExceededError as exc:
            return Response(
                {
                    "error_code": "ERR_QUOTA_EXCEEDED",
                    "message_en": str(exc),
                    "message_bn": "আপনার বর্তমান প্ল্যানের অনুমোদিত স্টাফ কোটা পূর্ণ হয়ে গেছে। অনুগ্রহ করে সাবস্ক্রিপশন আপগ্রেড করুন।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="resend")
    def resend(self, request, pk=None) -> Response:
        """Resends pending invitation, resetting token and refreshing 7-day expiration."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_manage_invitations(role)

        # If Branch Manager, verify invitation belongs to their branch
        if role == "BRANCH_MANAGER":
            inv = StaffInvitation.objects.filter(invitation_id=pk, tenant_id=tenant_id).first()
            if not inv:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": "Invitation not found."})
            if inv.branch_id != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot resend invitations for other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের ইনভাইটেশন পুনরায় পাঠাতে পারেন না।",
                    }
                )

        try:
            updated = StaffMembershipService.resend_invitation(
                tenant_id=tenant_id,
                invitation_id=pk,
                requester_username=getattr(request.user, "username", ""),
            )
            return Response(updated, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except QuotaExceededError as exc:
            return Response({"error_code": "ERR_QUOTA_EXCEEDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None) -> Response:
        """Revokes an outstanding invitation."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_manage_invitations(role)

        if role == "BRANCH_MANAGER":
            inv = StaffInvitation.objects.filter(invitation_id=pk, tenant_id=tenant_id).first()
            if not inv:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": "Invitation not found."})
            if inv.branch_id != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot revoke invitations for other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের ইনভাইটেশন বাতিল করতে পারেন না।",
                    }
                )

        reason = request.data.get("reason", "Revoked by pharmacy administrator")
        try:
            revoked = StaffMembershipService.revoke_invitation(
                tenant_id=tenant_id,
                invitation_id=pk,
                revoked_by_username=getattr(request.user, "username", ""),
                reason=reason,
            )
            return Response(revoked, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="accept", permission_classes=[permissions.AllowAny], throttle_classes=[PublicInvitationRateThrottle])
    def accept(self, request) -> Response:
        """Publicly accessible rate-throttled invitation acceptance endpoint."""
        data = request.data
        token = data.get("invite_token") or data.get("token")
        username = data.get("username")
        password = data.get("password")
        phone_number = data.get("phone_number")

        try:
            result = StaffMembershipService.accept_invitation(
                invite_token=token,
                username=username,
                password=password,
                phone_number=phone_number,
            )
            return Response(
                {
                    "message_en": "Invitation accepted successfully. Staff membership activated.",
                    "message_bn": "ইনভাইটেশন সফলভাবে গৃহীত হয়েছে এবং স্টাফ মেম্বারশিপ সক্রিয় করা হয়েছে।",
                    "membership": result,
                },
                status=status.HTTP_201_CREATED,
            )
        except QuotaExceededError as exc:
            return Response({"error_code": "ERR_QUOTA_EXCEEDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)


class StaffMembershipViewSet(BaseStaffAccessViewSet):
    """
    Manages active staff memberships, primary branch assignment, and multi-branch scoping.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _assert_can_view_memberships(self, role: str) -> None:
        allowed = ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR", "PHARMACY_OWNER", "TENANT_OWNER", "BRANCH_MANAGER"]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Role '{role}' is not authorized to view staff memberships.",
                    "message_bn": f"আপনার রোলে ('{role}') স্টাফ মেম্বারশিপ দেখার অনুমতি নেই।",
                }
            )

    def _assert_can_modify_memberships(self, role: str) -> None:
        allowed = ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR", "PHARMACY_OWNER", "TENANT_OWNER"]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Only pharmacy owners can update branch scopes or deactivate staff.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিকগণ ব্রাঞ্চ স্কোপ পরিবর্তন বা স্টাফ নিষ্ক্রিয় করতে পারেন।",
                }
            )

    def list(self, request) -> Response:
        """Lists staff memberships for tenant."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_view_memberships(role)

        branch_query = request.query_params.get("branch_id")
        if role == "BRANCH_MANAGER":
            if branch_query and branch_query != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot inspect other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের মেম্বারশিপ তালিকা দেখতে পারেন না।",
                    }
                )
            branch_query = user_branch_id

        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ["true", "1", "yes"]

        memberships = StaffMembershipService.list_memberships(
            tenant_id=tenant_id,
            branch_id=branch_query,
            is_active=is_active,
            role=request.query_params.get("role"),
        )
        return Response({"count": len(memberships), "results": memberships}, status=status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        """Updates primary branch and assigned roaming branch scopes for a membership."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_modify_memberships(role)

        data = request.data
        try:
            updated = StaffMembershipService.update_membership_branch_scopes(
                tenant_id=tenant_id,
                membership_id=pk,
                primary_branch_id=data.get("primary_branch_id"),
                assigned_branch_ids=data.get("assigned_branch_ids"),
                custom_role_title=data.get("custom_role_title"),
            )
            return Response(updated, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None) -> Response:
        """Deactivates staff membership."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_modify_memberships(role)

        reason = request.data.get("reason", "Deactivated by pharmacy owner")
        try:
            deactivated = StaffMembershipService.deactivate_membership(
                tenant_id=tenant_id,
                membership_id=pk,
                deactivated_by_username=getattr(request.user, "username", ""),
                reason=reason,
            )
            return Response(deactivated, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)
