"""
backend/apps/core/cash_transfer_reconciliation_service.py
Architecture Decision: ADR-0290 (P290).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Cash-Bank Treasury Transfers and Bank Reconciliation:
- Counter cash-to-bank deposits (till float to bank).
- Bank-to-cash withdrawals (ATM/Cheque till replenishments).
- Inter-bank and MFS clearing transfers.
- Invariant: Transfers strictly move between Asset GL accounts (Dr Dest Asset, Cr Source Asset).
  ZERO impact on Revenue (4000s) and ZERO impact on Operating Expenses (6000s).
- Atomic financial account registry balance updates.
- Bank statement reconciliation batching, statement item matching, and discrepancy auditing.
- Multi-branch scoping and strict multi-tenant isolation.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import DoubleEntryJournalService, q2
from apps.core.models import (
    BankReconciliationBatch,
    BankReconciliationStatus,
    BankStatementItem,
    Branch,
    CashBankTransfer,
    CashBankTransferStatus,
    CashBankTransferType,
    ChartOfAccount,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    JournalEntry,
    JournalSourceModule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

TREASURY_VIEW_ROLES = {
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

TREASURY_MANAGE_ROLES = {
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

RECONCILIATION_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

STAFF_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


def _resolve_account_gl_code(account: FinancialAccount) -> str:
    """
    Resolves canonical Asset GL account code:
    - CASH_REGISTER -> 1010 (Cash on Hand / Till Cash)
    - BANK_ACCOUNT / MFS_CLEARING / PAYMENT_GATEWAY -> 1020 (Bank & MFS Clearing)
    """
    if account.account_type == FinancialAccountType.CASH_REGISTER:
        return "1010"
    return "1020"


class CashTransferReconciliationService:
    """
    Domain service for Treasury Cash-Bank Transfers and Bank Statement Reconciliations.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in TREASURY_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier is not authorized to access treasury transfers or bank reconciliation.",
                    "message_en": "Cashier is not authorized to access treasury transfers or bank reconciliation.",
                    "message_bn": "ক্যাশিয়ার ট্রেজারি ট্রান্সফার বা ব্যাংক রিকনসিলিয়েশন অ্যাক্সেস করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_transfer_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in TREASURY_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TREASURY_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner, Finance Officer or Branch Manager can execute treasury transfers.",
                    "message_en": "Only Pharmacy Owner, Finance Officer or Branch Manager can execute treasury transfers.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার, ফিন্যান্স অফিসার বা ব্রাঞ্চ ম্যানেজার ট্রেজারি ফান্ড ট্রান্সফার করতে পারেন।",
                }
            )

    @classmethod
    def assert_reconciliation_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in RECONCILIATION_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_RECONCILIATION_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can perform bank reconciliation.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can perform bank reconciliation.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার ব্যাংক রিকনসিলিয়েশন পরিচালনা করতে পারেন।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
            or getattr(tenant, "subscription_status", None) == "SUSPENDED"
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Treasury operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Treasury operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ট্রেজারি অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    # =========================================================================
    # 1. CASH & BANK TREASURY TRANSFERS
    # =========================================================================

    @classmethod
    @transaction.atomic
    def execute_cash_bank_transfer(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Executes an internal fund transfer between two financial accounts (Cash till / Bank):
        - Source account debited, destination account credited in registry.
        - Balanced double-entry General Ledger journal posted (Dr Dest GL 1010/1020, Cr Src GL 1010/1020).
        - ZERO P&L impact (Revenue 4000s = 0, Expense 6000s = 0).
        """
        cls.assert_transfer_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        source_id = data.get("source_account_id")
        dest_id = data.get("destination_account_id")

        if not source_id or not dest_id:
            raise ValidationError(
                {"error_code": "ERR_MISSING_ACCOUNTS", "detail": "Both source_account_id and destination_account_id are required."}
            )

        if str(source_id).strip() == str(dest_id).strip():
            raise ValidationError(
                {
                    "error_code": "ERR_SAME_ACCOUNT_TRANSFER",
                    "detail": "Source and destination accounts must be different.",
                    "message_en": "Source and destination accounts must be different.",
                    "message_bn": "উৎস এবং গন্তব্য অ্যাকাউন্ট ভিন্ন হতে হবে।",
                }
            )

        # Retrieve source account with locking
        src_account = FinancialAccount.objects.select_for_update().filter(account_id=source_id, tenant=tenant).first()
        if not src_account:
            foreign_src = FinancialAccount.objects.filter(account_id=source_id).first()
            if foreign_src and str(foreign_src.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant account access forbidden."})
            raise NotFound({"error_code": "ERR_SOURCE_ACCOUNT_NOT_FOUND", "detail": f"Source financial account '{source_id}' not found."})

        # Retrieve destination account with locking
        dst_account = FinancialAccount.objects.select_for_update().filter(account_id=dest_id, tenant=tenant).first()
        if not dst_account:
            foreign_dst = FinancialAccount.objects.filter(account_id=dest_id).first()
            if foreign_dst and str(foreign_dst.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant account access forbidden."})
            raise NotFound({"error_code": "ERR_DESTINATION_ACCOUNT_NOT_FOUND", "detail": f"Destination financial account '{dest_id}' not found."})

        if src_account.status != FinancialAccountStatus.ACTIVE or dst_account.status != FinancialAccountStatus.ACTIVE:
            raise ValidationError(
                {"error_code": "ERR_ACCOUNT_NOT_ACTIVE", "detail": "Both accounts must be active to perform a treasury transfer."}
            )

        # Amount validation
        amount_raw = data.get("amount_bdt")
        if amount_raw is None:
            raise ValidationError({"error_code": "ERR_MISSING_AMOUNT", "detail": "Transfer amount is required."})
        try:
            amount = q2(Decimal(str(amount_raw)))
            if amount <= DECIMAL_ZERO:
                raise ValueError()
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_TRANSFER_AMOUNT", "detail": "Transfer amount must be a positive decimal."})

        # Branch resolution
        branch_id = data.get("branch_id")
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
        else:
            branch = src_account.branch or Branch.objects.filter(tenant=tenant).first()

        # Date validation
        transfer_date_raw = data.get("transfer_date")
        transfer_date = timezone.now().date()
        if transfer_date_raw:
            try:
                if isinstance(transfer_date_raw, str):
                    transfer_date = datetime.date.fromisoformat(transfer_date_raw)
                elif isinstance(transfer_date_raw, datetime.date):
                    transfer_date = transfer_date_raw
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "Invalid transfer date format (YYYY-MM-DD)."})

        # Determine Transfer Type
        if src_account.account_type == FinancialAccountType.CASH_REGISTER and dst_account.account_type in (
            FinancialAccountType.BANK_ACCOUNT,
            FinancialAccountType.MFS_CLEARING,
            FinancialAccountType.PAYMENT_GATEWAY_CLEARING,
        ):
            tx_type = CashBankTransferType.CASH_TO_BANK
        elif src_account.account_type in (
            FinancialAccountType.BANK_ACCOUNT,
            FinancialAccountType.MFS_CLEARING,
            FinancialAccountType.PAYMENT_GATEWAY_CLEARING,
        ) and dst_account.account_type == FinancialAccountType.CASH_REGISTER:
            tx_type = CashBankTransferType.BANK_TO_CASH
        else:
            tx_type = CashBankTransferType.BANK_TO_BANK

        # Resolve Asset GL Codes
        src_gl_code = _resolve_account_gl_code(src_account)
        dst_gl_code = _resolve_account_gl_code(dst_account)

        # Generate Voucher Number
        now_dt = timezone.now()
        prefix = f"TRF-{now_dt.strftime('%Y%m')}"
        random_code = uuid.uuid4().hex[:6].upper()
        transfer_number = f"{prefix}-{random_code}"

        ref_no = str(data.get("reference_number", "")).strip()
        notes = str(data.get("notes", "")).strip()

        # Double-Entry General Ledger Posting
        # Dr Destination Asset (e.g. 1020 Bank), Cr Source Asset (e.g. 1010 Cash)
        journal_lines = [
            {
                "account_code": dst_gl_code,
                "debit_amount_bdt": amount,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Treasury inflow to {dst_account.account_name} ({transfer_number})",
            },
            {
                "account_code": src_gl_code,
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": amount,
                "description": f"Treasury outflow from {src_account.account_name} ({transfer_number})",
            },
        ]

        journal_res = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=journal_lines,
            posting_date=transfer_date,
            source_module=JournalSourceModule.TREASURY_TRANSFER,
            source_reference=transfer_number,
            narration=f"Treasury Transfer {transfer_number}: {src_account.account_name} -> {dst_account.account_name} [{ref_no}]",
            branch_id=str(branch.branch_id) if branch else None,
            actor_user=actor_user,
        )
        journal = JournalEntry.objects.get(entry_id=journal_res["entry_id"])

        # Update registry account balances atomically
        src_account.current_balance = q2(Decimal(str(src_account.current_balance)) - amount)
        dst_account.current_balance = q2(Decimal(str(dst_account.current_balance)) + amount)
        src_account.save()
        dst_account.save()

        transfer = CashBankTransfer.objects.create(
            transfer_id=f"cbtrx-{uuid.uuid4().hex[:12]}",
            transfer_number=transfer_number,
            tenant=tenant,
            branch=branch,
            transfer_type=tx_type,
            source_account=src_account,
            destination_account=dst_account,
            amount_bdt=amount,
            transfer_date=transfer_date,
            reference_number=ref_no,
            status=CashBankTransferStatus.COMPLETED,
            notes=notes,
            posted_journal=journal,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="TREASURY_TRANSFER_EXECUTED",
            resource_type="CashBankTransfer",
            resource_id=transfer.transfer_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "transfer_number": transfer.transfer_number,
                "amount": str(amount),
                "from": src_account.account_name,
                "to": dst_account.account_name,
                "journal_number": journal.journal_number,
            },
        )

        return cls._serialize_transfer(transfer)

    @classmethod
    def list_transfers(
        cls,
        *,
        tenant_id: str,
        transfer_type: Optional[str] = None,
        account_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = CashBankTransfer.objects.filter(tenant=tenant).select_related(
            "branch", "source_account", "destination_account", "posted_journal"
        )
        if transfer_type:
            qs = qs.filter(transfer_type=transfer_type.upper().strip())
        if account_id:
            qs = qs.filter(source_account_id=account_id) | qs.filter(destination_account_id=account_id)

        return [cls._serialize_transfer(t) for t in qs[:200]]

    @classmethod
    def _serialize_transfer(cls, t: CashBankTransfer) -> Dict[str, Any]:
        return {
            "transfer_id": t.transfer_id,
            "transfer_number": t.transfer_number,
            "tenant_id": str(t.tenant_id),
            "branch_id": str(t.branch_id),
            "branch_name": t.branch.name if t.branch else "",
            "transfer_type": t.transfer_type,
            "source_account_id": t.source_account.account_id,
            "source_account_name": t.source_account.account_name,
            "source_account_code": t.source_account.account_code,
            "destination_account_id": t.destination_account.account_id,
            "destination_account_name": t.destination_account.account_name,
            "destination_account_code": t.destination_account.account_code,
            "amount_bdt": str(t.amount_bdt),
            "transfer_date": t.transfer_date.isoformat(),
            "reference_number": t.reference_number,
            "status": t.status,
            "notes": t.notes,
            "posted_journal_id": str(t.posted_journal_id) if t.posted_journal_id else None,
            "posted_journal_number": t.posted_journal.journal_number if t.posted_journal else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }

    # =========================================================================
    # 2. BANK RECONCILIATION ENGINE
    # =========================================================================

    @classmethod
    @transaction.atomic
    def create_reconciliation_batch(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Creates a new bank statement reconciliation batch:
        - Compares statement ending balance with internal GL book balance.
        - Calculates variance difference.
        """
        cls.assert_reconciliation_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        bank_acc_id = data.get("bank_account_id")
        if not bank_acc_id:
            raise ValidationError({"error_code": "ERR_MISSING_BANK_ACCOUNT", "detail": "bank_account_id is required."})

        bank_acc = FinancialAccount.objects.filter(account_id=bank_acc_id, tenant=tenant).first()
        if not bank_acc:
            foreign = FinancialAccount.objects.filter(account_id=bank_acc_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant bank account access forbidden."})
            raise NotFound({"error_code": "ERR_BANK_ACCOUNT_NOT_FOUND", "detail": f"Bank account '{bank_acc_id}' not found."})

        if bank_acc.account_type == FinancialAccountType.CASH_REGISTER:
            raise ValidationError(
                {"error_code": "ERR_INVALID_ACCOUNT_TYPE", "detail": "Bank reconciliation requires a Bank or MFS clearing account, not a cash till."}
            )

        # Statement Ending Balance
        end_balance_raw = data.get("statement_ending_balance")
        if end_balance_raw is None:
            raise ValidationError({"error_code": "ERR_MISSING_STATEMENT_BALANCE", "detail": "statement_ending_balance is required."})
        statement_ending = q2(Decimal(str(end_balance_raw)))

        start_balance_raw = data.get("statement_starting_balance", "0.00")
        statement_starting = q2(Decimal(str(start_balance_raw)))

        # Current book balance from financial account registry
        book_balance = q2(Decimal(str(bank_acc.current_balance)))
        diff = q2(statement_ending - book_balance)

        reconciled_status = BankReconciliationStatus.RECONCILED if diff == DECIMAL_ZERO else BankReconciliationStatus.DISCREPANCY

        now_dt = timezone.now()
        prefix = f"BREC-{now_dt.strftime('%Y%m')}"
        random_code = uuid.uuid4().hex[:6].upper()
        batch_number = f"{prefix}-{random_code}"

        branch = bank_acc.branch or Branch.objects.filter(tenant=tenant).first()

        batch = BankReconciliationBatch.objects.create(
            reconciliation_id=f"brec-{uuid.uuid4().hex[:12]}",
            batch_number=batch_number,
            tenant=tenant,
            branch=branch,
            bank_account=bank_acc,
            statement_date=data.get("statement_date") or now_dt.date(),
            statement_starting_balance=statement_starting,
            statement_ending_balance=statement_ending,
            book_balance=book_balance,
            reconciled_balance=book_balance if reconciled_status == BankReconciliationStatus.RECONCILED else statement_ending,
            difference_bdt=diff,
            status=reconciled_status,
            notes=str(data.get("notes", "")).strip(),
            reconciled_by=actor_user if reconciled_status == BankReconciliationStatus.RECONCILED else None,
            reconciled_at=now_dt if reconciled_status == BankReconciliationStatus.RECONCILED else None,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="BANK_RECONCILIATION_RUN",
            resource_type="BankReconciliationBatch",
            resource_id=batch.reconciliation_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "batch_number": batch.batch_number,
                "statement_ending": str(statement_ending),
                "book_balance": str(book_balance),
                "difference": str(diff),
                "status": batch.status,
            },
        )

        return cls._serialize_reconciliation(batch)

    @classmethod
    def list_reconciliations(
        cls,
        *,
        tenant_id: str,
        bank_account_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = BankReconciliationBatch.objects.filter(tenant=tenant).select_related("bank_account", "branch")
        if bank_account_id:
            qs = qs.filter(bank_account_id=bank_account_id)

        return [cls._serialize_reconciliation(b) for b in qs[:100]]

    @classmethod
    def _serialize_reconciliation(cls, b: BankReconciliationBatch) -> Dict[str, Any]:
        return {
            "reconciliation_id": b.reconciliation_id,
            "batch_number": b.batch_number,
            "tenant_id": str(b.tenant_id),
            "branch_id": str(b.branch_id),
            "bank_account_id": b.bank_account.account_id,
            "bank_account_name": b.bank_account.account_name,
            "bank_account_code": b.bank_account.account_code,
            "statement_date": b.statement_date.isoformat(),
            "statement_starting_balance": str(b.statement_starting_balance),
            "statement_ending_balance": str(b.statement_ending_balance),
            "book_balance": str(b.book_balance),
            "reconciled_balance": str(b.reconciled_balance),
            "difference_bdt": str(b.difference_bdt),
            "status": b.status,
            "notes": b.notes,
            "reconciled_by": str(b.reconciled_by.email) if b.reconciled_by else None,
            "reconciled_at": b.reconciled_at.isoformat() if b.reconciled_at else None,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
