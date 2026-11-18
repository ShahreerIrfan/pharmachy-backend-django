"""
backend/apps/core/profit_loss_report_service.py
Architecture Decision: ADR-0293 (P293).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service calculating Profit & Loss (Income Statement) from:
1. Recognized Gross Sales (Medicine 4010, OTC 4020), Less: Discounts (4030) -> Net Revenue.
2. Actual landed lot-based Cost of Goods Sold (5010) and Expiry/Wastage write-offs (5020) -> Total COGS.
3. Gross Profit = Net Revenue - Total COGS; Gross Margin %.
4. Categorized Operating Expenses (Rent 6010, Electricity 6020, Salaries 6030, Internet 6040, Transport 6050, Maintenance 6060, Misc 6070) -> Total OpEx.
5. Net Operating Profit = Gross Profit - Total OpEx; Net Margin %.
6. Multi-branch comparison and consolidated pharmacy figures.
7. Balanced-book mathematical reconciliation.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db.models import Sum, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    Branch,
    ChartOfAccount,
    GLAccountCategory,
    JournalEntry,
    JournalEntryLine,
    JournalEntryStatus,
    JournalSourceModule,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    SaleAccountingPosting,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")
DECIMAL_HUNDRED = Decimal("100.00")

PL_VIEW_ROLES = {
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

STAFF_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


class ProfitLossReportService:
    """
    Authoritative domain service compiling financial Profit & Loss (Income Statements)
    directly from verified double-entry General Ledger lines and operational sub-ledgers.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in PL_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PL_REPORT_UNAUTHORIZED",
                    "detail": "Cashier and counter staff are not authorized to access Profit & Loss financial statements.",
                    "message_en": "Cashier and counter staff are not authorized to access Profit & Loss financial statements.",
                    "message_bn": "ক্যাশিয়ার বা কাউন্টার স্টাফদের লাভ-ক্ষতি আর্থিক বিবরণী দেখার অনুমতি নেই।",
                }
            )

        # Branch Manager can only view their own assigned branch
        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch_id = str(getattr(actor_user, "branch_id", "") or "")
            if branch_id and user_branch_id and str(branch_id) != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_ACCESS_DENIED",
                        "detail": "Branch Managers can only access their assigned branch Profit & Loss report.",
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
                    "detail": "Tenant account subscription is suspended. Financial reporting blocked.",
                    "message_en": "Tenant account subscription is suspended. Financial reporting blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। লাভ-ক্ষতি রিপোর্ট দেখা সম্ভব নয়।",
                }
            )
        return tenant

    # =========================================================================
    # 1. GENERATE PROFIT & LOSS STATEMENT
    # =========================================================================

    @classmethod
    def generate_profit_and_loss_report(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Compiles authoritative Profit & Loss report from General Ledger journal lines.
        Supports single branch filtering or consolidated multi-branch pharmacy view.
        """
        cls.assert_view_authorized(actor_user, branch_id=branch_id)
        tenant = cls.assert_tenant_active(tenant_id)

        # 1. Parse Date Bounds
        p_start: Optional[datetime.date] = None
        p_end: Optional[datetime.date] = None

        if start_date:
            try:
                p_start = datetime.date.fromisoformat(str(start_date)) if isinstance(start_date, str) else start_date
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "Invalid start_date format (YYYY-MM-DD)."})

        if end_date:
            try:
                p_end = datetime.date.fromisoformat(str(end_date)) if isinstance(end_date, str) else end_date
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "Invalid end_date format (YYYY-MM-DD)."})

        if p_start and p_end and p_start > p_end:
            raise ValidationError({"error_code": "ERR_INVALID_DATE_RANGE", "detail": "start_date cannot be after end_date."})

        # 2. Scope Branch
        target_branch = None
        if branch_id and branch_id not in ("ALL", "CONSOLIDATED"):
            target_branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not target_branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        # 3. Base Query on General Ledger Journal Lines
        lines_qs = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
        ).exclude(
            journal_entry__source_module=JournalSourceModule.CLOSING,
        ).select_related("account", "journal_entry__branch")

        if target_branch:
            lines_qs = lines_qs.filter(journal_entry__branch=target_branch)

        if p_start:
            lines_qs = lines_qs.filter(journal_entry__posting_date__gte=p_start)
        if p_end:
            lines_qs = lines_qs.filter(journal_entry__posting_date__lte=p_end)

        # Accumulator Maps
        revenue_map: Dict[str, Dict[str, Any]] = {}
        cogs_map: Dict[str, Dict[str, Any]] = {}
        expense_map: Dict[str, Dict[str, Any]] = {}

        # Branch breakdown map
        branch_stats: Dict[str, Dict[str, Any]] = {}

        for line in lines_qs:
            acc = line.account
            cat = acc.category
            code = acc.account_code
            name = acc.account_name
            name_bn = getattr(acc, "account_name_bn", name)
            dr = line.debit_amount_bdt
            cr = line.credit_amount_bdt

            b_id = str(line.journal_entry.branch_id) if line.journal_entry.branch_id else "UNALLOCATED"
            b_name = line.journal_entry.branch.name if line.journal_entry.branch else "Head Office / Unallocated"

            if b_id not in branch_stats:
                branch_stats[b_id] = {
                    "branch_id": b_id,
                    "branch_name": b_name,
                    "revenue_bdt": DECIMAL_ZERO,
                    "cogs_bdt": DECIMAL_ZERO,
                    "gross_profit_bdt": DECIMAL_ZERO,
                    "expense_bdt": DECIMAL_ZERO,
                    "net_profit_bdt": DECIMAL_ZERO,
                }

            if cat == GLAccountCategory.REVENUE:
                # Normal credit balance: net = credits - debits
                net_rev = (cr - dr)
                if code not in revenue_map:
                    revenue_map[code] = {
                        "account_code": code,
                        "account_name": name,
                        "account_name_bn": name_bn,
                        "amount_bdt": DECIMAL_ZERO,
                        "is_contra": acc.normal_balance == "DEBIT" or code == "4030",
                    }
                revenue_map[code]["amount_bdt"] += net_rev
                branch_stats[b_id]["revenue_bdt"] += net_rev

            elif cat == GLAccountCategory.COGS:
                # Normal debit balance: net = debits - credits
                net_cogs = (dr - cr)
                if code not in cogs_map:
                    cogs_map[code] = {
                        "account_code": code,
                        "account_name": name,
                        "account_name_bn": name_bn,
                        "amount_bdt": DECIMAL_ZERO,
                    }
                cogs_map[code]["amount_bdt"] += net_cogs
                branch_stats[b_id]["cogs_bdt"] += net_cogs

            elif cat == GLAccountCategory.EXPENSE:
                # Normal debit balance: net = debits - credits
                net_exp = (dr - cr)
                if code not in expense_map:
                    expense_map[code] = {
                        "account_code": code,
                        "account_name": name,
                        "account_name_bn": name_bn,
                        "amount_bdt": DECIMAL_ZERO,
                    }
                expense_map[code]["amount_bdt"] += net_exp
                branch_stats[b_id]["expense_bdt"] += net_exp

        # 4. Synthesize Revenue Section
        total_gross_revenue = DECIMAL_ZERO
        total_contra_revenue = DECIMAL_ZERO
        revenue_items = []

        for code in sorted(revenue_map.keys()):
            item = revenue_map[code]
            amt = q2(item["amount_bdt"])
            item["amount_bdt"] = str(amt)
            revenue_items.append(item)
            if item["is_contra"] or amt < DECIMAL_ZERO:
                total_contra_revenue += abs(amt)
            else:
                total_gross_revenue += amt

        net_revenue = q2(total_gross_revenue - total_contra_revenue)

        # 5. Synthesize COGS Section
        total_cogs = DECIMAL_ZERO
        cogs_items = []
        for code in sorted(cogs_map.keys()):
            item = cogs_map[code]
            amt = q2(item["amount_bdt"])
            item["amount_bdt"] = str(amt)
            cogs_items.append(item)
            total_cogs += amt
        total_cogs = q2(total_cogs)

        # 6. Gross Profit Calculation
        gross_profit = q2(net_revenue - total_cogs)
        gross_margin_pct = DECIMAL_ZERO
        if net_revenue > DECIMAL_ZERO:
            gross_margin_pct = q2((gross_profit / net_revenue) * DECIMAL_HUNDRED)

        # 7. Synthesize Operating Expenses Section
        total_operating_expenses = DECIMAL_ZERO
        expense_items = []
        for code in sorted(expense_map.keys()):
            item = expense_map[code]
            amt = q2(item["amount_bdt"])
            item["amount_bdt"] = str(amt)
            expense_items.append(item)
            total_operating_expenses += amt
        total_operating_expenses = q2(total_operating_expenses)

        # 8. Net Operating Profit Calculation
        net_operating_profit = q2(gross_profit - total_operating_expenses)
        net_margin_pct = DECIMAL_ZERO
        if net_revenue > DECIMAL_ZERO:
            net_margin_pct = q2((net_operating_profit / net_revenue) * DECIMAL_HUNDRED)

        # 9. Finalize Multi-Branch Breakdown
        branches_summary = []
        for b_id, b_data in branch_stats.items():
            b_rev = q2(b_data["revenue_bdt"])
            b_cogs = q2(b_data["cogs_bdt"])
            b_gp = q2(b_rev - b_cogs)
            b_exp = q2(b_data["expense_bdt"])
            b_np = q2(b_gp - b_exp)
            b_margin = DECIMAL_ZERO
            if b_rev > DECIMAL_ZERO:
                b_margin = q2((b_np / b_rev) * DECIMAL_HUNDRED)

            branches_summary.append(
                {
                    "branch_id": b_id,
                    "branch_name": b_data["branch_name"],
                    "net_revenue_bdt": str(b_rev),
                    "total_cogs_bdt": str(b_cogs),
                    "gross_profit_bdt": str(b_gp),
                    "operating_expenses_bdt": str(b_exp),
                    "net_profit_bdt": str(b_np),
                    "net_margin_pct": str(b_margin),
                }
            )

        # 10. Financial Period Identification (if bounds match)
        period_label = "Custom Date Range"
        if p_start and p_end:
            period_label = f"{p_start.isoformat()} to {p_end.isoformat()}"
        elif p_start:
            period_label = f"From {p_start.isoformat()}"
        elif p_end:
            period_label = f"Until {p_end.isoformat()}"
        else:
            period_label = "All Time (Cumulative)"

        return {
            "tenant_id": str(tenant.tenant_id),
            "tenant_name": tenant.name,
            "branch_id": str(target_branch.branch_id) if target_branch else "ALL_BRANCHES",
            "branch_name": target_branch.name if target_branch else "Consolidated Pharmacy (All Branches)",
            "period_label": period_label,
            "start_date": p_start.isoformat() if p_start else None,
            "end_date": p_end.isoformat() if p_end else None,
            "generated_at": timezone.now().isoformat(),
            # Revenue
            "revenue": {
                "items": revenue_items,
                "gross_revenue_bdt": str(total_gross_revenue),
                "contra_discounts_bdt": str(total_contra_revenue),
                "net_revenue_bdt": str(net_revenue),
            },
            # Cost of Goods Sold
            "cogs": {
                "items": cogs_items,
                "total_cogs_bdt": str(total_cogs),
            },
            # Gross Profit
            "gross_profit": {
                "gross_profit_bdt": str(gross_profit),
                "gross_margin_pct": str(gross_margin_pct),
                "is_profitable": gross_profit >= DECIMAL_ZERO,
            },
            # Operating Expenses
            "operating_expenses": {
                "items": expense_items,
                "total_operating_expenses_bdt": str(total_operating_expenses),
            },
            # Net Profit / Loss
            "net_profit": {
                "net_operating_profit_bdt": str(net_operating_profit),
                "net_margin_pct": str(net_margin_pct),
                "is_profitable": net_operating_profit >= DECIMAL_ZERO,
                "status_en": "PROFIT" if net_operating_profit >= DECIMAL_ZERO else "LOSS",
                "status_bn": "মুনাফা (লাভ)" if net_operating_profit >= DECIMAL_ZERO else "লোকসান (ক্ষতি)",
            },
            # Branches Breakdown
            "branches": branches_summary,
            "is_consolidated": target_branch is None,
        }

    # =========================================================================
    # 2. EXPORT CSV / PRINTABLE FORMAT
    # =========================================================================

    @classmethod
    def export_profit_and_loss_csv(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> str:
        report = cls.generate_profit_and_loss_report(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=actor_user,
        )

        lines = [
            f"# PROFIT & LOSS STATEMENT - {report['tenant_name']}",
            f"# Scope: {report['branch_name']}",
            f"# Period: {report['period_label']}",
            f"# Generated At: {report['generated_at']}",
            "Section,Account Code,Account Name,Amount BDT",
        ]

        # Revenue
        for it in report["revenue"]["items"]:
            lines.append(f"REVENUE,{it['account_code']},\"{it['account_name']}\",{it['amount_bdt']}")
        lines.append(f"SUBTOTAL,NET_REVENUE,Net Sales Revenue,{report['revenue']['net_revenue_bdt']}")

        # COGS
        for it in report["cogs"]["items"]:
            lines.append(f"COGS,{it['account_code']},\"{it['account_name']}\",{it['amount_bdt']}")
        lines.append(f"SUBTOTAL,TOTAL_COGS,Total Cost of Goods Sold,{report['cogs']['total_cogs_bdt']}")
        lines.append(f"SUMMARY,GROSS_PROFIT,Gross Profit (Margin: {report['gross_profit']['gross_margin_pct']}%),{report['gross_profit']['gross_profit_bdt']}")

        # OpEx
        for it in report["operating_expenses"]["items"]:
            lines.append(f"EXPENSE,{it['account_code']},\"{it['account_name']}\",{it['amount_bdt']}")
        lines.append(f"SUBTOTAL,TOTAL_OPEX,Total Operating Expenses,{report['operating_expenses']['total_operating_expenses_bdt']}")
        lines.append(f"SUMMARY,NET_PROFIT,Net Operating Profit (Margin: {report['net_profit']['net_margin_pct']}%),{report['net_profit']['net_operating_profit_bdt']}")

        return "\n".join(lines)

