"""
backend/apps/core/accounting_reports_export_service.py
Architecture Decision: ADR-0298 (P298).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Accounting Reports & Permission-Scoped Historical Snapshots:
- Compiles 5 verified report packs from live GL & operational engines:
  1. TRIAL_BALANCE: Account-level debits, credits, net balances & balanced-book verification.
  2. JOURNAL_LEDGER: Immutable double-entry journal register with line summaries.
  3. PROFIT_AND_LOSS: Delegates directly to ProfitLossReportService (P293).
  4. OPERATING_EXPENSES: Approved operating expense register scoped by tenant/branch.
  5. RECONCILIATION_PACK: Delegates to InventoryCostValuationService (P295) + Trial Balance parity.
- Freezes tamper-evident SHA-256 digested historical snapshots (AccountingReportSnapshot)
  so past exports never drift even after subsequent transactions are posted.
- Strict permission boundaries (Owner/Finance/Auditor consolidated; Branch Manager strictly branch-only;
  Cashier/Pharmacist denied).
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import q2
from apps.core.inventory_cost_valuation_service import InventoryCostValuationService
from apps.core.models import (
    AccountingReportSnapshot,
    AccountingReportType,
    AccountingSnapshotStatus,
    Branch,
    ChartOfAccount,
    JournalEntry,
    JournalEntryLine,
    JournalEntryStatus,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.profit_loss_report_service import ProfitLossReportService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

REPORT_VIEW_ROLES = {
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

REPORT_ARCHIVE_ROLES = {
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


class AccountingReportsExportService:
    """
    Compiles, snapshots (with SHA-256 digest), and exports accounting reports.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in REPORT_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Counter staff are not authorized to generate or export accounting reports.",
                    "message_en": "Counter staff are not authorized to generate or export accounting reports.",
                    "message_bn": "কাউন্টার স্টাফ অ্যাকাউন্টিং রিপোর্ট বা এক্সপোর্ট অ্যাক্সেস করার অনুমতিপ্রাপ্ত নন।",
                }
            )

        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if not branch_id or str(branch_id) != user_branch:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_SCOPE_DENIED",
                        "detail": "Branch Manager can only access reports and snapshots for their assigned branch.",
                        "message_en": "Branch Manager is restricted to their assigned branch for accounting exports.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার শুধুমাত্র নিজস্ব শাখার অ্যাকাউন্টিং রিপোর্ট ও স্ন্যাপশট এক্সপোর্ট করতে পারবেন।",
                    }
                )

    @classmethod
    def assert_archive_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return
        role = getattr(actor_user, "role", None)
        if role not in REPORT_ARCHIVE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_SNAPSHOT_ARCHIVE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can archive historical snapshots.",
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
                    "detail": "Tenant subscription is suspended. Accounting exports blocked.",
                    "message_en": "Tenant subscription is suspended. Accounting exports blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। অ্যাকাউন্টিং এক্সপোর্ট বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    def _parse_dates(
        cls, start_date: Optional[Any], end_date: Optional[Any]
    ) -> Tuple[Optional[datetime.date], Optional[datetime.date]]:
        p_start = None
        p_end = None
        if start_date:
            try:
                p_start = datetime.date.fromisoformat(str(start_date)) if isinstance(start_date, str) else start_date
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "Invalid start_date (YYYY-MM-DD)."})
        if end_date:
            try:
                p_end = datetime.date.fromisoformat(str(end_date)) if isinstance(end_date, str) else end_date
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "Invalid end_date (YYYY-MM-DD)."})
        if p_start and p_end and p_start > p_end:
            raise ValidationError({"error_code": "ERR_INVALID_DATE_RANGE", "detail": "start_date cannot exceed end_date."})
        return p_start, p_end

    # =========================================================================
    # REPORT COMPILERS
    # =========================================================================

    @classmethod
    def _compile_trial_balance(
        cls,
        tenant: Tenant,
        branch: Optional[Branch],
        p_start: Optional[datetime.date],
        p_end: Optional[datetime.date],
    ) -> Tuple[Dict[str, Any], Decimal, Decimal, bool, int]:
        line_qs = JournalEntryLine.objects.filter(
            journal_entry__tenant=tenant,
            journal_entry__status=JournalEntryStatus.POSTED,
        ).select_related("account", "journal_entry")

        if branch:
            line_qs = line_qs.filter(journal_entry__branch=branch)
        if p_start:
            line_qs = line_qs.filter(journal_entry__posting_date__gte=p_start)
        if p_end:
            line_qs = line_qs.filter(journal_entry__posting_date__lte=p_end)

        grouped: Dict[str, Dict[str, Any]] = {}
        for ln in line_qs:
            code = ln.account.account_code
            if code not in grouped:
                grouped[code] = {
                    "account_code": code,
                    "account_name": ln.account.account_name,
                    "category": ln.account.category,
                    "normal_balance": ln.account.normal_balance,
                    "debit_bdt": DECIMAL_ZERO,
                    "credit_bdt": DECIMAL_ZERO,
                }
            grouped[code]["debit_bdt"] = q2(grouped[code]["debit_bdt"] + ln.debit_amount_bdt)
            grouped[code]["credit_bdt"] = q2(grouped[code]["credit_bdt"] + ln.credit_amount_bdt)

        rows: List[Dict[str, Any]] = []
        tot_dr = DECIMAL_ZERO
        tot_cr = DECIMAL_ZERO
        for code in sorted(grouped.keys()):
            item = grouped[code]
            dr = item["debit_bdt"]
            cr = item["credit_bdt"]
            tot_dr = q2(tot_dr + dr)
            tot_cr = q2(tot_cr + cr)
            net = q2(dr - cr) if item["normal_balance"] == "DEBIT" else q2(cr - dr)
            rows.append(
                {
                    "account_code": code,
                    "account_name": item["account_name"],
                    "category": item["category"],
                    "debit_bdt": str(dr),
                    "credit_bdt": str(cr),
                    "net_balance_bdt": str(net),
                }
            )

        balanced = tot_dr == tot_cr
        payload = {
            "report_type": AccountingReportType.TRIAL_BALANCE,
            "rows": rows,
            "total_debits_bdt": str(tot_dr),
            "total_credits_bdt": str(tot_cr),
            "is_book_balanced": balanced,
        }
        return payload, tot_dr, tot_cr, balanced, len(rows)

    @classmethod
    def _compile_journal_ledger(
        cls,
        tenant: Tenant,
        branch: Optional[Branch],
        p_start: Optional[datetime.date],
        p_end: Optional[datetime.date],
    ) -> Tuple[Dict[str, Any], Decimal, Decimal, bool, int]:
        qs = JournalEntry.objects.filter(tenant=tenant, status=JournalEntryStatus.POSTED)
        if branch:
            qs = qs.filter(branch=branch)
        if p_start:
            qs = qs.filter(posting_date__gte=p_start)
        if p_end:
            qs = qs.filter(posting_date__lte=p_end)

        rows: List[Dict[str, Any]] = []
        tot_dr = DECIMAL_ZERO
        tot_cr = DECIMAL_ZERO
        for je in qs.order_by("posting_date", "created_at")[:500]:
            dr = q2(je.total_debit_bdt)
            cr = q2(je.total_credit_bdt)
            tot_dr = q2(tot_dr + dr)
            tot_cr = q2(tot_cr + cr)
            rows.append(
                {
                    "entry_id": je.entry_id,
                    "journal_number": je.journal_number,
                    "posting_date": je.posting_date.isoformat(),
                    "source_module": je.source_module,
                    "source_reference": je.source_reference,
                    "narration": je.narration,
                    "debit_bdt": str(dr),
                    "credit_bdt": str(cr),
                    "is_balanced": je.is_balanced,
                }
            )

        balanced = tot_dr == tot_cr
        payload = {
            "report_type": AccountingReportType.JOURNAL_LEDGER,
            "rows": rows,
            "total_debits_bdt": str(tot_dr),
            "total_credits_bdt": str(tot_cr),
            "is_book_balanced": balanced,
        }
        return payload, tot_dr, tot_cr, balanced, len(rows)

    @classmethod
    def _compile_operating_expenses(
        cls,
        tenant: Tenant,
        branch: Optional[Branch],
        p_start: Optional[datetime.date],
        p_end: Optional[datetime.date],
    ) -> Tuple[Dict[str, Any], Decimal, Decimal, bool, int]:
        qs = OperatingExpenseEntry.objects.filter(
            tenant=tenant, status=OperatingExpenseStatus.APPROVED
        ).select_related("branch", "category")
        if branch:
            qs = qs.filter(branch=branch)
        if p_start:
            qs = qs.filter(expense_date__gte=p_start)
        if p_end:
            qs = qs.filter(expense_date__lte=p_end)

        rows: List[Dict[str, Any]] = []
        total_exp = DECIMAL_ZERO
        for ex in qs.order_by("expense_date", "created_at")[:500]:
            amt = q2(ex.amount_bdt)
            total_exp = q2(total_exp + amt)
            rows.append(
                {
                    "expense_id": ex.expense_id,
                    "voucher_number": ex.voucher_number,
                    "expense_date": ex.expense_date.isoformat(),
                    "branch_name": ex.branch.name if ex.branch else "",
                    "category_code": ex.category.code if ex.category else "",
                    "payee_name": ex.payee_name,
                    "payment_method": ex.payment_method,
                    "amount_bdt": str(amt),
                }
            )
        payload = {
            "report_type": AccountingReportType.OPERATING_EXPENSES,
            "rows": rows,
            "total_expenses_bdt": str(total_exp),
        }
        return payload, total_exp, total_exp, True, len(rows)

    @classmethod
    def compile_live_report(
        cls,
        *,
        tenant_id: str,
        report_type: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Tuple[Dict[str, Any], Decimal, Decimal, bool, int, Optional[Branch], Optional[datetime.date], Optional[datetime.date]]:
        r_type = str(report_type or "").upper().strip()
        if r_type not in AccountingReportType.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_REPORT_TYPE",
                    "detail": f"Invalid report_type '{r_type}'. Allowed: {', '.join(AccountingReportType.values)}.",
                }
            )

        role = getattr(actor_user, "role", None)
        effective_branch_id = branch_id
        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER") and not effective_branch_id:
            effective_branch_id = str(getattr(actor_user, "branch_id", "") or "")

        cls.assert_view_authorized(actor_user, branch_id=effective_branch_id)
        tenant = cls.assert_tenant_active(tenant_id)
        p_start, p_end = cls._parse_dates(start_date, end_date)

        target_branch = None
        if effective_branch_id and effective_branch_id not in ("ALL", "CONSOLIDATED"):
            target_branch = Branch.objects.filter(tenant=tenant, branch_id=effective_branch_id).first()
            if not target_branch:
                foreign = Branch.objects.filter(branch_id=effective_branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{effective_branch_id}' not found."})

        if r_type == AccountingReportType.TRIAL_BALANCE:
            payload, dr, cr, bal, cnt = cls._compile_trial_balance(tenant, target_branch, p_start, p_end)
        elif r_type == AccountingReportType.JOURNAL_LEDGER:
            payload, dr, cr, bal, cnt = cls._compile_journal_ledger(tenant, target_branch, p_start, p_end)
        elif r_type == AccountingReportType.OPERATING_EXPENSES:
            payload, dr, cr, bal, cnt = cls._compile_operating_expenses(tenant, target_branch, p_start, p_end)
        elif r_type == AccountingReportType.PROFIT_AND_LOSS:
            pl = ProfitLossReportService.generate_profit_and_loss_report(
                tenant_id=tenant_id,
                branch_id=str(target_branch.branch_id) if target_branch else None,
                start_date=p_start,
                end_date=p_end,
                actor_user=actor_user,
            )
            tb_payload, dr, cr, bal, _ = cls._compile_trial_balance(tenant, target_branch, p_start, p_end)
            payload = {"report_type": r_type, "profit_and_loss": pl, "trial_balance_balanced": bal}
            cnt = 1
        else:
            # RECONCILIATION_PACK
            inv = InventoryCostValuationService.generate_inventory_valuation_report(
                tenant_id=tenant_id,
                branch_id=str(target_branch.branch_id) if target_branch else None,
                as_of_date=p_end,
                actor_user=actor_user,
            )
            tb_payload, dr, cr, bal, tb_cnt = cls._compile_trial_balance(tenant, target_branch, p_start, p_end)
            payload = {
                "report_type": r_type,
                "inventory_valuation": inv,
                "trial_balance_summary": {
                    "total_debits_bdt": str(dr),
                    "total_credits_bdt": str(cr),
                    "is_book_balanced": bal,
                },
            }
            cnt = tb_cnt

        return payload, dr, cr, bal, cnt, target_branch, p_start, p_end

    # =========================================================================
    # SNAPSHOT LIFECYCLE & IMMUTABLE EXPORTS
    # =========================================================================

    @classmethod
    def _compute_digest(cls, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _serialize_snapshot(cls, snap: AccountingReportSnapshot, include_payload: bool = False) -> Dict[str, Any]:
        res = {
            "snapshot_id": snap.snapshot_id,
            "tenant_id": str(snap.tenant_id),
            "branch_id": str(snap.branch_id) if snap.branch_id else None,
            "branch_name": snap.branch.name if snap.branch else "All Branches (Consolidated)",
            "report_type": snap.report_type,
            "title": snap.title,
            "period_start": snap.period_start.isoformat() if snap.period_start else None,
            "period_end": snap.period_end.isoformat() if snap.period_end else None,
            "is_book_balanced": snap.is_book_balanced,
            "total_debits_bdt": str(q2(snap.total_debits_bdt)),
            "total_credits_bdt": str(q2(snap.total_credits_bdt)),
            "row_count": snap.row_count,
            "sha256_digest": snap.sha256_digest,
            "status": snap.status,
            "created_at": snap.created_at.isoformat(),
        }
        if include_payload:
            res["payload"] = snap.payload_json
            res["integrity_verified"] = cls._compute_digest(snap.payload_json) == snap.sha256_digest
        return res

    @classmethod
    @transaction.atomic
    def create_snapshot(
        cls,
        *,
        tenant_id: str,
        report_type: str,
        branch_id: Optional[str] = None,
        start_date: Optional[Any] = None,
        end_date: Optional[Any] = None,
        title: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        payload, dr, cr, bal, cnt, target_branch, p_start, p_end = cls.compile_live_report(
            tenant_id=tenant_id,
            report_type=report_type,
            branch_id=branch_id,
            start_date=start_date,
            end_date=end_date,
            actor_user=actor_user,
        )
        tenant = Tenant.objects.get(tenant_id=tenant_id)
        digest = cls._compute_digest(payload)
        clean_title = str(title or f"{report_type} Snapshot ({timezone.now().date().isoformat()})").strip()

        snap = AccountingReportSnapshot.objects.create(
            tenant=tenant,
            branch=target_branch,
            report_type=str(report_type).upper().strip(),
            period_start=p_start,
            period_end=p_end,
            title=clean_title,
            is_book_balanced=bal,
            total_debits_bdt=dr,
            total_credits_bdt=cr,
            row_count=cnt,
            payload_json=payload,
            sha256_digest=digest,
            status=AccountingSnapshotStatus.FINALIZED,
            generated_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="ACCOUNTING_SNAPSHOT_CREATE",
            resource_type="AccountingReportSnapshot",
            resource_id=snap.snapshot_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"report_type": snap.report_type, "sha256_digest": digest},
        )

        res = cls._serialize_snapshot(snap, include_payload=True)
        res["message_en"] = f"Historical accounting snapshot '{snap.snapshot_id}' created and sealed."
        res["message_bn"] = f"অ্যাকাউন্টিং রিপোর্ট স্ন্যাপশট '{snap.snapshot_id}' সফলভাবে সংরক্ষিত হয়েছে।"
        return res

    @classmethod
    def list_snapshots(
        cls,
        *,
        tenant_id: str,
        report_type: Optional[str] = None,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        role = getattr(actor_user, "role", None)
        eff_branch = branch_id
        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            eff_branch = str(getattr(actor_user, "branch_id", "") or "")
        cls.assert_view_authorized(actor_user, branch_id=eff_branch)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = AccountingReportSnapshot.objects.filter(
            tenant=tenant, status=AccountingSnapshotStatus.FINALIZED
        ).select_related("branch")
        if report_type:
            qs = qs.filter(report_type=report_type.upper().strip())
        if eff_branch and eff_branch not in ("ALL", "CONSOLIDATED"):
            qs = qs.filter(branch_id=eff_branch)

        items = [cls._serialize_snapshot(s, include_payload=False) for s in qs[:100]]
        return {"tenant_id": str(tenant.tenant_id), "count": len(items), "snapshots": items}

    @classmethod
    def get_snapshot(cls, *, tenant_id: str, snapshot_id: str, actor_user: Any = None) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        snap = AccountingReportSnapshot.objects.select_related("branch").filter(tenant=tenant, snapshot_id=snapshot_id).first()
        if not snap:
            foreign = AccountingReportSnapshot.objects.filter(snapshot_id=snapshot_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant snapshot access forbidden."})
            raise NotFound({"error_code": "ERR_SNAPSHOT_NOT_FOUND", "detail": f"Snapshot '{snapshot_id}' not found."})

        cls.assert_view_authorized(actor_user, branch_id=str(snap.branch_id) if snap.branch_id else None)
        return cls._serialize_snapshot(snap, include_payload=True)

    @classmethod
    @transaction.atomic
    def archive_snapshot(cls, *, tenant_id: str, snapshot_id: str, actor_user: Any = None) -> Dict[str, Any]:
        cls.assert_archive_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)
        snap = AccountingReportSnapshot.objects.select_for_update().filter(tenant=tenant, snapshot_id=snapshot_id).first()
        if not snap:
            foreign = AccountingReportSnapshot.objects.filter(snapshot_id=snapshot_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant archive forbidden."})
            raise NotFound({"error_code": "ERR_SNAPSHOT_NOT_FOUND", "detail": f"Snapshot '{snapshot_id}' not found."})
        snap.status = AccountingSnapshotStatus.ARCHIVED
        snap.save()
        return {"success": True, "snapshot_id": snap.snapshot_id, "status": snap.status}

    @classmethod
    def render_payload_csv(cls, report_type: str, payload: Dict[str, Any]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        if report_type == AccountingReportType.TRIAL_BALANCE:
            writer.writerow(["Account Code", "Account Name", "Category", "Debit (BDT)", "Credit (BDT)", "Net Balance (BDT)"])
            for r in payload.get("rows", []):
                writer.writerow([r["account_code"], r["account_name"], r["category"], r["debit_bdt"], r["credit_bdt"], r["net_balance_bdt"]])
            writer.writerow(["TOTAL", "", "", payload.get("total_debits_bdt", "0.00"), payload.get("total_credits_bdt", "0.00"), ""])
        elif report_type == AccountingReportType.JOURNAL_LEDGER:
            writer.writerow(["Journal Number", "Posting Date", "Source Module", "Reference", "Narration", "Debit (BDT)", "Credit (BDT)", "Balanced"])
            for r in payload.get("rows", []):
                writer.writerow([r["journal_number"], r["posting_date"], r["source_module"], r["source_reference"], r["narration"], r["debit_bdt"], r["credit_bdt"], r["is_balanced"]])
        elif report_type == AccountingReportType.OPERATING_EXPENSES:
            writer.writerow(["Voucher Number", "Expense Date", "Branch", "Category", "Payee", "Method", "Amount (BDT)"])
            for r in payload.get("rows", []):
                writer.writerow([r["voucher_number"], r["expense_date"], r["branch_name"], r["category_code"], r["payee_name"], r["payment_method"], r["amount_bdt"]])
        else:
            writer.writerow(["Report Type", "Summary JSON"])
            writer.writerow([report_type, json.dumps(payload, sort_keys=True, default=str)])
        return buf.getvalue()

    @classmethod
    def export_snapshot_csv(cls, *, tenant_id: str, snapshot_id: str, actor_user: Any = None) -> str:
        snap_data = cls.get_snapshot(tenant_id=tenant_id, snapshot_id=snapshot_id, actor_user=actor_user)
        return cls.render_payload_csv(snap_data["report_type"], snap_data["payload"])
