"""
backend/apps/core/cash_flow_overview_service.py
Architecture Decision: ADR-0294 (P294).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service calculating Cash Flow Overview:
1. Real receipts and collections (POS counter sales, customer AR collections).
2. Real operating disbursements (cash expenses, supplier AP payments, staff payroll).
3. Treasury cash-bank and inter-account transfers (isolated from revenue/expenses, zero net liquidity impact).
4. Opening cash/bank balance + Net cash flow = Closing cash/bank balance.
5. Contrast against accrual P&L (uncollected receivables, non-cash lot expiry write-offs, credit supplier terms).
6. Multi-branch breakdown and consolidated pharmacy cash flow.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import logging
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
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")
DECIMAL_HUNDRED = Decimal("100.00")

CASH_FLOW_VIEW_ROLES = {
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

CASH_FLOW_DENIED_ROLES = {
    UserRole.CASHIER,
    UserRole.STAFF_PHARMACIST,
    "CASHIER",
    "STAFF_PHARMACIST",
    "INVENTORY_CLERK",
    "DISPENSER",
}


class CashFlowOverviewService:
    """
    Authoritative domain service compiling financial Cash Flow Statement directly from
    posted double-entry General Ledger lines affecting liquid Cash & Bank accounts (1010, 1020).
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in CASH_FLOW_DENIED_ROLES or role not in CASH_FLOW_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CASH_FLOW_UNAUTHORIZED",
                    "detail": "Cashier and counter staff are not authorized to access Cash Flow financial statements.",
                    "message_en": "Cashier and counter staff are not authorized to access Cash Flow financial statements.",
                    "message_bn": "ক্যাশিয়ার বা কাউন্টার স্টাফদের ক্যাশ ফ্লো বিবরণী দেখার অনুমতি নেই।",
                }
            )

        # Branch Manager can only view their own assigned branch
        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch_id = str(getattr(actor_user, "branch_id", "") or "")
            if branch_id and user_branch_id and str(branch_id) != user_branch_id:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_ACCESS_DENIED",
                        "detail": "Branch Managers can only access their assigned branch Cash Flow overview.",
                        "message_en": "Branch Managers can only access their assigned branch Cash Flow overview.",
                        "message_bn": "শাখা ব্যবস্থাপক শুধুমাত্র নিজস্ব শাখার ক্যাশ ফ্লো দেখতে পারবেন।",
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
                    "detail": "Tenant account subscription is suspended. Cash flow reporting blocked.",
                    "message_en": "Tenant account subscription is suspended. Cash flow reporting blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। ক্যাশ ফ্লো রিপোর্ট দেখা সম্ভব নয়।",
                }
            )
        return tenant

    # =========================================================================
    # 1. GENERATE CASH FLOW OVERVIEW
    # =========================================================================

    @classmethod
    def generate_cash_flow_overview(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Compiles authoritative Cash Flow Statement from posted GL lines on Cash & Bank accounts.
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
        if branch_id and branch_id not in ("ALL", "CONSOLIDATED", "ALL_BRANCHES"):
            target_branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not target_branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        # 3. Identify Liquid Cash & Bank Accounts (Asset 1010, 1020, or 101x/102x)
        cash_accounts = ChartOfAccount.objects.filter(
            tenant=tenant,
            category=GLAccountCategory.ASSET,
        ).filter(
            Q(account_code__in=["1010", "1020"])
            | Q(account_code__startswith="101")
            | Q(account_code__startswith="102")
        )
        cash_account_ids = list(cash_accounts.values_list("account_id", flat=True))

        # 4. Compute Opening Cash & Bank Balance (prior to start_date)
        opening_balance = DECIMAL_ZERO
        if p_start:
            opening_qs = JournalEntryLine.objects.filter(
                journal_entry__tenant=tenant,
                journal_entry__status=JournalEntryStatus.POSTED,
                journal_entry__posting_date__lt=p_start,
                account_id__in=cash_account_ids,
            ).exclude(
                journal_entry__source_module=JournalSourceModule.CLOSING,
            )
            if target_branch:
                opening_qs = opening_qs.filter(journal_entry__branch=target_branch)

            dr_sum = opening_qs.aggregate(s=Sum("debit_amount_bdt"))["s"] or DECIMAL_ZERO
            cr_sum = opening_qs.aggregate(s=Sum("credit_amount_bdt"))["s"] or DECIMAL_ZERO
            opening_balance = q2(dr_sum - cr_sum)

        # 5. Query Active Period Cash Lines
        period_qs = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
            account_id__in=cash_account_ids,
        ).exclude(
            journal_entry__source_module=JournalSourceModule.CLOSING,
        ).select_related("account", "journal_entry__branch")

        if target_branch:
            period_qs = period_qs.filter(journal_entry__branch=target_branch)

        if p_start:
            period_qs = period_qs.filter(journal_entry__posting_date__gte=p_start)
        if p_end:
            period_qs = period_qs.filter(journal_entry__posting_date__lte=p_end)

        # Categorized Cash Flow Accumulators
        inflows_pos_sales = DECIMAL_ZERO
        inflows_customer_ar = DECIMAL_ZERO
        inflows_other = DECIMAL_ZERO

        outflows_operating_expense = DECIMAL_ZERO
        outflows_supplier_ap = DECIMAL_ZERO
        outflows_payroll = DECIMAL_ZERO
        outflows_other = DECIMAL_ZERO

        transfers_in = DECIMAL_ZERO
        transfers_out = DECIMAL_ZERO

        # Branch breakdown tracker
        branch_stats: Dict[str, Dict[str, Any]] = {}

        for line in period_qs:
            dr = line.debit_amount_bdt
            cr = line.credit_amount_bdt
            src = line.journal_entry.source_module

            b_id = str(line.journal_entry.branch_id) if line.journal_entry.branch_id else "UNALLOCATED"
            b_name = line.journal_entry.branch.name if line.journal_entry.branch else "Head Office / Unallocated"

            if b_id not in branch_stats:
                branch_stats[b_id] = {
                    "branch_id": b_id,
                    "branch_name": b_name,
                    "opening_balance_bdt": DECIMAL_ZERO,
                    "operating_inflows_bdt": DECIMAL_ZERO,
                    "operating_outflows_bdt": DECIMAL_ZERO,
                    "net_operating_cash_flow_bdt": DECIMAL_ZERO,
                    "transfers_in_bdt": DECIMAL_ZERO,
                    "transfers_out_bdt": DECIMAL_ZERO,
                    "net_cash_flow_bdt": DECIMAL_ZERO,
                    "closing_balance_bdt": DECIMAL_ZERO,
                }

            # Check if this line is an Internal Treasury Transfer
            if src == JournalSourceModule.TREASURY_TRANSFER:
                if dr > DECIMAL_ZERO:
                    transfers_in += dr
                    branch_stats[b_id]["transfers_in_bdt"] += dr
                if cr > DECIMAL_ZERO:
                    transfers_out += cr
                    branch_stats[b_id]["transfers_out_bdt"] += cr
                continue

            # Cash Inflow (Debit on Cash/Bank)
            if dr > DECIMAL_ZERO:
                if src == JournalSourceModule.POS_SALE:
                    inflows_pos_sales += dr
                elif src == JournalSourceModule.CUSTOMER_AR:
                    inflows_customer_ar += dr
                else:
                    inflows_other += dr
                branch_stats[b_id]["operating_inflows_bdt"] += dr

            # Cash Outflow (Credit on Cash/Bank)
            if cr > DECIMAL_ZERO:
                if src == JournalSourceModule.OPERATING_EXPENSE:
                    outflows_operating_expense += cr
                elif src in (JournalSourceModule.SUPPLIER_AP, JournalSourceModule.PROCUREMENT):
                    outflows_supplier_ap += cr
                elif src == JournalSourceModule.PAYROLL:
                    outflows_payroll += cr
                else:
                    outflows_other += cr
                branch_stats[b_id]["operating_outflows_bdt"] += cr

        # 6. Synthesize Inflows & Outflows
        total_operating_inflows = q2(inflows_pos_sales + inflows_customer_ar + inflows_other)
        total_operating_outflows = q2(outflows_operating_expense + outflows_supplier_ap + outflows_payroll + outflows_other)
        net_operating_cash_flow = q2(total_operating_inflows - total_operating_outflows)

        # Internal Treasury Transfers
        transfers_in = q2(transfers_in)
        transfers_out = q2(transfers_out)
        net_transfers = q2(transfers_in - transfers_out)

        # Net Liquidity Change
        net_cash_flow = q2(net_operating_cash_flow + net_transfers)
        closing_balance = q2(opening_balance + net_cash_flow)

        # 7. Finalize Branch Breakdown
        branches_summary = []
        for b_id, b_data in branch_stats.items():
            b_in = q2(b_data["operating_inflows_bdt"])
            b_out = q2(b_data["operating_outflows_bdt"])
            b_op_net = q2(b_in - b_out)
            b_tin = q2(b_data["transfers_in_bdt"])
            b_tout = q2(b_data["transfers_out_bdt"])
            b_net = q2(b_op_net + (b_tin - b_tout))

            # Branch opening balance if filtered or computed
            b_open = DECIMAL_ZERO
            if p_start:
                b_open_qs = JournalEntryLine.objects.filter(
                    journal_entry__tenant=tenant,
                    journal_entry__status=JournalEntryStatus.POSTED,
                    journal_entry__posting_date__lt=p_start,
                    account_id__in=cash_account_ids,
                ).exclude(
                    journal_entry__source_module=JournalSourceModule.CLOSING,
                )
                if b_id != "UNALLOCATED":
                    b_open_qs = b_open_qs.filter(journal_entry__branch_id=b_id)
                dr_b = b_open_qs.aggregate(s=Sum("debit_amount_bdt"))["s"] or DECIMAL_ZERO
                cr_b = b_open_qs.aggregate(s=Sum("credit_amount_bdt"))["s"] or DECIMAL_ZERO
                b_open = q2(dr_b - cr_b)

            b_close = q2(b_open + b_net)

            branches_summary.append(
                {
                    "branch_id": b_id,
                    "branch_name": b_data["branch_name"],
                    "opening_balance_bdt": str(b_open),
                    "operating_inflows_bdt": str(b_in),
                    "operating_outflows_bdt": str(b_out),
                    "net_operating_cash_flow_bdt": str(b_op_net),
                    "transfers_in_bdt": str(b_tin),
                    "transfers_out_bdt": str(b_tout),
                    "net_cash_flow_bdt": str(b_net),
                    "closing_balance_bdt": str(b_close),
                }
            )

        # 8. Accrual vs Cash Flow Reconciliation Metrics
        # Accrual Revenue = Net Sales recognized during period
        accrual_rev_qs = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
            account__category=GLAccountCategory.REVENUE,
        ).exclude(journal_entry__source_module=JournalSourceModule.CLOSING)
        if target_branch:
            accrual_rev_qs = accrual_rev_qs.filter(journal_entry__branch=target_branch)
        if p_start:
            accrual_rev_qs = accrual_rev_qs.filter(journal_entry__posting_date__gte=p_start)
        if p_end:
            accrual_rev_qs = accrual_rev_qs.filter(journal_entry__posting_date__lte=p_end)

        accrual_rev_cr = accrual_rev_qs.aggregate(s=Sum("credit_amount_bdt"))["s"] or DECIMAL_ZERO
        accrual_rev_dr = accrual_rev_qs.aggregate(s=Sum("debit_amount_bdt"))["s"] or DECIMAL_ZERO
        accrual_net_revenue = q2(accrual_rev_cr - accrual_rev_dr)

        # Accrual Expenses + COGS
        accrual_exp_qs = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
            account__category__in=[GLAccountCategory.EXPENSE, GLAccountCategory.COGS],
        ).exclude(journal_entry__source_module=JournalSourceModule.CLOSING)
        if target_branch:
            accrual_exp_qs = accrual_exp_qs.filter(journal_entry__branch=target_branch)
        if p_start:
            accrual_exp_qs = accrual_exp_qs.filter(journal_entry__posting_date__gte=p_start)
        if p_end:
            accrual_exp_qs = accrual_exp_qs.filter(journal_entry__posting_date__lte=p_end)

        accrual_exp_dr = accrual_exp_qs.aggregate(s=Sum("debit_amount_bdt"))["s"] or DECIMAL_ZERO
        accrual_exp_cr = accrual_exp_qs.aggregate(s=Sum("credit_amount_bdt"))["s"] or DECIMAL_ZERO
        accrual_total_expenses = q2(accrual_exp_dr - accrual_exp_cr)
        accrual_net_income = q2(accrual_net_revenue - accrual_total_expenses)

        revenue_cash_variance = q2(accrual_net_revenue - total_operating_inflows)
        expense_cash_variance = q2(accrual_total_expenses - total_operating_outflows)
        net_income_vs_cash_variance = q2(accrual_net_income - net_operating_cash_flow)

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
            # Opening & Closing Balances
            "opening_balance_bdt": str(opening_balance),
            "closing_balance_bdt": str(closing_balance),
            # Operating Inflows
            "operating_inflows": {
                "pos_sales_collections_bdt": str(q2(inflows_pos_sales)),
                "customer_ar_collections_bdt": str(q2(inflows_customer_ar)),
                "other_receipts_bdt": str(q2(inflows_other)),
                "total_operating_inflows_bdt": str(total_operating_inflows),
            },
            # Operating Outflows
            "operating_outflows": {
                "operating_expense_payments_bdt": str(q2(outflows_operating_expense)),
                "supplier_ap_payments_bdt": str(q2(outflows_supplier_ap)),
                "payroll_disbursements_bdt": str(q2(outflows_payroll)),
                "other_payments_bdt": str(q2(outflows_other)),
                "total_operating_outflows_bdt": str(total_operating_outflows),
            },
            # Net Operating Cash Flow
            "net_operating_cash_flow": {
                "net_operating_cash_flow_bdt": str(net_operating_cash_flow),
                "is_positive": net_operating_cash_flow >= DECIMAL_ZERO,
            },
            # Treasury Transfers
            "treasury_transfers": {
                "transfers_in_bdt": str(transfers_in),
                "transfers_out_bdt": str(transfers_out),
                "net_transfers_bdt": str(net_transfers),
                "transfer_commentary": (
                    "Internal cash-bank transfers do not create revenue or expenses; "
                    "net liquidity impact across consolidated accounts is BDT 0.00."
                ),
            },
            # Overall Net Cash Flow
            "net_cash_flow": {
                "net_cash_flow_bdt": str(net_cash_flow),
                "is_positive": net_cash_flow >= DECIMAL_ZERO,
            },
            # Accrual P&L vs Cash Flow Contrast
            "accrual_reconciliation": {
                "accrual_net_revenue_bdt": str(accrual_net_revenue),
                "cash_operating_inflows_bdt": str(total_operating_inflows),
                "revenue_timing_variance_bdt": str(revenue_cash_variance),
                "accrual_cogs_and_expenses_bdt": str(accrual_total_expenses),
                "cash_operating_outflows_bdt": str(total_operating_outflows),
                "expense_timing_variance_bdt": str(expense_cash_variance),
                "accrual_net_income_bdt": str(accrual_net_income),
                "cash_net_operating_flow_bdt": str(net_operating_cash_flow),
                "net_income_to_cash_variance_bdt": str(net_income_vs_cash_variance),
                "explanation_en": "Variance is driven by credit dues (AR), supplier payables (AP), and non-cash lot expiry write-offs.",
                "explanation_bn": "পার্থক্যটি বাকি বিক্রয় (AR), বাকিতে পণ্য ক্রয় (AP) এবং অনগদ মেয়াদোত্তীর্ণের লোকসানের কারণে সৃষ্ট।",
            },
            # Branch Breakdown
            "branches": branches_summary,
            "branch_breakdown": branches_summary,
            "is_consolidated": target_branch is None,
        }

    # =========================================================================
    # 2. EXPORT CSV
    # =========================================================================

    @classmethod
    def export_cash_flow_csv(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> str:
        """
        Exports the Cash Flow Statement into clean, tabular CSV format.
        """
        rep = cls.generate_cash_flow_overview(
            tenant_id=tenant_id,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=actor_user,
        )

        lines = [
            f"# CASH FLOW STATEMENT - {rep['tenant_name']}",
            f"# Scope: {rep['branch_name']}",
            f"# Period: {rep['period_label']}",
            f"# Generated At: {rep['generated_at']}",
            "Section,Activity / Item,Amount BDT",
            f"BALANCE,Opening Liquid Cash & Bank Balance,{rep['opening_balance_bdt']}",
            # Inflows
            f"OPERATING_INFLOW,POS Counter Sales Collections,{rep['operating_inflows']['pos_sales_collections_bdt']}",
            f"OPERATING_INFLOW,Customer Due (AR) Collections,{rep['operating_inflows']['customer_ar_collections_bdt']}",
            f"OPERATING_INFLOW,Other Operating Receipts,{rep['operating_inflows']['other_receipts_bdt']}",
            f"SUBTOTAL,Total Operating Cash Inflows,{rep['operating_inflows']['total_operating_inflows_bdt']}",
            # Outflows
            f"OPERATING_OUTFLOW,Operating Expense Cash Payments,{rep['operating_outflows']['operating_expense_payments_bdt']}",
            f"OPERATING_OUTFLOW,Supplier AP Payments,{rep['operating_outflows']['supplier_ap_payments_bdt']}",
            f"OPERATING_OUTFLOW,Staff Salaries & Payroll Disbursed,{rep['operating_outflows']['payroll_disbursements_bdt']}",
            f"OPERATING_OUTFLOW,Other Cash Payments,{rep['operating_outflows']['other_payments_bdt']}",
            f"SUBTOTAL,Total Operating Cash Outflows,{rep['operating_outflows']['total_operating_outflows_bdt']}",
            # Net Operating
            f"SUMMARY,Net Operating Cash Flow,{rep['net_operating_cash_flow']['net_operating_cash_flow_bdt']}",
            # Treasury Transfers
            f"TREASURY_TRANSFER,Internal Transfers In,{rep['treasury_transfers']['transfers_in_bdt']}",
            f"TREASURY_TRANSFER,Internal Transfers Out,{rep['treasury_transfers']['transfers_out_bdt']}",
            f"TREASURY_TRANSFER,Net Internal Transfers Impact,{rep['treasury_transfers']['net_transfers_bdt']}",
            # Net & Closing
            f"SUMMARY,Net Overall Cash Flow,{rep['net_cash_flow']['net_cash_flow_bdt']}",
            f"BALANCE,Closing Liquid Cash & Bank Balance,{rep['closing_balance_bdt']}",
        ]

        if rep["branches"]:
            lines.append("")
            lines.append("# MULTI-BRANCH CASH FLOW BREAKDOWN")
            lines.append("Branch ID,Branch Name,Opening BDT,Inflows BDT,Outflows BDT,Net Operating BDT,Transfers Net BDT,Closing BDT")
            for b in rep["branches"]:
                lines.append(
                    f"{b['branch_id']},\"{b['branch_name']}\",{b['opening_balance_bdt']},{b['operating_inflows_bdt']},"
                    f"{b['operating_outflows_bdt']},{b['net_operating_cash_flow_bdt']},{b['transfers_in_bdt']},{b['closing_balance_bdt']}"
                )

        return "\n".join(lines)
