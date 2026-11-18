"""
backend/apps/core/double_entry_journal_service.py
Architecture Decision: ADR-0282 (P282).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing the General Ledger Double-Entry Journal Engine:
- Balanced journal entry creation (total debits == total credits).
- GL account balance synchronization according to normal balances (Debit / Credit).
- Idempotency replay recovery for offline POS and network retries.
- Full cryptographic reversal trail with debit/credit swap.
- Strict multi-tenant isolation and role-based access control.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    ChartOfAccount,
    FinancialPeriod,
    FinancialPeriodStatus,
    GLAccountStatus,
    GLNormalBalance,
    JournalEntry,
    JournalEntryLine,
    JournalEntryStatus,
    JournalSourceModule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

JOURNAL_VIEW_ROLES = {
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

JOURNAL_POST_ROLES = {
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


def q2(val: Any) -> Decimal:
    """Strictly quantizes to 2 decimal places."""
    if val is None:
        return DECIMAL_ZERO
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"))
    except Exception:
        return DECIMAL_ZERO


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DoubleEntryJournalService:
    """
    Authoritative domain service for General Ledger double-entry postings and reversals.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in JOURNAL_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to access General Ledger journals.",
                    "message_en": "Cashier and staff pharmacist are not authorized to access General Ledger journals.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট জেনারেল লেজার জার্নাল দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_post_authorized(cls, actor_user: Any, source_module: str = JournalSourceModule.MANUAL) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        # Operational module postings (e.g. POS sales checkout, customer AR collections, treasury transfers) are authorized for operational staff
        if source_module in (
            JournalSourceModule.POS_SALE,
            JournalSourceModule.PROCUREMENT,
            JournalSourceModule.CUSTOMER_AR,
            JournalSourceModule.SUPPLIER_AP,
            JournalSourceModule.STOCK_ADJUSTMENT,
            JournalSourceModule.OPERATING_EXPENSE,
            JournalSourceModule.TREASURY_TRANSFER,
            JournalSourceModule.BANK_RECONCILIATION,
            JournalSourceModule.PAYROLL,
            JournalSourceModule.CLOSING,
        ):
            return

        if role not in JOURNAL_POST_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_JOURNAL_POST_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can post or reverse General Ledger journals.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can post or reverse General Ledger journals.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার জার্নাল পোস্ট বা রিভার্স করতে পারেন।",
                }
            )

    @classmethod
    def assert_period_open(cls, tenant: Tenant, posting_date: Any) -> None:
        """
        ADR-0292 (P292): Enforces financial period locks.
        Throws ValidationError with ERR_PERIOD_CLOSED if posting_date falls into a closed accounting period.
        """
        if not posting_date:
            return
        p_date = posting_date
        if hasattr(p_date, "date"):
            p_date = p_date.date()
        elif isinstance(p_date, str):
            try:
                p_date = timezone.datetime.fromisoformat(p_date).date()
            except Exception:
                return

        closed_period = FinancialPeriod.objects.filter(
            tenant=tenant,
            status=FinancialPeriodStatus.CLOSED,
            start_date__lte=p_date,
            end_date__gte=p_date,
        ).first()

        if closed_period:
            raise ValidationError(
                {
                    "error_code": "ERR_PERIOD_CLOSED",
                    "detail": f"Financial period '{closed_period.period_code}' ({closed_period.start_date} to {closed_period.end_date}) is CLOSED. Postings to locked periods are strictly forbidden.",
                    "message_en": f"Financial period '{closed_period.period_code}' is CLOSED. Postings to locked periods are strictly forbidden.",
                    "message_bn": f"আর্থিক পিরিয়ড '{closed_period.period_code}' বন্ধ ও লক করা হয়েছে। বন্ধ পিরিয়ডে লেনদেন পোস্ট করা সম্পূর্ণ নিষিদ্ধ।",
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
                    "detail": "Tenant account subscription is suspended. Journal operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Journal operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। জার্নাল অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def post_journal_entry(
        cls,
        *,
        tenant_id: str,
        lines: List[Dict[str, Any]],
        posting_date: Optional[Any] = None,
        source_module: str = JournalSourceModule.MANUAL,
        source_reference: str = "",
        narration: str = "",
        branch_id: Optional[str] = None,
        idempotency_key: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Atomically records a balanced double-entry General Ledger journal entry.
        Verifies: total debits == total credits, minimum 2 lines, active GL accounts.
        Updates GL account balances atomically.
        """
        cls.assert_post_authorized(actor_user, source_module=source_module)
        tenant = cls.assert_tenant_active(tenant_id)

        # ADR-0292 (P292): Financial Period Close Enforced Immutability
        if source_module != JournalSourceModule.CLOSING:
            cls.assert_period_open(tenant, posting_date)

        # 1. Idempotency Check
        clean_idem = (idempotency_key or "").strip()
        if clean_idem:
            existing = JournalEntry.objects.filter(tenant=tenant, idempotency_key=clean_idem).first()
            if existing:
                return cls._serialize_journal_entry(existing, is_idempotent_replay=True)

        if not lines or len(lines) < 2:
            raise ValidationError(
                {
                    "error_code": "ERR_MINIMUM_LINES_REQUIRED",
                    "detail": "A double-entry journal entry requires at least two lines (at least one debit and one credit).",
                    "message_en": "A double-entry journal entry requires at least two lines.",
                    "message_bn": "ডাবল-এন্ট্রি জার্নালের জন্য ন্যূনতম দুটি লাইন আবশ্যক (অন্তত একটি ডেবিট এবং একটি ক্রেডিট)।",
                }
            )

        # 2. Compute Debits and Credits and Validate
        total_debits = DECIMAL_ZERO
        total_credits = DECIMAL_ZERO
        parsed_lines = []

        for idx, l in enumerate(lines, start=1):
            account_id = l.get("account_id")
            account_code = l.get("account_code")

            account = None
            if account_id:
                account = ChartOfAccount.objects.select_for_update().filter(tenant=tenant, account_id=account_id).first()
            elif account_code:
                account = ChartOfAccount.objects.select_for_update().filter(tenant=tenant, account_code=account_code).first()

            if not account:
                raise ValidationError(
                    {
                        "error_code": "ERR_ACCOUNT_NOT_FOUND",
                        "detail": f"GL account '{account_id or account_code}' on line {idx} does not exist in this pharmacy tenant.",
                    }
                )

            if account.status != GLAccountStatus.ACTIVE:
                raise ValidationError(
                    {
                        "error_code": "ERR_INACTIVE_GL_ACCOUNT",
                        "detail": f"GL account '{account.account_code}' on line {idx} is inactive or archived.",
                    }
                )

            debit = q2(l.get("debit_amount_bdt", 0))
            credit = q2(l.get("credit_amount_bdt", 0))

            if debit < DECIMAL_ZERO or credit < DECIMAL_ZERO:
                raise ValidationError(
                    {"error_code": "ERR_NEGATIVE_AMOUNT", "detail": f"Line {idx} cannot have negative amounts."}
                )

            if debit == DECIMAL_ZERO and credit == DECIMAL_ZERO:
                raise ValidationError(
                    {"error_code": "ERR_ZERO_LINE_AMOUNT", "detail": f"Line {idx} must have non-zero debit or credit."}
                )

            if debit > DECIMAL_ZERO and credit > DECIMAL_ZERO:
                raise ValidationError(
                    {"error_code": "ERR_DUAL_LEG_LINE", "detail": f"Line {idx} cannot have both debit and credit amounts."}
                )

            total_debits += debit
            total_credits += credit

            parsed_lines.append({
                "account": account,
                "line_number": idx,
                "description": str(l.get("description") or "").strip(),
                "debit_amount_bdt": debit,
                "credit_amount_bdt": credit,
            })

        # 3. Strict Balanced Verification
        if total_debits != total_credits or total_debits <= DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_UNBALANCED_JOURNAL_ENTRY",
                    "detail": f"Unbalanced double-entry journal: Total Debits ({total_debits} BDT) does not equal Total Credits ({total_credits} BDT).",
                    "message_en": f"Unbalanced journal entry: Total Debits ({total_debits} BDT) != Total Credits ({total_credits} BDT).",
                    "message_bn": f"জার্নাল এন্ট্রি অসমান: মোট ডেবিট ({total_debits} টাকা) এবং মোট ক্রেডিট ({total_credits} টাকা) সমান নয়।",
                }
            )

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first() if branch_id else None
        p_date = posting_date or timezone.now().date()
        if isinstance(p_date, str):
            try:
                p_date = timezone.datetime.fromisoformat(p_date).date()
            except Exception:
                p_date = timezone.now().date()

        # 4. Create JournalEntry
        now = timezone.now()
        journal_number = f"JRN-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:6].upper()}"
        sha_sig = compute_sha256(f"{tenant.tenant_id}|{journal_number}|{total_debits}|{now.isoformat()}")

        entry = JournalEntry.objects.create(
            tenant=tenant,
            branch=branch,
            journal_number=journal_number,
            posting_date=p_date,
            source_module=source_module,
            source_reference=source_reference.strip(),
            narration=narration.strip(),
            status=JournalEntryStatus.POSTED,
            total_debit_bdt=total_debits,
            total_credit_bdt=total_credits,
            is_balanced=True,
            idempotency_key=clean_idem,
            sha256_hash=sha_sig,
            posted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        # 5. Create Lines and Update Account Balances
        for pl in parsed_lines:
            JournalEntryLine.objects.create(
                journal_entry=entry,
                account=pl["account"],
                line_number=pl["line_number"],
                description=pl["description"],
                debit_amount_bdt=pl["debit_amount_bdt"],
                credit_amount_bdt=pl["credit_amount_bdt"],
            )

            acc = pl["account"]
            if acc.normal_balance == GLNormalBalance.DEBIT:
                acc.current_balance_bdt += (pl["debit_amount_bdt"] - pl["credit_amount_bdt"])
            else:
                acc.current_balance_bdt += (pl["credit_amount_bdt"] - pl["debit_amount_bdt"])
            acc.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="JOURNAL_POST",
            resource_type="JournalEntry",
            resource_id=entry.entry_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"journal_number": entry.journal_number, "total_bdt": str(total_debits)},
        )

        return cls._serialize_journal_entry(entry, is_idempotent_replay=False)

    @classmethod
    @transaction.atomic
    def reverse_journal_entry(
        cls,
        *,
        tenant_id: str,
        entry_id: str,
        reason: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Reverses a posted journal entry by generating a compensating reversal entry
        with debit and credit legs swapped, adjusting GL account balances accordingly.
        """
        # Reversals always require explicit owner or finance role authorization
        cls.assert_post_authorized(actor_user, source_module=JournalSourceModule.MANUAL)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REVERSAL_REASON",
                    "detail": "A mandatory explanation reason is required for journal reversal.",
                    "message_en": "A mandatory explanation reason is required for journal reversal.",
                    "message_bn": "জার্নাল রিভার্সালের জন্য সুনির্দিষ্ট কারণ উল্লেখ করা বাধ্যতামূলক।",
                }
            )

        orig = JournalEntry.objects.select_for_update().filter(tenant=tenant, entry_id=entry_id).first()
        if not orig:
            foreign = JournalEntry.objects.filter(entry_id=entry_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_JOURNAL_NOT_FOUND", "detail": f"Journal entry '{entry_id}' not found."})

        if orig.status == JournalEntryStatus.REVERSED:
            raise ValidationError(
                {
                    "error_code": "ERR_ALREADY_REVERSED",
                    "detail": f"Journal entry '{orig.journal_number}' has already been reversed.",
                    "message_en": "Journal entry has already been reversed.",
                    "message_bn": "এই জার্নাল এন্ট্রিটি পূর্বেই রিভার্স করা হয়েছে।",
                }
            )

        orig_lines = list(orig.lines.all().order_by("line_number"))

        now = timezone.now()
        rev_number = f"REV-{orig.journal_number}"
        sha_sig = compute_sha256(f"{tenant.tenant_id}|{rev_number}|{orig.total_debit_bdt}|{now.isoformat()}")

        reversal_entry = JournalEntry.objects.create(
            tenant=tenant,
            branch=orig.branch,
            journal_number=rev_number,
            posting_date=now.date(),
            source_module=JournalSourceModule.CLOSING,
            source_reference=orig.journal_number,
            narration=f"Reversal of {orig.journal_number}: {clean_reason}",
            status=JournalEntryStatus.POSTED,
            total_debit_bdt=orig.total_debit_bdt,
            total_credit_bdt=orig.total_credit_bdt,
            is_balanced=True,
            reversal_of=orig,
            reversal_reason=clean_reason,
            sha256_hash=sha_sig,
            posted_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        for line in orig_lines:
            swapped_debit = line.credit_amount_bdt
            swapped_credit = line.debit_amount_bdt

            JournalEntryLine.objects.create(
                journal_entry=reversal_entry,
                account=line.account,
                line_number=line.line_number,
                description=f"Reversal leg: {line.description}",
                debit_amount_bdt=swapped_debit,
                credit_amount_bdt=swapped_credit,
            )

            acc = ChartOfAccount.objects.select_for_update().get(account_id=line.account_id)
            if acc.normal_balance == GLNormalBalance.DEBIT:
                acc.current_balance_bdt += (swapped_debit - swapped_credit)
            else:
                acc.current_balance_bdt += (swapped_credit - swapped_debit)
            acc.save()

        orig.status = JournalEntryStatus.REVERSED
        orig.reversal_reason = clean_reason
        orig.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="JOURNAL_REVERSAL",
            resource_type="JournalEntry",
            resource_id=reversal_entry.entry_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"original_journal": orig.journal_number, "reason": clean_reason},
        )

        return {
            "success": True,
            "original_journal_id": orig.entry_id,
            "original_status": orig.status,
            "reversal_journal_id": reversal_entry.entry_id,
            "reversal_journal_number": reversal_entry.journal_number,
            "total_bdt": str(reversal_entry.total_debit_bdt),
            "reversal_reason": clean_reason,
            "message_en": f"Journal entry '{orig.journal_number}' successfully reversed.",
            "message_bn": f"জার্নাল এন্ট্রি '{orig.journal_number}' সফলভাবে রিভার্স করা হয়েছে।",
        }

    @classmethod
    def get_journal_entry(
        cls,
        *,
        tenant_id: str,
        entry_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """Retrieves authoritative detail of a journal entry and its lines."""
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        entry = JournalEntry.objects.filter(tenant=tenant, entry_id=entry_id).first()
        if not entry:
            raise NotFound({"error_code": "ERR_JOURNAL_NOT_FOUND", "detail": f"Journal '{entry_id}' not found."})

        return cls._serialize_journal_entry(entry)

    @classmethod
    def list_journal_entries(
        cls,
        *,
        tenant_id: str,
        source_module: Optional[str] = None,
        status: Optional[str] = None,
        branch_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        """Lists tenant journal entries with filter controls."""
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = JournalEntry.objects.filter(tenant=tenant)
        if source_module:
            qs = qs.filter(source_module=source_module.upper())
        if status:
            qs = qs.filter(status=status.upper())
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if start_date:
            qs = qs.filter(posting_date__gte=start_date)
        if end_date:
            qs = qs.filter(posting_date__lte=end_date)

        return [cls._serialize_journal_entry(e) for e in qs.order_by("-posting_date", "-created_at")[:100]]

    @classmethod
    def _serialize_journal_entry(cls, entry: JournalEntry, is_idempotent_replay: bool = False) -> Dict[str, Any]:
        lines = [
            {
                "line_id": l.line_id,
                "line_number": l.line_number,
                "account_id": l.account.account_id,
                "account_code": l.account.account_code,
                "account_name": l.account.account_name,
                "category": l.account.category,
                "debit_amount_bdt": str(l.debit_amount_bdt),
                "credit_amount_bdt": str(l.credit_amount_bdt),
                "description": l.description,
            }
            for l in entry.lines.all().order_by("line_number")
        ]

        return {
            "success": True,
            "entry_id": entry.entry_id,
            "journal_number": entry.journal_number,
            "posting_date": entry.posting_date.isoformat() if hasattr(entry.posting_date, "isoformat") else str(entry.posting_date),
            "source_module": entry.source_module,
            "source_reference": entry.source_reference,
            "narration": entry.narration,
            "status": entry.status,
            "total_debit_bdt": str(entry.total_debit_bdt),
            "total_credit_bdt": str(entry.total_credit_bdt),
            "is_balanced": entry.is_balanced,
            "is_idempotent_replay": is_idempotent_replay,
            "lines_count": len(lines),
            "lines": lines,
            "sha256_hash": entry.sha256_hash,
            "created_at": entry.created_at.isoformat(),
        }
