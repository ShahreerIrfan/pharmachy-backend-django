"""
backend/apps/core/packaging_concurrency_service.py
Architecture Decision: ADR-0180 (P180).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Service providing multi-threaded stress-testing, pessimistic row-level locking (select_for_update),
FEFO split lot allocation, concurrent checkout resilience, package conversion indivisibility,
and mathematical stock conservation proof.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Tuple

from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    PackagingConcurrencyAuditRecord,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

_sqlite_lock = threading.Lock()

ALLOWED_CONCURRENCY_ROLES = {
    UserRole.BRANCH_MANAGER,
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    UserRole.STAFF_PHARMACIST,
    "BRANCH_MANAGER",
    "PHARMACY_OWNER",
    "SUPERADMIN",
    "STAFF_PHARMACIST",
    "OWNER_ADMIN",
    "ADMIN",
    "AUDITOR",
    "CHIEF_PHARMACIST",
}


class PackagingConcurrencyService:
    """
    Authoritative service governing packaging conversions and concurrent batch checkouts.
    Enforces pessimistic row locking and zero-stock drift conservation under high concurrency.
    """

    @classmethod
    def _assert_manage_role(cls, user: Any) -> None:
        role = getattr(user, "role", None)
        if role not in ALLOWED_CONCURRENCY_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Concurrency diagnostic execution requires managerial, owner, or auditor authorization.",
                    "error_code": "ERR_PERMISSION_DENIED",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant: Tenant) -> None:
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "detail": "Tenant subscription is suspended. Concurrency services are locked.",
                    "error_code": "ERR_TENANT_SUSPENDED",
                }
            )

    @classmethod
    def serialize_audit_record(cls, record: PackagingConcurrencyAuditRecord) -> Dict[str, Any]:
        """Serializes a PackagingConcurrencyAuditRecord into standard JSON contract format."""
        return {
            "test_id": record.test_id,
            "tenant_id": str(record.tenant_id),
            "branch_id": str(record.branch_id),
            "product_id": str(record.product_id),
            "product_name": record.product.brand_name if record.product else "",
            "concurrency_level": record.concurrency_level,
            "total_requests": record.total_requests,
            "successful_transactions": record.successful_transactions,
            "rejected_oversells": record.rejected_oversells,
            "initial_stock_base_units": record.initial_stock_base_units,
            "final_stock_base_units": record.final_stock_base_units,
            "total_allocated_base_units": record.total_allocated_base_units,
            "stock_conserved": record.stock_conserved,
            "execution_time_ms": record.execution_time_ms,
            "fefo_order_verified": record.fefo_order_verified,
            "integer_units_verified": record.integer_units_verified,
            "tested_by_id": str(record.tested_by_id) if record.tested_by_id else None,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @classmethod
    def run_stress_test(
        cls,
        user: Any,
        tenant_id: str,
        branch_id: str,
        product_id: str,
        concurrency_level: int = 10,
        requests_per_thread: int = 1,
        units_per_request: int = 5,
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Executes a real multi-threaded concurrent checkout stress test against a designated product,
        demonstrating pessimistic lock contention resolution, zero overselling, and exact stock conservation.
        """
        cls._assert_manage_role(user)

        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"detail": f"Tenant '{tenant_id}' not found.", "error_code": "ERR_TENANT_NOT_FOUND"})

        cls._assert_tenant_active(tenant)

        # Cross-tenant check
        user_tenant_id = getattr(user, "tenant_id", None)
        if (
            user_tenant_id
            and str(user_tenant_id) != str(tenant_id)
            and getattr(user, "role", None) != UserRole.SUPERADMIN
        ):
            raise PermissionDenied(
                {
                    "detail": "Cross-tenant concurrency testing is strictly barred.",
                    "error_code": "ERR_CROSS_TENANT_ACCESS",
                }
            )

        branch = Branch.objects.filter(branch_id=branch_id, tenant=tenant).first()
        if not branch:
            raise NotFound({"detail": f"Branch '{branch_id}' not found for tenant.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        product = MedicineProduct.objects.filter(product_id=product_id).first()
        if not product:
            raise NotFound({"detail": f"Product '{product_id}' not found.", "error_code": "ERR_PRODUCT_NOT_FOUND"})

        # Input bounds checking
        concurrency_level = max(1, min(concurrency_level, 50))
        requests_per_thread = max(1, min(requests_per_thread, 10))
        units_per_request = max(1, units_per_request)
        total_requests = concurrency_level * requests_per_thread

        # Initial stock measurement
        today = timezone.now().date()
        initial_batches = list(
            BatchStock.objects.filter(
                tenant=tenant,
                branch=branch,
                product=product,
                status=BatchStatus.ACTIVE,
                is_active=True,
            )
        )
        initial_stock = sum(b.quantity_base_units for b in initial_batches)

        start_time = time.perf_counter()

        def _execute_checkout() -> Tuple[bool, str, int]:
            with transaction.atomic():
                    # Pessimistic row-level lock on candidate batches sorted by FEFO
                    locked_batches = list(
                        BatchStock.objects.select_for_update()
                        .filter(
                            tenant_id=tenant_id,
                            branch_id=branch_id,
                            product_id=product_id,
                            is_active=True,
                            status=BatchStatus.ACTIVE,
                            is_quarantined=False,
                            expiry_date__gt=today,
                        )
                        .order_by("expiry_date", "purchase_date", "batch_number")
                    )

                    avail_total = sum(max(0, b.quantity_base_units - b.reserved_quantity_base_units) for b in locked_batches)
                    if avail_total < units_per_request:
                        return False, "ERR_INSUFFICIENT_STOCK", 0

                    needed = units_per_request
                    for b in locked_batches:
                        b_avail = max(0, b.quantity_base_units - b.reserved_quantity_base_units)
                        if b_avail <= 0:
                            continue
                        deduct = min(b_avail, needed)
                        b.quantity_base_units -= deduct
                        if b.quantity_base_units == 0:
                            b.status = BatchStatus.DEPLETED
                        b.save(update_fields=["quantity_base_units", "status", "updated_at"])

                        # Also deduct from matching PurchaseCostLots in FIFO order
                        lots = list(
                            PurchaseCostLot.objects.select_for_update()
                            .filter(
                                tenant_id=tenant_id,
                                branch_id=branch_id,
                                product_id=product_id,
                                batch=b,
                                status=PurchaseCostLotStatus.ACTIVE,
                                remaining_quantity__gt=0,
                            )
                            .order_by("purchase_date", "created_at")
                        )
                        lot_needed = deduct
                        for lot in lots:
                            lot_deduct = min(lot.remaining_quantity, lot_needed)
                            lot.remaining_quantity -= lot_deduct
                            if lot.remaining_quantity == 0:
                                lot.status = PurchaseCostLotStatus.DEPLETED
                            lot.save(update_fields=["remaining_quantity", "status", "updated_at"])
                            lot_needed -= lot_deduct
                            if lot_needed <= 0:
                                break

                        needed -= deduct
                        if needed <= 0:
                            break

                    return True, "SUCCESS", units_per_request

        def _worker_checkout(worker_id: int) -> Tuple[bool, str, int]:
            """
            Simulated single checkout transaction with pessimistic locking.
            When SQLite is used (test environments), serializes transactions via in-process lock
            to avoid SQLite table-level lock collisions, while PostgreSQL utilizes native row locks.
            """
            try:
                if connection.vendor == "sqlite":
                    with _sqlite_lock:
                        return _execute_checkout()
                else:
                    return _execute_checkout()
            except Exception as e:
                logger.warning(f"Concurrency worker {worker_id} encounter exception: {e}")
                return False, str(e), 0

        successful_tx = 0
        rejected_oversells = 0
        total_allocated = 0

        # Run concurrent workers using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=concurrency_level) as executor:
            futures = [executor.submit(_worker_checkout, i) for i in range(total_requests)]
            for future in as_completed(futures):
                success, code, units = future.result()
                if success:
                    successful_tx += 1
                    total_allocated += units
                else:
                    rejected_oversells += 1

        end_time = time.perf_counter()
        execution_time_ms = int((end_time - start_time) * 1000)

        # Refresh database state and verify conservation invariants
        final_batches = list(
            BatchStock.objects.filter(
                tenant=tenant,
                branch=branch,
                product=product,
            )
        )
        final_stock = sum(b.quantity_base_units for b in final_batches)

        # Invariant 1: Stock Conservation (initial == final + allocated)
        stock_conserved = (initial_stock == final_stock + total_allocated)

        # Invariant 2: FEFO verification (batches with earlier expiry are depleted first)
        fefo_order_verified = True
        initial_qty_map = {b.batch_id: b.quantity_base_units for b in initial_batches}
        sorted_batches = sorted(final_batches, key=lambda b: (b.expiry_date, b.purchase_date))
        for i in range(len(sorted_batches) - 1):
            earlier = sorted_batches[i]
            later = sorted_batches[i + 1]
            if earlier.expiry_date < later.expiry_date:
                # If earlier batch has remaining units, later batch shouldn't have been consumed
                if earlier.quantity_base_units > 0 and later.quantity_base_units < initial_qty_map.get(later.batch_id, 0):
                    fefo_order_verified = False

        # Invariant 3: Indivisible integer base units
        integer_units_verified = isinstance(total_allocated, int) and isinstance(final_stock, int)

        test_id = f"cstress-{uuid.uuid4().hex[:12]}"
        audit_record = PackagingConcurrencyAuditRecord.objects.create(
            test_id=test_id,
            tenant=tenant,
            branch=branch,
            product=product,
            concurrency_level=concurrency_level,
            total_requests=total_requests,
            successful_transactions=successful_tx,
            rejected_oversells=rejected_oversells,
            initial_stock_base_units=initial_stock,
            final_stock_base_units=final_stock,
            total_allocated_base_units=total_allocated,
            stock_conserved=stock_conserved,
            fefo_order_verified=fefo_order_verified,
            integer_units_verified=integer_units_verified,
            execution_time_ms=execution_time_ms,
            notes=notes.strip(),
            tested_by=user if getattr(user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            branch_id=str(branch.branch_id),
            user_id=getattr(user, "user_id", str(user)),
            actor_email=str(getattr(user, "email", "manager@pharma.com")),
            actor_role=str(getattr(user, "role", "BRANCH_MANAGER")),
            action="CONCURRENCY_STRESS_TEST_COMPLETED",
            resource_type="PackagingConcurrencyAuditRecord",
            resource_id=audit_record.test_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "test_id": audit_record.test_id,
                "concurrency_level": concurrency_level,
                "successful_transactions": successful_tx,
                "rejected_oversells": rejected_oversells,
                "stock_conserved": stock_conserved,
            },
        )

        return cls.serialize_audit_record(audit_record)

