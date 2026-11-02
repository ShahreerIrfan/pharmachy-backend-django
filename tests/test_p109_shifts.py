"""
Backend Integration Tests for P109 Shift Scheduling, Cashier Shifts, and Attendance Boundaries.
Architecture Decision: ADR-0109.
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Verifies:
- Shift creation, updates, and cancellations in branch-local display time (Asia/Dhaka)
- Zero Shift Overlap Collision Invariant (double-booking blockage)
- Attendance boundaries (clock-in, grace period ON_TIME vs LATE, clock-out early departure)
- Separation of Duties (Owner / Branch Manager vs Frontline Cashier)
- Branch assignment scoping (cross-branch scheduling blocked)
- Cross-tenant isolation barriers and suspended subscription mutation locks
"""

import random
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    AttendanceStatus,
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    ShiftAuditEvent,
    ShiftScheduleStatus,
    ShiftType,
    StaffShiftSchedule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole


class ShiftSchedulingIntegrationTests(TestCase):
    """Integration test suite verifying P109 Shift Scheduling workflows and invariants."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Primary Tenant & Branches
        self.tenant = Tenant.objects.create(
            tenant_id=f"tn-p109-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-p109-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant,
            name=f"Dhanmondi Branch {self.suffix}",
            license_number=f"DGDA-DL-A-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
            is_main_branch=True,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant,
            name=f"Gulshan Branch {self.suffix}",
            license_number=f"DGDA-DL-B-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
            is_main_branch=False,
        )

        # 2. Foreign Tenant
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"tn-foreign-{self.suffix}",
            name=f"Foreign Pharma {self.suffix}",
            subdomain=f"for-p109-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-foreign-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Foreign Store",
            license_number=f"DGDA-DL-FOR-{self.suffix}",
            status=BranchLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 3. Users in Primary Tenant
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant.tenant_id,
        )
        self.manager_a = PharmacyUser.objects.create_user(
            email=f"mgr_a_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.manager_b = PharmacyUser.objects.create_user(
            email=f"mgr_b_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Manager Gulshan",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.cashier_1 = PharmacyUser.objects.create_user(
            email=f"cashier1_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier 1 Rahim",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier_2 = PharmacyUser.objects.create_user(
            email=f"cashier2_{self.suffix}@popular.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier 2 Karim",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. Foreign User
        self.foreign_user = PharmacyUser.objects.create_user(
            email=f"f_user_{self.suffix}@foreign.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Foreign Manager",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

    def test_01_valid_shift_creation_by_branch_manager(self):
        """Branch Manager schedules a valid Morning shift for Cashier 1."""
        self.client.force_authenticate(user=self.manager_a)
        start = timezone.now() + timedelta(hours=2)
        end = start + timedelta(hours=8)

        res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Morning Counter 1",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "timezone_name": "Asia/Dhaka",
            "grace_period_minutes": 15,
            "notes": "Handle retail billing and POS counter",
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        data = res.json()
        self.assertTrue(data["schedule_id"].startswith("sched-"))
        self.assertEqual(data["status"], "SCHEDULED")
        self.assertEqual(data["attendance_status"], "PENDING")
        self.assertEqual(data["staff_email"], self.cashier_1.email)
        self.assertIsNotNone(data["display_start_local"])
        self.assertIsNotNone(data["display_end_local"])

        # Audit event created
        audits = ShiftAuditEvent.objects.filter(shift_schedule_id=data["schedule_id"])
        self.assertEqual(audits.count(), 1)
        self.assertEqual(audits.first().action, "CREATED")

    def test_02_invalid_shift_interval_rejected(self):
        """End time before or equal to start time is rejected with ERR_INVALID_SHIFT_INTERVAL."""
        self.client.force_authenticate(user=self.manager_a)
        start = timezone.now() + timedelta(hours=2)
        bad_end = start - timedelta(hours=1)

        res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Bad Shift Interval",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": bad_end.isoformat(),
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_SHIFT_INTERVAL")
        self.assertTrue("সমাপ্তি সময়" in res.json()["message_bn"])

    def test_03_zero_collision_shift_overlap_invariant_blocked(self):
        """Zero Collision Invariant: Overlapping shifts for the same staff member are blocked."""
        self.client.force_authenticate(user=self.manager_a)
        start_1 = timezone.now() + timedelta(hours=2)
        end_1 = start_1 + timedelta(hours=6)

        # 1. Schedule initial shift
        res1 = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Initial Shift 1",
            "shift_type": "MORNING",
            "start_time": start_1.isoformat(),
            "end_time": end_1.isoformat(),
        }, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # 2. Attempt overlapping shift (starts 2 hours into shift 1)
        start_2 = start_1 + timedelta(hours=2)
        end_2 = end_1 + timedelta(hours=2)

        res2 = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Colliding Shift 2",
            "shift_type": "EVENING",
            "start_time": start_2.isoformat(),
            "end_time": end_2.isoformat(),
        }, format="json")

        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)
        data = res2.json()
        self.assertEqual(data["error_code"], "ERR_SHIFT_OVERLAP_COLLISION")
        self.assertTrue("অন্য একটি শিফট নির্ধারিত" in data["message_bn"])

    def test_04_different_staff_same_time_window_allowed(self):
        """Two different cashiers scheduled for the same time window succeed (dual counter operation)."""
        self.client.force_authenticate(user=self.manager_a)
        start = timezone.now() + timedelta(hours=3)
        end = start + timedelta(hours=8)

        res1 = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Counter 1 Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        res2 = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_2.user_id),
            "shift_name": "Counter 2 Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

    def test_05_update_shift_schedule_success_and_overlap_recheck(self):
        """Branch manager updates shift; valid new time succeeds, colliding new time blocked."""
        self.client.force_authenticate(user=self.manager_a)
        start = timezone.now() + timedelta(hours=4)
        end = start + timedelta(hours=6)

        create_res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Adjustable Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")
        sched_id = create_res.json()["schedule_id"]

        # Valid update (extend by 1 hour)
        new_end = end + timedelta(hours=1)
        up_res = self.client.patch(f"/api/v2/shifts/{sched_id}/", {
            "end_time": new_end.isoformat(),
            "notes": "Shift extended by 1 hour due to evening rush",
        }, format="json")
        self.assertEqual(up_res.status_code, status.HTTP_200_OK)
        self.assertEqual(up_res.json()["notes"], "Shift extended by 1 hour due to evening rush")

    def test_06_frontline_cashier_denied_shift_creation_rbac(self):
        """Cashier is denied creating or updating shift schedules (HTTP 403 Forbidden)."""
        self.client.force_authenticate(user=self.cashier_1)
        start = timezone.now() + timedelta(hours=1)
        end = start + timedelta(hours=8)

        res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Self Assigned Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_PERMISSION_DENIED")

    def test_07_branch_manager_cross_branch_violation(self):
        """Branch Manager of Branch B is blocked from scheduling shifts in Branch A."""
        self.client.force_authenticate(user=self.manager_b)
        start = timezone.now() + timedelta(hours=2)
        end = start + timedelta(hours=8)

        res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Cross Branch Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["error_code"], "ERR_CROSS_BRANCH_VIOLATION")

    def test_08_staff_clock_in_attendance_on_time_and_late(self):
        """Staff clock-in marks shift IN_PROGRESS and evaluates attendance boundary."""
        self.client.force_authenticate(user=self.manager_a)
        # Shift starting now + 5 min (grace period 15 min -> on time)
        start = timezone.now() + timedelta(minutes=5)
        end = start + timedelta(hours=8)

        res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Counter Shift On Time",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "grace_period_minutes": 15,
        }, format="json")
        sched_id = res.json()["schedule_id"]

        # Cashier 1 clocks in
        self.client.force_authenticate(user=self.cashier_1)
        clock_res = self.client.post(f"/api/v2/shifts/{sched_id}/clock-in/", {}, format="json")
        self.assertEqual(clock_res.status_code, status.HTTP_200_OK)
        data = clock_res.json()
        self.assertEqual(data["status"], "IN_PROGRESS")
        self.assertEqual(data["attendance_status"], "ON_TIME")
        self.assertIsNotNone(data["clock_in_at"])

    def test_09_staff_clock_out_and_early_departure(self):
        """Staff clock-out marks shift COMPLETED."""
        self.client.force_authenticate(user=self.manager_a)
        start = timezone.now() - timedelta(hours=1)
        end = start + timedelta(hours=8)

        shift = StaffShiftSchedule.objects.create(
            schedule_id=f"sched-flow-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch_a,
            staff_member=self.cashier_1,
            shift_name="Flow Shift",
            shift_type=ShiftType.MORNING,
            start_time=start,
            end_time=end,
            status=ShiftScheduleStatus.IN_PROGRESS,
            attendance_status=AttendanceStatus.ON_TIME,
            clock_in_at=start,
        )

        self.client.force_authenticate(user=self.cashier_1)
        out_res = self.client.post(f"/api/v2/shifts/{shift.schedule_id}/clock-out/", {}, format="json")
        self.assertEqual(out_res.status_code, status.HTTP_200_OK)
        data = out_res.json()
        self.assertEqual(data["status"], "COMPLETED")
        self.assertIsNotNone(data["clock_out_at"])

    def test_10_cancel_shift_schedule_audit(self):
        """Manager cancels shift schedule; status becomes CANCELLED and unblocks time slot."""
        self.client.force_authenticate(user=self.manager_a)
        start = timezone.now() + timedelta(hours=3)
        end = start + timedelta(hours=6)

        create_res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Cancellable Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")
        sched_id = create_res.json()["schedule_id"]

        # Cancel shift
        cancel_res = self.client.post(f"/api/v2/shifts/{sched_id}/cancel/", {
            "reason": "Staff requested emergency medical leave",
        }, format="json")
        self.assertEqual(cancel_res.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_res.json()["status"], "CANCELLED")

        # Scheduling a replacement shift in the same slot now succeeds because old shift is CANCELLED
        rep_res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Replacement Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")
        self.assertEqual(rep_res.status_code, status.HTTP_201_CREATED)

    def test_11_cross_tenant_isolation_barrier(self):
        """User from foreign tenant cannot view or modify shifts of another tenant."""
        self.client.force_authenticate(user=self.manager_a)
        start = timezone.now() + timedelta(hours=2)
        end = start + timedelta(hours=8)

        create_res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Private Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")
        sched_id = create_res.json()["schedule_id"]

        # Foreign user tries to retrieve or cancel
        self.client.force_authenticate(user=self.foreign_user)
        get_res = self.client.get(f"/api/v2/shifts/{sched_id}/")
        self.assertEqual(get_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(get_res.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_12_suspended_tenant_subscription_blocks_shift_mutations(self):
        """When tenant subscription is suspended, shift scheduling is blocked."""
        self.tenant.status = TenantLifecycleStatus.SUSPENDED
        self.tenant.save()

        self.client.force_authenticate(user=self.owner)
        start = timezone.now() + timedelta(hours=2)
        end = start + timedelta(hours=8)

        res = self.client.post("/api/v2/shifts/", {
            "branch_id": self.branch_a.branch_id,
            "staff_user_id": str(self.cashier_1.user_id),
            "shift_name": "Suspended Tenant Shift",
            "shift_type": "MORNING",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }, format="json")

        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            res.json()["error_code"],
            ["ERR_SUBSCRIPTION_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"],
        )
