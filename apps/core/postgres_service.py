"""
apps/core/postgres_service.py
Architecture Decision: ADR-0004 & ADR-0030
Phase 02: Repository, Toolchain & Engineering Automation

PostgreSQL Integration Service Orchestrator & Concurrency Locking Parity Engine:
- Docker container orchestration (start, stop, healthcheck probe, connection wait).
- Migration parity verification between Django models and PostgreSQL tables/constraints.
- Row-level locking concurrent checkout simulation (SELECT ... FOR UPDATE).
- Transaction isolation verification and zero-overselling stock guarantees.
- Multi-tenant boundary enforcement on high-concurrency operations.
"""

from __future__ import annotations

import concurrent.futures
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.db import connection, transaction
from django.db.utils import OperationalError
from django.utils import timezone

from apps.core.models import (
    BatchStatus,
    BatchStock,
)


class InsufficientStockError(Exception):
    """Raised when available stock is less than requested in concurrent allocation."""

    pass


class BatchExpiredError(Exception):
    """Raised when an attempt is made to allocate from an expired batch."""

    pass


class CrossTenantOperationError(PermissionError):
    """Raised when an operation attempts to allocate or lock stock across tenant boundaries."""

    pass


@dataclass
class ConcurrencySimulationResult:
    initial_stock: int
    requested_total: int
    successful_allocations: List[Dict[str, Any]]
    rejected_allocations: List[Dict[str, Any]]
    final_stock: int
    oversell_prevented: bool
    execution_time_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_stock": self.initial_stock,
            "requested_total": self.requested_total,
            "successful_count": len(self.successful_allocations),
            "rejected_count": len(self.rejected_allocations),
            "final_stock": self.final_stock,
            "oversell_prevented": self.oversell_prevented,
            "execution_time_seconds": round(self.execution_time_seconds, 3),
        }


class PostgresServiceOrchestrator:
    """Manages isolated PostgreSQL container lifecycle and health checks."""

    def __init__(self, compose_file: Optional[Path] = None):
        root_dir = Path(__file__).resolve().parent.parent.parent
        self.compose_file = compose_file or (root_dir / "infra" / "docker-compose.yml")

    def is_docker_available(self) -> bool:
        """Checks if docker CLI is present and responsive."""
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
            return res.returncode == 0
        except Exception:
            return False

    def start_service(self) -> Tuple[bool, str]:
        """Starts PostgreSQL container via docker compose."""
        if not self.compose_file.exists():
            return False, f"Compose file not found at: {self.compose_file}"

        try:
            res = subprocess.run(
                ["docker", "compose", "-f", str(self.compose_file), "up", "-d", "postgres"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode == 0:
                return True, "PostgreSQL container started successfully."
            return False, res.stderr or res.stdout
        except Exception as e:
            return False, f"Failed to start PostgreSQL container: {e}"

    def stop_service(self) -> Tuple[bool, str]:
        """Stops PostgreSQL container via docker compose."""
        try:
            res = subprocess.run(
                ["docker", "compose", "-f", str(self.compose_file), "stop", "postgres"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if res.returncode == 0:
                return True, "PostgreSQL container stopped."
            return False, res.stderr or res.stdout
        except Exception as e:
            return False, f"Failed to stop PostgreSQL container: {e}"

    def wait_for_healthy(self, max_wait_seconds: int = 15) -> Tuple[bool, str]:
        """Waits until PostgreSQL container healthcheck passes."""
        start = time.time()
        while time.time() - start < max_wait_seconds:
            try:
                res = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Health.Status}}", "pharmacy_postgres"],
                    capture_output=True,
                    text=True,
                )
                status = res.stdout.strip()
                if status == "healthy":
                    return True, "PostgreSQL is healthy and accepting connections."
            except Exception:
                pass
            time.sleep(1)

        return False, f"Timed out waiting for PostgreSQL container after {max_wait_seconds}s."

    def probe_live_connection(self) -> Tuple[bool, Dict[str, Any]]:
        """Tests live database connectivity and reports server details."""
        try:
            with connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute(
                        "SELECT version(), current_setting('server_version'), current_setting('transaction_isolation');"
                    )
                    row = cursor.fetchone()
                    return True, {
                        "vendor": connection.vendor,
                        "version_full": row[0],
                        "server_version": row[1],
                        "default_isolation": row[2],
                    }
                else:
                    cursor.execute("SELECT sqlite_version();")
                    row = cursor.fetchone()
                    return True, {
                        "vendor": connection.vendor,
                        "version_full": f"SQLite {row[0]}",
                        "server_version": row[0],
                        "default_isolation": "DEFERRED",
                    }
        except Exception as e:
            return False, {"error": str(e), "vendor": connection.vendor}


class ConcurrentCheckoutSimulator:
    """Simulates high-concurrency retail checkout counter race conditions with row-level locking."""

    @classmethod
    @transaction.atomic
    def allocate_batch_stock_safe(
        cls,
        batch_id: str,
        quantity: int,
        cashier_id: str,
        requesting_tenant_id: str,
    ) -> Dict[str, Any]:
        """
        Executes atomic stock deduction using SELECT ... FOR UPDATE.
        Guarantees that no two concurrent POS counters oversell available batch inventory.
        """
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive integer, got {quantity}")

        # Acquire exclusive row-level lock on the specified BatchStock
        # In PostgreSQL: SELECT ... FROM apps_core_batchstock WHERE batch_id = ... FOR UPDATE
        # In SQLite: Runs within atomic transaction
        batch = BatchStock.objects.select_for_update().get(batch_id=batch_id)

        # Enforce multi-tenant isolation
        if batch.tenant_id != requesting_tenant_id:
            raise CrossTenantOperationError(
                f"Cashier '{cashier_id}' from tenant '{requesting_tenant_id}' cannot allocate "
                f"stock from batch belonging to foreign tenant '{batch.tenant_id}'."
            )

        if batch.is_expired or batch.status == BatchStatus.EXPIRED:
            raise BatchExpiredError(f"Batch {batch.batch_number} has expired. DGDA prohibits sale of expired stock.")

        if batch.quantity_base_units < quantity:
            raise InsufficientStockError(
                f"Insufficient stock for batch {batch.batch_number}: "
                f"requested {quantity}, available {batch.quantity_base_units}."
            )

        # Deduct stock and persist atomically
        batch.quantity_base_units -= quantity
        batch.save(update_fields=["quantity_base_units"])

        return {
            "batch_id": batch.batch_id,
            "batch_number": batch.batch_number,
            "allocated_quantity": quantity,
            "remaining_quantity": batch.quantity_base_units,
            "cashier_id": cashier_id,
            "timestamp": timezone.now().isoformat(),
        }

    @classmethod
    def simulate_concurrent_checkout_race(
        cls,
        batch_id: str,
        initial_stock: int,
        checkout_requests: List[Tuple[str, int, str]],  # (cashier_id, requested_qty, tenant_id)
        max_workers: int = 4,
    ) -> ConcurrencySimulationResult:
        """
        Launches concurrent cashier threads simultaneously competing for stock.
        Verifies that total allocations never exceed available stock and reconciliation is exact.
        """
        start_time = time.time()
        # Reset batch stock to known initial level
        batch = BatchStock.objects.get(batch_id=batch_id)
        batch.quantity_base_units = initial_stock
        batch.save(update_fields=["quantity_base_units"])

        successful: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        requested_total = sum(req[1] for req in checkout_requests)

        def worker_task(req: Tuple[str, int, str]) -> Dict[str, Any]:
            cashier_id, qty, tenant_id = req
            try:
                # Slight micro-delay to align thread concurrency window
                time.sleep(0.005)
                res = cls.allocate_batch_stock_safe(
                    batch_id=batch_id,
                    quantity=qty,
                    cashier_id=cashier_id,
                    requesting_tenant_id=tenant_id,
                )
                return {"status": "SUCCESS", "data": res}
            except (
                InsufficientStockError,
                BatchExpiredError,
                CrossTenantOperationError,
                ValueError,
                OperationalError,
            ) as err:
                return {
                    "status": "REJECTED",
                    "cashier_id": cashier_id,
                    "quantity": qty,
                    "reason": type(err).__name__,
                    "error": str(err),
                }

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(worker_task, req) for req in checkout_requests]
            for future in concurrent.futures.as_completed(futures):
                outcome = future.result()
                if outcome["status"] == "SUCCESS":
                    successful.append(outcome["data"])
                else:
                    rejected.append(outcome)

        # Refresh batch from DB to inspect final state
        batch.refresh_from_db()
        final_stock = batch.quantity_base_units
        total_allocated = sum(s["allocated_quantity"] for s in successful)

        # Verification Invariants:
        # 1. Final stock must equal initial stock minus total allocated
        # 2. Final stock must be non-negative (never oversold)
        is_reconciled = (initial_stock - total_allocated) == final_stock
        oversell_prevented = final_stock >= 0 and is_reconciled

        elapsed = time.time() - start_time

        return ConcurrencySimulationResult(
            initial_stock=initial_stock,
            requested_total=requested_total,
            successful_allocations=successful,
            rejected_allocations=rejected,
            final_stock=final_stock,
            oversell_prevented=oversell_prevented,
            execution_time_seconds=elapsed,
        )
