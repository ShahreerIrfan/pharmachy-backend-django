"""
Integration tests for P117: Cashier register assignment and zero concurrent drawer collisions (ADR-0117).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

import json
import random
import uuid
from pathlib import Path

from django.test import TestCase
from jsonschema import validate
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    StaffLifecycleStatus,
    StaffMember,
    StaffRole,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "cashier_register_assignment.schema.json"


class CashierRegisterAssignmentIntegrationTests(TestCase):
    """End-to-end integration tests for P117 Cashier Register Assignment."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Primary Tenant & Branches
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p117-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-p117-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name="Gulshan Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. StaffMembers in Tenant A
        self.staff_owner = StaffMember.objects.create(
            staff_id=f"st-own-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            username=f"owner_{self.suffix}@lazz.local",
            full_name="Pharmacy Owner",
            role=StaffRole.TENANT_OWNER,
            lifecycle_status=StaffLifecycleStatus.ACTIVE,
            is_active=True,
        )
        self.staff_manager = StaffMember.objects.create(
            staff_id=f"st-mgr-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            username=f"mgr_{self.suffix}@lazz.local",
            full_name="Branch Manager Dhanmondi",
            role=StaffRole.BRANCH_MANAGER,
            lifecycle_status=StaffLifecycleStatus.ACTIVE,
            is_active=True,
        )
        self.staff_cashier1 = StaffMember.objects.create(
            staff_id=f"st-csh1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            username=f"csh1_{self.suffix}@lazz.local",
            full_name="Cashier Rahim",
            role=StaffRole.CASHIER,
            lifecycle_status=StaffLifecycleStatus.ACTIVE,
            is_active=True,
        )
        self.staff_cashier2 = StaffMember.objects.create(
            staff_id=f"st-csh2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            username=f"csh2_{self.suffix}@lazz.local",
            full_name="Cashier Karim",
            role=StaffRole.CASHIER,
            lifecycle_status=StaffLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 3. Users for Auth
        self.user_owner = PharmacyUser.objects.create_user(
            email=self.staff_owner.username,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacy Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.user_manager = PharmacyUser.objects.create_user(
            email=self.staff_manager.username,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Branch Manager Dhanmondi",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.user_cashier1 = PharmacyUser.objects.create_user(
            email=self.staff_cashier1.username,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier Rahim",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.user_cashier2 = PharmacyUser.objects.create_user(
            email=self.staff_cashier2.username,
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier Karim",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 4. Foreign Tenant & User
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p117-b-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-p117-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-b1-{self.suffix}",
            tenant=self.tenant_b,
            name="Banani Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )
        self.user_foreign = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@square.local",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Foreign Owner",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

    def test_01_valid_register_assignment_and_schema_validation(self):
        """Owner assigns register REG-01 to Cashier 1; matches JSON schema."""
        self.client.force_authenticate(user=self.user_owner)
        resp = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
                "notes": "Morning Shift Counter 1",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        data = resp.json()
        self.assertEqual(data["register_id"], "REG-01")
        self.assertEqual(data["cashier_id"], self.staff_cashier1.staff_id)
        self.assertEqual(data["cashier_name"], "Cashier Rahim")
        self.assertEqual(data["status"], "ACTIVE")
        self.assertEqual(data["opening_float_amount"], "500.00")
        self.assertIsNone(data["closing_float_amount"])

        # Validate with JSON schema contract
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=data, schema=schema)

    def test_02_concurrent_register_collision_rejected(self):
        """Register already assigned actively cannot be assigned to another cashier (409 Conflict)."""
        # First assignment
        self.client.force_authenticate(user=self.user_owner)
        res1 = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Attempt to assign same REG-01 to Cashier 2
        res2 = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier2.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res2.data.get("error_code"), "ERR_REGISTER_ALREADY_ASSIGNED")

    def test_03_concurrent_cashier_collision_rejected(self):
        """Cashier already actively assigned cannot be assigned to another register (409 Conflict)."""
        self.client.force_authenticate(user=self.user_owner)
        res1 = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # Attempt to assign Cashier 1 to REG-02 concurrently
        res2 = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-02",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res2.data.get("error_code"), "ERR_CASHIER_ALREADY_ASSIGNED")

    def test_04_branch_manager_cross_branch_violation(self):
        """Branch manager cannot assign register in foreign branch (403 Forbidden)."""
        self.client.force_authenticate(user=self.user_manager)
        resp = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a2.branch_id,  # Foreign branch
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data.get("error_code"), "ERR_CROSS_BRANCH_VIOLATION")

    def test_05_cashier_assign_other_staff_forbidden(self):
        """Cashier cannot assign register to another cashier (403 Forbidden)."""
        self.client.force_authenticate(user=self.user_cashier1)
        resp = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-02",
                "cashier_id": self.staff_cashier2.staff_id,  # Other cashier
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data.get("error_code"), "ERR_PERMISSION_DENIED")

    def test_06_cashier_self_assignment_success(self):
        """Cashier can bind self to unassigned register (201 Created)."""
        self.client.force_authenticate(user=self.user_cashier2)
        resp = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-03",
                "cashier_id": self.staff_cashier2.staff_id,  # Self
                "opening_float_amount": "300.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data.get("register_id"), "REG-03")
        self.assertEqual(resp.data.get("cashier_id"), self.staff_cashier2.staff_id)

    def test_07_cross_tenant_access_forbidden(self):
        """Foreign tenant user cannot assign register in Tenant A (403 Forbidden)."""
        self.client.force_authenticate(user=self.user_foreign)
        resp = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.data.get("error_code"), "ERR_CROSS_TENANT_VIOLATION")

    def test_08_suspended_tenant_assignment_blocked(self):
        """Suspended tenant subscription blocks register assignment."""
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.user_owner)
        resp = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        data = resp.json()
        self.assertEqual(data.get("error_code"), "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    def test_09_drawer_closing_and_subsequent_reassignment(self):
        """Closing drawer records closing float, releases lock, allows new assignment."""
        self.client.force_authenticate(user=self.user_owner)
        res1 = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier1.staff_id,
                "opening_float_amount": "500.00",
            },
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        assignment_id = res1.data["assignment_id"]

        # Close the drawer
        res_close = self.client.post(
            f"/api/v2/registers/assignments/{assignment_id}/close/",
            {
                "closing_float_amount": "750.50",
                "notes": "Handover to evening shift completed.",
            },
            format="json",
        )
        self.assertEqual(res_close.status_code, status.HTTP_200_OK)
        closed_data = res_close.json()
        self.assertEqual(closed_data["status"], "CLOSED")
        self.assertEqual(closed_data["closing_float_amount"], "750.50")
        self.assertIsNotNone(closed_data["closed_at"])

        # Subsequent assignment to REG-01 is now permitted without 409 conflict
        res2 = self.client.post(
            "/api/v2/registers/assignments/",
            {
                "branch_id": self.branch_a1.branch_id,
                "register_id": "REG-01",
                "cashier_id": self.staff_cashier2.staff_id,
                "opening_float_amount": "750.50",
            },
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.data["cashier_id"], self.staff_cashier2.staff_id)
