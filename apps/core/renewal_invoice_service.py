"""
backend/apps/core/renewal_invoice_service.py
Architecture Decision: ADR-0015, ADR-0033, ADR-0084, ADR-0085 & ADR-0087
Phase 05: Basic/Standard/Pro Subscription & Billing (P087)

Renewal Invoice Generation Service:
- Generates renewal invoices with correct coverage horizons (monthly/annual).
- Computes subtotal, annual prepay discount, and statutory VAT (tax) precision in BDT.
- Generates unique, non-colliding human-readable invoice references.
- Enforces append-only immutable payment transition logs.
- Settles payment, extends subscription renewal date, and reactivates delinquent subscriptions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    PlanTier,
    RenewalInvoiceStatus,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    SubscriptionStatus,
    Tenant,
    TenantLifecycleStatus,
)

logger = logging.getLogger("pharmacy.billing.renewal_invoices")


class RenewalInvoiceService:
    PLAN_BASE_RATES = {
        PlanTier.BASIC: Decimal("2500.00"),
        PlanTier.STANDARD: Decimal("6500.00"),
        PlanTier.PRO: Decimal("15000.00"),
    }

    @classmethod
    def _calculate_period_end(cls, start_date: date, billing_cycle: str) -> date:
        """Calculates precise end date based on monthly or annual cadence."""
        cycle = str(billing_cycle or "MONTHLY").upper().strip()
        if cycle == "ANNUAL":
            try:
                return start_date.replace(year=start_date.year + 1)
            except ValueError:
                # Leap year handling (Feb 29 -> Feb 28)
                return start_date + timedelta(days=365)
        else:
            # 1 Calendar month cadence
            # Approximate by 30 days or next month day
            month = start_date.month + 1
            year = start_date.year
            if month > 12:
                month = 1
                year += 1
            try:
                return start_date.replace(year=year, month=month)
            except ValueError:
                return (start_date + timedelta(days=30))

    @classmethod
    def generate_renewal_invoice(
        cls,
        tenant_id: str,
        billing_cycle: str | None = None,
        tax_rate_percent: Decimal = Decimal("5.00"),
        operator: str = "system",
    ) -> SubscriptionRenewalInvoice:
        """
        Generates or retrieves existing idempotent renewal invoice for upcoming cycle.
        """
        clean_tid = str(tenant_id).strip()
        tenant = Tenant.objects.filter(tenant_id=clean_tid).first()
        if not tenant:
            raise ValidationError(f"Tenant '{clean_tid}' not found.")

        sub = SubscriptionRecord.objects.filter(tenant=tenant).first()
        if not sub:
            raise ValidationError(f"Tenant '{clean_tid}' does not have an active subscription record.")

        tier = sub.plan_tier or tenant.plan_tier or PlanTier.STANDARD
        cycle = str(billing_cycle or sub.billing_cycle or "MONTHLY").upper().strip()
        if cycle not in ["MONTHLY", "ANNUAL"]:
            raise ValidationError(f"Invalid billing cycle '{cycle}'. Must be MONTHLY or ANNUAL.")

        base_monthly = cls.PLAN_BASE_RATES.get(tier, Decimal("6500.00"))
        if cycle == "ANNUAL":
            subtotal = (base_monthly * Decimal("12.00")).quantize(Decimal("0.01"))
            # 2 months free annual prepay discount
            discount = (base_monthly * Decimal("2.00")).quantize(Decimal("0.01"))
        else:
            subtotal = base_monthly.quantize(Decimal("0.01"))
            discount = Decimal("0.00")

        taxable_amount = max(Decimal("0.00"), subtotal - discount)
        tax_rate = Decimal(str(tax_rate_percent)).quantize(Decimal("0.01"))
        if tax_rate < Decimal("0.00"):
            raise ValidationError("Tax rate cannot be negative.")
        tax_amount = (taxable_amount * tax_rate / Decimal("100.00")).quantize(Decimal("0.01"))
        total_bdt = (taxable_amount + tax_amount).quantize(Decimal("0.01"))

        period_start = sub.renewal_date if sub.renewal_date else timezone.now().date()
        period_end = cls._calculate_period_end(period_start, cycle)

        # Idempotent Deduplication: Check if open issued invoice already exists for period
        existing_invoice = SubscriptionRenewalInvoice.objects.filter(
            tenant=tenant,
            period_start=period_start,
            period_end=period_end,
            status__in=[RenewalInvoiceStatus.ISSUED, RenewalInvoiceStatus.DRAFT],
        ).first()

        if existing_invoice:
            logger.info(f"Returning existing open renewal invoice '{existing_invoice.invoice_number}' for tenant '{clean_tid}'.")
            return existing_invoice

        now_iso = timezone.now().isoformat()
        clean_suffix = clean_tid.split("-")[-1].upper() if "-" in clean_tid else clean_tid[-4:].upper()
        unique_ref = f"INV-REN-{period_start.strftime('%Y%m')}-{clean_suffix}-{uuid.uuid4().hex[:6].upper()}"

        initial_history = [
            {
                "from_status": "DRAFT",
                "to_status": "ISSUED",
                "timestamp": now_iso,
                "actor": operator,
                "reason": f"Automated {cycle} renewal invoice generation for {tier} plan.",
            }
        ]

        invoice = SubscriptionRenewalInvoice.objects.create(
            invoice_id=f"ren-inv-{uuid.uuid4().hex[:12]}",
            invoice_number=unique_ref,
            tenant=tenant,
            subscription=sub,
            plan_tier=tier,
            billing_cycle=cycle,
            period_start=period_start,
            period_end=period_end,
            subtotal_bdt=subtotal,
            discount_bdt=discount,
            tax_rate_percent=tax_rate,
            tax_amount_bdt=tax_amount,
            total_bdt=total_bdt,
            status=RenewalInvoiceStatus.ISSUED,
            status_history_json=initial_history,
        )

        AuditLogEntry.objects.create(
            action="RENEWAL_INVOICE_GENERATED",
            resource_type="BILLING_INVOICE",
            resource_id=invoice.invoice_number,
            tenant_id=tenant.tenant_id,
            user_id=operator,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_number": invoice.invoice_number,
                "total_bdt": str(total_bdt),
                "plan_tier": tier,
                "billing_cycle": cycle,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            },
        )

        return invoice

    @classmethod
    def settle_renewal_invoice(
        cls,
        invoice_id: str,
        payment_method: str,
        payment_reference: str,
        actor_user: Any = None,
    ) -> SubscriptionRenewalInvoice:
        """
        Settles renewal invoice, records immutable history, and extends subscription renewal horizon.
        """
        clean_inv_id = str(invoice_id).strip()
        invoice = (
            SubscriptionRenewalInvoice.objects.filter(invoice_id=clean_inv_id).select_related("tenant", "subscription").first()
            or SubscriptionRenewalInvoice.objects.filter(invoice_number=clean_inv_id).select_related("tenant", "subscription").first()
        )
        if not invoice:
            raise ValidationError(f"Renewal invoice '{clean_inv_id}' not found.")

        if invoice.status == RenewalInvoiceStatus.PAID:
            raise ValidationError(f"Renewal invoice '{invoice.invoice_number}' has already been paid on {invoice.paid_at} and cannot accept payment.")

        if invoice.status == RenewalInvoiceStatus.VOID:
            raise ValidationError(f"Renewal invoice '{invoice.invoice_number}' is VOID and cannot be settled.")

        clean_method = str(payment_method or "BKASH").upper().strip()
        clean_ref = str(payment_reference or "").strip()
        if len(clean_ref) < 4:
            raise ValidationError("Valid payment transaction reference (min 4 characters) is required.")

        actor_id = str(getattr(actor_user, "email", None) or getattr(actor_user, "user_id", None) or "system")
        now = timezone.now()
        previous_status = invoice.status

        # Update Invoice
        invoice.status = RenewalInvoiceStatus.PAID
        invoice.payment_method = clean_method
        invoice.payment_reference = clean_ref
        invoice.paid_at = now

        history = list(invoice.status_history_json or [])
        history.append(
            {
                "from_status": previous_status,
                "to_status": RenewalInvoiceStatus.PAID,
                "timestamp": now.isoformat(),
                "actor": actor_id,
                "transaction_ref": clean_ref,
                "payment_method": clean_method,
            }
        )
        invoice.status_history_json = history
        invoice.save(update_fields=["status", "payment_method", "payment_reference", "paid_at", "status_history_json", "updated_at"])

        # Update Subscription Renewal Horizon & Reactivate
        sub = invoice.subscription
        if sub:
            sub.renewal_date = invoice.period_end
            sub.amount_bdt = invoice.total_bdt
            sub.billing_cycle = invoice.billing_cycle
            sub.plan_tier = invoice.plan_tier
            if sub.status in [SubscriptionStatus.TRIAL, SubscriptionStatus.PAST_DUE, SubscriptionStatus.SUSPENDED, SubscriptionStatus.GRACE_PERIOD]:
                sub.status = SubscriptionStatus.ACTIVE
            sub.save(update_fields=["renewal_date", "amount_bdt", "billing_cycle", "plan_tier", "status"])

        # Update Tenant Status if Suspended
        tenant = invoice.tenant
        if tenant and tenant.status in [TenantLifecycleStatus.SUSPENDED, TenantLifecycleStatus.TRIAL]:
            tenant.status = TenantLifecycleStatus.ACTIVE
            tenant.plan_tier = invoice.plan_tier
            tenant.save(update_fields=["status", "plan_tier"])

        AuditLogEntry.objects.create(
            action="RENEWAL_INVOICE_PAID",
            resource_type="BILLING_INVOICE",
            resource_id=invoice.invoice_number,
            tenant_id=tenant.tenant_id,
            user_id=actor_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_number": invoice.invoice_number,
                "amount_paid_bdt": str(invoice.total_bdt),
                "payment_method": clean_method,
                "transaction_ref": clean_ref,
                "new_renewal_date": invoice.period_end.isoformat(),
            },
        )

        logger.info(f"Renewal invoice '{invoice.invoice_number}' settled successfully via {clean_method} ({clean_ref}).")
        return invoice

    @classmethod
    def serialize_invoice(cls, invoice: SubscriptionRenewalInvoice) -> dict[str, Any]:
        """Serializes invoice model into standard ADR-0087 JSON contract."""
        return {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "tenant_id": invoice.tenant.tenant_id,
            "plan_tier": invoice.plan_tier,
            "billing_cycle": invoice.billing_cycle,
            "period_start": invoice.period_start.isoformat(),
            "period_end": invoice.period_end.isoformat(),
            "subtotal_bdt": str(invoice.subtotal_bdt),
            "discount_bdt": str(invoice.discount_bdt),
            "tax_rate_percent": str(invoice.tax_rate_percent),
            "tax_amount_bdt": str(invoice.tax_amount_bdt),
            "total_bdt": str(invoice.total_bdt),
            "status": invoice.status,
            "payment_method": invoice.payment_method or None,
            "payment_reference": invoice.payment_reference or None,
            "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
            "status_history": invoice.status_history_json or [],
        }

