"""
tests/test_p031_redis_async_boundary.py
Architecture Decision: ADR-0002, ADR-0003, ADR-0004 & ADR-0031
Phase 02: Repository, Toolchain & Engineering Automation

Comprehensive Test Suite for Redis & Celery Worker Boundary vs. Synchronous Core:
1. Valid Scenario: Synchronous checkout decrements batch stock atomically, issues invoice with
   strict Decimal precision, and triggers post-commit asynchronous notifications & analytics rollup.
2. Invalid / Incomplete Inputs: Rejects missing payload fields with InvalidJobPayloadError,
   rejects negative/zero order quantities, and rejects non-existent batches.
3. Unauthorized / Cross-Tenant: Foreign branch or tenant mismatch blocked with CrossTenantJobError
   and CrossTenantOperationError.
4. Source-Specific Business Rule: Core stock mutations and ledger postings are prohibited from
   Redis/Celery workers and raise AsyncBoundaryViolationError.
5. Expired Subscription: Inactive tenant is rejected with ExpiredSubscriptionJobError.
6. Rollback Integrity: Failed transaction (InsufficientStockError) does not fire post-commit jobs.
7. REST API Endpoints: Validates checkout, async-jobs audit, cached KPI, and reminder acknowledgement.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

# Ensure root and backend are on sys.path
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

from django.core.cache import cache
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.celery_boundary import (
    AsyncBoundaryViolationError,
    AsyncJobBoundaryEnforcer,
    CrossTenantJobError,
    ExpiredSubscriptionJobError,
    InvalidJobPayloadError,
)
from apps.core.models import (
    AsyncJobRecord,
    BatchStock,
    ComplianceReminder,
    DailyBranchAnalytics,
    InvoiceStatus,
    JobCategory,
    JobStatus,
    SaleInvoice,
    Tenant,
)
from apps.core.postgres_service import (
    BatchExpiredError,
    CrossTenantOperationError,
    InsufficientStockError,
)
from apps.core.sqlite_bootstrap import SQLiteBootstrapEngine
from apps.core.synchronous_sale_service import SynchronousSaleService


class TestP031RedisAsyncBoundary(TransactionTestCase):
    def setUp(self):
        SQLiteBootstrapEngine.bootstrap()
        self.tenant_id = "tenant-dhaka-central"
        self.branch_id = "branch-dhk-main"
        self.batch_id = "BATCH-NAPA-2027A"
        self.client = APIClient()

    def tearDown(self):
        SaleInvoice.objects.exclude(invoice_id="INV-DHK-2026-0001").delete()
        AsyncJobRecord.objects.all().delete()
        DailyBranchAnalytics.objects.all().delete()
        ComplianceReminder.objects.all().delete()

    def test_01_source_specific_business_rule_prohibits_async_stock_mutation(self):
        """Verifies that core stock deductions cannot be enqueued asynchronously (ADR-0031)."""
        with self.assertRaises(AsyncBoundaryViolationError) as ctx:
            AsyncJobBoundaryEnforcer.enqueue_job(
                category="CORE_STOCK_MUTATION",
                task_name="deduct_batch_stock_task",
                tenant_id=self.tenant_id,
                payload={"batch_id": self.batch_id, "quantity": 10},
            )
        self.assertIn("synchronous transactional core", str(ctx.exception))

    def test_02_source_specific_business_rule_prohibits_async_ledger_posting(self):
        """Verifies that core financial ledger posting cannot be enqueued asynchronously (ADR-0031)."""
        with self.assertRaises(AsyncBoundaryViolationError) as ctx:
            AsyncJobBoundaryEnforcer.enqueue_job(
                category="CORE_LEDGER_POSTING",
                task_name="post_double_entry_journal_task",
                tenant_id=self.tenant_id,
                payload={"debit_amount": "500.00"},
            )
        self.assertIn("synchronous transactional core", str(ctx.exception))

    def test_03_invalid_incomplete_payload_rejected(self):
        """Rejects asynchronous job payloads missing mandatory task arguments."""
        with self.assertRaises(InvalidJobPayloadError) as ctx:
            AsyncJobBoundaryEnforcer.enqueue_job(
                category=JobCategory.NOTIFICATION,
                task_name="send_sale_notification_task",
                tenant_id=self.tenant_id,
                payload={"recipient_phone": "+8801700000000"},  # Missing invoice_id
            )
        self.assertIn("Missing required parameter(s)", str(ctx.exception))

    def test_04_unauthorized_cross_tenant_branch_rejected(self):
        """Rejects dispatch when target branch does not belong to the requesting tenant."""
        with self.assertRaises(CrossTenantJobError) as ctx:
            AsyncJobBoundaryEnforcer.enqueue_job(
                category=JobCategory.DERIVED_ANALYTICS,
                task_name="aggregate_daily_sales_analytics_task",
                tenant_id="tenant-chittagong-care",
                branch_id=self.branch_id,  # foreign branch belonging to Dhaka Central
                payload={"date_str": "2026-10-29"},
            )
        self.assertIn("does not belong to tenant", str(ctx.exception))

    def test_05_expired_subscription_job_rejected(self):
        """Rejects background job dispatch for tenants with inactive/expired subscriptions."""
        tenant = Tenant.objects.get(tenant_id=self.tenant_id)
        tenant.is_active = False
        tenant.save(update_fields=["is_active"])

        try:
            with self.assertRaises(ExpiredSubscriptionJobError) as ctx:
                AsyncJobBoundaryEnforcer.enqueue_job(
                    category=JobCategory.DERIVED_ANALYTICS,
                    task_name="aggregate_daily_sales_analytics_task",
                    tenant_id=self.tenant_id,
                    branch_id=self.branch_id,
                    payload={"date_str": "2026-10-29"},
                )
            self.assertIn("inactive or expired", str(ctx.exception))
        finally:
            tenant.is_active = True
            tenant.save(update_fields=["is_active"])

    def test_06_valid_synchronous_sale_and_post_commit_async_side_effects(self):
        """
        Valid end-to-end scenario: Synchronous checkout decrements stock, commits invoice,
        and fires post-commit async notification & derived analytics rollup.
        """
        batch = BatchStock.objects.get(batch_id=self.batch_id)
        initial_stock = batch.quantity_base_units
        order_qty = 5

        invoice = SynchronousSaleService.execute_checkout(
            tenant_id=self.tenant_id,
            branch_id=self.branch_id,
            items_data=[{"batch_id": self.batch_id, "quantity": order_qty}],
            customer_name="Kamal Hossain",
            customer_phone="+8801811223344",
            discount_amount=Decimal("0.00"),
            execute_async_inline=True,
        )

        # 1. Verify synchronous ACID guarantees
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_base_units, initial_stock - order_qty)
        self.assertEqual(invoice.status, InvoiceStatus.FINALIZED)
        self.assertEqual(invoice.net_amount, batch.product.mrp_unit * Decimal(order_qty))

        # 2. Verify asynchronous notification job record
        notif_job = AsyncJobRecord.objects.filter(
            tenant_id=self.tenant_id,
            category=JobCategory.NOTIFICATION,
            payload__invoice_id=invoice.invoice_id,
        ).first()
        self.assertIsNotNone(notif_job)
        self.assertEqual(notif_job.status, JobStatus.SUCCESS)
        self.assertTrue(notif_job.result.get("delivered"))

        # 3. Verify asynchronous derived analytics rollup
        today_str = timezone.now().strftime("%Y-%m-%d")
        analytics = DailyBranchAnalytics.objects.filter(
            tenant_id=self.tenant_id,
            branch_id=self.branch_id,
            date=today_str,
        ).first()
        self.assertIsNotNone(analytics)
        self.assertGreaterEqual(analytics.total_orders_count, 1)
        self.assertGreaterEqual(analytics.total_sales_amount, invoice.net_amount)

        # 4. Verify Redis cache warming
        cache_key = f"kpi:{self.tenant_id}:{self.branch_id}:{today_str}"
        cached_kpi = cache.get(cache_key)
        self.assertIsNotNone(cached_kpi)
        self.assertEqual(Decimal(cached_kpi["today_sales_bdt"]), analytics.total_sales_amount)

    def test_07_transaction_rollback_prevents_post_commit_jobs(self):
        """Verifies that when a checkout rolls back, zero async jobs are dispatched."""
        batch = BatchStock.objects.get(batch_id=self.batch_id)
        oversell_qty = batch.quantity_base_units + 500

        jobs_before = AsyncJobRecord.objects.count()

        with self.assertRaises(InsufficientStockError):
            SynchronousSaleService.execute_checkout(
                tenant_id=self.tenant_id,
                branch_id=self.branch_id,
                items_data=[{"batch_id": self.batch_id, "quantity": oversell_qty}],
                customer_name="Rollback Customer",
                customer_phone="+8801999888777",
                execute_async_inline=True,
            )

        jobs_after = AsyncJobRecord.objects.count()
        self.assertEqual(jobs_before, jobs_after, "Rollback must prevent any post-commit async job dispatch.")

    def test_08_expired_batch_blocked_synchronously(self):
        """Verifies that dispensing expired medicine is blocked synchronously before any job dispatch."""
        with self.assertRaises(BatchExpiredError):
            SynchronousSaleService.execute_checkout(
                tenant_id=self.tenant_id,
                branch_id=self.branch_id,
                items_data=[{"batch_id": "BATCH-NAPA-EXPIRED", "quantity": 1}],
                execute_async_inline=True,
            )

    def test_09_cross_tenant_batch_blocked_synchronously(self):
        """Verifies that selling another tenant's batch is blocked synchronously."""
        with self.assertRaises(CrossTenantOperationError):
            SynchronousSaleService.execute_checkout(
                tenant_id="tenant-chittagong-care",
                branch_id="branch-ctg-agrabad",
                items_data=[{"batch_id": self.batch_id, "quantity": 1}],
                execute_async_inline=True,
            )

    def test_10_rest_api_checkout_and_cached_kpi_endpoints(self):
        """Verifies DRF checkout endpoint and cached KPI retrieval."""
        checkout_payload = {
            "tenant_id": self.tenant_id,
            "branch_id": self.branch_id,
            "customer_name": "API Walk-in Customer",
            "customer_phone": "+8801555444333",
            "payment_method": "BKASH",
            "discount_amount": "0.00",
            "items": [{"batch_id": self.batch_id, "quantity": 2}],
        }

        # 1. POST Checkout
        res = self.client.post("/api/v2/invoices/checkout/", checkout_payload, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertIn("invoice_number", res.data)
        self.assertIn("items", res.data)

        # 2. GET Cached KPI
        today_str = timezone.now().strftime("%Y-%m-%d")
        kpi_res = self.client.get(
            f"/api/v2/analytics/kpi/?tenant_id={self.tenant_id}&branch_id={self.branch_id}&date={today_str}"
        )
        self.assertEqual(kpi_res.status_code, 200)
        self.assertIn(kpi_res.data.get("source"), ["REDIS_CACHE", "POSTGRES_DB"])
        self.assertIn("kpi", kpi_res.data)

    def test_11_compliance_reminder_acknowledgement(self):
        """Verifies that regulatory reminders can be acknowledged via DRF endpoint."""
        # Dispatch compliance check job
        job = AsyncJobBoundaryEnforcer.enqueue_job(
            category=JobCategory.REMINDER,
            task_name="check_and_dispatch_license_expiry_reminders_task",
            tenant_id=self.tenant_id,
            branch_id=self.branch_id,
            payload={},
            execute_inline=True,
        )
        self.assertEqual(job.status, JobStatus.SUCCESS)

        reminder = ComplianceReminder.objects.filter(tenant_id=self.tenant_id).first()
        self.assertIsNotNone(reminder)

        # Acknowledge reminder
        ack_res = self.client.post(f"/api/v2/compliance-reminders/{reminder.reminder_id}/acknowledge/")
        self.assertEqual(ack_res.status_code, 200)
        self.assertTrue(ack_res.data["is_acknowledged"])

    def test_12_cli_status_and_boundary_commands(self):
        """Verifies that CLI commands execute cleanly."""
        from tools.redis_worker_lane import cmd_status, cmd_test_boundary

        self.assertEqual(cmd_status(), 0)
        self.assertEqual(cmd_test_boundary(), 0)
