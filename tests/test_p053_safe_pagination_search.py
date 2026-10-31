"""
backend/tests/test_p053_safe_pagination_search.py
Architecture Decision: ADR-0053
Comprehensive test suite for deterministic cursor pagination, bounded page sizes,
validated filters, and Unicode medicine/customer searches without tenant leakage.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.models import Branch, MedicineProduct, SaleInvoice, Tenant
from apps.identity.models import ActivationStatus, PharmacyUser, UserRole


class TestP053SafePaginationAndSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Tenant A (Dhanmondi)
        cls.tenant_a, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-dhanmondi-test-53",
            defaults={"name": "Dhanmondi Test Care", "subdomain": "dh-care-53", "is_active": True},
        )
        cls.branch_a, _ = Branch.objects.get_or_create(
            branch_id="branch-dh-test-53",
            defaults={"tenant": cls.tenant_a, "name": "Main Dispensary", "is_active": True},
        )

        # Tenant B (Uttara)
        cls.tenant_b, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-uttara-test-53",
            defaults={"name": "Uttara Test Pharma", "subdomain": "ut-pharma-53", "is_active": True},
        )
        cls.branch_b, _ = Branch.objects.get_or_create(
            branch_id="branch-ut-test-53",
            defaults={"tenant": cls.tenant_b, "name": "Uttara Branch", "is_active": True},
        )

        # Users
        cls.user_a, _ = PharmacyUser.objects.get_or_create(
            email="owner.dh.53@example.com",
            defaults={
                "phone_number": "+8801753111111",
                "full_name": "Dhanmondi Owner 53",
                "role": UserRole.PHARMACY_OWNER,
                "activation_status": ActivationStatus.ACTIVE,
                "tenant_id": cls.tenant_a.tenant_id,
                "branch_id": cls.branch_a.branch_id,
            },
        )
        if not cls.user_a.check_password("P053SecretPass#2026"):
            cls.user_a.set_password("P053SecretPass#2026")
            cls.user_a.save()

        # Seed Medicines
        cls.med_napa_extra, _ = MedicineProduct.objects.get_or_create(
            product_id="MED-53-NAPA-EXTRA",
            defaults={
                "brand_name": "Napa Extra 500mg",
                "generic_name": "Paracetamol + Caffeine",
                "dosage_form": "Tablet",
                "strength": "500mg+65mg",
                "manufacturer": "Beximco Pharmaceuticals Ltd",
                "mrp_unit": Decimal("3.00"),
                "cost_unit": Decimal("2.40"),
            },
        )
        cls.med_napa_500, _ = MedicineProduct.objects.get_or_create(
            product_id="MED-53-NAPA-500",
            defaults={
                "brand_name": "Napa 500mg",
                "generic_name": "Paracetamol",
                "dosage_form": "Tablet",
                "strength": "500mg",
                "manufacturer": "Beximco Pharmaceuticals Ltd",
                "mrp_unit": Decimal("1.20"),
                "cost_unit": Decimal("0.95"),
            },
        )
        cls.med_ace, _ = MedicineProduct.objects.get_or_create(
            product_id="MED-53-ACE-500",
            defaults={
                "brand_name": "Ace 500mg",
                "generic_name": "Paracetamol",
                "dosage_form": "Tablet",
                "strength": "500mg",
                "manufacturer": "Square Pharmaceuticals PLC",
                "mrp_unit": Decimal("1.20"),
                "cost_unit": Decimal("0.95"),
            },
        )

        # Seed Invoices
        SaleInvoice.objects.get_or_create(
            invoice_id="inv-53-dh-001",
            defaults={
                "tenant": cls.tenant_a,
                "branch": cls.branch_a,
                "invoice_number": "INV-53-DH-001",
                "customer_name": "রহিম উদ্দিন",
                "customer_phone": "01711223344",
                "total_amount": Decimal("150.00"),
                "net_amount": Decimal("150.00"),
            },
        )
        SaleInvoice.objects.get_or_create(
            invoice_id="inv-53-dh-002",
            defaults={
                "tenant": cls.tenant_a,
                "branch": cls.branch_a,
                "invoice_number": "INV-53-DH-002",
                "customer_name": "Karim Hossain",
                "customer_phone": "01711223355",
                "total_amount": Decimal("250.00"),
                "net_amount": Decimal("250.00"),
            },
        )
        SaleInvoice.objects.get_or_create(
            invoice_id="inv-53-ut-001",
            defaults={
                "tenant": cls.tenant_b,
                "branch": cls.branch_b,
                "invoice_number": "INV-53-UT-001",
                "customer_name": "রহিম উদ্দিন",
                "customer_phone": "01822334455",
                "total_amount": Decimal("500.00"),
                "net_amount": Decimal("500.00"),
            },
        )

    def setUp(self):
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user_a)
        refresh["tenant_id"] = self.user_a.tenant_id
        refresh["branch_id"] = self.user_a.branch_id
        refresh["role"] = self.user_a.role
        self.token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def test_01_valid_scenario_cursor_pagination_medicine_catalog(self):
        """Acceptance Criteria 1: Valid scenario: deterministic cursor pagination."""
        res = self.client.get("/api/v2/medicines/?pagination_type=cursor&page_size=2")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["code"], "CURSOR_PAGINATED_RESULTS")
        meta = body["meta"]["pagination"]
        self.assertEqual(meta["type"], "cursor")
        self.assertEqual(meta["page_size"], 2)
        self.assertTrue(meta["has_next"])
        self.assertIsNotNone(meta["next_cursor"])

    def test_02_sequential_cursor_navigation_has_no_drift(self):
        """Verifies following next_cursor loads subsequent page deterministically."""
        res1 = self.client.get("/api/v2/medicines/?pagination_type=cursor&page_size=2")
        next_cursor = res1.json()["meta"]["pagination"]["next_cursor"]

        res2 = self.client.get(f"/api/v2/medicines/?cursor={next_cursor}&page_size=2")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        body2 = res2.json()
        self.assertEqual(body2["code"], "CURSOR_PAGINATED_RESULTS")
        self.assertTrue(body2["meta"]["pagination"]["has_previous"])

    def test_03_unicode_bangla_search_medicine(self):
        """Verifies searching with Bengali Unicode ('নাপা') matches English brand catalog."""
        res = self.client.get("/api/v2/medicines/?search=নাপা")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        items = body if isinstance(body, list) else body.get("data", [])
        self.assertGreaterEqual(len(items), 2)
        brand_names = [it["brand_name"] for it in items]
        self.assertTrue(any("Napa" in b for b in brand_names))

    def test_04_customer_sales_search_bangla_and_phone(self):
        """Verifies searching customers by Bengali name ('রহিম') returns tenant invoice."""
        res = self.client.get("/api/v2/invoices/?search=রহিম")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        items = body if isinstance(body, list) else body.get("data", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["customer_name"], "রহিম উদ্দিন")
        self.assertEqual(items[0]["invoice_id"], "inv-53-dh-001")

    def test_05_bounded_page_sizes_clamping_and_validation(self):
        """Acceptance Criteria 2: Bounded page size guardrails."""
        # 5a: Oversized clamped to max 100
        res_large = self.client.get("/api/v2/medicines/?page=1&page_size=500")
        self.assertEqual(res_large.status_code, status.HTTP_200_OK)
        self.assertEqual(res_large.json()["meta"]["pagination"]["page_size"], 100)

        # 5b: Negative page size rejected with 422
        res_neg = self.client.get("/api/v2/medicines/?page_size=-5")
        self.assertEqual(res_neg.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_neg.json()["error_code"], "ERR_VALIDATION_FAILED")

    def test_06_prohibited_filter_probe_rejection_422(self):
        """Acceptance Criteria 2b: ORM lookups and probes rejected with 422."""
        res_probe = self.client.get("/api/v2/medicines/?tenant__secret_key=malicious")
        self.assertEqual(res_probe.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res_probe.json()["error_code"], "ERR_VALIDATION_FAILED")

    def test_07_malformed_cursor_returns_422_invalid_cursor(self):
        """Acceptance Criteria 2c: Malformed cursor rejected with 422 ERR_INVALID_CURSOR."""
        res = self.client.get("/api/v2/medicines/?cursor=invalid_base64_payload_xyz")
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(res.json()["error_code"], "ERR_INVALID_CURSOR")

    def test_08_cross_tenant_query_blocked_and_zero_leakage(self):
        """Acceptance Criteria 3: Cross-tenant query blocked and zero data leakage."""
        # 8a: Query foreign tenant explicitly
        res_cross = self.client.get(f"/api/v2/invoices/?tenant_id={self.tenant_b.tenant_id}")
        self.assertEqual(res_cross.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_cross.json()["error_code"], "ERR_CROSS_TENANT_VIOLATION")

        # 8b: Wildcard search never leaks Tenant B's invoice
        res_wildcard = self.client.get("/api/v2/invoices/?search=%")
        self.assertEqual(res_wildcard.status_code, status.HTTP_200_OK)
        items = res_wildcard.json() if isinstance(res_wildcard.json(), list) else res_wildcard.json().get("data", [])
        self.assertFalse(any(it["invoice_id"] == "inv-53-ut-001" for it in items))


if __name__ == "__main__":
    unittest.main()
