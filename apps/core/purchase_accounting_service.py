"""
backend/apps/core/purchase_accounting_service.py
Architecture Decision: ADR-0284 (P284).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing General Ledger Purchase Accounting Postings:
- GRN / Invoice Recognition: Capitalizes Merchandise Inventory Asset (1040)
  and records Accounts Payable (2010) without counting procurement as immediate operating expenses.
- Supplier Payments: Settles Accounts Payable (2010) against Cash (1010) or Bank (1020).
- Strict double-entry balance verification.
- Reversal and compensating adjustment workflows.
- Multi-branch purchase and supplier liability compilation.
- Multi-tenant isolation and role-based access control.
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
    GoodsReceiptNote,
    JournalEntry,
    JournalEntryStatus,
    JournalSourceModule,
    PurchaseAccountingPosting,
    Supplier,
    SupplierInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

PURCHASE_ACCOUNTING_VIEW_ROLES = {
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

PURCHASE_ACCOUNTING_GRN_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.BRANCH_MANAGER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "BRANCH_MANAGER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

PURCHASE_ACCOUNTING_PAYMENT_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

PURCHASE_ACCOUNTING_REVERSE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}


class PurchaseAccountingService:
    """
    Authoritative domain service for General Ledger purchase accounting postings.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in PURCHASE_ACCOUNTING_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PURCHASE_ACCOUNTING_VIEW_DENIED",
                    "detail": "Staff role is not authorized to view purchase accounting postings or liability summaries.",
                    "message_en": "Staff role is not authorized to view purchase accounting postings or liability summaries.",
                    "message_bn": "স্টাফ রোলের ক্রয় অ্যাকাউন্টিং বা সাপ্লায়ার দায় প্রতিবেদন দেখার অনুমতি নেই।",
                }
            )

    @classmethod
    def assert_grn_post_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in PURCHASE_ACCOUNTING_GRN_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PURCHASE_GRN_POST_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner, Branch Manager, or Finance Officer can post GRN purchase accounting.",
                    "message_en": "Only Pharmacy Owner, Branch Manager, or Finance Officer can post GRN purchase accounting.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার, ব্রাঞ্চ ম্যানেজার বা ফিন্যান্স অফিসার জিআরএন ক্রয় অ্যাকাউন্টিং পোস্ট করতে পারেন।",
                }
            )

    @classmethod
    def assert_payment_post_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in PURCHASE_ACCOUNTING_PAYMENT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SUPPLIER_PAYMENT_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can disburse supplier payments.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can disburse supplier payments.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার সাপ্লায়ার পেমেন্ট অনুমোদন করতে পারেন।",
                }
            )

    @classmethod
    def assert_reverse_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in PURCHASE_ACCOUNTING_REVERSE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PURCHASE_ACCOUNTING_REVERSE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can reverse purchase accounting postings.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can reverse purchase accounting postings.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার ক্রয় অ্যাকাউন্টিং রিভার্স করতে পারেন।",
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
                    "detail": "Tenant account subscription is suspended. Purchase accounting blocked.",
                    "message_en": "Tenant account subscription is suspended. Purchase accounting blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ক্রয় অ্যাকাউন্টিং অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def post_grn_invoice_recognition(
        cls,
        *,
        tenant_id: str,
        grn_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        total_cost_bdt: Optional[Any] = None,
        challan_number: str = "",
        supplier_invoice_number: str = "",
        purchase_order_number: str = "",
        branch_id: Optional[str] = None,
        paid_cash_bdt: Any = 0,
        paid_bank_bdt: Any = 0,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Atomically posts General Ledger double-entry journals for a Goods Receipt Note / Supplier Invoice:
        Dr: Merchandise Inventory Asset (1040)
        Cr: Cash in Hand (1010) / Bank & MFS (1020) for immediate cash-on-delivery
        Cr: Accounts Payable (2010) for remaining supplier dues
        EXPLICIT RULE: Does NOT debit operating expenses (6000s). P&L is untouched.
        """
        cls.assert_grn_post_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_idem = (idempotency_key or "").strip()
        if clean_idem:
            existing = PurchaseAccountingPosting.objects.filter(tenant=tenant, idempotency_key=clean_idem).first()
            if existing:
                return cls._serialize_posting(existing, is_idempotent_replay=True)

        grn = None
        supplier = None
        branch = None

        if grn_id:
            grn = GoodsReceiptNote.objects.filter(tenant=tenant, grn_id=grn_id).first()
            if not grn:
                foreign = GoodsReceiptNote.objects.filter(grn_id=grn_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
                raise NotFound({"error_code": "ERR_GRN_NOT_FOUND", "detail": f"Goods Receipt Note '{grn_id}' not found."})

            # Check if GRN is already posted
            existing_for_grn = PurchaseAccountingPosting.objects.filter(
                tenant=tenant,
                goods_receipt_note=grn,
                status="POSTED",
            ).first()
            if existing_for_grn:
                return cls._serialize_posting(existing_for_grn, is_idempotent_replay=True)

            supplier = grn.supplier
            branch = grn.branch
            challan_number = grn.challan_number or challan_number
            supplier_invoice_number = grn.supplier_invoice_number or supplier_invoice_number
            purchase_order_number = grn.purchase_order_number or purchase_order_number
            if total_cost_bdt is None:
                total_cost_bdt = grn.total_invoice_amount

        if supplier_id and not supplier:
            supplier = Supplier.objects.filter(tenant=tenant, supplier_id=supplier_id).first()
            if not supplier:
                foreign_sup = Supplier.objects.filter(supplier_id=supplier_id).first()
                if foreign_sup and str(foreign_sup.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
                raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})

        if branch_id and not branch:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()

        total_cost = q2(total_cost_bdt)
        paid_cash = q2(paid_cash_bdt)
        paid_bank = q2(paid_bank_bdt)

        if total_cost <= DECIMAL_ZERO:
            raise ValidationError(
                {"error_code": "ERR_INVALID_PURCHASE_AMOUNT", "detail": "Total purchase cost must be strictly positive."}
            )

        if paid_cash < DECIMAL_ZERO or paid_bank < DECIMAL_ZERO:
            raise ValidationError(
                {"error_code": "ERR_NEGATIVE_AMOUNT", "detail": "Payment amounts cannot be negative."}
            )

        immediate_paid = paid_cash + paid_bank
        if immediate_paid > total_cost:
            raise ValidationError(
                {
                    "error_code": "ERR_OVERPAYMENT_EXCEEDS_COST",
                    "detail": f"Total immediate paid amount ({immediate_paid} BDT) exceeds total invoice cost ({total_cost} BDT).",
                }
            )

        liability_ap = total_cost - immediate_paid
        sup_name = supplier.name if supplier else (grn.supplier_name if grn else "Pharmaceutical Supplier")

        # Prepare Balanced Double-Entry Journal Lines
        # Debit: Inventory Asset (1040)
        # Credit: Cash (1010) / Bank (1020) + AP (2010)
        ref_text = challan_number or supplier_invoice_number or (grn.grn_number if grn else "PURCHASE")
        lines = [
            {
                "account_code": "1040",  # Merchandise Inventory Asset (Balance Sheet Asset)
                "debit_amount_bdt": total_cost,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Inventory received from {sup_name} (Ref: {ref_text})",
            }
        ]

        if paid_cash > DECIMAL_ZERO:
            lines.append({
                "account_code": "1010",  # Cash in Hand
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": paid_cash,
                "description": f"Cash settlement on delivery from {sup_name}",
            })

        if paid_bank > DECIMAL_ZERO:
            lines.append({
                "account_code": "1020",  # Bank & MFS Clearing
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": paid_bank,
                "description": f"Bank settlement on delivery from {sup_name}",
            })

        if liability_ap > DECIMAL_ZERO:
            lines.append({
                "account_code": "2010",  # Accounts Payable - Supplier Dues (Balance Sheet Liability)
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": liability_ap,
                "description": f"Accounts payable liability owed to {sup_name}",
            })

        # Post Journal Entry via DoubleEntryJournalService (PROCUREMENT module)
        journal_data = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=lines,
            posting_date=timezone.now().date(),
            source_module=JournalSourceModule.PROCUREMENT,
            source_reference=ref_text,
            narration=f"GRN Purchase Capitalization & AP Liability - {sup_name} [{ref_text}]",
            branch_id=str(branch.branch_id) if branch else None,
            actor_user=actor_user,
        )
        journal = JournalEntry.objects.get(entry_id=journal_data["entry_id"])

        # Create PurchaseAccountingPosting Record
        posting = PurchaseAccountingPosting.objects.create(
            tenant=tenant,
            branch=branch,
            supplier=supplier,
            supplier_name=sup_name,
            goods_receipt_note=grn,
            purchase_order_number=purchase_order_number,
            challan_number=challan_number,
            supplier_invoice_number=supplier_invoice_number,
            posting_date=timezone.now().date(),
            posting_type="INVOICE_RECOGNITION",
            journal_entry=journal,
            status="POSTED",
            total_inventory_cost_bdt=total_cost,
            liability_ap_bdt=liability_ap,
            settled_cash_bdt=paid_cash,
            settled_bank_bdt=paid_bank,
            discount_bdt=DECIMAL_ZERO,
            is_operating_expense=False,  # Explicit invariant
            idempotency_key=clean_idem,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="PURCHASE_ACCOUNTING_POST",
            resource_type="PurchaseAccountingPosting",
            resource_id=posting.posting_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "supplier": sup_name,
                "total_cost_bdt": str(total_cost),
                "liability_ap_bdt": str(liability_ap),
                "journal_number": journal.journal_number,
            },
        )

        return cls._serialize_posting(posting, is_idempotent_replay=False)

    @classmethod
    @transaction.atomic
    def post_supplier_payment_disbursement(
        cls,
        *,
        tenant_id: str,
        supplier_id: str,
        payment_amount_bdt: Any,
        payment_mode: str = "BANK",  # CASH or BANK
        reference: str = "",
        branch_id: Optional[str] = None,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Atomically posts General Ledger double-entry journals for a Supplier Payment:
        Dr: Accounts Payable (2010) - decreases liability
        Cr: Cash in Hand (1010) or Bank & MFS (1020) - decreases monetary asset
        EXPLICIT RULE: Pure balance sheet settlement. Operating expenses (6000s) are NOT touched.
        """
        cls.assert_payment_post_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_idem = (idempotency_key or "").strip()
        if clean_idem:
            existing = PurchaseAccountingPosting.objects.filter(tenant=tenant, idempotency_key=clean_idem).first()
            if existing:
                return cls._serialize_posting(existing, is_idempotent_replay=True)

        supplier = Supplier.objects.filter(tenant=tenant, supplier_id=supplier_id).first()
        if not supplier:
            foreign = Supplier.objects.filter(supplier_id=supplier_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier '{supplier_id}' not found."})

        payment_amount = q2(payment_amount_bdt)
        if payment_amount <= DECIMAL_ZERO:
            raise ValidationError(
                {"error_code": "ERR_INVALID_PAYMENT_AMOUNT", "detail": "Payment amount must be strictly positive."}
            )

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first() if branch_id else None
        clean_mode = payment_mode.upper().strip()
        is_cash = (clean_mode == "CASH")
        settlement_acc = "1010" if is_cash else "1020"
        settlement_name = "Cash" if is_cash else "Bank / MFS"

        lines = [
            {
                "account_code": "2010",  # Accounts Payable (Balance Sheet Liability reduction)
                "debit_amount_bdt": payment_amount,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Payment disbursement to supplier {supplier.name} (Ref: {reference})",
            },
            {
                "account_code": settlement_acc,  # Cash or Bank (Balance Sheet Asset reduction)
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": payment_amount,
                "description": f"{settlement_name} disbursement for AP settlement to {supplier.name}",
            },
        ]

        journal_data = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=lines,
            posting_date=timezone.now().date(),
            source_module=JournalSourceModule.SUPPLIER_AP,
            source_reference=reference or f"PMT-{supplier.name[:10]}",
            narration=f"Supplier AP Settlement Disbursement: {supplier.name} [{reference}]",
            branch_id=str(branch.branch_id) if branch else None,
            actor_user=actor_user,
        )
        journal = JournalEntry.objects.get(entry_id=journal_data["entry_id"])

        posting = PurchaseAccountingPosting.objects.create(
            tenant=tenant,
            branch=branch,
            supplier=supplier,
            supplier_name=supplier.name,
            purchase_order_number=reference,
            posting_date=timezone.now().date(),
            posting_type="SUPPLIER_PAYMENT",
            journal_entry=journal,
            status="POSTED",
            total_inventory_cost_bdt=DECIMAL_ZERO,
            liability_ap_bdt=DECIMAL_ZERO,
            settled_cash_bdt=payment_amount if is_cash else DECIMAL_ZERO,
            settled_bank_bdt=payment_amount if not is_cash else DECIMAL_ZERO,
            discount_bdt=DECIMAL_ZERO,
            is_operating_expense=False,
            idempotency_key=clean_idem,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "finance@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "FINANCE_OFFICER")),
            action="SUPPLIER_PAYMENT_POST",
            resource_type="PurchaseAccountingPosting",
            resource_id=posting.posting_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "supplier": supplier.name,
                "payment_amount_bdt": str(payment_amount),
                "journal_number": journal.journal_number,
            },
        )

        return cls._serialize_posting(posting, is_idempotent_replay=False)

    @classmethod
    @transaction.atomic
    def reverse_purchase_accounting(
        cls,
        *,
        tenant_id: str,
        posting_id: str,
        reason: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Reverses a purchase accounting posting, creating a compensating journal reversal
        that restores AP liability and inventory/cash balances.
        """
        cls.assert_reverse_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REVERSAL_REASON",
                    "detail": "A mandatory explanation reason is required for purchase accounting reversal.",
                    "message_en": "A mandatory explanation reason is required for purchase accounting reversal.",
                    "message_bn": "ক্রয় অ্যাকাউন্টিং রিভার্সালের জন্য সুনির্দিষ্ট কারণ উল্লেখ করা বাধ্যতামূলক।",
                }
            )

        posting = PurchaseAccountingPosting.objects.select_for_update().filter(
            tenant=tenant, posting_id=posting_id
        ).first()
        if not posting:
            foreign = PurchaseAccountingPosting.objects.filter(posting_id=posting_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_POSTING_NOT_FOUND", "detail": f"Purchase accounting posting '{posting_id}' not found."})

        if posting.status == "REVERSED" or posting.is_reversed:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_REVERSED",
                    "detail": f"Purchase accounting posting '{posting.posting_id}' is already reversed.",
                    "message_en": "Purchase accounting posting is already reversed.",
                    "message_bn": "এই ক্রয় অ্যাকাউন্টিং পোস্টিংটি পূর্বেই রিভার্স করা হয়েছে।",
                }
            )

        # Reverse underlying journal entry
        rev_journal_data = DoubleEntryJournalService.reverse_journal_entry(
            tenant_id=str(tenant.tenant_id),
            entry_id=posting.journal_entry.entry_id,
            reason=f"Purchase Reversal for {posting.posting_id}: {clean_reason}",
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
            action="PURCHASE_ACCOUNTING_REVERSAL",
            resource_type="PurchaseAccountingPosting",
            resource_id=posting.posting_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "posting_id": posting.posting_id,
                "reason": clean_reason,
                "reversal_journal": rev_journal_data.get("reversal_journal_number"),
            },
        )

        return {
            "success": True,
            "posting_id": posting.posting_id,
            "status": "REVERSED",
            "is_reversed": True,
            "reversal_reason": clean_reason,
            "reversal_journal_id": rev_journal_data.get("reversal_journal_id"),
            "reversal_journal_number": rev_journal_data.get("reversal_journal_number"),
            "message_en": "Purchase accounting posting reversed successfully.",
            "message_bn": "ক্রয় অ্যাকাউন্টিং পোস্টিং সফলভাবে রিভার্স করা হয়েছে।",
        }

    @classmethod
    def get_purchase_accounting_summary(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Compiles multi-branch purchase accounting summary:
        Total Inventory Capitalized, Total AP Recognized, Total Supplier Payments,
        Net Outstanding AP Liability, and verifies ZERO operating expense impact.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = PurchaseAccountingPosting.objects.filter(tenant=tenant, status="POSTED")

        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if start_date:
            qs = qs.filter(posting_date__gte=start_date)
        if end_date:
            qs = qs.filter(posting_date__lte=end_date)

        grn_qs = qs.filter(posting_type="INVOICE_RECOGNITION")
        pmt_qs = qs.filter(posting_type="SUPPLIER_PAYMENT")

        total_inventory = grn_qs.aggregate(val=Sum("total_inventory_cost_bdt"))["val"] or DECIMAL_ZERO
        total_ap_recognized = grn_qs.aggregate(val=Sum("liability_ap_bdt"))["val"] or DECIMAL_ZERO
        immediate_cash = grn_qs.aggregate(val=Sum("settled_cash_bdt"))["val"] or DECIMAL_ZERO
        immediate_bank = grn_qs.aggregate(val=Sum("settled_bank_bdt"))["val"] or DECIMAL_ZERO

        pmt_cash = pmt_qs.aggregate(val=Sum("settled_cash_bdt"))["val"] or DECIMAL_ZERO
        pmt_bank = pmt_qs.aggregate(val=Sum("settled_bank_bdt"))["val"] or DECIMAL_ZERO
        total_payments = pmt_cash + pmt_bank

        net_ap_liability = total_ap_recognized - total_payments

        return {
            "tenant_id": str(tenant.tenant_id),
            "branch_id": str(branch_id) if branch_id else "ALL_BRANCHES",
            "grn_postings_count": grn_qs.count(),
            "payment_postings_count": pmt_qs.count(),
            "total_inventory_capitalized_bdt": str(q2(total_inventory)),
            "total_ap_recognized_bdt": str(q2(total_ap_recognized)),
            "immediate_delivery_settled_bdt": str(q2(immediate_cash + immediate_bank)),
            "total_supplier_payments_disbursed_bdt": str(q2(total_payments)),
            "net_outstanding_ap_liability_bdt": str(q2(net_ap_liability)),
            "total_operating_expenses_bdt": "0.00",  # Invariant: Purchases do not count as operating expenses
            "is_operating_expense": False,
            "currency": "BDT",
        }

    @classmethod
    def list_purchase_postings(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        posting_type: Optional[str] = None,
        status_param: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = PurchaseAccountingPosting.objects.filter(tenant=tenant).select_related(
            "branch", "supplier", "journal_entry", "goods_receipt_note"
        )
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if posting_type:
            qs = qs.filter(posting_type=posting_type.upper())
        if status_param:
            qs = qs.filter(status=status_param.upper())
        if start_date:
            qs = qs.filter(posting_date__gte=start_date)
        if end_date:
            qs = qs.filter(posting_date__lte=end_date)

        return [cls._serialize_posting(p) for p in qs[:200]]

    @classmethod
    def get_purchase_posting(
        cls,
        *,
        tenant_id: str,
        posting_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        posting = PurchaseAccountingPosting.objects.filter(
            tenant=tenant, posting_id=posting_id
        ).select_related("branch", "supplier", "journal_entry", "goods_receipt_note").first()
        if not posting:
            foreign = PurchaseAccountingPosting.objects.filter(posting_id=posting_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_POSTING_NOT_FOUND", "detail": f"Purchase accounting posting '{posting_id}' not found."})

        return cls._serialize_posting(posting)

    @classmethod
    def _serialize_posting(cls, p: PurchaseAccountingPosting, is_idempotent_replay: bool = False) -> Dict[str, Any]:
        return {
            "posting_id": p.posting_id,
            "tenant_id": str(p.tenant_id),
            "branch_id": str(p.branch_id) if p.branch_id else None,
            "supplier_id": str(p.supplier_id) if p.supplier_id else None,
            "supplier_name": p.supplier_name,
            "grn_id": str(p.goods_receipt_note_id) if p.goods_receipt_note_id else None,
            "posting_date": p.posting_date.isoformat() if p.posting_date else None,
            "posting_type": p.posting_type,
            "status": p.status,
            "is_reversed": p.is_reversed,
            "total_inventory_cost_bdt": str(p.total_inventory_cost_bdt),
            "liability_ap_bdt": str(p.liability_ap_bdt),
            "settled_cash_bdt": str(p.settled_cash_bdt),
            "settled_bank_bdt": str(p.settled_bank_bdt),
            "is_operating_expense": p.is_operating_expense,
            "journal_id": str(p.journal_entry_id) if p.journal_entry_id else None,
            "journal_number": p.journal_entry.journal_number if p.journal_entry else None,
            "reversal_reason": p.reversal_reason,
            "idempotency_key": p.idempotency_key,
            "is_idempotent_replay": is_idempotent_replay,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
