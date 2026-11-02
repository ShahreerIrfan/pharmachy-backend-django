"""
Shift Scheduling Service for P109 Staff Assignments, Cashier Shifts, and Attendance Boundaries.
Architecture Decision: ADR-0109.
Maintains staff shift rosters, prevents double booking via zero-overlap invariants,
and tracks clock-in / clock-out attendance boundaries in branch-local time (Asia/Dhaka).
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from apps.core.models import (
    AttendanceStatus,
    Branch,
    ShiftAuditEvent,
    ShiftScheduleStatus,
    ShiftType,
    StaffShiftSchedule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.postgres_service import CrossTenantOperationError
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)


class ShiftOverlapCollisionError(Exception):
    """Raised when scheduling a shift that collides with an existing shift."""

    def __init__(self, detail: Dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail))


class SubscriptionSuspendedError(Exception):
    """Raised when tenant subscription is suspended."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ShiftSchedulingService:
    """Core domain service orchestrating pharmacy staff shift rosters and attendance."""

    @classmethod
    def _get_tenant(cls, tenant_id: str) -> Tenant:
        try:
            return Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise CrossTenantOperationError(f"Tenant '{tenant_id}' does not exist.")

    @classmethod
    def _check_subscription_active(cls, tenant: Tenant) -> None:
        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise SubscriptionSuspendedError(
                f"Tenant '{tenant.tenant_id}' subscription is suspended. Shift scheduling mutations are locked."
            )

    @classmethod
    def _get_actor_name(cls, user: Optional[Any]) -> str:
        if not user:
            return ""
        return (
            getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "full_name", None)
            or str(user)
        )

    @classmethod
    def _coerce_datetime(cls, dt_val: Any) -> datetime:
        if isinstance(dt_val, datetime):
            if timezone.is_naive(dt_val):
                return timezone.make_aware(dt_val, timezone.utc)
            return dt_val
        if isinstance(dt_val, str):
            parsed = parse_datetime(dt_val.strip())
            if parsed is None:
                raise ValidationError({"error_code": "INVALID_DATETIME", "message": f"Cannot parse datetime '{dt_val}'."})
            if timezone.is_naive(parsed):
                return timezone.make_aware(parsed, timezone.utc)
            return parsed
        raise ValidationError({"error_code": "INVALID_DATETIME", "message": "Expected datetime or ISO-8601 string."})

    @classmethod
    def create_shift_schedule(
        cls,
        tenant_id: str,
        branch_id: str,
        staff_user_id: str,
        shift_name: str,
        shift_type: str,
        start_time: Any,
        end_time: Any,
        timezone_name: str = "Asia/Dhaka",
        grace_period_minutes: int = 15,
        notes: str = "",
        creator_user: Optional[PharmacyUser] = None,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Creates a new staff shift schedule.
        Enforces:
        - Active subscription check
        - End time > Start time (minimum 30m, maximum 24h)
        - Zero Shift Overlap Collision invariant
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant)
        except Branch.DoesNotExist:
            raise CrossTenantOperationError(f"Branch '{branch_id}' not found for tenant '{tenant_id}'.")

        try:
            staff_member = PharmacyUser.objects.get(user_id=staff_user_id, tenant_id=tenant_id)
        except PharmacyUser.DoesNotExist:
            raise CrossTenantOperationError(f"Staff member '{staff_user_id}' not found for tenant '{tenant_id}'.")

        clean_name = (shift_name or "").strip()
        if len(clean_name) < 2:
            raise ValidationError({"error_code": "INVALID_SHIFT_NAME", "message": "Shift name must be at least 2 characters."})

        clean_type = (shift_type or "").strip().upper()
        if clean_type not in ShiftType.values:
            raise ValidationError({"error_code": "INVALID_SHIFT_TYPE", "message": f"Unsupported shift type '{shift_type}'."})

        dt_start = cls._coerce_datetime(start_time)
        dt_end = cls._coerce_datetime(end_time)

        if dt_end <= dt_start:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_SHIFT_INTERVAL",
                    "message_en": "Shift end time must be after start time.",
                    "message_bn": "শিফটের সমাপ্তি সময় অবশ্যই শুরুর সময়ের পরে হতে হবে।",
                }
            )

        duration = dt_end - dt_start
        if duration < timedelta(minutes=30):
            raise ValidationError(
                {"error_code": "ERR_SHIFT_TOO_SHORT", "message": "Shift duration must be at least 30 minutes."}
            )
        if duration > timedelta(hours=24):
            raise ValidationError(
                {"error_code": "ERR_SHIFT_TOO_LONG", "message": "Shift duration cannot exceed 24 hours."}
            )

        # ----------------------------------------------------------------------
        # Zero Collision Invariant: Check for Overlapping Shifts for this Staff
        # (Existing Start < New End) AND (Existing End > New Start)
        # ----------------------------------------------------------------------
        overlap_qs = StaffShiftSchedule.objects.filter(
            tenant=tenant,
            staff_member=staff_member,
            start_time__lt=dt_end,
            end_time__gt=dt_start,
        ).exclude(status=ShiftScheduleStatus.CANCELLED)

        if overlap_qs.exists():
            colliding = overlap_qs.first()
            raise ShiftOverlapCollisionError(
                {
                    "error_code": "ERR_SHIFT_OVERLAP_COLLISION",
                    "message_en": (
                        f"Staff member '{staff_member.email}' already has an overlapping shift assignment "
                        f"'{colliding.shift_name}' ({colliding.start_time.isoformat()} to {colliding.end_time.isoformat()})."
                    ),
                    "message_bn": f"কর্মী '{staff_member.email}'-এর এই সময়সূচীতে ইতিমধ্যে অন্য একটি শিফট নির্ধারিত রয়েছে।",
                    "colliding_schedule_id": colliding.schedule_id,
                }
            )

        sched_id = f"sched-{uuid.uuid4().hex[:12]}"
        actor_name = cls._get_actor_name(creator_user)

        with transaction.atomic():
            schedule = StaffShiftSchedule.objects.create(
                schedule_id=sched_id,
                tenant=tenant,
                branch=branch,
                staff_member=staff_member,
                shift_name=clean_name,
                shift_type=clean_type,
                start_time=dt_start,
                end_time=dt_end,
                timezone_name=timezone_name or "Asia/Dhaka",
                status=ShiftScheduleStatus.SCHEDULED,
                grace_period_minutes=max(0, int(grace_period_minutes)),
                attendance_status=AttendanceStatus.PENDING,
                notes=(notes or "").strip(),
                created_by_username=actor_name,
                updated_by_username=actor_name,
            )

            ShiftAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                shift_schedule=schedule,
                action="CREATED",
                actor_username=actor_name,
                notes=f"Scheduled {clean_type} shift: {clean_name}",
                metadata={"start": dt_start.isoformat(), "end": dt_end.isoformat(), "branch": branch.name},
                ip_address=ip_address,
            )

        logger.info("Created Shift Schedule %s for %s at %s", sched_id, staff_member.email, branch.name)
        return cls.get_shift_detail(tenant_id=tenant_id, schedule_id=sched_id)

    @classmethod
    def update_shift_schedule(
        cls,
        tenant_id: str,
        schedule_id: str,
        shift_name: Optional[str] = None,
        shift_type: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        grace_period_minutes: Optional[int] = None,
        notes: Optional[str] = None,
        updater_user: Optional[PharmacyUser] = None,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """Updates a shift schedule, re-evaluating interval and overlap constraints."""
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            schedule = StaffShiftSchedule.objects.get(schedule_id=schedule_id, tenant=tenant)
        except StaffShiftSchedule.DoesNotExist:
            raise CrossTenantOperationError(f"Shift schedule '{schedule_id}' not found for tenant '{tenant_id}'.")

        if schedule.status in [ShiftScheduleStatus.COMPLETED, ShiftScheduleStatus.CANCELLED]:
            raise ValidationError(
                {"error_code": "CANNOT_UPDATE_FINALIZED_SHIFT", "message": f"Cannot modify shift in status {schedule.status}."}
            )

        dt_start = cls._coerce_datetime(start_time) if start_time is not None else schedule.start_time
        dt_end = cls._coerce_datetime(end_time) if end_time is not None else schedule.end_time

        if dt_end <= dt_start:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_SHIFT_INTERVAL",
                    "message_en": "Shift end time must be after start time.",
                    "message_bn": "শিফটের সমাপ্তি সময় অবশ্যই শুরুর সময়ের পরে হতে হবে।",
                }
            )

        # Check overlap excluding self
        overlap_qs = StaffShiftSchedule.objects.filter(
            tenant=tenant,
            staff_member=schedule.staff_member,
            start_time__lt=dt_end,
            end_time__gt=dt_start,
        ).exclude(schedule_id=schedule_id).exclude(status=ShiftScheduleStatus.CANCELLED)

        if overlap_qs.exists():
            colliding = overlap_qs.first()
            raise ShiftOverlapCollisionError(
                {
                    "error_code": "ERR_SHIFT_OVERLAP_COLLISION",
                    "message_en": f"Shift update collides with existing shift '{colliding.shift_name}'.",
                    "message_bn": "শিফট পরিবর্তনের সময় অন্য একটি নির্ধারিত শিফটের সাথে সংঘাত তৈরি করছে।",
                    "colliding_schedule_id": colliding.schedule_id,
                }
            )

        actor_name = cls._get_actor_name(updater_user)

        with transaction.atomic():
            if shift_name is not None:
                schedule.shift_name = shift_name.strip()
            if shift_type is not None:
                clean_type = shift_type.strip().upper()
                if clean_type in ShiftType.values:
                    schedule.shift_type = clean_type
            schedule.start_time = dt_start
            schedule.end_time = dt_end
            if grace_period_minutes is not None:
                schedule.grace_period_minutes = max(0, int(grace_period_minutes))
            if notes is not None:
                schedule.notes = notes.strip()
            schedule.updated_by_username = actor_name
            schedule.save()

            ShiftAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                shift_schedule=schedule,
                action="UPDATED",
                actor_username=actor_name,
                notes="Shift schedule details updated",
                metadata={"start": dt_start.isoformat(), "end": dt_end.isoformat()},
                ip_address=ip_address,
            )

        logger.info("Updated Shift Schedule %s", schedule_id)
        return cls.get_shift_detail(tenant_id=tenant_id, schedule_id=schedule_id)

    @classmethod
    def clock_in(
        cls,
        tenant_id: str,
        schedule_id: str,
        actor_user: PharmacyUser,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Clocks in the assigned staff member. Evaluates attendance boundaries (ON_TIME vs LATE).
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            schedule = StaffShiftSchedule.objects.get(schedule_id=schedule_id, tenant=tenant)
        except StaffShiftSchedule.DoesNotExist:
            raise CrossTenantOperationError(f"Shift schedule '{schedule_id}' not found for tenant '{tenant_id}'.")

        role = getattr(actor_user, "role", "")
        # Enforce that only assigned staff member or manager/owner can clock in
        if actor_user.user_id != schedule.staff_member_id and role not in [UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER]:
            raise ValidationError(
                {"error_code": "ERR_CANNOT_CLOCK_IN_FOR_OTHERS", "message": "You can only clock in to your own assigned shift."}
            )

        if schedule.status == ShiftScheduleStatus.IN_PROGRESS:
            raise ValidationError({"error_code": "ALREADY_CLOCKED_IN", "message": "Shift is already in progress."})
        if schedule.status == ShiftScheduleStatus.COMPLETED:
            raise ValidationError({"error_code": "SHIFT_ALREADY_COMPLETED", "message": "Shift is already completed."})
        if schedule.status == ShiftScheduleStatus.CANCELLED:
            raise ValidationError({"error_code": "SHIFT_CANCELLED", "message": "Cannot clock in to a cancelled shift."})

        now = timezone.now()

        # Arrival boundary check
        grace_limit = schedule.start_time + timedelta(minutes=schedule.grace_period_minutes)
        if now <= grace_limit:
            att_status = AttendanceStatus.ON_TIME
        else:
            att_status = AttendanceStatus.LATE

        actor_name = cls._get_actor_name(actor_user)

        with transaction.atomic():
            schedule.clock_in_at = now
            schedule.status = ShiftScheduleStatus.IN_PROGRESS
            schedule.attendance_status = att_status
            schedule.updated_by_username = actor_name
            schedule.save()

            ShiftAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                shift_schedule=schedule,
                action="CLOCK_IN",
                actor_username=actor_name,
                notes=f"Clocked in ({att_status})",
                metadata={"clock_in_at": now.isoformat(), "attendance": att_status},
                ip_address=ip_address,
            )

        logger.info("Clocked in shift %s for %s (%s)", schedule_id, schedule.staff_member.email, att_status)
        return cls.get_shift_detail(tenant_id=tenant_id, schedule_id=schedule_id)

    @classmethod
    def clock_out(
        cls,
        tenant_id: str,
        schedule_id: str,
        actor_user: PharmacyUser,
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """
        Clocks out the assigned staff member, marking shift COMPLETED.
        Detects early departures if clocked out more than 15 mins before end_time.
        """
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            schedule = StaffShiftSchedule.objects.get(schedule_id=schedule_id, tenant=tenant)
        except StaffShiftSchedule.DoesNotExist:
            raise CrossTenantOperationError(f"Shift schedule '{schedule_id}' not found for tenant '{tenant_id}'.")

        role = getattr(actor_user, "role", "")
        if actor_user.user_id != schedule.staff_member_id and role not in [UserRole.SUPERADMIN, UserRole.PHARMACY_OWNER, UserRole.BRANCH_MANAGER]:
            raise ValidationError(
                {"error_code": "ERR_CANNOT_CLOCK_OUT_FOR_OTHERS", "message": "You can only clock out of your own assigned shift."}
            )

        if schedule.status != ShiftScheduleStatus.IN_PROGRESS:
            raise ValidationError(
                {"error_code": "SHIFT_NOT_IN_PROGRESS", "message": f"Cannot clock out a shift in status '{schedule.status}'."}
            )

        now = timezone.now()
        actor_name = cls._get_actor_name(actor_user)

        att_status = schedule.attendance_status
        # Early departure check: 15 minutes before scheduled end
        if now < (schedule.end_time - timedelta(minutes=15)):
            if att_status == AttendanceStatus.ON_TIME:
                att_status = AttendanceStatus.EARLY_DEPARTURE

        with transaction.atomic():
            schedule.clock_out_at = now
            schedule.status = ShiftScheduleStatus.COMPLETED
            schedule.attendance_status = att_status
            schedule.updated_by_username = actor_name
            schedule.save()

            ShiftAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                shift_schedule=schedule,
                action="CLOCK_OUT",
                actor_username=actor_name,
                notes=f"Clocked out ({att_status})",
                metadata={"clock_out_at": now.isoformat(), "attendance": att_status},
                ip_address=ip_address,
            )

        logger.info("Clocked out shift %s for %s", schedule_id, schedule.staff_member.email)
        return cls.get_shift_detail(tenant_id=tenant_id, schedule_id=schedule_id)

    @classmethod
    def cancel_shift(
        cls,
        tenant_id: str,
        schedule_id: str,
        actor_user: PharmacyUser,
        reason: str = "",
        ip_address: str = "",
    ) -> Dict[str, Any]:
        """Cancels a shift schedule."""
        tenant = cls._get_tenant(tenant_id)
        cls._check_subscription_active(tenant)

        try:
            schedule = StaffShiftSchedule.objects.get(schedule_id=schedule_id, tenant=tenant)
        except StaffShiftSchedule.DoesNotExist:
            raise CrossTenantOperationError(f"Shift schedule '{schedule_id}' not found for tenant '{tenant_id}'.")

        if schedule.status in [ShiftScheduleStatus.COMPLETED, ShiftScheduleStatus.CANCELLED]:
            raise ValidationError(
                {"error_code": "CANNOT_CANCEL_FINALIZED_SHIFT", "message": f"Cannot cancel shift in status '{schedule.status}'."}
            )

        actor_name = cls._get_actor_name(actor_user)
        clean_reason = (reason or "Shift cancelled by management.").strip()

        with transaction.atomic():
            schedule.status = ShiftScheduleStatus.CANCELLED
            schedule.notes = f"{schedule.notes}\n[CANCELLED]: {clean_reason}".strip()
            schedule.updated_by_username = actor_name
            schedule.save()

            ShiftAuditEvent.objects.create(
                event_id=f"audit-{uuid.uuid4().hex[:12]}",
                tenant=tenant,
                shift_schedule=schedule,
                action="CANCELLED",
                actor_username=actor_name,
                notes=clean_reason,
                metadata={},
                ip_address=ip_address,
            )

        logger.info("Cancelled shift %s by %s", schedule_id, actor_name)
        return cls.get_shift_detail(tenant_id=tenant_id, schedule_id=schedule_id)

    @classmethod
    def get_shift_detail(cls, tenant_id: str, schedule_id: str) -> Dict[str, Any]:
        """Returns shift schedule detail conforming to shift_scheduling.schema.json in branch-local time."""
        tenant = cls._get_tenant(tenant_id)
        try:
            schedule = StaffShiftSchedule.objects.get(schedule_id=schedule_id, tenant=tenant)
        except StaffShiftSchedule.DoesNotExist:
            raise CrossTenantOperationError(f"Shift schedule '{schedule_id}' not found for tenant '{tenant_id}'.")

        # Convert to branch-local display time (Asia/Dhaka)
        try:
            tz = ZoneInfo(schedule.timezone_name or "Asia/Dhaka")
        except Exception:
            tz = ZoneInfo("Asia/Dhaka")

        local_start = schedule.start_time.astimezone(tz)
        local_end = schedule.end_time.astimezone(tz)

        return {
            "schedule_id": schedule.schedule_id,
            "tenant_id": schedule.tenant_id,
            "branch_id": schedule.branch_id,
            "staff_user_id": str(schedule.staff_member.user_id),
            "staff_email": schedule.staff_member.email,
            "staff_name": schedule.staff_member.full_name or schedule.staff_member.email,
            "shift_name": schedule.shift_name,
            "shift_type": schedule.shift_type,
            "start_time": schedule.start_time.isoformat(),
            "end_time": schedule.end_time.isoformat(),
            "display_start_local": local_start.strftime("%I:%M %p BST"),
            "display_end_local": local_end.strftime("%I:%M %p BST"),
            "timezone_name": schedule.timezone_name,
            "status": schedule.status,
            "grace_period_minutes": schedule.grace_period_minutes,
            "clock_in_at": schedule.clock_in_at.isoformat() if schedule.clock_in_at else None,
            "clock_out_at": schedule.clock_out_at.isoformat() if schedule.clock_out_at else None,
            "attendance_status": schedule.attendance_status,
            "notes": schedule.notes,
            "created_at": schedule.created_at.isoformat(),
            "updated_at": schedule.updated_at.isoformat(),
        }

    @classmethod
    def list_shift_schedules(
        cls,
        tenant_id: str,
        branch_id: Optional[str] = None,
        staff_user_id: Optional[str] = None,
        status: Optional[str] = None,
        shift_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lists shift schedules with optional branch and staff filtering."""
        tenant = cls._get_tenant(tenant_id)
        qs = StaffShiftSchedule.objects.filter(tenant=tenant)

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if staff_user_id:
            qs = qs.filter(staff_member_id=staff_user_id)
        if status:
            qs = qs.filter(status=status.upper())
        if shift_type:
            qs = qs.filter(shift_type=shift_type.upper())

        return [cls.get_shift_detail(tenant_id=tenant_id, schedule_id=s.schedule_id) for s in qs]
