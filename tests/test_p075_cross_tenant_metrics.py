"""
backend/tests/test_p075_cross_tenant_metrics.py

Architecture Decision: ADR-0075
Automated Integration Tests for Cross-Tenant Metrics Safe Aggregation & Benchmarks.
Validates k-anonymity privacy suppression, zero-PHI guarantees, RBAC separation,
and cross-tenant isolation.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import (
    Branch,
    PrescriptionRecord,
    SaleInvoice,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP075CrossTenantMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p075_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{phone_suffix}",
                "is_active": True,
            },
        )

        # 2. Tenant A: Dhaka, PRO tier
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-dhaka-a-{cls.suffix}",
            name=f"Dhaka Central Pharmacy {cls.suffix}",
            subdomain=f"dhaka-a-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"b-dhaka-a-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Dhanmondi Branch",
            address="House 12, Road 4, Dhanmondi, DHAKA",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p075_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Owner A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"p075_cashier_a_{cls.suffix}@pharmacy.com",
            full_name=f"Cashier A {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )

        # 3. Tenant B: Dhaka, STANDARD tier
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-dhaka-b-{cls.suffix}",
            name=f"Dhaka North Pharmacy {cls.suffix}",
            subdomain=f"dhaka-b-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
        )
        cls.branch_b = Branch.objects.create(
            branch_id=f"b-dhaka-b-{cls.suffix}",
            tenant=cls.tenant_b,
            name="Gulshan Branch",
            address="Plot 5, Gulshan Avenue, DHAKA",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p075_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Owner B {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_b.tenant_id,
            branch_id=cls.branch_b.branch_id,
            is_active=True,
        )

        # 4. Tenant C: Sylhet, BASIC tier
        cls.tenant_c = Tenant.objects.create(
            tenant_id=f"t-sylhet-c-{cls.suffix}",
            name=f"Sylhet Local Dispensary {cls.suffix}",
            subdomain=f"sylhet-c-{cls.suffix}",
            plan_tier="BASIC",
            status="ACTIVE",
        )
        cls.branch_c = Branch.objects.create(
            branch_id=f"b-sylhet-c-{cls.suffix}",
            tenant=cls.tenant_c,
            name="Zindabazar Branch",
            address="Zindabazar Main Road, SYLHET",
        )

        # Activity Seeding:
        # Tenant A: 2 prescriptions (1 antibiotic), 1 invoice for 500 BDT
        PrescriptionRecord.objects.create(
            prescription_id=f"rx-{cls.suffix}-a1",
            tenant=cls.tenant_a,
            branch=cls.branch_a,
            patient_token=f"pt-a1-{cls.suffix}",
            doctor_name=f"Dr. Confidentia Doctor A1 {cls.suffix}",
            doctor_bmdc_no="BMDC-9901",
            diagnosis="Bacterial Infection - Needs Amoxicillin Antibiotic",
            notes="Antibiotic course prescribed",
            prescription_date=timezone.now().date(),
            status="VERIFIED",
        )
        PrescriptionRecord.objects.create(
            prescription_id=f"rx-{cls.suffix}-a2",
            tenant=cls.tenant_a,
            branch=cls.branch_a,
            patient_token=f"pt-a2-{cls.suffix}",
            doctor_name=f"Dr. General Physician A2 {cls.suffix}",
            doctor_bmdc_no="BMDC-9902",
            diagnosis="Hypertension routine check",
            notes="Maintenance refill",
            prescription_date=timezone.now().date(),
            status="VERIFIED",
        )
        SaleInvoice.objects.create(
            invoice_id=f"inv-{cls.suffix}-a1",
            invoice_number=f"INV-A-{cls.suffix}-01",
            tenant=cls.tenant_a,
            branch=cls.branch_a,
            customer_name=f"Confidential Patient Name A1 {cls.suffix}",
            customer_phone=f"+88017{random.randint(10000000, 99999999)}",
            total_amount=Decimal("500.00"),
            net_amount=Decimal("500.00"),
            status="POSTED",
        )

        # Tenant B: 1 prescription (general), 1 invoice for 300 BDT
        PrescriptionRecord.objects.create(
            prescription_id=f"rx-{cls.suffix}-b1",
            tenant=cls.tenant_b,
            branch=cls.branch_b,
            patient_token=f"pt-b1-{cls.suffix}",
            doctor_name=f"Dr. Physician B1 {cls.suffix}",
            doctor_bmdc_no="BMDC-9903",
            diagnosis="Seasonal fever",
            prescription_date=timezone.now().date(),
            status="VERIFIED",
        )
        SaleInvoice.objects.create(
            invoice_id=f"inv-{cls.suffix}-b1",
            invoice_number=f"INV-B-{cls.suffix}-01",
            tenant=cls.tenant_b,
            branch=cls.branch_b,
            customer_name=f"Confidential Patient Name B1 {cls.suffix}",
            customer_phone=f"+88017{random.randint(10000000, 99999999)}",
            total_amount=Decimal("300.00"),
            net_amount=Decimal("300.00"),
            status="POSTED",
        )

        # Tenant C: 1 prescription, 1 invoice for 200 BDT in Sylhet
        PrescriptionRecord.objects.create(
            prescription_id=f"rx-{cls.suffix}-c1",
            tenant=cls.tenant_c,
            branch=cls.branch_c,
            patient_token=f"pt-c1-{cls.suffix}",
            doctor_name=f"Dr. Physician C1 {cls.suffix}",
            doctor_bmdc_no="BMDC-9904",
            diagnosis="Allergic rhinitis",
            prescription_date=timezone.now().date(),
            status="VERIFIED",
        )
        SaleInvoice.objects.create(
            invoice_id=f"inv-{cls.suffix}-c1",
            invoice_number=f"INV-C-{cls.suffix}-01",
            tenant=cls.tenant_c,
            branch=cls.branch_c,
            customer_name=f"Confidential Patient Name C1 {cls.suffix}",
            customer_phone=f"+88017{random.randint(10000000, 99999999)}",
            total_amount=Decimal("200.00"),
            net_amount=Decimal("200.00"),
            status="POSTED",
        )

    def setUp(self):
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(user=self.superadmin)

        self.client_owner_a = APIClient()
        self.client_owner_a.force_authenticate(user=self.owner_a)

        self.client_owner_b = APIClient()
        self.client_owner_b.force_authenticate(user=self.owner_b)

        self.client_cashier_a = APIClient()
        self.client_cashier_a.force_authenticate(user=self.cashier_a)

    def test_01_superadmin_generate_metrics_snapshot_success(self):
        res = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {
                "metric_period": "MONTHLY",
                "min_cohort_threshold": 2,
                "notes": "End-to-end integration test aggregation",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.data["data"]

        self.assertTrue(data["snapshot_id"].startswith("ctm-"))
        self.assertEqual(data["metric_period"], "MONTHLY")
        self.assertEqual(data["status"], "DRAFT")
        self.assertTrue(data["privacy_filter_applied"])
        self.assertGreaterEqual(data["contributing_tenants_count"], 3)

        # Clinical metrics check
        clinical = data["clinical_metrics"]
        self.assertGreaterEqual(clinical["total_prescriptions"], 4)
        self.assertIn("antibiotic_dispensing_ratio_pct", clinical)

        # Financial metrics check
        financial = data["financial_metrics"]
        self.assertEqual(financial["currency"], "BDT")
        self.assertGreaterEqual(Decimal(financial["total_dispensing_volume_bdt"]), Decimal("1000.00"))

    def test_02_k_anonymity_privacy_filter_cohort_suppression(self):
        # We set min_cohort_threshold = 2
        # DHAKA has 2 contributing tenants (A and B) -> must be UN-SUPPRESSED
        # SYLHET has only 1 contributing tenant (C) -> must be SUPPRESSED
        res = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {
                "metric_period": "MONTHLY",
                "min_cohort_threshold": 2,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        cohorts = res.data["data"]["cohort_breakdowns"]

        dhaka_cohort = next((c for c in cohorts if c["cohort_key"] == "DHAKA"), None)
        self.assertIsNotNone(dhaka_cohort)
        self.assertFalse(dhaka_cohort["is_suppressed"])
        self.assertIsNotNone(dhaka_cohort["summary"])
        self.assertGreaterEqual(dhaka_cohort["contributing_tenants_count"], 2)

        sylhet_cohort = next((c for c in cohorts if c["cohort_key"] == "SYLHET"), None)
        self.assertIsNotNone(sylhet_cohort)
        # Suppressed because 1 < 2
        self.assertTrue(sylhet_cohort["is_suppressed"])
        self.assertIsNone(sylhet_cohort["summary"])

    @classmethod
    def tearDownClass(cls):
        try:
            Tenant.objects.filter(
                tenant_id__in=[cls.tenant_a.tenant_id, cls.tenant_b.tenant_id, cls.tenant_c.tenant_id]
            ).delete()
        except Exception:
            pass
        super().tearDownClass()

    def test_03_zero_phi_guarantee_in_response(self):
        res = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {"metric_period": "MONTHLY"},
            format="json",
        )
        raw_response = str(res.data)

        # Assert zero patient PHI leakage
        self.assertNotIn("Confidential Patient", raw_response)
        self.assertNotIn("patient_name", raw_response)
        self.assertNotIn("patient_phone", raw_response)
        self.assertNotIn("patient_mobile", raw_response)
        self.assertNotIn("doctor_name", raw_response)

    def test_04_publish_and_archive_snapshot_lifecycle(self):
        res_create = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {"metric_period": "MONTHLY"},
            format="json",
        )
        snapshot_id = res_create.data["data"]["snapshot_id"]

        # Publish snapshot
        res_publish = self.client_admin.post(
            f"/api/v2/platform/cross-tenant-metrics/{snapshot_id}/publish/",
            format="json",
        )
        self.assertEqual(res_publish.status_code, status.HTTP_200_OK)
        self.assertEqual(res_publish.data["data"]["status"], "PUBLISHED")
        self.assertIsNotNone(res_publish.data["data"]["published_at"])

        # Archive snapshot
        res_archive = self.client_admin.post(
            f"/api/v2/platform/cross-tenant-metrics/{snapshot_id}/archive/",
            format="json",
        )
        self.assertEqual(res_archive.status_code, status.HTTP_200_OK)
        self.assertEqual(res_archive.data["data"]["status"], "ARCHIVED")

        # Attempt to publish ARCHIVED snapshot must fail
        res_republish = self.client_admin.post(
            f"/api/v2/platform/cross-tenant-metrics/{snapshot_id}/publish/",
            format="json",
        )
        self.assertIn(res_republish.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_05_input_validation_errors(self):
        # Invalid date range (start > end)
        res_date = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {
                "start_date": "2026-11-30",
                "end_date": "2026-11-01",
            },
            format="json",
        )
        self.assertIn(res_date.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Invalid metric_period
        res_period = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {"metric_period": "HOURLY_INVALID"},
            format="json",
        )
        self.assertIn(res_period.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Invalid threshold < 2
        res_thresh = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {"min_cohort_threshold": 1},
            format="json",
        )
        self.assertIn(res_thresh.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_06_platform_admin_rbac_protection(self):
        # Pharmacy Owner cannot manage platform cross-tenant metrics
        res_owner = self.client_owner_a.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {"metric_period": "MONTHLY"},
            format="json",
        )
        self.assertEqual(res_owner.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("PLATFORM_ACCESS_DENIED", str(res_owner.data))

        # Cashier cannot view platform cross-tenant metrics
        res_cashier = self.client_cashier_a.get(
            "/api/v2/platform/cross-tenant-metrics/",
            format="json",
        )
        self.assertEqual(res_cashier.status_code, status.HTTP_403_FORBIDDEN)

    def test_07_tenant_platform_benchmarks_and_isolation(self):
        # Ensure at least one published snapshot exists
        res_create = self.client_admin.post(
            "/api/v2/platform/cross-tenant-metrics/",
            {"metric_period": "MONTHLY"},
            format="json",
        )
        snapshot_id = res_create.data["data"]["snapshot_id"]
        self.client_admin.post(f"/api/v2/platform/cross-tenant-metrics/{snapshot_id}/publish/")

        # Pharmacy Owner A retrieves own benchmarks
        res_bench = self.client_owner_a.get("/api/v2/analytics/platform-benchmarks/")
        self.assertEqual(res_bench.status_code, status.HTTP_200_OK)
        bench_data = res_bench.data["data"]

        self.assertEqual(bench_data["tenant_id"], self.tenant_a.tenant_id)
        self.assertIn("tenant_metrics", bench_data)
        self.assertIn("platform_average", bench_data)
        self.assertTrue(bench_data["k_anonymity_guarantee"])

        # Cross-Tenant Violation: Owner A attempts to inspect Tenant B's benchmarks
        res_cross = self.client_owner_a.get(
            f"/api/v2/analytics/platform-benchmarks/?tenant_id={self.tenant_b.tenant_id}"
        )
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("CROSS_TENANT_ACCESS_FORBIDDEN", str(res_cross.data))

        # Cashier A attempts to view benchmarks -> Forbidden
        res_cashier_bench = self.client_cashier_a.get("/api/v2/analytics/platform-benchmarks/")
        self.assertEqual(res_cashier_bench.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("BENCHMARK_ACCESS_DENIED", str(res_cashier_bench.data))

    def test_08_audit_logging(self):
        # Check audit log entries
        agg_logs = AuditLogEntry.objects.filter(action="CROSS_TENANT_METRICS_AGGREGATED")
        self.assertTrue(agg_logs.exists())

        bench_logs = AuditLogEntry.objects.filter(action="TENANT_BENCHMARK_VIEWED")
        self.assertTrue(bench_logs.exists())


if __name__ == "__main__":
    unittest.main()
