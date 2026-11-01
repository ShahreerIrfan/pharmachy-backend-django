"""
apps/core/synchronous_sale_service.py
Architecture Decision: ADR-0002, ADR-0003 & ADR-0031
Phase 02: Repository, Toolchain & Engineering Automation

Synchronous Transactional Sale & Stock Ledger Service:
1. All sale finalizations, stock deductions, and financial invoice postings execute
   synchronously within django.db.transaction.atomic() with PostgreSQL row-level locks.
2. Expired batches are blocked from dispensing (DGDA compliance).
3. Zero oversell guarantee via atomic quantity decrements.
4. Asynchronous side-effects (receipt SMS, daily analytics aggregation) are scheduled
   strictly post-commit via transaction.on_commit(). If the transaction rolls back,
   no background tasks are dispatched.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.celery_boundary import AsyncJobBoundaryEnforcer
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InvoiceStatus,
    JobCategory,
    PaymentMethod,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
)
from apps.core.postgres_service import (
    BatchExpiredError,
    CrossTenantOperationError,
    InsufficientStockError,
)

logger = logging.getLogger("pharmacy.sale_service")


class SynchronousSaleService:
    """
    Executes synchronous, transactional retail checkout with atomic stock deductions.
    Architecture Decision: ADR-0031.
    """

    @classmethod
    def execute_checkout(
        cls,
        tenant_id: str,
        branch_id: str,
        items_data: list[dict[str, Any]],
        customer_name: str = "Walk-in Customer",
        customer_phone: str = "",
        payment_method: str = PaymentMethod.CASH,
        discount_amount: Decimal = Decimal("0.00"),
        execute_async_inline: bool = True,
    ) -> SaleInvoice:
        """
        Executes a complete counter checkout transaction.
        Synchronous & ACID: Locks stock rows, deducts quantity, creates invoice, commits.
        Post-commit side-effects: Enqueues SMS notification and derived analytics refresh.
        """
        if not items_data:
            raise ValueError("Checkout cannot proceed with an empty cart.")

        # 1. Validate tenant and branch
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id, is_active=True)
        except Tenant.DoesNotExist as err:
            raise PermissionError(f"Tenant '{tenant_id}' is inactive or does not exist.") from err

        if not tenant.is_checkout_allowed():
            raise PermissionError(
                f"Checkout rejected: Tenant '{tenant_id}' is in status '{tenant.status}'. Dispensing operations are blocked."
            )

        try:
            branch = Branch.objects.get(branch_id=branch_id, tenant=tenant, is_active=True)
        except Branch.DoesNotExist as err:
            raise CrossTenantOperationError(f"Branch '{branch_id}' does not belong to tenant '{tenant_id}'.") from err

        # Validate input quantities
        for item in items_data:
            qty = int(item.get("quantity", 0))
            if qty <= 0:
                raise ValueError(f"Checkout quantity must be positive. Received: {qty}")

        # 2. Synchronous Transactional Execution
        with transaction.atomic():
            total_mrp = Decimal("0.00")
            line_allocations = []

            for item in items_data:
                batch_id = item["batch_id"]
                qty = int(item["quantity"])

                # Select for update row-level lock
                try:
                    batch = BatchStock.objects.select_for_update().get(batch_id=batch_id)
                except BatchStock.DoesNotExist as err:
                    raise ValueError(f"Batch '{batch_id}' not found.") from err

                # Cross-tenant boundary check
                if batch.tenant_id != tenant_id or batch.branch_id != branch_id:
                    raise CrossTenantOperationError(
                        f"Cross-tenant violation: Batch '{batch_id}' belongs to tenant '{batch.tenant_id}', "
                        f"not requesting tenant '{tenant_id}'."
                    )

                # DGDA compliance check: Block expired stock
                if batch.is_expired or batch.status == BatchStatus.EXPIRED:
                    raise BatchExpiredError(
                        f"DGDA compliance violation: Batch '{batch.batch_number}' expired on {batch.expiry_date}. "
                        f"Dispensing of expired pharmaceuticals is strictly prohibited."
                    )

                # Stock availability check
                if batch.quantity_base_units < qty:
                    raise InsufficientStockError(
                        f"Insufficient stock for batch '{batch.batch_number}'. "
                        f"Available: {batch.quantity_base_units}, Requested: {qty}."
                    )

                # Deduct stock synchronously
                batch.quantity_base_units -= qty
                batch.save(update_fields=["quantity_base_units"])

                unit_price = batch.product.mrp_unit
                subtotal = unit_price * Decimal(qty)
                total_mrp += subtotal

                line_allocations.append(
                    {
                        "batch": batch,
                        "product": batch.product,
                        "quantity": qty,
                        "unit_price": unit_price,
                        "subtotal": subtotal,
                    }
                )

            net_amount = max(Decimal("0.00"), total_mrp - discount_amount)
            invoice_id = f"inv_{uuid.uuid4().hex[:16]}"
            invoice_num = f"INV-{timezone.now().strftime('%Y%m%d')}-{invoice_id[-6:].upper()}"

            invoice = SaleInvoice.objects.create(
                invoice_id=invoice_id,
                tenant=tenant,
                branch=branch,
                invoice_number=invoice_num,
                customer_name=customer_name,
                customer_phone=customer_phone,
                total_amount=total_mrp,
                discount_amount=discount_amount,
                net_amount=net_amount,
                payment_method=payment_method,
                status=InvoiceStatus.FINALIZED,
            )

            for alloc in line_allocations:
                SaleInvoiceItem.objects.create(
                    invoice=invoice,
                    batch=alloc["batch"],
                    product=alloc["product"],
                    quantity=alloc["quantity"],
                    unit_price=alloc["unit_price"],
                    subtotal=alloc["subtotal"],
                )

            # 3. Schedule post-commit side-effects (Only fired IF transaction commits)
            today_str = timezone.now().strftime("%Y-%m-%d")

            def _post_commit_side_effects():
                # Side-effect 1: Customer receipt notification
                if customer_phone:
                    AsyncJobBoundaryEnforcer.enqueue_job(
                        category=JobCategory.NOTIFICATION,
                        task_name="send_sale_notification_task",
                        tenant_id=tenant_id,
                        branch_id=branch_id,
                        payload={
                            "invoice_id": invoice.invoice_id,
                            "recipient_phone": customer_phone,
                        },
                        execute_inline=execute_async_inline,
                    )

                # Side-effect 2: Derived daily sales analytics refresh
                AsyncJobBoundaryEnforcer.enqueue_job(
                    category=JobCategory.DERIVED_ANALYTICS,
                    task_name="aggregate_daily_sales_analytics_task",
                    tenant_id=tenant_id,
                    branch_id=branch_id,
                    payload={"date_str": today_str},
                    execute_inline=execute_async_inline,
                )

            transaction.on_commit(_post_commit_side_effects)

        return invoice
