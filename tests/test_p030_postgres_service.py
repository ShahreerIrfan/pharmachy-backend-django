"""
tests/test_p030_postgres_service.py
Architecture Decision: ADR-0004 & ADR-0030
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for PostgreSQL Integration Service, Concurrency Locking,
ACID Row-Level Isolation, and Multi-Tenant Parity:
1. Valid scenario: Orchestrator connection probe, active engine details, and concurrent checkout simulation.
2. Race condition prevention: Row-level locking prevents overselling (zero negative inventory).
3. Invalid / Incomplete cases: Insufficient stock error, negative quantity error, expired batch block.
4. Unauthorized / Cross-tenant: Foreign tenant cashier rejected with CrossTenantOperationError.
5. Transaction atomicity & rollback integrity.
6. Unique constraint parity verification.
7. CLI tool status command validation.
"""

from __future__ import annotations

import os
import subprocess
import sys
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

from django.db import IntegrityError, transaction
from django.test import TransactionTestCase

from apps.core.models import (
    BatchStatus,
    BatchStock,
)
from apps.core.postgres_service import (
    BatchExpiredError,
    ConcurrentCheckoutSimulator,
    CrossTenantOperationError,
    InsufficientStockError,
    PostgresServiceOrchestrator,
)
from apps.core.sqlite_bootstrap import SQLiteBootstrapEngine


class TestP030PostgresService(TransactionTestCase):
    def setUp(self):
        # Ensure database is bootstrapped with standard deterministic fixtures
        SQLiteBootstrapEngine.bootstrap()

    # --------------------------------------------------------------------------
    # 1. Valid Scenario: Orchestrator Connection Probe
    # --------------------------------------------------------------------------
    def test_valid_orchestrator_probes_live_database_connection(self):
        """Orchestrator probe_live_connection must connect and return active vendor and isolation."""
        orchestrator = PostgresServiceOrchestrator()
        connected, details = orchestrator.probe_live_connection()

        self.assertTrue(connected, f"Failed to connect to active database: {details}")
        self.assertIn("vendor", details)
        self.assertIn(details["vendor"], {"sqlite", "postgresql"})
        self.assertIn("server_version", details)
        self.assertIn("default_isolation", details)

    # --------------------------------------------------------------------------
    # 2. Concurrency Simulation & Row-Level Locking Race Prevention
    # --------------------------------------------------------------------------
    def test_concurrent_checkout_prevents_overselling_race_condition(self):
        """
        Simulates 3 concurrent POS cashier counters competing for 12 units of stock.
        Requests: Cashier A wants 6, Cashier B wants 5, Cashier C wants 4 (Total 15 > 12).
        With atomic row-locking, final stock must be >= 0 and never oversold.
        """
        batch = BatchStock.objects.filter(product_id="PROD-NAPA-500", status=BatchStatus.ACTIVE).first()
        self.assertIsNotNone(batch, "Active Napa batch must be present")

        initial_stock = 12
        requests = [
            ("Cashier_A", 6, batch.tenant_id),
            ("Cashier_B", 5, batch.tenant_id),
            ("Cashier_C", 4, batch.tenant_id),
        ]

        result = ConcurrentCheckoutSimulator.simulate_concurrent_checkout_race(
            batch_id=batch.batch_id,
            initial_stock=initial_stock,
            checkout_requests=requests,
            max_workers=3,
        )

        self.assertTrue(result.oversell_prevented, "Stock was oversold!")
        self.assertGreaterEqual(result.final_stock, 0)
        self.assertEqual(len(result.successful_allocations) + len(result.rejected_allocations), 3)

        # Verify exact balance reconciliation: initial - allocated == final
        total_allocated = sum(s["allocated_quantity"] for s in result.successful_allocations)
        self.assertEqual(initial_stock - total_allocated, result.final_stock)

        # Total checkouts handled
        self.assertEqual(len(result.successful_allocations) + len(result.rejected_allocations), 3)

        # Verify exact balance reconciliation: initial - allocated == final
        total_allocated = sum(s["allocated_quantity"] for s in result.successful_allocations)
        self.assertEqual(initial_stock - total_allocated, result.final_stock)

        # Under PostgreSQL: exactly 1 request rejected with InsufficientStockError
        # Under SQLite: table-level lock contention safely rejects concurrent writers
        if django.db.connection.vendor == "postgresql":
            self.assertEqual(len(result.rejected_allocations), 1)
            self.assertEqual(result.rejected_allocations[0]["reason"], "InsufficientStockError")
        else:
            self.assertTrue(len(result.rejected_allocations) >= 1)
            for r in result.rejected_allocations:
                self.assertIn(r["reason"], {"InsufficientStockError", "OperationalError"})

    # --------------------------------------------------------------------------
    # 3. Invalid / Incomplete Input Scenarios
    # --------------------------------------------------------------------------
    def test_insufficient_stock_allocation_raises_error(self):
        """Requesting more stock than available in a single transaction must raise InsufficientStockError."""
        batch = BatchStock.objects.filter(product_id="PROD-SECLO-20", status=BatchStatus.ACTIVE).first()
        self.assertIsNotNone(batch)

        # Attempt to allocate 99999 units
        with self.assertRaises(InsufficientStockError) as ctx:
            ConcurrentCheckoutSimulator.allocate_batch_stock_safe(
                batch_id=batch.batch_id,
                quantity=99999,
                cashier_id="Cashier_Overlimit",
                requesting_tenant_id=batch.tenant_id,
            )
        self.assertIn("Insufficient stock", str(ctx.exception))

    def test_negative_or_zero_quantity_allocation_raises_value_error(self):
        """Allocation with non-positive quantity must raise ValueError."""
        batch = BatchStock.objects.filter(status=BatchStatus.ACTIVE).first()
        self.assertIsNotNone(batch)

        for invalid_qty in [0, -1, -50]:
            with self.assertRaises(ValueError):
                ConcurrentCheckoutSimulator.allocate_batch_stock_safe(
                    batch_id=batch.batch_id,
                    quantity=invalid_qty,
                    cashier_id="Cashier_BadQty",
                    requesting_tenant_id=batch.tenant_id,
                )

    def test_expired_batch_allocation_raises_batch_expired_error(self):
        """Attempting to allocate stock from an expired batch must raise BatchExpiredError (DGDA rule)."""
        expired_batch = BatchStock.objects.get(batch_id="BATCH-NAPA-EXPIRED")

        with self.assertRaises(BatchExpiredError) as ctx:
            ConcurrentCheckoutSimulator.allocate_batch_stock_safe(
                batch_id=expired_batch.batch_id,
                quantity=5,
                cashier_id="Cashier_Exp",
                requesting_tenant_id=expired_batch.tenant_id,
            )
        self.assertIn("expired", str(ctx.exception).lower())

    # --------------------------------------------------------------------------
    # 4. Unauthorized / Cross-Tenant Isolation Scenario
    # --------------------------------------------------------------------------
    def test_cross_tenant_batch_allocation_strictly_blocked(self):
        """Cashier from tenant-chittagong-care cannot allocate stock from tenant-dhaka-central batch."""
        dhaka_batch = BatchStock.objects.filter(tenant_id="tenant-dhaka-central").first()
        self.assertIsNotNone(dhaka_batch)

        with self.assertRaises(CrossTenantOperationError) as ctx:
            ConcurrentCheckoutSimulator.allocate_batch_stock_safe(
                batch_id=dhaka_batch.batch_id,
                quantity=2,
                cashier_id="Cashier_Intruder",
                requesting_tenant_id="tenant-chittagong-care",  # Foreign tenant!
            )
        self.assertIn("foreign tenant", str(ctx.exception))

    # --------------------------------------------------------------------------
    # 5. Transaction Rollback & Atomicity Integrity
    # --------------------------------------------------------------------------
    def test_failed_transaction_leaves_stock_ledger_unmodified(self):
        """If an error occurs after a tentative read, stock deduction must roll back completely."""
        batch = BatchStock.objects.filter(product_id="PROD-MOXACIL-500", status=BatchStatus.ACTIVE).first()
        initial_stock = batch.quantity_base_units

        try:
            with transaction.atomic():
                batch.quantity_base_units -= 10
                batch.save(update_fields=["quantity_base_units"])
                # Simulate mid-transaction business rule failure
                raise RuntimeError("Simulated terminal network disruption during checkout")
        except RuntimeError:
            pass

        # Verify database rolled back to exact initial value
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_base_units, initial_stock)

    # --------------------------------------------------------------------------
    # 6. Database Unique Constraints Parity
    # --------------------------------------------------------------------------
    def test_unique_constraint_on_tenant_branch_product_batch(self):
        """Database must reject duplicate (tenant, branch, product, batch_number) entries."""
        existing = BatchStock.objects.filter(status=BatchStatus.ACTIVE).first()
        self.assertIsNotNone(existing)

        with self.assertRaises(IntegrityError):
            BatchStock.objects.create(
                batch_id="BATCH-TEST-COLLISION-ID",
                tenant=existing.tenant,
                branch=existing.branch,
                product=existing.product,
                batch_number=existing.batch_number,  # Colliding batch number
                quantity_base_units=100,
                expiry_date=existing.expiry_date,
                purchase_date=existing.purchase_date,
                cost_per_unit=existing.cost_per_unit,
            )

    # --------------------------------------------------------------------------
    # 7. CLI Tool Status Validation
    # --------------------------------------------------------------------------
    def test_cli_postgres_lane_status_returns_success(self):
        """tools/postgres_lane.py --status must execute and return exit code 0."""
        script_path = _ROOT_DIR / "tools" / "postgres_lane.py"
        res = subprocess.run(
            ["python", str(script_path), "--status"],
            cwd=str(_ROOT_DIR),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CLI command failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("Database Engine Status: CONNECTED", res.stdout)


if __name__ == "__main__":
    import unittest

    unittest.main()
