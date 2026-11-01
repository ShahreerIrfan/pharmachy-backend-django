"""
backend/apps/core/owner_dashboard_service.py

Architecture Decision: ADR-0101
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility (P101)
Requirement Trace: FR-031, FR-032, FR-033, FR-044, FR-066, FR-070

Tenant-Scoped Owner Dashboard Service:
- Computes authoritative sales, gross profit, net profit, Cost of Goods Sold (COGS),
  and gross margins directly from finalized transactions.
- Tracks customer retail dues, credit sales, and repayments (CustomerDueRecord).
- Evaluates live inventory value, out-of-stock, and low-stock items.
- Identifies DGDA expiry risks (<30 days, <90 days, expired/quarantined batches).
- Aggregates clinical prescription activity (pending vs verified).
- Monitors subscription state, grace periods, and mutation locking.
- Tracks POS sync health (registered devices, offline leases, hold queues).
- Enforces strict multi-tenant boundaries and branch-level scoping.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    CustomerDueRecord,
    DeviceLeaseCertificate,
    DeviceSeat,
    DueStatus,
    InvoiceStatus,
    LapsedSubscriptionHoldQueue,
    OfflineLeaseStatus,
    PrescriptionRecord,
    PrescriptionStatus,
    SaleInvoice,
    SaleInvoiceItem,
    SubscriptionRecord,
    SubscriptionStatus,
    Tenant,
)

logger = logging.getLogger("pharmacy.owner_dashboard")


class TenantOwnerDashboardService:
    """Authoritative service for compiling tenant-scoped and branch-scoped executive dashboard metrics."""

    @classmethod
    def get_dashboard_summary(
        cls,
        tenant_id: str,
        branch_id: str | None = None,
        date_from: datetime.date | None = None,
        date_to: datetime.date | None = None,
    ) -> dict[str, Any]:
        """
        Compiles the full executive dashboard summary for a pharmacy tenant.
        """
        clean_tenant_id = str(tenant_id or "").strip()
        try:
            tenant = Tenant.objects.get(tenant_id=clean_tenant_id)
        except Tenant.DoesNotExist as err:
            raise ValidationError(f"Tenant '{clean_tenant_id}' does not exist.") from err

        branch = None
        if branch_id:
            clean_branch_id = str(branch_id).strip()
            try:
                branch = Branch.objects.get(branch_id=clean_branch_id, tenant=tenant)
            except Branch.DoesNotExist as err:
                raise ValidationError(f"Branch '{clean_branch_id}' does not belong to tenant '{clean_tenant_id}'.") from err

        now = timezone.now()
        today = now.date()
        target_date_from = date_from or today
        target_date_to = date_to or today

        if target_date_from > target_date_to:
            raise ValidationError("date_from cannot be after date_to.")

        # ---------------------------------------------------------------------
        # 1. Sales & Orders
        # ---------------------------------------------------------------------
        invoices_base = SaleInvoice.objects.filter(tenant=tenant, status=InvoiceStatus.FINALIZED)
        if branch:
            invoices_base = invoices_base.filter(branch=branch)

        today_invoices = invoices_base.filter(created_at__date=today)
        period_invoices = invoices_base.filter(
            created_at__date__gte=target_date_from,
            created_at__date__lte=target_date_to,
        )

        today_sales_sum = today_invoices.aggregate(s=Sum("net_amount"))["s"] or Decimal("0.00")
        period_sales_sum = period_invoices.aggregate(s=Sum("net_amount"))["s"] or Decimal("0.00")
        total_orders = period_invoices.count()
        avg_order_value = (
            (period_sales_sum / Decimal(total_orders)).quantize(Decimal("0.01"))
            if total_orders > 0
            else Decimal("0.00")
        )

        sales_summary = {
            "today_sales_bdt": f"{today_sales_sum:.2f}",
            "period_sales_bdt": f"{period_sales_sum:.2f}",
            "total_orders_count": total_orders,
            "average_order_value_bdt": f"{avg_order_value:.2f}",
        }

        # ---------------------------------------------------------------------
        # 2. Profitability & COGS
        # ---------------------------------------------------------------------
        gross_sales_sum = period_invoices.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")
        discounts_sum = period_invoices.aggregate(s=Sum("discount_amount"))["s"] or Decimal("0.00")

        # Atomic COGS calculation: sum(item.quantity * batch.cost_per_unit)
        period_items = SaleInvoiceItem.objects.filter(invoice__in=period_invoices).select_related("batch")
        cogs_sum = Decimal("0.00")
        for item in period_items:
            cost = item.batch.cost_per_unit if item.batch else Decimal("0.00")
            cogs_sum += Decimal(item.quantity) * cost
        cogs_sum = cogs_sum.quantize(Decimal("0.01"))

        gross_profit = (period_sales_sum - cogs_sum).quantize(Decimal("0.01"))
        gross_margin = (
            ((gross_profit / period_sales_sum) * Decimal("100.00")).quantize(Decimal("0.01"))
            if period_sales_sum > Decimal("0.00")
            else Decimal("0.00")
        )
        net_profit = gross_profit  # In retail POS, net sales already deducts item/invoice discounts

        profitability_summary = {
            "gross_sales_bdt": f"{gross_sales_sum:.2f}",
            "cogs_bdt": f"{cogs_sum:.2f}",
            "gross_profit_bdt": f"{gross_profit:.2f}",
            "gross_margin_percent": f"{gross_margin:.2f}",
            "discounts_bdt": f"{discounts_sum:.2f}",
            "net_profit_bdt": f"{net_profit:.2f}",
        }

        # ---------------------------------------------------------------------
        # 3. Customer Dues & Credit
        # ---------------------------------------------------------------------
        dues_base = CustomerDueRecord.objects.filter(tenant=tenant)
        if branch:
            dues_base = dues_base.filter(branch=branch)

        unsettled_dues = dues_base.filter(status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL])
        total_dues_sum = unsettled_dues.aggregate(s=Sum("remaining_due_bdt"))["s"] or Decimal("0.00")
        outstanding_customers_count = unsettled_dues.values("customer_phone").distinct().count()
        overdue_invoices_count = unsettled_dues.filter(due_date__lt=today).count()

        customer_dues_summary = {
            "total_dues_bdt": f"{total_dues_sum:.2f}",
            "outstanding_customers_count": outstanding_customers_count,
            "overdue_invoices_count": overdue_invoices_count,
        }

        # ---------------------------------------------------------------------
        # 4. Inventory Valuation & Stock Alerts
        # ---------------------------------------------------------------------
        active_batches = BatchStock.objects.filter(tenant=tenant, status=BatchStatus.ACTIVE)
        if branch:
            active_batches = active_batches.filter(branch=branch)

        total_stock_value = Decimal("0.00")
        for b in active_batches:
            total_stock_value += Decimal(b.quantity_base_units) * b.cost_per_unit
        total_stock_value = total_stock_value.quantize(Decimal("0.01"))

        # Low-stock (<10 units) and out-of-stock (0 units) calculations
        product_stock = (
            active_batches.values("product_id")
            .annotate(total_units=Sum("quantity_base_units"))
        )
        low_stock_count = sum(1 for p in product_stock if 0 < p["total_units"] <= 10)
        out_of_stock_count = sum(1 for p in product_stock if p["total_units"] <= 0)

        inventory_summary = {
            "total_stock_value_bdt": f"{total_stock_value:.2f}",
            "active_batches_count": active_batches.count(),
            "low_stock_items_count": low_stock_count,
            "out_of_stock_items_count": out_of_stock_count,
        }

        # ---------------------------------------------------------------------
        # 5. DGDA Expiry Risk Monitoring
        # ---------------------------------------------------------------------
        all_batches = BatchStock.objects.filter(tenant=tenant)
        if branch:
            all_batches = all_batches.filter(branch=branch)

        in_30_days = today + datetime.timedelta(days=30)
        in_90_days = today + datetime.timedelta(days=90)

        exp_30_count = all_batches.filter(
            status=BatchStatus.ACTIVE,
            expiry_date__gt=today,
            expiry_date__lte=in_30_days,
        ).count()

        exp_90_count = all_batches.filter(
            status=BatchStatus.ACTIVE,
            expiry_date__gt=today,
            expiry_date__lte=in_90_days,
        ).count()

        expired_batches = all_batches.filter(
            Q(expiry_date__lte=today) | Q(status__in=[BatchStatus.EXPIRED, BatchStatus.QUARANTINED])
        )
        expired_count = expired_batches.count()
        expired_stock_value = sum(
            (Decimal(b.quantity_base_units) * b.cost_per_unit for b in expired_batches),
            Decimal("0.00"),
        ).quantize(Decimal("0.01"))

        expiry_summary = {
            "expiring_in_30_days_count": exp_30_count,
            "expiring_in_90_days_count": exp_90_count,
            "expired_batches_count": expired_count,
            "expired_stock_value_bdt": f"{expired_stock_value:.2f}",
        }

        # ---------------------------------------------------------------------
        # 6. Clinical Prescription Activity
        # ---------------------------------------------------------------------
        rx_base = PrescriptionRecord.objects.filter(tenant=tenant)
        if branch:
            rx_base = rx_base.filter(branch=branch)

        pending_rx_count = rx_base.filter(status=PrescriptionStatus.PENDING).count()
        verified_today_count = rx_base.filter(
            status=PrescriptionStatus.VERIFIED,
            verified_at__date=today,
        ).count()

        prescription_summary = {
            "pending_rx_count": pending_rx_count,
            "verified_today_count": verified_today_count,
            "total_rx_count": rx_base.count(),
        }

        # ---------------------------------------------------------------------
        # 7. Subscription State & Entitlements
        # ---------------------------------------------------------------------
        sub = SubscriptionRecord.objects.filter(tenant=tenant).order_by("-created_at").first()
        plan_tier = sub.plan_tier if sub else tenant.plan_tier
        sub_status = sub.status if sub else "TRIAL"
        renewal_date_str = sub.renewal_date.isoformat() if (sub and sub.renewal_date) else None
        is_grace_period = sub_status == SubscriptionStatus.GRACE_PERIOD
        days_until_renewal = (
            (sub.renewal_date - today).days
            if (sub and sub.renewal_date and sub.renewal_date >= today)
            else 0
        )
        is_mutation_locked = sub_status in [SubscriptionStatus.EXPIRED, SubscriptionStatus.SUSPENDED]

        subscription_summary = {
            "plan_tier": plan_tier,
            "status": sub_status,
            "renewal_date": renewal_date_str,
            "is_grace_period": is_grace_period,
            "days_until_renewal": days_until_renewal,
            "is_mutation_locked": is_mutation_locked,
        }

        # ---------------------------------------------------------------------
        # 8. POS Sync Health & Workstation Connectivity
        # ---------------------------------------------------------------------
        seats_base = DeviceSeat.objects.filter(tenant=tenant)
        leases_base = DeviceLeaseCertificate.objects.filter(tenant=tenant, status=OfflineLeaseStatus.ACTIVE)
        held_queue = LapsedSubscriptionHoldQueue.objects.filter(tenant=tenant, hold_status="HELD")

        if branch:
            seats_base = seats_base.filter(branch=branch)
            leases_base = leases_base.filter(branch=branch)
            held_queue = held_queue.filter(branch=branch)

        unreconciled_count = held_queue.count()
        sync_status = "HEALTHY" if unreconciled_count == 0 else "NEEDS_SYNC"

        # Find latest device heartbeat or sync
        latest_seat = seats_base.order_by("-last_heartbeat_at").first()
        last_sync_at = (
            latest_seat.last_heartbeat_at.isoformat()
            if (latest_seat and latest_seat.last_heartbeat_at)
            else now.isoformat()
        )

        sync_health_summary = {
            "enrolled_devices_count": seats_base.count(),
            "active_leases_count": leases_base.count(),
            "unreconciled_held_count": unreconciled_count,
            "sync_status": sync_status,
            "last_sync_at": last_sync_at,
        }

        # ---------------------------------------------------------------------
        # 9. Multi-Branch Overview
        # ---------------------------------------------------------------------
        branches_overview = []
        for b in Branch.objects.filter(tenant=tenant, is_active=True).order_by("name"):
            b_invoices = invoices_base.filter(branch=b)
            b_sales = b_invoices.aggregate(s=Sum("net_amount"))["s"] or Decimal("0.00")
            branches_overview.append(
                {
                    "branch_id": b.branch_id,
                    "branch_name": b.name,
                    "sales_bdt": f"{b_sales:.2f}",
                    "orders_count": b_invoices.count(),
                    "is_active": b.is_active,
                }
            )

        return {
            "tenant_id": tenant.tenant_id,
            "tenant_name": tenant.name,
            "branch_filter": branch.branch_id if branch else None,
            "date_range": {
                "date_from": target_date_from.isoformat(),
                "date_to": target_date_to.isoformat(),
            },
            "sales_summary": sales_summary,
            "profitability_summary": profitability_summary,
            "customer_dues_summary": customer_dues_summary,
            "inventory_summary": inventory_summary,
            "expiry_summary": expiry_summary,
            "prescription_summary": prescription_summary,
            "subscription_summary": subscription_summary,
            "sync_health_summary": sync_health_summary,
            "branches_overview": branches_overview,
            "as_of_timestamp": now.isoformat(),
        }

    @classmethod
    def record_customer_due(
        cls,
        tenant_id: str,
        branch_id: str,
        customer_name: str,
        customer_phone: str,
        total_due_bdt: Decimal | str | float,
        invoice_id: str | None = None,
        due_date: datetime.date | None = None,
        notes: str = "",
    ) -> CustomerDueRecord:
        """
        Records a customer credit/due balance tied to a branch sale or manual credit ledger.
        """
        clean_tenant_id = str(tenant_id).strip()
        tenant = Tenant.objects.get(tenant_id=clean_tenant_id)

        clean_branch_id = str(branch_id).strip()
        branch = Branch.objects.get(branch_id=clean_branch_id, tenant=tenant)

        amount = Decimal(str(total_due_bdt or "0.00")).quantize(Decimal("0.01"))
        if amount <= Decimal("0.00"):
            raise ValidationError("Due amount must be greater than zero.")

        clean_name = str(customer_name or "").strip()
        if len(clean_name) < 2:
            raise ValidationError("Customer name (minimum 2 characters) is required.")

        clean_phone = str(customer_phone or "").strip()
        if len(clean_phone) < 6:
            raise ValidationError("Valid customer contact number is required.")

        invoice = None
        if invoice_id:
            clean_inv_id = str(invoice_id).strip()
            try:
                invoice = SaleInvoice.objects.get(invoice_id=clean_inv_id, tenant=tenant)
            except SaleInvoice.DoesNotExist as err:
                raise ValidationError(f"Sale invoice '{clean_inv_id}' does not exist for tenant '{clean_tenant_id}'.") from err

        due_id = f"due-{uuid.uuid4().hex[:12]}"

        record = CustomerDueRecord.objects.create(
            due_id=due_id,
            tenant=tenant,
            branch=branch,
            invoice=invoice,
            customer_name=clean_name,
            customer_phone=clean_phone,
            total_due_bdt=amount,
            paid_amount_bdt=Decimal("0.00"),
            remaining_due_bdt=amount,
            status=DueStatus.OUTSTANDING,
            due_date=due_date,
            notes=str(notes or "").strip(),
        )
        logger.info(
            "Recorded customer due %s for %s (%s BDT) at branch %s",
            due_id,
            clean_name,
            amount,
            clean_branch_id,
        )
        return record

    @classmethod
    def settle_customer_due(
        cls,
        due_id: str,
        payment_amount_bdt: Decimal | str | float,
        actor_user: Any = None,
    ) -> CustomerDueRecord:
        """
        Settles all or part of an outstanding customer due balance.
        """
        clean_due_id = str(due_id).strip()
        try:
            record = CustomerDueRecord.objects.get(due_id=clean_due_id)
        except CustomerDueRecord.DoesNotExist as err:
            raise ValidationError(f"Customer due record '{clean_due_id}' does not exist.") from err

        if record.status == DueStatus.SETTLED:
            raise ValidationError(f"Due record '{clean_due_id}' has already been settled.")

        payment = Decimal(str(payment_amount_bdt or "0.00")).quantize(Decimal("0.01"))
        if payment <= Decimal("0.00"):
            raise ValidationError("Payment amount must be greater than zero.")

        if payment > record.remaining_due_bdt:
            raise ValidationError(
                f"Payment amount ({payment} BDT) exceeds remaining due ({record.remaining_due_bdt} BDT)."
            )

        record.paid_amount_bdt = (record.paid_amount_bdt + payment).quantize(Decimal("0.01"))
        record.remaining_due_bdt = (record.remaining_due_bdt - payment).quantize(Decimal("0.01"))

        if record.remaining_due_bdt == Decimal("0.00"):
            record.status = DueStatus.SETTLED
        else:
            record.status = DueStatus.PARTIAL

        record.save(update_fields=["paid_amount_bdt", "remaining_due_bdt", "status", "updated_at"])
        logger.info(
            "Settled %s BDT against due %s. Remaining: %s BDT (%s)",
            payment,
            clean_due_id,
            record.remaining_due_bdt,
            record.status,
        )
        return record
