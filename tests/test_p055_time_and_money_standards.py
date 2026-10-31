"""
backend/tests/test_p055_time_and_money_standards.py
Architecture Decision: ADR-0055
Comprehensive test suite for Time and Money Standards:
- Exact Decimal representation (rejection of binary floats)
- Scale 2 quantization with ROUND_HALF_UP
- Aware UTC timestamps at rest (rejection of naive datetimes)
- Asia/Dhaka (UTC+6) display conversions and FEFO batch expiry
- Tenant authorization and cross-tenant barrier enforcement
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditLogEntry
from apps.core.models import Branch, Tenant
from apps.core.time_and_money import MoneyStandard, TimeStandard
from apps.identity.models import PharmacyUser


class TestP055TimeAndMoneyStandards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant, _ = Tenant.objects.get_or_create(
            tenant_id="tenant-p055-standard",
            defaults={"name": "P055 Standard Care", "subdomain": "p055-standard", "is_active": True},
        )
        cls.branch, _ = Branch.objects.get_or_create(
            branch_id="branch-p055-01",
            defaults={"tenant": cls.tenant, "name": "Standard Dispensary", "is_active": True},
        )
        cls.user, _ = PharmacyUser.objects.get_or_create(
            email="p055_pharmacist@pharmacy.bd",
            defaults={
                "phone_number": "+8801700000055",
                "full_name": "Pharmacist P055",
                "role": "PHARMACIST",
                "tenant_id": cls.tenant.tenant_id,
                "branch_id": cls.branch.branch_id,
            },
        )

    def setUp(self):
        self.client = APIClient()

    def test_pure_decimal_arithmetic_precision(self):
        """Verifies MoneyStandard enforces scale 2, ROUND_HALF_UP and rejects raw floats."""
        # 1. Rounding half up verification
        self.assertEqual(MoneyStandard.to_decimal("10.004"), Decimal("10.00"))
        self.assertEqual(MoneyStandard.to_decimal("10.005"), Decimal("10.01"))

        # 2. Rejection of raw floats
        with self.assertRaises(ValueError) as ctx:
            MoneyStandard.to_decimal(10.55)
        self.assertIn("Floating-point representation detected", str(ctx.exception))

        # 3. South Asian grouping
        self.assertEqual(MoneyStandard.format_currency(Decimal("152345.50"), locale="en-US"), "৳ 1,52,345.50")
        self.assertEqual(MoneyStandard.format_currency(Decimal("152345.50"), locale="bn-BD"), "৳১,৫২,৩৪৫.৫০")

    def test_pure_time_standard_utc_and_dhaka(self):
        """Verifies TimeStandard strictly accepts aware timestamps and converts to Dhaka."""
        # Aware UTC ISO string
        utc_dt = TimeStandard.ensure_aware_utc("2026-10-31T06:00:00Z")
        self.assertEqual(utc_dt.tzinfo, ZoneInfo("UTC"))

        # Conversion to Dhaka (+06:00) -> 12:00 PM
        dhaka_dt = TimeStandard.to_dhaka(utc_dt)
        self.assertEqual(dhaka_dt.hour, 12)
        self.assertEqual(dhaka_dt.minute, 0)

        # Naive datetime rejection
        naive = datetime(2026, 10, 31, 12, 0, 0)
        with self.assertRaises(ValueError) as ctx:
            TimeStandard.ensure_aware_utc(naive)
        self.assertIn("Naive datetime detected", str(ctx.exception))

    def test_batch_expiry_against_dhaka_date(self):
        """Verifies medicine batch expiry against current date in Asia/Dhaka."""
        ref_utc = datetime(2026, 10, 31, 10, 0, 0, tzinfo=ZoneInfo("UTC"))
        # Reference date in Dhaka is 2026-10-31 (16:00)
        self.assertTrue(TimeStandard.is_batch_expired("2026-10-30", reference_time_utc=ref_utc))
        self.assertFalse(TimeStandard.is_batch_expired("2026-11-01", reference_time_utc=ref_utc))

    def test_unauthenticated_request_rejected(self):
        """Demonstrates unauthenticated request is rejected with 401 Unauthorized."""
        resp = self.client.get("/api/v2/standards/time-money/policy/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cross_tenant_access_denied_and_audited(self):
        """Demonstrates cross-tenant mismatch returns 403 Forbidden and audits the incident."""
        self.client.force_authenticate(user=self.user)
        initial_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()

        resp = self.client.get(
            "/api/v2/standards/time-money/policy/",
            {"tenant_id": "alien-tenant-cross-violation"},
            HTTP_X_TENANT_ID=self.tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(resp.data.get("code"), ["ERR_PERMISSION_DENIED", "FORBIDDEN_CROSS_TENANT_ACCESS"])

        new_count = AuditLogEntry.objects.filter(action="SECURITY_INCIDENT_CROSS_TENANT").count()
        self.assertGreater(new_count, initial_count)

    def test_policy_endpoint_authenticated_success(self):
        """Verifies authenticated GET /api/v2/standards/time-money/policy/ returns 200 OK."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(
            "/api/v2/standards/time-money/policy/",
            HTTP_X_TENANT_ID=self.tenant.tenant_id,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("code"), "POLICY_RETRIEVED")
        policy = data.get("data", {})
        self.assertEqual(policy.get("currency_code"), "BDT")
        self.assertEqual(policy.get("monetary_scale"), 2)
        self.assertEqual(policy.get("rounding_strategy"), "ROUND_HALF_UP")
        self.assertEqual(policy.get("storage_timezone"), "UTC")
        self.assertEqual(policy.get("display_timezone"), "Asia/Dhaka")

    def test_invoice_calculation_exact_decimal_success(self):
        """Verifies deterministic Decimal invoice calculation through the API."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "items": [
                {
                    "sku_id": "MED-001",
                    "item_name": "Napa Extra 500mg",
                    "unit_price": "2.50",
                    "quantity": 10,
                    "discount_percent": "0.00",
                    "tax_percent": "5.00",
                },
                {
                    "sku_id": "MED-002",
                    "item_name": "Seclo 20mg Capsule",
                    "unit_price": "80.00",
                    "quantity": 2,
                    "discount_percent": "0.00",
                    "tax_percent": "5.00",
                },
            ],
            "invoice_discount": "10.00",
            "paid_amount": "200.00",
        }
        resp = self.client.post("/api/v2/standards/time-money/calculate-invoice/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        calc = data.get("data", {})
        self.assertEqual(calc.get("subtotal"), "185.00")
        self.assertEqual(calc.get("invoice_discount"), "10.00")
        self.assertEqual(calc.get("total_tax"), "9.25")
        self.assertEqual(calc.get("grand_total"), "184.25")
        self.assertEqual(calc.get("paid_amount"), "200.00")
        self.assertEqual(calc.get("change_amount"), "15.75")
        self.assertEqual(calc.get("due_amount"), "0.00")
        self.assertIn("display_at_dhaka", calc)

    def test_invoice_calculation_rejects_floats(self):
        """Verifies 422 Unprocessable Entity when raw float values are submitted."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "items": [
                {
                    "sku_id": "MED-001",
                    "unit_price": 2.50,  # RAW FLOAT!
                    "quantity": 10,
                }
            ],
        }
        resp = self.client.post("/api/v2/standards/time-money/calculate-invoice/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = resp.data
        self.assertFalse(data.get("success", False))
        self.assertEqual(data.get("code"), "ERR_VALIDATION_FAILED")

    def test_validate_money_endpoint_success(self):
        """Verifies POST /api/v2/standards/time-money/validate-money/ accepts valid Decimal & aware UTC."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "amount": "152345.50",
            "timestamp": "2026-10-31T06:00:00Z",
        }
        resp = self.client.post("/api/v2/standards/time-money/validate-money/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        self.assertTrue(data.get("success"))
        val_data = data.get("data", {})
        self.assertEqual(val_data.get("amount_decimal"), "152345.50")
        self.assertEqual(val_data.get("amount_formatted_en"), "৳ 1,52,345.50")
        self.assertEqual(val_data.get("amount_formatted_bn"), "৳১,৫২,৩৪৫.৫০")

    def test_validate_money_endpoint_rejects_negative_and_naive(self):
        """Verifies rejection of negative amount and naive timestamp."""
        self.client.force_authenticate(user=self.user)
        payload = {
            "tenant_id": self.tenant.tenant_id,
            "amount": "-50.00",
            "timestamp": "2026-10-31 12:00:00",  # Naive without offset/Z
        }
        resp = self.client.post("/api/v2/standards/time-money/validate-money/", payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = resp.data
        field_errors = data.get("field_errors", {})
        self.assertIn("amount", field_errors)
        self.assertIn("timestamp", field_errors)

    def test_verify_expiry_endpoint(self):
        """Verifies POST /api/v2/standards/time-money/verify-expiry/ correctly flags expired medicine."""
        self.client.force_authenticate(user=self.user)
        # 1. Past date
        payload_expired = {
            "tenant_id": self.tenant.tenant_id,
            "expiry_date": "2024-01-01",
            "reference_timestamp": "2026-10-31T06:00:00Z",
        }
        resp1 = self.client.post("/api/v2/standards/time-money/verify-expiry/", payload_expired, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_200_OK)
        self.assertTrue(resp1.data["data"]["is_expired"])
        self.assertEqual(resp1.data["data"]["action_required"], "QUARANTINE_BATCH")

        # 2. Future date
        payload_valid = {
            "tenant_id": self.tenant.tenant_id,
            "expiry_date": "2028-12-31",
            "reference_timestamp": "2026-10-31T06:00:00Z",
        }
        resp2 = self.client.post("/api/v2/standards/time-money/verify-expiry/", payload_valid, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertFalse(resp2.data["data"]["is_expired"])
        self.assertEqual(resp2.data["data"]["action_required"], "DISPENSE_ALLOWED")


if __name__ == "__main__":
    unittest.main()
