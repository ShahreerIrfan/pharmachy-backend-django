"""
Integration tests for P115: Staff performance metrics, sales, refunds,
reconciliation variances, and reporting freshness (ADR-0115).
"""

import json
import random
import uuid
from decimal import Decimal
from pathlib import Path

from django.test import Client, TestCase
from django.utils import timezone
from jsonschema import validate
from rest_framework import status

from apps.core.models import (
    Branch,
    BranchLifecycleStatus,
    PlanTier,
    StaffLifecycleStatus,
    StaffMember,
    StaffRole,
    Tenant,
)
from apps.core.plan_catalogue_service import PlanCatalogueService
from apps.core.staff_performance_service import StaffPerformanceService
from apps.identity.models import PharmacyUser, UserRole

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "shared" / "contracts" / "schemas" / "staff_performance_metrics.schema.json"


class StaffPerformanceMetricsIntegrationTests(TestCase):
    """End-to-end integration tests for P115 Staff Performance Metrics."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        PlanCatalogueService.ensure_canonical_plans_seeded()

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.suffix = uuid.uuid4().hex[:8]

        # 1. Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p115-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-p115-{self.suffix}",
            plan_tier=PlanTier.PRO,
            status="ACTIVE",
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

        # 2. Staff Members in Tenant A
        self.staff_c1 = StaffMember.objects.create(
            staff_id=f"stf-c1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            username=f"cashier1_{self.suffix}@lazz.local",
            full_name="Kader Cashier",
            role=StaffRole.CASHIER,
            mobile_number=f"01700{random.randint(100000, 999999)}",
            lifecycle_status=StaffLifecycleStatus.ACTIVE,
            is_active=True,
        )
        self.staff_c2 = StaffMember.objects.create(
            staff_id=f"stf-c2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a2,
            username=f"cashier2_{self.suffix}@lazz.local",
            full_name="Salma Cashier",
            role=StaffRole.CASHIER,
            mobile_number=f"01700{random.randint(100000, 999999)}",
            lifecycle_status=StaffLifecycleStatus.ACTIVE,
            is_active=True,
        )

        # 3. Users for Auth
        self.password = "StrongPassword123!"

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Pharmacy Owner A",
            password=self.password,
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.manager_a1 = PharmacyUser.objects.create_user(
            email=f"mgr1_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Manager Dhanmondi",
            password=self.password,
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        self.user_c1 = PharmacyUser.objects.create_user(
            email=self.staff_c1.username,
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Kader Cashier",
            password=self.password,
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 4. Inactive Staff User
        self.user_inactive = PharmacyUser.objects.create_user(
            email=f"inactive_{self.suffix}@lazz.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Terminated Staff",
            password=self.password,
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.user_inactive.is_active = False
        self.user_inactive.save()

        # 5. Tenant B (Foreign)
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p115-b-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"square-p115-{self.suffix}",
            plan_tier=PlanTier.BASIC,
            status="ACTIVE",
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@square.local",
            phone_number=f"01700{random.randint(100000, 999999)}",
            full_name="Foreign Owner B",
            password=self.password,
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 6. Seed Snapshots
        self.today = timezone.now().date()
        StaffPerformanceService.record_snapshot(
            tenant=self.tenant_a,
            staff_member=self.staff_c1,
            metric_date=self.today,
            total_sales_count=40,
            total_sales_amount=Decimal("120000.00"),
            total_refunds_count=2,
            total_refunds_amount=Decimal("3000.00"),
            reconciliation_variance_amount=Decimal("0.00"),
            branch=self.branch_a1,
        )
        StaffPerformanceService.record_snapshot(
            tenant=self.tenant_a,
            staff_member=self.staff_c2,
            metric_date=self.today,
            total_sales_count=35,
            total_sales_amount=Decimal("95000.00"),
            total_refunds_count=1,
            total_refunds_amount=Decimal("1500.00"),
            reconciliation_variance_amount=Decimal("-250.00"),
            branch=self.branch_a2,
        )

    def _login(self, user):
        resp = self.client.post(
            "/api/v2/auth/login/",
            data={"identifier": user.email, "password": self.password},
            content_type="application/json",
            HTTP_X_DEVICE_ID="test-device",
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        return resp.json()["access"]

    def test_owner_lists_cross_branch_metrics_with_schema_validation(self):
        """Pharmacy Owner can view all staff performance metrics across all branches."""
        token = self._login(self.owner_a)
        resp = self.client.get(
            "/api/v2/analytics/staff-performance/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data["count"], 2)

        first_metric = data["results"][0]
        self.assertIn("total_sales_amount", first_metric)
        self.assertIn("net_revenue", first_metric)
        self.assertIn("reporting_freshness_seconds", first_metric)

        # Schema Validation
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validate(instance=first_metric, schema=schema)

    def test_branch_manager_scoped_to_assigned_branch(self):
        """Branch Manager sees only staff within their assigned branch."""
        token = self._login(self.manager_a1)
        resp = self.client.get(
            "/api/v2/analytics/staff-performance/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["branch_id"], self.branch_a1.branch_id)
        self.assertEqual(results[0]["staff_id"], self.staff_c1.staff_id)

    def test_branch_manager_cross_branch_query_denied(self):
        """Branch Manager querying another branch is denied with ERR_CROSS_BRANCH_VIOLATION."""
        token = self._login(self.manager_a1)
        resp = self.client.get(
            f"/api/v2/analytics/staff-performance/?branch_id={self.branch_a2.branch_id}",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_CROSS_BRANCH_VIOLATION")

    def test_cashier_strictly_restricted_to_self_metrics(self):
        """Cashier sees only their own performance metrics."""
        token = self._login(self.user_c1)
        resp = self.client.get(
            "/api/v2/analytics/staff-performance/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["staff_id"], self.staff_c1.staff_id)

    def test_cashier_querying_other_staff_denied(self):
        """Cashier attempting to view another cashier's metrics is denied."""
        token = self._login(self.user_c1)
        resp = self.client.get(
            f"/api/v2/analytics/staff-performance/?staff_id={self.staff_c2.staff_id}",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_PERMISSION_DENIED")

    def test_cross_tenant_access_denied(self):
        """Tenant B owner querying Tenant A metrics is rejected with ERR_CROSS_TENANT_VIOLATION."""
        token_b = self._login(self.owner_b)
        resp = self.client.get(
            f"/api/v2/analytics/staff-performance/?tenant_id={self.tenant_a.tenant_id}",
            HTTP_AUTHORIZATION=f"Bearer {token_b}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

    def test_suspended_tenant_subscription_blocks_metrics(self):
        """Suspended tenant subscription blocks metrics access."""
        token = self._login(self.owner_a)
        self.tenant_a.status = "SUSPENDED"
        self.tenant_a.save()

        resp = self.client.get(
            "/api/v2/analytics/staff-performance/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(resp.json()["error_code"], "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    def test_inactive_staff_role_denied(self):
        """Deactivated staff account cannot query performance metrics."""
        self.user_inactive.is_active = True  # temporarily enable for JWT issuance
        self.user_inactive.save()
        token = self._login(self.user_inactive)
        self.user_inactive.is_active = False  # deactivate
        self.user_inactive.save()

        resp = self.client.get(
            "/api/v2/analytics/staff-performance/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

