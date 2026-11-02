"""
REST API ViewSet for Staff Shift Scheduling, Cashier Rosters, and Attendance Boundaries.
Architecture Decision: ADR-0005, ADR-0104, ADR-0109 (P109).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Enforces:
- Branch-local display time (Asia/Dhaka)
- Zero-overlap shift collision prevention
- Role-based separation of duties (Manager/Owner schedules; Staff clocks in/out)
- Attendance boundaries (On-time, Late, Early departure)
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.postgres_service import CrossTenantOperationError
from apps.core.shift_service import (
    ShiftOverlapCollisionError,
    ShiftSchedulingService,
    SubscriptionSuspendedError,
)

logger = logging.getLogger("pharmacy.shifts.views")


class StaffShiftScheduleViewSet(viewsets.ViewSet):
    """
    REST API ViewSet managing staff shift assignments, rosters, and attendance tracking.
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
            or request.headers.get("X-Tenant-ID")
            or request.query_params.get("tenant_id")
        )
        if hasattr(tenant_id, "tenant_id"):
            tenant_id = tenant_id.tenant_id

        if not tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_NOT_IDENTIFIED",
                    "message_en": "Tenant identification missing from user session or header.",
                    "message_bn": "টেন্যান্ট চিহ্নিত করা যায়নি।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        if hasattr(user_branch_id, "branch_id"):
            user_branch_id = user_branch_id.branch_id

        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None

    def _assert_can_manage_roster(self, role: str) -> None:
        allowed = ["SUPERADMIN", "PHARMACY_OWNER", "BRANCH_MANAGER"]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Role '{role}' is not authorized to create or modify staff shift schedules.",
                    "message_bn": "শিফট শিডিউল তৈরি বা পরিবর্তন করার অনুমতি আপনার নেই।",
                }
            )

    def list(self, request) -> Response:
        """Lists shift schedules for tenant, optionally filtered by branch, staff, status, or shift_type."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)

        branch_id = request.query_params.get("branch_id")
        if role == "BRANCH_MANAGER" and user_branch_id and not branch_id:
            branch_id = user_branch_id

        staff_id = request.query_params.get("staff_user_id")
        status_filter = request.query_params.get("status")
        type_filter = request.query_params.get("shift_type")

        try:
            results = ShiftSchedulingService.list_shift_schedules(
                tenant_id=tenant_id,
                branch_id=branch_id,
                staff_user_id=staff_id,
                status=status_filter,
                shift_type=type_filter,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)

    @action(detail=False, methods=["get"], url_path="my-shifts")
    def my_shifts(self, request) -> Response:
        """Returns the logged-in staff member's personal assigned shift schedules."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        try:
            results = ShiftSchedulingService.list_shift_schedules(
                tenant_id=tenant_id,
                staff_user_id=str(request.user.user_id),
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)

    def create(self, request) -> Response:
        """Creates a new shift schedule for a staff member (Manager / Owner)."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)
        self._assert_can_manage_roster(role)

        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "")
        branch_id = data.get("branch_id") or user_branch_id

        # Branch Manager cross-branch restriction
        if role == "BRANCH_MANAGER" and user_branch_id and branch_id != user_branch_id:
            return Response(
                {
                    "error_code": "ERR_CROSS_BRANCH_VIOLATION",
                    "message_en": f"Branch Manager assigned to branch '{user_branch_id}' cannot schedule shifts for branch '{branch_id}'.",
                    "message_bn": "ব্রাঞ্চ ম্যানেজার অন্য ব্রাঞ্চের জন্য শিফট নির্ধারণ করতে পারেন না।",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if not branch_id:
            return Response(
                {"error_code": "BRANCH_REQUIRED", "message": "Branch identifier is required for shift schedule."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = ShiftSchedulingService.create_shift_schedule(
                tenant_id=tenant_id,
                branch_id=branch_id,
                staff_user_id=data.get("staff_user_id"),
                shift_name=data.get("shift_name"),
                shift_type=data.get("shift_type"),
                start_time=data.get("start_time"),
                end_time=data.get("end_time"),
                timezone_name=data.get("timezone_name", "Asia/Dhaka"),
                grace_period_minutes=data.get("grace_period_minutes", 15),
                notes=data.get("notes", ""),
                creator_user=request.user,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except ShiftOverlapCollisionError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None) -> Response:
        """Retrieves shift schedule detail."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        try:
            detail = ShiftSchedulingService.get_shift_detail(tenant_id=tenant_id, schedule_id=pk)
            return Response(detail, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)

    def partial_update(self, request, pk=None) -> Response:
        """Updates shift schedule details (times, grace period, notes)."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_roster(role)

        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = ShiftSchedulingService.update_shift_schedule(
                tenant_id=tenant_id,
                schedule_id=pk,
                shift_name=data.get("shift_name"),
                shift_type=data.get("shift_type"),
                start_time=data.get("start_time"),
                end_time=data.get("end_time"),
                grace_period_minutes=data.get("grace_period_minutes"),
                notes=data.get("notes"),
                updater_user=request.user,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ShiftOverlapCollisionError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="clock-in")
    def clock_in(self, request, pk=None) -> Response:
        """Clock-in action for assigned staff member."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = ShiftSchedulingService.clock_in(
                tenant_id=tenant_id,
                schedule_id=pk,
                actor_user=request.user,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="clock-out")
    def clock_out(self, request, pk=None) -> Response:
        """Clock-out action for assigned staff member."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = ShiftSchedulingService.clock_out(
                tenant_id=tenant_id,
                schedule_id=pk,
                actor_user=request.user,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None) -> Response:
        """Cancels a shift schedule."""
        tenant_id, role, _ = self._resolve_tenant_and_role(request)
        self._assert_can_manage_roster(role)
        ip_addr = request.META.get("REMOTE_ADDR", "")
        reason = request.data.get("reason", "")

        try:
            result = ShiftSchedulingService.cancel_shift(
                tenant_id=tenant_id,
                schedule_id=pk,
                actor_user=request.user,
                reason=reason,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
