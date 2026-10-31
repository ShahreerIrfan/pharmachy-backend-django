"""
backend/tests/test_p046_input_validation_contracts.py
Architecture Decision: ADR-0014 & ADR-0046
Backend Acceptance Tests for Input Validation Contracts, Domain Enums,
Decimal Monetary Constraints, Cold-Chain Invariants, and Cross-Tenant Integrity.
"""

from __future__ import annotations

import datetime
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

from django.test import Client, TestCase

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
)


class BackendTestP046InputValidationContracts(TestCase):
    """
    Acceptance test suite for DRF endpoints and validation invariants of ADR-0046.
    """

    def setUp(self):
        self.client = Client()
        self.tenant_a = "tenant-dhanmondi-care"
        self.tenant_b = "tenant-uttara-pharma"

        self.t_a = Tenant.objects.create(tenant_id=self.tenant_a, name="Dhanmondi Care", subdomain="dhanmondi-val")
        self.t_b = Tenant.objects.create(tenant_id=self.tenant_b, name="Uttara Pharma", subdomain="uttara-val")

        self.b_a = Branch.objects.create(
            branch_id="b-dhanmondi-01", tenant=self.t_a, name="Main Branch", license_number="DGDA-D-101"
        )
        self.b_b = Branch.objects.create(
            branch_id="b-uttara-01", tenant=self.t_b, name="Uttara Branch", license_number="DGDA-U-102"
        )

        self.product = MedicineProduct.objects.create(
            product_id="prod-napa-500",
            brand_name="Napa 500mg",
            generic_name="Paracetamol",
            dosage_form="TABLET",
            strength="500mg",
            manufacturer="Beximco Pharmaceuticals Ltd.",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.95"),
        )

        self.today = datetime.date.today()
        self.valid_batch = BatchStock.objects.create(
            batch_id="batch-napa-good",
            tenant=self.t_a,
            branch=self.b_a,
            product=self.product,
            batch_number="NAPA-2026-X1",
            quantity_base_units=500,
            expiry_date=self.today + datetime.timedelta(days=365),
            purchase_date=self.today,
            cost_per_unit=Decimal("0.95"),
            status=BatchStatus.ACTIVE,
        )

        self.expired_batch = BatchStock.objects.create(
            batch_id="batch-napa-exp",
            tenant=self.t_a,
            branch=self.b_a,
            product=self.product,
            batch_number="NAPA-2024-EXP",
            quantity_base_units=100,
            expiry_date=self.today - datetime.timedelta(days=15),
            purchase_date=self.today - datetime.timedelta(days=400),
            cost_per_unit=Decimal("0.95"),
            status=BatchStatus.EXPIRED,
        )

        self.other_batch = BatchStock.objects.create(
            batch_id="batch-other-tenant",
            tenant=self.t_b,
            branch=self.b_b,
            product=self.product,
            batch_number="NAPA-UTT-01",
            quantity_base_units=300,
            expiry_date=self.today + datetime.timedelta(days=200),
            purchase_date=self.today,
            cost_per_unit=Decimal("0.95"),
            status=BatchStatus.ACTIVE,
        )

    def tearDown(self):
        SaleInvoiceItem.objects.filter(invoice__tenant__in=[self.t_a, self.t_b]).delete()
        SaleInvoice.objects.filter(tenant__in=[self.t_a, self.t_b]).delete()
        BatchStock.objects.filter(tenant__in=[self.t_a, self.t_b]).delete()
        MedicineProduct.objects.filter(product_id__in=["prod-napa-500", "prod-val-insulin"]).delete()
        Branch.objects.filter(tenant__in=[self.t_a, self.t_b]).delete()
        Tenant.objects.filter(tenant_id__in=[self.tenant_a, self.tenant_b]).delete()

    def test_valid_checkout_command_succeeds(self):
        """Verifies compliant checkout command returns 201 Created with exact Decimal totals."""
        payload = {
            "tenant_id": self.tenant_a,
            "branch_id": self.b_a.branch_id,
            "customer_name": "Karim Mia",
            "customer_phone": "+8801711223344",
            "payment_method": "CASH",
            "discount_amount": "2.00",
            "items": [
                {"batch_id": self.valid_batch.batch_id, "quantity": 10},
            ],
        }
        res = self.client.post("/api/v2/invoices/checkout/", payload, content_type="application/json")
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["total_amount"], "12.00")
        self.assertEqual(data["discount_amount"], "2.00")
        self.assertEqual(data["net_amount"], "10.00")

    def test_negative_quantity_rejected_with_422(self):
        """Verifies negative dispensing quantity is rejected with 422 ERR_VALIDATION_FAILED."""
        payload = {
            "tenant_id": self.tenant_a,
            "branch_id": self.b_a.branch_id,
            "items": [
                {"batch_id": self.valid_batch.batch_id, "quantity": -3},
            ],
        }
        res = self.client.post("/api/v2/invoices/checkout/", payload, content_type="application/json")
        self.assertEqual(res.status_code, 422)
        data = res.json()
        self.assertEqual(data.get("error_code"), "ERR_VALIDATION_FAILED")
        self.assertIn("items", data.get("field_errors", {}))

    def test_excessive_discount_rejected_with_422(self):
        """Verifies discount exceeding total bill amount is rejected with 422."""
        payload = {
            "tenant_id": self.tenant_a,
            "branch_id": self.b_a.branch_id,
            "discount_amount": "500.00",  # Total will only be 1.20 * 2 = 2.40
            "items": [
                {"batch_id": self.valid_batch.batch_id, "quantity": 2},
            ],
        }
        res = self.client.post("/api/v2/invoices/checkout/", payload, content_type="application/json")
        self.assertEqual(res.status_code, 422)
        data = res.json()
        self.assertEqual(data.get("error_code"), "ERR_VALIDATION_FAILED")

    def test_expired_batch_allocation_rejected_with_422(self):
        """Verifies DGDA compliance invariant: dispensing expired pharmaceuticals is blocked with 422."""
        payload = {
            "tenant_id": self.tenant_a,
            "branch_id": self.b_a.branch_id,
            "items": [
                {"batch_id": self.expired_batch.batch_id, "quantity": 1},
            ],
        }
        res = self.client.post("/api/v2/invoices/checkout/", payload, content_type="application/json")
        self.assertEqual(res.status_code, 422)
        data = res.json()
        self.assertEqual(data.get("error_code"), "ERR_VALIDATION_FAILED")
        self.assertIn("items", data.get("field_errors", {}))

    def test_oversell_quantity_rejected_with_422(self):
        """Verifies requesting more than available batch stock is rejected with 422."""
        payload = {
            "tenant_id": self.tenant_a,
            "branch_id": self.b_a.branch_id,
            "items": [
                {"batch_id": self.valid_batch.batch_id, "quantity": 9999},
            ],
        }
        res = self.client.post("/api/v2/invoices/checkout/", payload, content_type="application/json")
        self.assertEqual(res.status_code, 422)
        data = res.json()
        self.assertEqual(data.get("error_code"), "ERR_VALIDATION_FAILED")

    def test_cross_tenant_batch_referential_violation_rejected(self):
        """Verifies attempting to checkout another tenant's batch is strictly blocked."""
        payload = {
            "tenant_id": self.tenant_a,
            "branch_id": self.b_a.branch_id,
            "items": [
                {"batch_id": self.other_batch.batch_id, "quantity": 1},
            ],
        }
        res = self.client.post("/api/v2/invoices/checkout/", payload, content_type="application/json")
        self.assertIn(res.status_code, [403, 422])
        data = res.json()
        self.assertIn(data.get("error_code"), ["ERR_VALIDATION_FAILED", "ERR_CROSS_TENANT_VIOLATION"])

    def test_validate_command_endpoint_success_and_failure(self):
        """Verifies /api/v2/validation/validate-command/ evaluates domain rules accurately."""
        valid_cmd = {
            "command_type": "CREATE_MEDICINE",
            "tenant_id": self.tenant_a,
            "payload": {
                "product_id": "prod-val-insulin",
                "brand_name": "Insulatard 100IU",
                "generic_name": "Isophane Insulin",
                "dosage_form": "INJECTION",
                "strength": "100IU/ml",
                "manufacturer": "Novo Nordisk",
                "mrp_unit": "450.00",
                "cost_unit": "380.00",
                "requires_cold_chain": True,
                "min_temp_celsius": "2.00",
                "max_temp_celsius": "8.00",
            },
        }
        res_ok = self.client.post("/api/v2/validation/validate-command/", valid_cmd, content_type="application/json")
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.json().get("valid"))

        # Inverted temperature test (min_temp > max_temp)
        invalid_cmd = {
            "command_type": "CREATE_MEDICINE",
            "tenant_id": self.tenant_a,
            "payload": {
                "product_id": "prod-val-insulin-bad",
                "brand_name": "Bad Temp Insulin",
                "generic_name": "Isophane Insulin",
                "dosage_form": "INJECTION",
                "strength": "100IU/ml",
                "manufacturer": "Novo Nordisk",
                "mrp_unit": "450.00",
                "cost_unit": "380.00",
                "requires_cold_chain": True,
                "min_temp_celsius": "15.00",
                "max_temp_celsius": "4.00",  # Inverted range
            },
        }
        res_fail = self.client.post(
            "/api/v2/validation/validate-command/", invalid_cmd, content_type="application/json"
        )
        self.assertEqual(res_fail.status_code, 422)
        self.assertEqual(res_fail.json().get("error_code"), "ERR_VALIDATION_FAILED")


if __name__ == "__main__":
    unittest.main()
