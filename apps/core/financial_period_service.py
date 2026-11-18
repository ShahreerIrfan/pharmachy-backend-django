"""
backend/apps/core/financial_period_service.py
Architecture Decision: ADR-0292 (P292).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Financial Accounting Period Close and Controlled Reopening:
1. Lifecycle Management: OPEN -> CLOSING -> CLOSED -> REOPENED.
2. Pre-Closing Ledger Integrity Checks:
   - Trial Balance debit/credit parity.
   - Detection of pending unapproved draft operating expenses and draft payroll batches.
   - Calculation of Period Total Revenues (4000s), Total Expenses (5000s COGS + 6000s OpEx), and Net Profit/Loss.
3. Closing Double-Entry Journal Posting:
   - Closes nominal Revenue accounts (Debit) and nominal Expense accounts (Credit).
   - Transfers Net Profit (Credit) or Net Loss (Debit) to GL Account 3020 (Retained Earnings).
   - Invariant: Total Debits == Total Credits.
4. Strict Immutability Enforcement:
   - Closed periods lock the general ledger: all retroactive postings are blocked (ERR_PERIOD_CLOSED).
5. Controlled Audited Reopening:
   - Strictly gated to Pharmacy Owner & SuperAdmin.
   - Requires mandatory formal audit reason and reverses/voids the closing journal entry.
"""

from __future__ import annotations

from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import DoubleEntryJournalService, q2
from apps.core.models import (
    Branch,
    ChartOfAccount,
    FinancialPeriod,
    FinancialPeriodReopenLog,
    FinancialPeriodStatus,
    FinancialPeriodType,
    GLAccountCategory,
    JournalEntry,
    JournalEntryLine,
    JournalEntryStatus,
    JournalSourceModule,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    PayrollRunBatch,
    PayrollRunStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

PERIOD_VIEW_ROLES = {
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

PERIOD_CLOSE_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.FINANCE_OFFICER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
    "FINANCE_OFFICER",
    "SUPERADMIN",
    "OWNER_ADMIN",
    "ADMIN",
}

PERIOD_REOPEN_ROLES = {
    UserRole.PHARMACY_OWNER,
    UserRole.SUPERADMIN,
    "PHARMACY_OWNER",
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


class FinancialPeriodService:
    """
    Authoritative service managing accounting period closures, ledger balance resets,
    immutability locking, and audited reopening.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in PERIOD_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERIOD_VIEW_UNAUTHORIZED",
                    "detail": "Cashier and pharmacy staff are not authorized to access financial accounting periods.",
                    "message_en": "Cashier and pharmacy staff are not authorized to access financial accounting periods.",
                    "message_bn": "ক্যাশিয়ার বা সাধারণ স্টাফদের আর্থিক হিসাব পিরিয়ড দেখার অনুমতি নেই।",
                }
            )

    @classmethod
    def assert_close_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in PERIOD_CLOSE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERIOD_CLOSE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Senior Finance Officer can close a financial accounting period.",
                    "message_en": "Only Pharmacy Owner or Senior Finance Officer can close a financial accounting period.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা সিনিয়র ফিন্যান্স অফিসার আর্থিক পিরিয়ড বন্ধ বা সিল করতে পারেন।",
                }
            )

    @classmethod
    def assert_reopen_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in PERIOD_REOPEN_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERIOD_REOPEN_UNAUTHORIZED",
                    "detail": "Controlled reopening of a closed financial period strictly requires Pharmacy Owner or SuperAdmin authorization.",
                    "message_en": "Controlled reopening of a closed financial period strictly requires Pharmacy Owner authorization.",
                    "message_bn": "বন্ধ আর্থিক পিরিয়ড পুনরায় উন্মুক্ত করার জন্য শুধুমাত্র ফার্মেসি ওনারের অনুমোদন আবশ্যক।",
                }
            )

    @classmethod
    def assert_tenant_active(cls, tenant_id: str) -> Tenant:
        tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
        if not tenant:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant '{tenant_id}' not found."})
        if (
            tenant.status == TenantLifecycleStatus.SUSPENDED
            or getattr(tenant, "subscription_status", None) == "SUSPENDED"
        ):
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Financial period operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Financial period operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। আর্থিক পিরিয়ড কার্যক্রম বন্ধ রাখা হয়েছে।",
                }
            )
        return tenant

    # =========================================================================
    # 1. CREATE FINANCIAL PERIOD
    # =========================================================================

    @classmethod
    @transaction.atomic
    def create_period(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_close_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        period_code = str(data.get("period_code", "")).strip().upper()
        if not period_code:
            raise ValidationError({"error_code": "ERR_MISSING_PERIOD_CODE", "detail": "period_code is required (e.g. FY2026-M11)."})

        if FinancialPeriod.objects.filter(tenant=tenant, period_code=period_code).exists():
            raise ValidationError(
                {"error_code": "ERR_DUPLICATE_PERIOD_CODE", "detail": f"Period code '{period_code}' already exists in this pharmacy."}
            )

        start_raw = data.get("start_date")
        end_raw = data.get("end_date")
        if not start_raw or not end_raw:
            raise ValidationError({"error_code": "ERR_MISSING_DATES", "detail": "Both start_date and end_date are required."})

        try:
            p_start = timezone.datetime.fromisoformat(str(start_raw)).date()
            p_end = timezone.datetime.fromisoformat(str(end_raw)).date()
            if p_start > p_end:
                raise ValueError()
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_PERIOD_DATES", "detail": "start_date must be before or equal to end_date."})

        branch = None
        branch_id = data.get("branch_id")
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        p_type = str(data.get("period_type", FinancialPeriodType.MONTHLY)).upper().strip()
        if p_type not in FinancialPeriodType.values:
            p_type = FinancialPeriodType.MONTHLY

        period_name = str(data.get("period_name", "")).strip() or f"Accounting Period {period_code}"

        period = FinancialPeriod.objects.create(
            period_id=f"fper-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            period_code=period_code,
            period_name=period_name,
            period_type=p_type,
            start_date=p_start,
            end_date=p_end,
            status=FinancialPeriodStatus.OPEN,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="FINANCIAL_PERIOD_CREATE",
            resource_type="FinancialPeriod",
            resource_id=period.period_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"period_code": period_code, "start": str(p_start), "end": str(p_end)},
        )

        return cls._serialize_period(period)

    # =========================================================================
    # 2. VALIDATE LEDGER INTEGRITY
    # =========================================================================

    @classmethod
    def validate_period_integrity(
        cls,
        *,
        tenant_id: str,
        period_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Executes pre-close ledger integrity checks:
        1. Checks Trial Balance parity (Sum Dr == Sum Cr across all tenant journals).
        2. Checks for unposted draft operating expenses within the period.
        3. Checks for unposted draft payroll batches within the period.
        4. Calculates period revenues (4000s), COGS (5000s), operating expenses (6000s), and projected net income.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        period = FinancialPeriod.objects.filter(period_id=period_id, tenant=tenant).first()
        if not period:
            foreign = FinancialPeriod.objects.filter(period_id=period_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant period access forbidden."})
            raise NotFound({"error_code": "ERR_PERIOD_NOT_FOUND", "detail": f"Financial period '{period_id}' not found."})

        blocking_issues: List[str] = []

        # 1. Trial Balance debit/credit parity check
        totals = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
        ).aggregate(
            total_dr=Sum("debit_amount_bdt"),
            total_cr=Sum("credit_amount_bdt"),
        )
        total_dr = q2(totals.get("total_dr") or DECIMAL_ZERO)
        total_cr = q2(totals.get("total_cr") or DECIMAL_ZERO)
        trial_balance_balanced = (total_dr == total_cr)
        if not trial_balance_balanced:
            blocking_issues.append(f"Trial Balance out of balance: Total Dr ({total_dr}) != Total Cr ({total_cr}).")

        # 2. Unapproved Draft Expenses check
        pending_expenses_count = OperatingExpenseEntry.objects.filter(
            tenant=tenant,
            status=OperatingExpenseStatus.DRAFT,
            expense_date__gte=period.start_date,
            expense_date__lte=period.end_date,
        ).count()
        if pending_expenses_count > 0:
            blocking_issues.append(f"There are {pending_expenses_count} unapproved draft operating expenses in this period.")

        # 3. Unapproved Draft Payroll check
        pending_payroll_count = PayrollRunBatch.objects.filter(
            tenant=tenant,
            status=PayrollRunStatus.DRAFT,
            period_start__gte=period.start_date,
            period_end__lte=period.end_date,
        ).count()
        if pending_payroll_count > 0:
            blocking_issues.append(f"There are {pending_payroll_count} unapproved draft payroll runs in this period.")

        # 4. Period P&L Calculation
        period_lines = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
            journal_entry__posting_date__gte=period.start_date,
            journal_entry__posting_date__lte=period.end_date,
        ).exclude(
            journal_entry__source_module=JournalSourceModule.CLOSING,
        ).select_related("account")

        total_revenues = DECIMAL_ZERO
        total_cogs = DECIMAL_ZERO
        total_expenses = DECIMAL_ZERO

        for line in period_lines:
            cat = line.account.category
            dr = line.debit_amount_bdt
            cr = line.credit_amount_bdt
            if cat == GLAccountCategory.REVENUE:
                total_revenues += (cr - dr)
            elif cat == GLAccountCategory.EXPENSE:
                total_expenses += (dr - cr)

        total_revenues = q2(total_revenues)
        total_expenses = q2(total_expenses)
        net_income = q2(total_revenues - total_expenses)

        is_ready = (len(blocking_issues) == 0)

        return {
            "period_id": period.period_id,
            "period_code": period.period_code,
            "is_ready_to_close": is_ready,
            "trial_balance_balanced": trial_balance_balanced,
            "blocking_issues": blocking_issues,
            "financial_summary": {
                "total_revenues_bdt": str(total_revenues),
                "total_expenses_bdt": str(total_expenses),
                "net_income_bdt": str(net_income),
                "is_profitable": net_income >= DECIMAL_ZERO,
            },
        }

    # =========================================================================
    # 3. CLOSE FINANCIAL PERIOD
    # =========================================================================

    @classmethod
    @transaction.atomic
    def close_period(
        cls,
        *,
        tenant_id: str,
        period_id: str,
        closing_notes: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Closes the financial period and atomically posts balanced double-entry closing journal:
        - Nominal Revenues (4000s) Debited to close to Retained Earnings (3020).
        - Nominal Expenses (6000s) Credited to close to Retained Earnings (3020).
        - Net Income Credited (if profit) or Debited (if loss) to 3020.
        - Sets period status to CLOSED.
        - Future retroactive postings to this period are blocked.
        """
        cls.assert_close_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        period = FinancialPeriod.objects.select_for_update().filter(period_id=period_id, tenant=tenant).first()
        if not period:
            foreign = FinancialPeriod.objects.filter(period_id=period_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant period access forbidden."})
            raise NotFound({"error_code": "ERR_PERIOD_NOT_FOUND", "detail": f"Financial period '{period_id}' not found."})

        if period.status == FinancialPeriodStatus.CLOSED:
            raise ValidationError(
                {
                    "error_code": "ERR_PERIOD_ALREADY_CLOSED",
                    "detail": f"Financial period '{period.period_code}' is already closed and sealed.",
                    "message_en": "Financial period is already closed.",
                    "message_bn": "আর্থিক হিসাব পিরিয়ড ইতোমধ্যে বন্ধ ও সিল করা হয়েছে।",
                }
            )

        # Run integrity validation
        integrity = cls.validate_period_integrity(tenant_id=tenant_id, period_id=period_id, actor_user=actor_user)
        if not integrity["is_ready_to_close"]:
            raise ValidationError(
                {
                    "error_code": "ERR_PERIOD_INTEGRITY_FAILED",
                    "detail": "Period cannot be closed due to ledger integrity violations.",
                    "blocking_issues": integrity["blocking_issues"],
                }
            )

        # Calculate nominal account balances for closing journal
        period_lines = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
            journal_entry__posting_date__gte=period.start_date,
            journal_entry__posting_date__lte=period.end_date,
        ).exclude(
            journal_entry__source_module=JournalSourceModule.CLOSING,
        ).select_related("account")

        account_net_map: Dict[str, Dict[str, Any]] = {}
        for line in period_lines:
            acc = line.account
            if acc.category not in (GLAccountCategory.REVENUE, GLAccountCategory.EXPENSE, GLAccountCategory.COGS):
                continue
            code = acc.account_code
            if code not in account_net_map:
                account_net_map[code] = {
                    "account": acc,
                    "net_balance": DECIMAL_ZERO,
                }
            if acc.category == GLAccountCategory.REVENUE:
                account_net_map[code]["net_balance"] += (line.credit_amount_bdt - line.debit_amount_bdt)
            elif acc.category in (GLAccountCategory.EXPENSE, GLAccountCategory.COGS):
                account_net_map[code]["net_balance"] += (line.debit_amount_bdt - line.credit_amount_bdt)

        # Construct Balanced Double-Entry Closing Lines
        closing_lines = []
        total_rev_closed = DECIMAL_ZERO
        total_exp_closed = DECIMAL_ZERO

        for code, item in account_net_map.items():
            acc = item["account"]
            bal = q2(item["net_balance"])
            if bal == DECIMAL_ZERO:
                continue

            if acc.category == GLAccountCategory.REVENUE:
                # Credit balance (normal): debit revenue to reset to 0
                # Debit balance (contra-revenue, e.g. 4030): credit revenue to reset to 0
                if bal > DECIMAL_ZERO:
                    closing_lines.append(
                        {
                            "account_code": code,
                            "debit_amount_bdt": bal,
                            "credit_amount_bdt": DECIMAL_ZERO,
                            "description": f"Close revenue {acc.account_name} for period {period.period_code}",
                        }
                    )
                else:
                    closing_lines.append(
                        {
                            "account_code": code,
                            "debit_amount_bdt": DECIMAL_ZERO,
                            "credit_amount_bdt": abs(bal),
                            "description": f"Close contra-revenue {acc.account_name} for period {period.period_code}",
                        }
                    )
                total_rev_closed += bal
            elif acc.category in (GLAccountCategory.EXPENSE, GLAccountCategory.COGS):
                # Debit balance (normal): credit expense/cogs to reset to 0
                # Credit balance: debit expense/cogs to reset to 0
                if bal > DECIMAL_ZERO:
                    closing_lines.append(
                        {
                            "account_code": code,
                            "debit_amount_bdt": DECIMAL_ZERO,
                            "credit_amount_bdt": bal,
                            "description": f"Close expense/COGS {acc.account_name} for period {period.period_code}",
                        }
                    )
                else:
                    closing_lines.append(
                        {
                            "account_code": code,
                            "debit_amount_bdt": abs(bal),
                            "credit_amount_bdt": DECIMAL_ZERO,
                            "description": f"Close contra-expense {acc.account_name} for period {period.period_code}",
                        }
                    )
                total_exp_closed += bal

        net_profit = q2(total_rev_closed - total_exp_closed)

        # Retained Earnings balancing line (GL 3020)
        if net_profit > DECIMAL_ZERO:
            # Profit -> Credit Retained Earnings
            closing_lines.append(
                {
                    "account_code": "3020",
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": net_profit,
                    "description": f"Transfer net profit to Retained Earnings for {period.period_code}",
                }
            )
        elif net_profit < DECIMAL_ZERO:
            # Loss -> Debit Retained Earnings
            closing_lines.append(
                {
                    "account_code": "3020",
                    "debit_amount_bdt": abs(net_profit),
                    "credit_amount_bdt": DECIMAL_ZERO,
                    "description": f"Transfer net loss to Retained Earnings for {period.period_code}",
                }
            )

        closing_journal = None
        if len(closing_lines) >= 2:
            journal_res = DoubleEntryJournalService.post_journal_entry(
                tenant_id=str(tenant.tenant_id),
                lines=closing_lines,
                posting_date=period.end_date,
                source_module=JournalSourceModule.CLOSING,
                source_reference=f"CLOSE-{period.period_code}",
                narration=f"Closing Entry for Period {period.period_code} ({period.start_date} to {period.end_date}) -> Retained Earnings",
                branch_id=str(period.branch.branch_id) if period.branch else None,
                actor_user=actor_user,
            )
            closing_journal = JournalEntry.objects.get(entry_id=journal_res["entry_id"])

        period.status = FinancialPeriodStatus.CLOSED
        period.closed_at = timezone.now()
        period.closed_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        period.closing_notes = closing_notes.strip()
        period.closing_journal = closing_journal
        period.total_revenues_bdt = total_rev_closed
        period.total_expenses_bdt = total_exp_closed
        period.net_income_bdt = net_profit
        period.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="FINANCIAL_PERIOD_CLOSE",
            resource_type="FinancialPeriod",
            resource_id=period.period_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "period_code": period.period_code,
                "net_income_bdt": str(net_profit),
                "closing_journal_id": closing_journal.entry_id if closing_journal else None,
            },
        )

        return cls._serialize_period(period)

    # =========================================================================
    # 4. CONTROLLED REOPENING (ELEVATED PHARMACY OWNER / SUPERADMIN ONLY)
    # =========================================================================

    @classmethod
    @transaction.atomic
    def reopen_period(
        cls,
        *,
        tenant_id: str,
        period_id: str,
        reopen_reason: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Controlled reopening of a closed financial period.
        Strictly requires Pharmacy Owner authorization and a mandatory formal audit reason.
        Reverses the period's closing journal entry to restore nominal account balances.
        """
        cls.assert_reopen_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reopen_reason or "").strip()
        if not clean_reason:
            raise ValidationError(
                {
                    "error_code": "ERR_MISSING_REOPEN_REASON",
                    "detail": "A formal audit explanation is mandatory to reopen a sealed financial period.",
                    "message_en": "A formal audit explanation is mandatory to reopen a sealed financial period.",
                    "message_bn": "সিলকৃত আর্থিক পিরিয়ড পুনরায় উন্মুক্ত করার জন্য অডিট কারণ উল্লেখ করা বাধ্যতামূলক।",
                }
            )

        period = FinancialPeriod.objects.select_for_update().filter(period_id=period_id, tenant=tenant).first()
        if not period:
            foreign = FinancialPeriod.objects.filter(period_id=period_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant period access forbidden."})
            raise NotFound({"error_code": "ERR_PERIOD_NOT_FOUND", "detail": f"Financial period '{period_id}' not found."})

        if period.status != FinancialPeriodStatus.CLOSED:
            raise ValidationError(
                {
                    "error_code": "ERR_PERIOD_NOT_CLOSED",
                    "detail": f"Only CLOSED periods can be reopened. Current status is '{period.status}'.",
                }
            )

        prev_journal = period.closing_journal

        # Reverse the closing journal if one was posted
        if prev_journal and prev_journal.status != JournalEntryStatus.REVERSED:
            DoubleEntryJournalService.reverse_journal_entry(
                tenant_id=str(tenant.tenant_id),
                entry_id=prev_journal.entry_id,
                reason=f"Period {period.period_code} reopened: {clean_reason}",
                actor_user=actor_user,
            )

        # Log reopen audit event
        FinancialPeriodReopenLog.objects.create(
            log_id=f"preopen-{uuid.uuid4().hex[:12]}",
            period=period,
            tenant=tenant,
            reopened_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            reopen_reason=clean_reason,
            previous_closing_journal=prev_journal,
        )

        period.status = FinancialPeriodStatus.REOPENED
        period.reopened_at = timezone.now()
        period.reopened_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        period.reopen_reason = clean_reason
        period.reopen_count += 1
        period.closing_journal = None
        period.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="FINANCIAL_PERIOD_REOPEN",
            resource_type="FinancialPeriod",
            resource_id=period.period_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"period_code": period.period_code, "reason": clean_reason, "reopen_count": period.reopen_count},
        )

        return cls._serialize_period(period)

    # =========================================================================
    # 5. QUERY & SERIALIZATION
    # =========================================================================

    @classmethod
    def list_periods(
        cls,
        *,
        tenant_id: str,
        status: Optional[str] = None,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = FinancialPeriod.objects.filter(tenant=tenant).select_related("branch", "closed_by", "reopened_by", "closing_journal")
        if status:
            qs = qs.filter(status=status.upper())
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        return [cls._serialize_period(p) for p in qs]

    @classmethod
    def get_period(
        cls,
        *,
        tenant_id: str,
        period_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        period = FinancialPeriod.objects.filter(period_id=period_id, tenant=tenant).select_related(
            "branch", "closed_by", "reopened_by", "closing_journal"
        ).first()
        if not period:
            foreign = FinancialPeriod.objects.filter(period_id=period_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant period access forbidden."})
            raise NotFound({"error_code": "ERR_PERIOD_NOT_FOUND", "detail": f"Financial period '{period_id}' not found."})

        return cls._serialize_period(period)

    @classmethod
    def _serialize_period(cls, period: FinancialPeriod) -> Dict[str, Any]:
        return {
            "period_id": period.period_id,
            "tenant_id": str(period.tenant_id),
            "branch_id": str(period.branch_id) if period.branch_id else None,
            "branch_name": period.branch.name if period.branch else "Consolidated Pharmacy",
            "period_code": period.period_code,
            "period_name": period.period_name,
            "period_type": period.period_type,
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "status": period.status,
            "closed_at": period.closed_at.isoformat() if period.closed_at else None,
            "closed_by_id": str(period.closed_by_id) if period.closed_by_id else None,
            "closing_notes": period.closing_notes,
            "closing_journal_id": str(period.closing_journal_id) if period.closing_journal_id else None,
            "total_revenues_bdt": str(period.total_revenues_bdt),
            "total_expenses_bdt": str(period.total_expenses_bdt),
            "net_income_bdt": str(period.net_income_bdt),
            "reopened_at": period.reopened_at.isoformat() if period.reopened_at else None,
            "reopened_by_id": str(period.reopened_by_id) if period.reopened_by_id else None,
            "reopen_reason": period.reopen_reason,
            "reopen_count": period.reopen_count,
            "created_at": period.created_at.isoformat(),
            "updated_at": period.updated_at.isoformat(),
        }
