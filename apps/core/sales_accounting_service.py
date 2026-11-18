"""
backend/apps/core/sales_accounting_service.py
Architecture Decision: ADR-0283 (P283).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing General Ledger Sales Accounting Postings:
- Atomically posts Revenue Settlement, Customer AR Receivables, VAT, and Discounts.
- Posts FEFO purchase lot landed COGS & Merchandise Inventory reductions.
- Enforces strict double-entry mathematical balance.
- Multi-branch sales profitability summary (Gross Profit, Margin %).
- Cryptographic compensating reversal workflows for refunded / voided invoices.
- Tenant isolation and role-based access control.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import DoubleEntryJournalService, q2
from apps.core.models import (
    Branch,
    ChartOfAccount,
    InvoiceStatus,
    JournalEntry,
    JournalEntryStatus,
    JournalSourceModule,
    PaymentMethod,
    PosSaleOrder,
    SaleAccountingPosting,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

SALES_ACCOUNTING_VIEW_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.AUDITOR,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "AUDITOR",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

SALES_ACCOUNTING_POST_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "CASHIER",
    "STAFF_PHARMACIST",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

SALES_ACCOUNTING_REVERSE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class SalesAccountingService:
    """
    Authoritative domain service for sales accounting postings and profitability.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in SALES_ACCOUNTING_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SALES_ACCOUNTING_VIEW_DENIED",
                    "detail": "Staff role is not authorized to view sales accounting postings or profitability summaries.",
                    "message_en": "Staff role is not authorized to view sales accounting postings or profitability summaries.",
                    "message_bn": "স্টাফ রোলের সেলস অ্যাকাউন্টিং পোস্টিং বা মুনাফা প্রতিবেদন দেখার অনুমতি নেই।",
                }
            )

    @classmethod
    def assert_post_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in SALES_ACCOUNTING_POST_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SALES_ACCOUNTING_POST_UNAUTHORIZED",
                    "detail": "User is not authorized to post sales accounting entries.",
                }
            )

    @classmethod
    def assert_reverse_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in SALES_ACCOUNTING_REVERSE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SALES_ACCOUNTING_REVERSE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can reverse sales accounting postings.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can reverse sales accounting postings.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার সেলস অ্যাকাউন্টিং রিভার্স করতে পারেন।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "subscription_status", None) == "SUSPENDED"
            or getattr(tenant, "status", None) in ("SUSPENDED", TenantLifecycleStatus.SUSPENDED)
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Sales accounting blocked.",
                    "message_en": "Tenant account subscription is suspended. Sales accounting blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। সেলস অ্যাকাউন্টিং অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def post_sale_invoice_accounting(
        cls,
        *,
        tenant_id: str,
        invoice_id: str,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Atomically posts General Ledger double-entry journals for a SaleInvoice:
        1. Revenue & Settlement Journal (Cash/Bank/AR + Discount vs Sales Revenue + VAT)
        2. COGS & Inventory Reduction Journal (COGS Expense vs Merchandise Inventory Asset)
        """
        cls.assert_post_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_idem = (idempotency_key or "").strip()
        if clean_idem:
            existing = SaleAccountingPosting.objects.filter(tenant=tenant, idempotency_key=clean_idem).first()
            if existing:
                return cls._serialize_posting(existing, is_idempotent_replay=True)

        invoice = SaleInvoice.objects.filter(tenant=tenant, invoice_id=invoice_id).first()
        if not invoice:
            foreign = SaleInvoice.objects.filter(invoice_id=invoice_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_INVOICE_NOT_FOUND", "detail": f"Sale invoice '{invoice_id}' not found."})

        # Check if already posted for this invoice
        existing_for_inv = SaleAccountingPosting.objects.filter(
            tenant=tenant,
            sale_invoice=invoice,
            status="POSTED",
        ).first()
        if existing_for_inv:
            return cls._serialize_posting(existing_for_inv, is_idempotent_replay=True)

        gross_sales = q2(invoice.total_amount)
        discount = q2(invoice.discount_amount)
        tax_vat = q2(invoice.tax_amount)
        net_amount = q2(invoice.net_amount)

        if gross_sales <= DECIMAL_ZERO:
            raise ValidationError(
                {"error_code": "ERR_INVALID_SALE_AMOUNT", "detail": "Gross sales amount must be strictly positive."}
            )

        # Determine Settled vs Receivable Due
        is_due = (invoice.payment_method == PaymentMethod.DUE)
        if is_due:
            receivable_due = net_amount
            net_settled = DECIMAL_ZERO
        else:
            receivable_due = DECIMAL_ZERO
            net_settled = net_amount

        # 1. Prepare Revenue & Settlement Journal Lines
        # Revenue lines equation: Debits (Settled + AR + Discount) == Credits (Gross Sales + VAT)
        rev_lines: List[Dict[str, Any]] = []

        if net_settled > DECIMAL_ZERO:
            if invoice.payment_method == PaymentMethod.CASH:
                settlement_code = "1010"  # Cash in Hand
                settlement_desc = f"Cash received from invoice {invoice.invoice_number}"
            else:
                settlement_code = "1020"  # Bank & MFS Clearing
                settlement_desc = f"{invoice.payment_method} settlement for invoice {invoice.invoice_number}"

            rev_lines.append({
                "account_code": settlement_code,
                "debit_amount_bdt": net_settled,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": settlement_desc,
            })

        if receivable_due > DECIMAL_ZERO:
            rev_lines.append({
                "account_code": "1030",  # Accounts Receivable
                "debit_amount_bdt": receivable_due,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Customer due receivable on invoice {invoice.invoice_number}",
            })

        if discount > DECIMAL_ZERO:
            rev_lines.append({
                "account_code": "4030",  # Sales Discounts & Allowances (Contra-Revenue)
                "debit_amount_bdt": discount,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"POS sales concession/discount on invoice {invoice.invoice_number}",
            })

        # Credit Leg: Medicine Sales Revenue
        rev_lines.append({
            "account_code": "4010",  # Medicine Sales Revenue
            "debit_amount_bdt": DECIMAL_ZERO,
            "credit_amount_bdt": gross_sales,
            "description": f"Dispensing medicine sales revenue {invoice.invoice_number}",
        })

        if tax_vat > DECIMAL_ZERO:
            rev_lines.append({
                "account_code": "2030",  # NBR VAT & Tax Payable
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": tax_vat,
                "description": f"Sales VAT collected for NBR on invoice {invoice.invoice_number}",
            })

        # Post Revenue Journal
        rev_journal_data = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=rev_lines,
            posting_date=invoice.created_at.date() if invoice.created_at else timezone.now().date(),
            source_module=JournalSourceModule.POS_SALE,
            source_reference=invoice.invoice_number,
            narration=f"POS Sales Settlement & Revenue: {invoice.invoice_number}",
            branch_id=str(invoice.branch_id) if invoice.branch_id else None,
            actor_user=actor_user,
        )
        rev_journal = JournalEntry.objects.get(entry_id=rev_journal_data["entry_id"])

        # 2. Compute COGS from Batch Purchase Lots
        total_cogs = DECIMAL_ZERO
        items = list(invoice.items.select_related("batch", "product").all())

        for it in items:
            cost_per_unit = q2(getattr(it.batch, "cost_per_unit", DECIMAL_ZERO))
            line_cost = q2(cost_per_unit * it.quantity)
            total_cogs += line_cost

        cogs_journal = None
        if total_cogs > DECIMAL_ZERO:
            cogs_lines = [
                {
                    "account_code": "5010",  # COGS - Medicine Lots
                    "debit_amount_bdt": total_cogs,
                    "credit_amount_bdt": DECIMAL_ZERO,
                    "description": f"Cost of goods sold for invoice {invoice.invoice_number}",
                },
                {
                    "account_code": "1040",  # Merchandise Inventory Asset
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": total_cogs,
                    "description": f"Inventory reduction at lot cost for invoice {invoice.invoice_number}",
                },
            ]
            cogs_journal_data = DoubleEntryJournalService.post_journal_entry(
                tenant_id=str(tenant.tenant_id),
                lines=cogs_lines,
                posting_date=invoice.created_at.date() if invoice.created_at else timezone.now().date(),
                source_module=JournalSourceModule.POS_SALE,
                source_reference=invoice.invoice_number,
                narration=f"POS Inventory COGS Reduction: {invoice.invoice_number}",
                branch_id=str(invoice.branch_id) if invoice.branch_id else None,
                actor_user=actor_user,
            )
            cogs_journal = JournalEntry.objects.get(entry_id=cogs_journal_data["entry_id"])

        # 3. Compute Gross Profit: (Gross Sales - Discount) - COGS
        net_sales = gross_sales - discount
        gross_profit = net_sales - total_cogs

        # 4. Save SaleAccountingPosting Record
        posting = SaleAccountingPosting.objects.create(
            tenant=tenant,
            branch=invoice.branch,
            sale_invoice=invoice,
            invoice_number=invoice.invoice_number,
            posting_date=invoice.created_at.date() if invoice.created_at else timezone.now().date(),
            revenue_journal=rev_journal,
            cogs_journal=cogs_journal,
            status="POSTED",
            gross_sales_bdt=gross_sales,
            discount_bdt=discount,
            tax_vat_bdt=tax_vat,
            net_settled_bdt=net_settled,
            receivable_due_bdt=receivable_due,
            total_cogs_bdt=total_cogs,
            gross_profit_bdt=gross_profit,
            idempotency_key=clean_idem,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "cashier@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "CASHIER")),
            action="SALES_ACCOUNTING_POST",
            resource_type="SaleAccountingPosting",
            resource_id=posting.posting_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_number": invoice.invoice_number,
                "revenue_journal": rev_journal.journal_number,
                "cogs_journal": cogs_journal.journal_number if cogs_journal else None,
                "gross_profit_bdt": str(gross_profit),
            },
        )

        return cls._serialize_posting(posting, is_idempotent_replay=False)

    @classmethod
    @transaction.atomic
    def post_pos_sale_order_accounting(
        cls,
        *,
        tenant_id: str,
        sale_id: str,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Atomically posts General Ledger double-entry journals for a canonical PosSaleOrder.
        """
        cls.assert_post_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_idem = (idempotency_key or "").strip()
        if clean_idem:
            existing = SaleAccountingPosting.objects.filter(tenant=tenant, idempotency_key=clean_idem).first()
            if existing:
                return cls._serialize_posting(existing, is_idempotent_replay=True)

        sale = PosSaleOrder.objects.filter(tenant=tenant, sale_id=sale_id).first()
        if not sale:
            foreign = PosSaleOrder.objects.filter(sale_id=sale_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_SALE_NOT_FOUND", "detail": f"POS Sale order '{sale_id}' not found."})

        # Check existing
        existing_for_sale = SaleAccountingPosting.objects.filter(
            tenant=tenant,
            pos_sale_order=sale,
            status="POSTED",
        ).first()
        if existing_for_sale:
            return cls._serialize_posting(existing_for_sale, is_idempotent_replay=True)

        gross_sales = q2(sale.subtotal_bdt or sale.grand_total_bdt)
        discount = q2(sale.discount_amount_bdt)
        tax_vat = q2(sale.tax_amount_bdt)
        net_settled = q2(sale.paid_amount_bdt)
        receivable_due = q2(sale.due_amount_bdt)

        if gross_sales <= DECIMAL_ZERO:
            raise ValidationError(
                {"error_code": "ERR_INVALID_SALE_AMOUNT", "detail": "Gross sales amount must be strictly positive."}
            )

        # Revenue lines:
        rev_lines: List[Dict[str, Any]] = []

        if net_settled > DECIMAL_ZERO:
            rev_lines.append({
                "account_code": "1010",  # Cash in Hand
                "debit_amount_bdt": net_settled,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Paid cash on order {sale.order_number}",
            })

        if receivable_due > DECIMAL_ZERO:
            rev_lines.append({
                "account_code": "1030",  # Accounts Receivable
                "debit_amount_bdt": receivable_due,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Customer credit due on order {sale.order_number}",
            })

        if discount > DECIMAL_ZERO:
            rev_lines.append({
                "account_code": "4030",  # Sales Discounts & Allowances
                "debit_amount_bdt": discount,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Order discount on {sale.order_number}",
            })

        rev_lines.append({
            "account_code": "4010",  # Medicine Sales Revenue
            "debit_amount_bdt": DECIMAL_ZERO,
            "credit_amount_bdt": gross_sales,
            "description": f"POS sales revenue on order {sale.order_number}",
        })

        if tax_vat > DECIMAL_ZERO:
            rev_lines.append({
                "account_code": "2030",  # NBR VAT & Tax
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": tax_vat,
                "description": f"VAT collected on order {sale.order_number}",
            })

        rev_journal_data = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=rev_lines,
            posting_date=timezone.now().date(),
            source_module=JournalSourceModule.POS_SALE,
            source_reference=sale.order_number,
            narration=f"POS Sale Order Settlement: {sale.order_number}",
            branch_id=str(sale.branch_id) if sale.branch_id else None,
            actor_user=actor_user,
        )
        rev_journal = JournalEntry.objects.get(entry_id=rev_journal_data["entry_id"])

        # Compute COGS
        total_cogs = DECIMAL_ZERO
        lines = list(sale.lines.all())
        for l in lines:
            cost = q2(getattr(l, "cost_amount_bdt", getattr(l, "unit_cost_bdt", DECIMAL_ZERO)))
            total_cogs += cost

        cogs_journal = None
        if total_cogs > DECIMAL_ZERO:
            cogs_lines = [
                {
                    "account_code": "5010",
                    "debit_amount_bdt": total_cogs,
                    "credit_amount_bdt": DECIMAL_ZERO,
                    "description": f"COGS on order {sale.order_number}",
                },
                {
                    "account_code": "1040",
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": total_cogs,
                    "description": f"Inventory reduction on order {sale.order_number}",
                },
            ]
            cogs_journal_data = DoubleEntryJournalService.post_journal_entry(
                tenant_id=str(tenant.tenant_id),
                lines=cogs_lines,
                posting_date=timezone.now().date(),
                source_module=JournalSourceModule.POS_SALE,
                source_reference=sale.order_number,
                narration=f"COGS Reduction: {sale.order_number}",
                branch_id=str(sale.branch_id) if sale.branch_id else None,
                actor_user=actor_user,
            )
            cogs_journal = JournalEntry.objects.get(entry_id=cogs_journal_data["entry_id"])

        net_sales = gross_sales - discount
        gross_profit = net_sales - total_cogs

        posting = SaleAccountingPosting.objects.create(
            tenant=tenant,
            branch=sale.branch,
            pos_sale_order=sale,
            sale_invoice=sale.linked_invoice,
            invoice_number=sale.order_number,
            posting_date=timezone.now().date(),
            revenue_journal=rev_journal,
            cogs_journal=cogs_journal,
            status="POSTED",
            gross_sales_bdt=gross_sales,
            discount_bdt=discount,
            tax_vat_bdt=tax_vat,
            net_settled_bdt=net_settled,
            receivable_due_bdt=receivable_due,
            total_cogs_bdt=total_cogs,
            gross_profit_bdt=gross_profit,
            idempotency_key=clean_idem,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        return cls._serialize_posting(posting, is_idempotent_replay=False)

    @classmethod
    @transaction.atomic
    def reverse_sale_accounting(
        cls,
        *,
        tenant_id: str,
        posting_id: str,
        reason: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Reverses an existing sales accounting posting, triggering compensating
        journal reversals for both the revenue/settlement journal and the COGS journal.
        """
        cls.assert_reverse_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REVERSAL_REASON",
                    "detail": "A mandatory explanation reason is required for sales accounting reversal.",
                    "message_en": "A mandatory explanation reason is required for sales accounting reversal.",
                    "message_bn": "সেলস অ্যাকাউন্টিং রিভার্সালের জন্য সুনির্দিষ্ট কারণ উল্লেখ করা বাধ্যতামূলক।",
                }
            )

        posting = SaleAccountingPosting.objects.select_for_update().filter(tenant=tenant, posting_id=posting_id).first()
        if not posting:
            foreign = SaleAccountingPosting.objects.filter(posting_id=posting_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_POSTING_NOT_FOUND", "detail": f"Sales accounting posting '{posting_id}' not found."})

        if posting.status == "REVERSED" or posting.is_reversed:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_REVERSED",
                    "detail": f"Sales accounting posting '{posting.invoice_number}' is already reversed.",
                    "message_en": "Sales accounting posting is already reversed.",
                    "message_bn": "এই সেলস অ্যাকাউন্টিং পোস্টিংটি পূর্বেই রিভার্স করা হয়েছে।",
                }
            )

        # 1. Reverse Revenue Journal
        rev_rev_data = DoubleEntryJournalService.reverse_journal_entry(
            tenant_id=str(tenant.tenant_id),
            entry_id=posting.revenue_journal.entry_id,
            reason=f"Sales Reversal for {posting.invoice_number}: {clean_reason}",
            actor_user=actor_user,
        )

        # 2. Reverse COGS Journal if exists
        cogs_rev_data = None
        if posting.cogs_journal:
            cogs_rev_data = DoubleEntryJournalService.reverse_journal_entry(
                tenant_id=str(tenant.tenant_id),
                entry_id=posting.cogs_journal.entry_id,
                reason=f"COGS Reversal for {posting.invoice_number}: {clean_reason}",
                actor_user=actor_user,
            )

        posting.status = "REVERSED"
        posting.is_reversed = True
        posting.reversal_reason = clean_reason
        posting.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="SALES_ACCOUNTING_REVERSAL",
            resource_type="SaleAccountingPosting",
            resource_id=posting.posting_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "invoice_number": posting.invoice_number,
                "reason": clean_reason,
                "revenue_reversal": rev_rev_data.get("reversal_journal_number"),
                "cogs_reversal": cogs_rev_data.get("reversal_journal_number") if cogs_rev_data else None,
            },
        )

        return {
            "success": True,
            "posting_id": posting.posting_id,
            "invoice_number": posting.invoice_number,
            "status": "REVERSED",
            "is_reversed": True,
            "reversal_reason": clean_reason,
            "revenue_reversal_journal_id": rev_rev_data.get("reversal_journal_id"),
            "cogs_reversal_journal_id": cogs_rev_data.get("reversal_journal_id") if cogs_rev_data else None,
            "message_en": f"Sales accounting posting for invoice '{posting.invoice_number}' reversed successfully.",
            "message_bn": f"চালান '{posting.invoice_number}'-এর সেলস অ্যাকাউন্টিং সফলভাবে রিভার্স করা হয়েছে।",
        }

    @classmethod
    def get_sales_profitability_summary(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Compiles multi-branch sales profitability summary:
        Total Gross Sales, Total Discounts, Net Sales, Total COGS, Gross Profit, and Gross Margin %.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = SaleAccountingPosting.objects.filter(tenant=tenant, status="POSTED")

        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if start_date:
            qs = qs.filter(posting_date__gte=start_date)
        if end_date:
            qs = qs.filter(posting_date__lte=end_date)

        agg = qs.aggregate(
            total_gross=Sum("gross_sales_bdt"),
            total_discount=Sum("discount_bdt"),
            total_tax=Sum("tax_vat_bdt"),
            total_settled=Sum("net_settled_bdt"),
            total_due=Sum("receivable_due_bdt"),
            total_cogs=Sum("total_cogs_bdt"),
            total_profit=Sum("gross_profit_bdt"),
        )

        total_gross = q2(agg.get("total_gross") or DECIMAL_ZERO)
        total_discount = q2(agg.get("total_discount") or DECIMAL_ZERO)
        total_tax = q2(agg.get("total_tax") or DECIMAL_ZERO)
        total_settled = q2(agg.get("total_settled") or DECIMAL_ZERO)
        total_due = q2(agg.get("total_due") or DECIMAL_ZERO)
        total_cogs = q2(agg.get("total_cogs") or DECIMAL_ZERO)
        total_profit = q2(agg.get("total_profit") or DECIMAL_ZERO)

        net_sales = total_gross - total_discount
        margin_pct = DECIMAL_ZERO
        if net_sales > DECIMAL_ZERO:
            margin_pct = q2((total_profit / net_sales) * Decimal("100.00"))

        return {
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(branch_id) if branch_id else "ALL_BRANCHES",
            "total_sales_count": qs.count(),
            "gross_sales_bdt": str(total_gross),
            "total_discount_bdt": str(total_discount),
            "net_sales_bdt": str(net_sales),
            "tax_vat_bdt": str(total_tax),
            "net_settled_bdt": str(total_settled),
            "receivable_due_bdt": str(total_due),
            "total_cogs_bdt": str(total_cogs),
            "gross_profit_bdt": str(total_profit),
            "gross_margin_pct": str(margin_pct),
            "is_profitable": total_profit >= DECIMAL_ZERO,
            "currency": "BDT",
        }

    @classmethod
    def list_sales_postings(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        status_param: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        invoice_number: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = SaleAccountingPosting.objects.filter(tenant=tenant).select_related(
            "branch", "revenue_journal", "cogs_journal"
        )
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if status_param:
            qs = qs.filter(status=status_param.upper())
        if start_date:
            qs = qs.filter(posting_date__gte=start_date)
        if end_date:
            qs = qs.filter(posting_date__lte=end_date)
        if invoice_number:
            qs = qs.filter(invoice_number__icontains=invoice_number.strip())

        return [cls._serialize_posting(p) for p in qs[:200]]

    @classmethod
    def get_sales_posting(
        cls,
        *,
        tenant_id: str,
        posting_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        posting = SaleAccountingPosting.objects.filter(tenant=tenant, posting_id=posting_id).select_related(
            "branch", "revenue_journal", "cogs_journal"
        ).first()
        if not posting:
            foreign = SaleAccountingPosting.objects.filter(posting_id=posting_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_POSTING_NOT_FOUND", "detail": f"Sales accounting posting '{posting_id}' not found."})

        return cls._serialize_posting(posting)

    @classmethod
    def _serialize_posting(cls, p: SaleAccountingPosting, is_idempotent_replay: bool = False) -> Dict[str, Any]:
        return {
            "posting_id": p.posting_id,
            "tenant_id": str(p.tenant_id),
            "branch_id": str(p.branch_id) if p.branch_id else None,
            "invoice_number": p.invoice_number,
            "sale_invoice_id": str(p.sale_invoice_id) if p.sale_invoice_id else None,
            "pos_sale_order_id": str(p.pos_sale_order_id) if p.pos_sale_order_id else None,
            "posting_date": p.posting_date.isoformat() if p.posting_date else None,
            "status": p.status,
            "is_reversed": p.is_reversed,
            "gross_sales_bdt": str(p.gross_sales_bdt),
            "discount_bdt": str(p.discount_bdt),
            "tax_vat_bdt": str(p.tax_vat_bdt),
            "net_settled_bdt": str(p.net_settled_bdt),
            "receivable_due_bdt": str(p.receivable_due_bdt),
            "total_cogs_bdt": str(p.total_cogs_bdt),
            "gross_profit_bdt": str(p.gross_profit_bdt),
            "revenue_journal_id": str(p.revenue_journal_id) if p.revenue_journal_id else None,
            "revenue_journal_number": p.revenue_journal.journal_number if p.revenue_journal else None,
            "cogs_journal_id": str(p.cogs_journal_id) if p.cogs_journal_id else None,
            "cogs_journal_number": p.cogs_journal.journal_number if p.cogs_journal else None,
            "reversal_reason": p.reversal_reason,
            "idempotency_key": p.idempotency_key,
            "is_idempotent_replay": is_idempotent_replay,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
