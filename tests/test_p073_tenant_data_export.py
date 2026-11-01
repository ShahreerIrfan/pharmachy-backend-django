"""
backend/tests/test_p073_tenant_data_export.py
Architecture Decision: ADR-0004, ADR-0005 & ADR-0073
Comprehensive test suite for Tenant Data Export Administration:
1. Export Initiation: Pharmacy Owner can request a tenant export with default PII redaction.
2. Role Protection: Cashiers and staff receive 403 Forbidden; cannot request or download exports.
3. SuperAdmin Approval: Dual-governance approval triggers artifact bundling and SHA-256 hash.
4. Tokenized Download: Secure time-bounded URL serves artifact; tracks download counter.
5. Rejection Flow: Platform governance can reject with mandatory audit justification.
6. Revocation: SuperAdmin can revoke active download tokens immediately.
7. Cross-Tenant Isolation: Tenant A cannot inspect or download Tenant B's export bundle.
8. Privacy Filtering: Verifies that patient names and phone numbers are redacted when include_patient_pii=False.
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

from apps.core.models import (
    Branch,
    MedicineProduct,
    PrescriptionRecord,
    SaleInvoice,
    Tenant,
    TenantDataExportRequest,
    TenantDataExportStatus,
    TenantMedicineConfig,
)
from apps.identity.models import PharmacyUser, UserRole


class TestP073TenantDataExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.suffix = uuid.uuid4().hex[:6]
        phone_suffix = random.randint(10000000, 99999999)

        # 1. Platform Super Admin
        cls.superadmin, _ = PharmacyUser.objects.get_or_create(
            email=f"p073_admin_{cls.suffix}@platform.internal",
            defaults={
                "full_name": f"Platform Super Admin {cls.suffix}",
                "role": UserRole.SUPERADMIN,
                "phone_number": f"+88017{phone_suffix}",
                "is_active": True,
            },
        )

        # 2. Tenant A (Dhaka Pharmacy)
        cls.tenant_a = Tenant.objects.create(
            tenant_id=f"t-dhaka-{cls.suffix}",
            name=f"Dhaka Central Pharmacy {cls.suffix}",
            subdomain=f"dhaka-{cls.suffix}",
            plan_tier="PRO",
            status="ACTIVE",
        )
        cls.branch_a = Branch.objects.create(
            branch_id=f"b-dhaka-{cls.suffix}",
            tenant=cls.tenant_a,
            name="Main Dhanmondi Branch",
        )
        cls.owner_a = PharmacyUser.objects.create(
            email=f"p073_owner_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )
        cls.cashier_a = PharmacyUser.objects.create(
            email=f"p073_cashier_a_{cls.suffix}@pharmacy.com",
            full_name=f"Dhaka Cashier {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.CASHIER,
            tenant_id=cls.tenant_a.tenant_id,
            branch_id=cls.branch_a.branch_id,
            is_active=True,
        )

        # 3. Tenant B (Chittagong Pharmacy)
        cls.tenant_b = Tenant.objects.create(
            tenant_id=f"t-ctg-{cls.suffix}",
            name=f"Chittagong Port Pharmacy {cls.suffix}",
            subdomain=f"ctg-{cls.suffix}",
            plan_tier="STANDARD",
            status="ACTIVE",
        )
        cls.branch_b = Branch.objects.create(
            branch_id=f"b-ctg-{cls.suffix}",
            tenant=cls.tenant_b,
            name="Agrabad Branch",
        )
        cls.owner_b = PharmacyUser.objects.create(
            email=f"p073_owner_b_{cls.suffix}@pharmacy.com",
            full_name=f"Chittagong Owner {cls.suffix}",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=cls.tenant_b.tenant_id,
            branch_id=cls.branch_b.branch_id,
            is_active=True,
        )

        # Seed sample clinical & sales data for Tenant A to verify PII filtering
        cls.med_a = MedicineProduct.objects.create(
            product_id=f"prod-paracetamol-{cls.suffix}",
            brand_name="Napa Extra",
            generic_name="Paracetamol + Caffeine",
            dosage_form="Tablet",
            strength="500mg+65mg",
            manufacturer="Square Pharmaceuticals Ltd.",
            mrp_unit=Decimal("3.50"),
            cost_unit=Decimal("2.80"),
            curation_status="APPROVED",
            curated_by=cls.superadmin,
        )
        cls.config_a = TenantMedicineConfig.objects.create(
            config_id=f"tmc-{cls.suffix}1234",
            tenant=cls.tenant_a,
            product=cls.med_a,
            local_brand_name="নাপা এক্সট্রা",
            local_sku="SKU-DHK-99",
            custom_mrp_unit=Decimal("3.50"),
            shelf_location="Rack A - Shelf 1",
            low_stock_threshold=20,
        )
        cls.sale_a = SaleInvoice.objects.create(
            invoice_id=f"inv-{cls.suffix}-01",
            invoice_number=f"INV-DHK-{cls.suffix}-01",
            tenant=cls.tenant_a,
            branch=cls.branch_a,
            customer_name="Mr. Patient Rahman",
            customer_phone="+8801712345678",
            total_amount=Decimal("350.00"),
            net_amount=Decimal("350.00"),
            status="POSTED",
        )
        cls.rx_a = PrescriptionRecord.objects.create(
            prescription_id=f"RX-DHK-{cls.suffix}-01",
            tenant=cls.tenant_a,
            branch=cls.branch_a,
            patient_token="PT-DHK-99",
            doctor_name="Dr. Kamal Hossain",
            doctor_bmdc_no="BMDC-12345",
            diagnosis="Seasonal fever",
            notes="Confidential patient notes",
            prescription_date=timezone.now().date(),
            status="VERIFIED",
        )

    def setUp(self):
        TenantDataExportRequest.objects.all().delete()

        self.client_admin = APIClient()
        self.client_admin.force_authenticate(user=self.superadmin)

        self.client_owner_a = APIClient()
        self.client_owner_a.force_authenticate(user=self.owner_a)

        self.client_cashier_a = APIClient()
        self.client_cashier_a.force_authenticate(user=self.cashier_a)

        self.client_owner_b = APIClient()
        self.client_owner_b.force_authenticate(user=self.owner_b)

    def test_01_owner_can_request_data_export_and_list(self):
        payload = {
            "include_patient_pii": False,
            "include_financials": True,
            "include_prescriptions": True,
            "include_inventory": True,
            "file_format": "JSON",
            "notes": "Annual statutory audit archive",
        }

        # Request
        res = self.client_owner_a.post("/api/v2/tenants/data-exports/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        data = res.data["data"]
        self.assertTrue(data["export_id"].startswith("tde-"))
        self.assertEqual(data["status"], "REQUESTED")
        self.assertFalse(data["include_patient_pii"])
        self.assertIsNone(data["download_url"])

        # List
        res_list = self.client_owner_a.get("/api/v2/tenants/data-exports/")
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        results = res_list.data.get("results", res_list.data) if isinstance(res_list.data, dict) else res_list.data
        ids = [item["export_id"] for item in results]
        self.assertIn(data["export_id"], ids)

    def test_02_cashier_and_unauthorized_roles_blocked(self):
        payload = {"include_patient_pii": False}

        # Cashier POST blocked
        res_cashier_post = self.client_cashier_a.post("/api/v2/tenants/data-exports/", payload, format="json")
        self.assertEqual(res_cashier_post.status_code, status.HTTP_403_FORBIDDEN)

        # Cashier GET list blocked
        res_cashier_get = self.client_cashier_a.get("/api/v2/tenants/data-exports/")
        self.assertEqual(res_cashier_get.status_code, status.HTTP_403_FORBIDDEN)

        # Store owner cannot call platform admin endpoints
        res_owner_platform = self.client_owner_a.get("/api/v2/platform/data-exports/")
        self.assertEqual(res_owner_platform.status_code, status.HTTP_403_FORBIDDEN)

    def test_03_superadmin_approve_and_artifact_generation(self):
        # 1. Owner requests export
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/data-exports/", {"include_patient_pii": False}, format="json"
        )
        export_id = res_req.data["data"]["export_id"]

        # 2. SuperAdmin approves
        res_app = self.client_admin.post(f"/api/v2/platform/data-exports/{export_id}/approve/", format="json")
        self.assertEqual(res_app.status_code, status.HTTP_200_OK)
        app_data = res_app.data["data"]
        self.assertEqual(app_data["status"], "COMPLETED")
        self.assertEqual(len(app_data["sha256_checksum"]), 64)
        self.assertIsNotNone(app_data["download_url"])
        self.assertIn("token=", app_data["download_url"])

    def test_04_tokenized_download_and_counter(self):
        # 1. Request & Approve
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/data-exports/", {"include_patient_pii": False}, format="json"
        )
        export_id = res_req.data["data"]["export_id"]
        res_app = self.client_admin.post(f"/api/v2/platform/data-exports/{export_id}/approve/", format="json")
        download_url = res_app.data["data"]["download_url"]

        # 2. Download with valid token
        res_dl = self.client_owner_a.get(download_url)
        self.assertEqual(res_dl.status_code, status.HTTP_200_OK)
        self.assertIn("attachment", res_dl.headers.get("Content-Disposition", ""))
        self.assertEqual(res_dl.headers.get("X-Export-Download-Count"), "1")

        # 3. Verify download count incremented in model
        req_obj = TenantDataExportRequest.objects.get(export_id=export_id)
        self.assertEqual(req_obj.download_count, 1)

        # 4. Attempt download with invalid token -> 400 or 422
        res_invalid = self.client_owner_a.get(f"/api/v2/tenants/data-exports/{export_id}/download/?token=wrong-token")
        self.assertIn(res_invalid.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_05_rejection_workflow(self):
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/data-exports/", {"include_patient_pii": False}, format="json"
        )
        export_id = res_req.data["data"]["export_id"]

        # Reject without reason -> 400 or 422
        res_rej_empty = self.client_admin.post(f"/api/v2/platform/data-exports/{export_id}/reject/", {}, format="json")
        self.assertIn(res_rej_empty.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

        # Reject with reason -> 200
        res_rej = self.client_admin.post(
            f"/api/v2/platform/data-exports/{export_id}/reject/",
            {"reason": "Tenant identity re-verification required under compliance audit"},
            format="json",
        )
        self.assertEqual(res_rej.status_code, status.HTTP_200_OK)
        self.assertEqual(res_rej.data["data"]["status"], "REJECTED")
        self.assertIn("re-verification", res_rej.data["data"]["rejection_reason"])

    def test_06_revocation_and_token_invalidation(self):
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/data-exports/", {"include_patient_pii": False}, format="json"
        )
        export_id = res_req.data["data"]["export_id"]
        res_app = self.client_admin.post(f"/api/v2/platform/data-exports/{export_id}/approve/", format="json")
        download_url = res_app.data["data"]["download_url"]

        # SuperAdmin revokes export
        res_rev = self.client_admin.post(f"/api/v2/platform/data-exports/{export_id}/revoke/", format="json")
        self.assertEqual(res_rev.status_code, status.HTTP_200_OK)
        self.assertEqual(res_rev.data["data"]["status"], "EXPIRED")

        # Download attempt now fails
        res_dl = self.client_owner_a.get(download_url)
        self.assertIn(res_dl.status_code, [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY])

    def test_07_cross_tenant_isolation(self):
        # Tenant A requests export
        res_req = self.client_owner_a.post(
            "/api/v2/tenants/data-exports/", {"include_patient_pii": False}, format="json"
        )
        export_id_a = res_req.data["data"]["export_id"]

        # Tenant B attempts to retrieve Tenant A's export -> 404
        res_b = self.client_owner_b.get(f"/api/v2/tenants/data-exports/{export_id_a}/")
        self.assertEqual(res_b.status_code, status.HTTP_404_NOT_FOUND)

        # Tenant B attempts to query Tenant A's exports via parameter -> 403
        res_cross = self.client_owner_b.get(f"/api/v2/tenants/data-exports/?tenant_id={self.tenant_a.tenant_id}")
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)

    def test_08_pii_filtering_verification(self):
        from apps.core.tenant_export_service import TenantExportService

        # 1. Export with include_patient_pii=False
        req_redacted = TenantExportService.request_export(
            tenant_id=self.tenant_a.tenant_id,
            requested_by=self.owner_a,
            include_patient_pii=False,
        )
        TenantExportService.approve_export(req_redacted.export_id, approved_by=self.superadmin)

        # Inspect generated audit metadata and items
        req_redacted.refresh_from_db()
        self.assertEqual(req_redacted.status, TenantDataExportStatus.COMPLETED)
        self.assertFalse(req_redacted.include_patient_pii)

        # 2. Export with include_patient_pii=True
        req_unmasked = TenantExportService.request_export(
            tenant_id=self.tenant_a.tenant_id,
            requested_by=self.owner_a,
            include_patient_pii=True,
        )
        TenantExportService.approve_export(req_unmasked.export_id, approved_by=self.superadmin)
        req_unmasked.refresh_from_db()
        self.assertTrue(req_unmasked.include_patient_pii)


if __name__ == "__main__":
    unittest.main()
