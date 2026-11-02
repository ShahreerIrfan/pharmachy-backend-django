"""
backend/apps/core/staff_lifecycle_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0033, ADR-0082, ADR-0102, ADR-0105, ADR-0106
Phase 06: Milestone M03 - Staff Lifecycle Management (P106)
Requirement Trace: FR-031, FR-032, FR-049, FR-070, FR-071, FR-082

REST API ViewSets for:
- Staff onboarding and provisioning (create)
- Profile & role modifications (update)
- Temporary suspension (deactivate)
- Quota-gated restoration (reactivate)
- Permanent termination with full session, lease, and credential revocations
- Administrative password reset
- Administrative MFA recovery
- Explicit session and device credential revocations
- Immutable lifecycle event audit logs
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import StaffMember
from apps.core.postgres_service import CrossTenantOperationError
from apps.core.quota_guard_service import QuotaExceededError
from apps.core.staff_lifecycle_service import StaffLifecycleService

logger = logging.getLogger("pharmacy.staff_lifecycle.views")


class StaffLifecycleViewSet(viewsets.ViewSet):
    """
    REST API ViewSet managing the complete staff employment and security lifecycle.
    - Pharmacy Owner / Superadmin: Full tenant-wide authority.
    - Branch Manager: Limited to viewing and updating operational staff in own branch.
    - Frontline Staff: 403 Forbidden on all lifecycle mutations.
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

        req_tenant = request.query_params.get("tenant_id") or request.data.get("tenant_id")
        if (
            req_tenant
            and tenant_id
            and req_tenant != tenant_id
            and role not in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        ):
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

    def _get_actor_identifier(self, request) -> str:
        u = getattr(request, "user", None)
        if not u:
            return "admin"
        return getattr(u, "email", None) or getattr(u, "username", "admin")

    def _assert_can_view_staff(self, role: str) -> None:
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
                    "message_en": f"Role '{role}' is not authorized to view staff lifecycle.",
                    "message_bn": f"আপনার রোলে ('{role}') স্টাফ মেম্বারদের তথ্য দেখার অনুমতি নেই।",
                }
            )

    def _assert_can_manage_lifecycle(self, role: str) -> None:
        allowed = [
            "SUPERADMIN",
            "SUPER_ADMIN",
            "PLATFORM_OPERATOR",
            "PHARMACY_OWNER",
            "TENANT_OWNER",
        ]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": "Only pharmacy owners or superadmins can perform this lifecycle action.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক বা সুপারএডমিন এই লাইফসাইকেল পরিবর্তন করতে পারেন।",
                }
            )

    def list(self, request) -> Response:
        """Lists staff members within tenant with optional branch, role, and lifecycle status filters."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_view_staff(role)

        branch_query = request.query_params.get("branch_id")
        if role == "BRANCH_MANAGER":
            if branch_query and branch_query != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot inspect other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের স্টাফ তালিকা দেখতে পারেন না।",
                    }
                )
            branch_query = user_branch_id

        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ["true", "1", "yes"]

        try:
            staff_list = StaffLifecycleService.list_staff(
                tenant_id=tenant_id,
                branch_id=branch_query,
                role=request.query_params.get("role"),
                lifecycle_status=request.query_params.get("lifecycle_status"),
                is_active=is_active,
            )
            return Response({"count": len(staff_list), "results": staff_list}, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})

    def create(self, request) -> Response:
        """Direct administrative staff onboarding."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.create_staff(
                tenant_id=tenant_id,
                username=data.get("username"),
                email=data.get("email"),
                full_name=data.get("full_name"),
                role=data.get("role"),
                branch_id=data.get("branch_id"),
                mobile_number=data.get("mobile_number", ""),
                pcb_registration_no=data.get("pcb_registration_no", ""),
                password=data.get("password"),
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except QuotaExceededError as exc:
            return Response(
                {"error_code": "ERR_QUOTA_EXCEEDED", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None) -> Response:
        """Retrieves staff member detail with active session/lease counts and MFA status."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_view_staff(role)

        try:
            detail = StaffLifecycleService.get_staff_detail(tenant_id=tenant_id, staff_id=pk)
            if role == "BRANCH_MANAGER" and detail["branch_id"] != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot inspect staff from other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের স্টাফ দেখতে পারেন না।",
                    }
                )
            return Response(detail, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})

    def partial_update(self, request, pk=None) -> Response:
        """Updates staff profile details."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)

        # Branch Manager can update operational staff in own branch
        if role == "BRANCH_MANAGER":
            sm = StaffMember.objects.filter(staff_id=pk, tenant_id=tenant_id).first()
            if not sm:
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": "Staff not found."})
            if sm.branch_id != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot update staff from other branches.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের স্টাফ পরিবর্তন করতে পারেন না।",
                    }
                )
            if request.data.get("role") in ["BRANCH_MANAGER", "PHARMACY_OWNER", "TENANT_OWNER"]:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_PERMISSION_DENIED",
                        "message_en": "Branch Manager cannot promote staff to managerial roles.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার কাউকে ম্যানেজার বা ওনার রোলে প্রমোট করতে পারেন না।",
                    }
                )
        else:
            self._assert_can_manage_lifecycle(role)

        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            updated = StaffLifecycleService.update_staff(
                tenant_id=tenant_id,
                staff_id=pk,
                full_name=data.get("full_name"),
                role=data.get("role"),
                branch_id=data.get("branch_id"),
                mobile_number=data.get("mobile_number"),
                pcb_registration_no=data.get("pcb_registration_no"),
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None) -> Response:
        """Temporarily suspends a staff member and revokes all active sessions."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        reason = request.data.get("reason", "Staff deactivation by administrator")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.deactivate_staff(
                tenant_id=tenant_id,
                staff_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reactivate")
    def reactivate(self, request, pk=None) -> Response:
        """Reactivates a suspended staff member subject to subscription quota."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        reason = request.data.get("reason", "Staff reactivated by administrator")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.reactivate_staff(
                tenant_id=tenant_id,
                staff_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except QuotaExceededError as exc:
            return Response(
                {"error_code": "ERR_QUOTA_EXCEEDED", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="terminate")
    def terminate(self, request, pk=None) -> Response:
        """Permanently terminates staff employment with full session and device credential revocations."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        reason = request.data.get("reason", "")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.terminate_staff(
                tenant_id=tenant_id,
                staff_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reset-credentials")
    def reset_credentials(self, request, pk=None) -> Response:
        """Administrative password reset with session invalidation."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        new_password = request.data.get("password")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.reset_credentials(
                tenant_id=tenant_id,
                staff_id=pk,
                new_password=new_password,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="mfa-recovery")
    def mfa_recovery(self, request, pk=None) -> Response:
        """Administrative MFA recovery / reset."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        reason = request.data.get("reason", "Administrative MFA device recovery")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.mfa_recovery(
                tenant_id=tenant_id,
                staff_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="revoke-sessions")
    def revoke_sessions(self, request, pk=None) -> Response:
        """Immediately revokes all active sessions for this staff member."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        reason = request.data.get("reason", "ADMIN_REVOCATION")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.revoke_sessions(
                tenant_id=tenant_id,
                staff_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="revoke-devices")
    def revoke_devices(self, request, pk=None) -> Response:
        """Invalidates all offline device lease certificates for this staff member."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_lifecycle(role)

        reason = request.data.get("reason", "DEVICE_CREDENTIALS_REVOKED")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = StaffLifecycleService.revoke_device_credentials(
                tenant_id=tenant_id,
                staff_id=pk,
                reason=reason,
                performed_by_username=self._get_actor_identifier(request),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="events")
    def events(self, request, pk=None) -> Response:
        """Returns the immutable lifecycle event history for this staff member."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_view_staff(role)

        try:
            event_list = StaffLifecycleService.list_lifecycle_events(tenant_id=tenant_id, staff_id=pk)
            return Response({"count": len(event_list), "results": event_list}, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})

