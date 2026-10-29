"""
tests/test_p029_sqlite_bootstrap.py
Architecture Decision: ADR-0004 & ADR-0029
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for Local SQLite Backend Bootstrap, Deterministic Seeder,
Strict Decimal Monetary Invariants, and PostgreSQL Parity Lane:
1. Valid scenario: Full bootstrap executes migrations, seeds deterministic fixtures, and calculates exact Decimal statistics.
2. Domain validation: FEFO batch ordering, Cold-Chain temperature bounds, expired batch quarantine.
3. Invalid / Incomplete cases: Negative monetary amounts, duplicate batch keys, and invalid quantities rejected.
4. Cross-tenant isolation: Tenant-scoping on batch and invoice queries.
5. PostgreSQL parity lane: Probing service status and reporting concurrency locking contracts.
6. DRF ViewSet live endpoint integration.
"""

from __future__ import annotations

import datetime
import os
import sys
from decimal import Decimal
from pathlib import Path

# Ensure backend and root are on sys.path
_ROOT_DIR = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _ROOT_DIR / "backend"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

try:
    django.setup()
except RuntimeError:
    pass

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    SaleInvoice,
    Tenant,
)
from apps.core.sqlite_bootstrap import (
    PostgreSQLParityLane,
    SQLiteBootstrapEngine,
)


class TestP029SQLiteBootstrap(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Clean existing records to guarantee deterministic isolated state
        SaleInvoice.objects.all().delete()
        BatchStock.objects.all().delete()
        Branch.objects.all().delete()
        Tenant.objects.all().delete()
        MedicineProduct.objects.all().delete()
        # Seed deterministic database fixtures
        SQLiteBootstrapEngine.bootstrap()

    # --------------------------------------------------------------------------
    # 1. Valid Scenario: Bootstrap & Deterministic Seeding
    # --------------------------------------------------------------------------
    def test_valid_bootstrap_seeds_all_entities(self):
        """Bootstrap must apply migrations, seed deterministic fixtures, and compute metrics."""
        stats = SQLiteBootstrapEngine.get_database_stats()

        self.assertEqual(stats["tenants_count"], 2)
        self.assertEqual(stats["branches_count"], 3)
        self.assertEqual(stats["medicines_count"], 6)
        self.assertEqual(stats["batches_total"], 6)
        self.assertEqual(stats["batches_active"], 5)
        self.assertEqual(stats["batches_expired"], 1)
        self.assertEqual(stats["invoices_count"], 1)

        # Verify strict Decimal string representations
        self.assertEqual(stats["total_inventory_valuation_bdt"], "52815.00")
        self.assertEqual(stats["total_sales_volume_bdt"], "150.00")

    def test_strict_decimal_monetary_invariants(self):
        """All currency and price fields must strictly be Decimal instances, never IEEE 754 float."""
        for product in MedicineProduct.objects.all():
            self.assertIsInstance(product.mrp_unit, Decimal)
            self.assertIsInstance(product.cost_unit, Decimal)
            self.assertNotIsInstance(product.mrp_unit, float)
            self.assertNotIsInstance(product.cost_unit, float)

        for batch in BatchStock.objects.all():
            self.assertIsInstance(batch.cost_per_unit, Decimal)
            self.assertNotIsInstance(batch.cost_per_unit, float)

        for invoice in SaleInvoice.objects.all():
            self.assertIsInstance(invoice.total_amount, Decimal)
            self.assertIsInstance(invoice.tax_amount, Decimal)
            self.assertIsInstance(invoice.discount_amount, Decimal)
            self.assertIsInstance(invoice.net_amount, Decimal)
            self.assertNotIsInstance(invoice.net_amount, float)

    # --------------------------------------------------------------------------
    # 2. Domain Requirements: FEFO Ordering & Cold-Chain Bounds
    # --------------------------------------------------------------------------
    def test_fefo_batch_ordering_first_expiry_first_out(self):
        """Batches for the same medicine must order by expiry_date ascending (FEFO)."""
        batches = list(
            BatchStock.objects.filter(product_id="PROD-NAPA-500", status=BatchStatus.ACTIVE).order_by("expiry_date")
        )
        self.assertGreaterEqual(len(batches), 2)
        # 2027 lot must precede 2028 lot
        self.assertLess(batches[0].expiry_date, batches[1].expiry_date)
        self.assertEqual(batches[0].batch_number, "NP27A01")
        self.assertEqual(batches[1].batch_number, "NP28B02")

    def test_cold_chain_insulin_storage_temperature_bounds(self):
        """Biological cold-chain medicine (Lantus Insulin) must specify valid 2.0C - 8.0C bounds."""
        lantus = MedicineProduct.objects.get(product_id="PROD-LANTUS-100")
        self.assertTrue(lantus.requires_cold_chain)
        self.assertEqual(lantus.min_temp_celsius, Decimal("2.00"))
        self.assertEqual(lantus.max_temp_celsius, Decimal("8.00"))
        self.assertLess(lantus.min_temp_celsius, lantus.max_temp_celsius)

    def test_expired_batch_quarantine_flag(self):
        """Expired batch must be marked EXPIRED and is_expired must return True."""
        expired_batch = BatchStock.objects.get(batch_id="BATCH-NAPA-EXPIRED")
        self.assertEqual(expired_batch.status, BatchStatus.EXPIRED)
        self.assertTrue(expired_batch.is_expired)

    # --------------------------------------------------------------------------
    # 3. Invalid / Incomplete Input Scenarios
    # --------------------------------------------------------------------------
    def test_negative_mrp_unit_validation_rejected(self):
        """Creating a medicine with negative MRP unit must fail model validation."""
        bad_medicine = MedicineProduct(
            product_id="PROD-INVALID-PRICE",
            brand_name="BadMedicine",
            generic_name="BadGeneric",
            dosage_form="Tablet",
            strength="10mg",
            manufacturer="Generic Ltd.",
            mrp_unit=Decimal("-5.00"),  # Negative MRP
            cost_unit=Decimal("2.00"),
        )
        with self.assertRaises(ValidationError):
            bad_medicine.full_clean()

    def test_duplicate_batch_unique_constraint_rejected(self):
        """Creating duplicate batch lot for same tenant, branch, product, and batch_number must fail."""
        tenant = Tenant.objects.get(tenant_id="tenant-dhaka-central")
        branch = Branch.objects.get(branch_id="branch-dhk-main")
        product = MedicineProduct.objects.get(product_id="PROD-NAPA-500")

        # Attempt to insert identical batch number NP27A01
        with self.assertRaises(IntegrityError):
            BatchStock.objects.create(
                batch_id="BATCH-DUPLICATE-ID",
                tenant=tenant,
                branch=branch,
                product=product,
                batch_number="NP27A01",  # Existing batch number
                quantity_base_units=500,
                expiry_date=timezone.now().date() + datetime.timedelta(days=200),
                purchase_date=timezone.now().date(),
                cost_per_unit=Decimal("0.95"),
            )

    # --------------------------------------------------------------------------
    # 4. Unauthorized / Cross-Tenant Isolation
    # --------------------------------------------------------------------------
    def test_cross_tenant_invoice_filtering(self):
        """Invoices belonging to tenant-dhaka-central must not appear in tenant-chittagong-care queries."""
        response = self.client.get("/api/v2/invoices/?tenant_id=tenant-chittagong-care")
        self.assertEqual(response.status_code, 200)
        # Chittagong tenant has 0 invoices seeded
        self.assertEqual(len(response.data), 0)

        # Dhaka tenant has 1 invoice seeded
        response_dhaka = self.client.get("/api/v2/invoices/?tenant_id=tenant-dhaka-central")
        self.assertEqual(response_dhaka.status_code, 200)
        self.assertEqual(len(response_dhaka.data), 1)
        self.assertEqual(response_dhaka.data[0]["invoice_id"], "INV-DHK-2026-0001")

    # --------------------------------------------------------------------------
    # 5. PostgreSQL Parity Lane Probe
    # --------------------------------------------------------------------------
    def test_postgres_parity_lane_probe_reports_safe_offline_state(self):
        """PostgreSQLParityLane reports offline status under SQLite and warns on row-level locking."""
        live, msg = PostgreSQLParityLane.probe_postgres_service()
        concurrency = PostgreSQLParityLane.evaluate_concurrency_locking_contract()

        self.assertFalse(live)
        self.assertIn("Active Django DB_ENGINE is 'sqlite'", msg)
        self.assertFalse(concurrency["row_level_locking_active"])
        self.assertFalse(concurrency["concurrency_safe"])
        self.assertIn("SQLite does not support row-level locks", concurrency["notes"])

    # --------------------------------------------------------------------------
    # 6. Live DRF Endpoints Verification
    # --------------------------------------------------------------------------
    def test_drf_medicines_endpoint_returns_catalog(self):
        """GET /api/v2/medicines/ returns seeded 6 pharmaceutical products."""
        response = self.client.get("/api/v2/medicines/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 6)
        brand_names = {item["brand_name"] for item in response.data}
        self.assertIn("Napa", brand_names)
        self.assertIn("Lantus Solostar", brand_names)
        self.assertIn("Zimax", brand_names)


if __name__ == "__main__":
    import unittest

    unittest.main()
