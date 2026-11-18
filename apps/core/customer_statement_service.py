"""
backend/apps/core/customer_statement_service.py
Architecture Decision: ADR-0267 (P267).
Phase 14: Customers, Suppliers, Dues & Payments.

Authoritative domain service for:
- Generating official printable customer statements of account
- Opening balance, period charges (debits), payments (credits), and closing amount
- Multi-tier due aging analysis (Current, 31-60d, 61-90d, 91d+)
- Printable HTML and slip formats with pharmacy headers and DGDA license info
- Persistent statement generation and reprint audit logging (CustomerStatementAudit)
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal
import html
import math
from typing import Any, Dict, List, Optional
import uuid

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.core.models import (
    Branch,
    CustomerDueRecord,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    CustomerStatementAudit,
    DueStatus,
    PosCustomer,
)
from apps.core.pos_domain_service import PosDomainService

MONEY_Q = Decimal("0.01")
MONEY_ZERO = Decimal("0.00")


def q2(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else "0.00")).quantize(
        MONEY_Q, rounding=ROUND_HALF_UP
    )


class CustomerStatementService:
    """
    Computes customer account statements with due aging and renders printable templates.
    """

    @classmethod
    def compute_due_aging(
        cls,
        tenant: Any,
        customer: PosCustomer,
        as_of_date: Optional[datetime.date] = None,
    ) -> Dict[str, Decimal]:
        """
        Computes 4-tier due aging buckets for all outstanding invoice dues:
        - current_bdt: not overdue or overdue by 0-30 days
        - days_31_60_bdt: overdue 31 to 60 days
        - days_61_90_bdt: overdue 61 to 90 days
        - days_91_plus_bdt: overdue > 90 days
        Guarantees that sum(buckets) matches total open due.
        """
        ref_date = as_of_date or timezone.now().date()
        open_dues = CustomerDueRecord.objects.filter(
            tenant=tenant,
            customer=customer,
            remaining_due_bdt__gt=MONEY_ZERO,
            status__in=[DueStatus.OUTSTANDING, DueStatus.PARTIAL],
        )

        b_current = MONEY_ZERO
        b_31_60 = MONEY_ZERO
        b_61_90 = MONEY_ZERO
        b_91_plus = MONEY_ZERO

        for due in open_dues:
            rem = q2(due.remaining_due_bdt)
            # Use due_date if available; otherwise creation date + 30 days
            effective_due_date = due.due_date or (due.created_at.date() + datetime.timedelta(days=30))
            days_overdue = (ref_date - effective_due_date).days

            if days_overdue <= 30:
                b_current = q2(b_current + rem)
            elif 31 <= days_overdue <= 60:
                b_31_60 = q2(b_31_60 + rem)
            elif 61 <= days_overdue <= 90:
                b_61_90 = q2(b_61_90 + rem)
            else:
                b_91_plus = q2(b_91_plus + rem)

        total_aging = q2(b_current + b_31_60 + b_61_90 + b_91_plus)
        return {
            "current_bdt": b_current,
            "days_31_60_bdt": b_31_60,
            "days_61_90_bdt": b_61_90,
            "days_91_plus_bdt": b_91_plus,
            "total_aging_bdt": total_aging,
        }

    @classmethod
    @transaction.atomic
    def generate_statement(
        cls,
        user: Any,
        customer_id: str,
        *,
        branch_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        save_audit: bool = True,
    ) -> Dict[str, Any]:
        """
        Generates a comprehensive statement of account with opening balance,
        charges, payments, closing balance, due aging, and printable formats.
        """
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        PosDomainService.assert_tenant_active(tenant)
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "customer statements")

        clean_cid = str(customer_id or "").strip()
        customer = PosCustomer.objects.select_related("primary_branch").filter(
            tenant=tenant, customer_id=clean_cid
        ).first()
        if not customer:
            raise NotFound(
                {
                    "error_code": "ERR_CUSTOMER_NOT_FOUND",
                    "customer_id": f"Customer '{clean_cid}' was not found in active tenant.",
                }
            )

        # Parse dates
        today = timezone.now().date()
        parsed_start: Optional[datetime.date] = None
        parsed_end: Optional[datetime.date] = None

        if start_date:
            try:
                parsed_start = datetime.date.fromisoformat(str(start_date).strip())
            except (ValueError, TypeError):
                raise ValidationError({"start_date": "Invalid start_date format (YYYY-MM-DD expected)."})
        else:
            # Default to 30 days prior
            parsed_start = today - datetime.timedelta(days=30)

        if end_date:
            try:
                parsed_end = datetime.date.fromisoformat(str(end_date).strip())
            except (ValueError, TypeError):
                raise ValidationError({"end_date": "Invalid end_date format (YYYY-MM-DD expected)."})
        else:
            parsed_end = today

        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValidationError(
                {"error_code": "ERR_INVALID_DATE_RANGE", "start_date": "start_date cannot be later than end_date."}
            )

        start_dt = timezone.make_aware(datetime.datetime.combine(parsed_start, datetime.time.min))
        end_dt = timezone.make_aware(datetime.datetime.combine(parsed_end, datetime.time.max))

        # Base Ledger Query
        base_qs = CustomerReceivableLedgerEntry.objects.filter(
            tenant=tenant,
            customer=customer,
        )
        if branch_id:
            base_qs = base_qs.filter(branch_id=branch_id)

        # 1. Opening balance before start_dt
        prior_qs = base_qs.filter(posted_at__lt=start_dt)
        prior_debits = prior_qs.aggregate(s=Sum("debit_amount_bdt"))["s"] or MONEY_ZERO
        prior_credits = prior_qs.aggregate(s=Sum("credit_amount_bdt"))["s"] or MONEY_ZERO
        opening_balance = max(MONEY_ZERO, q2(prior_debits - prior_credits))

        # 2. Period activity
        period_qs = base_qs.filter(posted_at__gte=start_dt, posted_at__lte=end_dt).order_by("posted_at", "entry_id")
        period_debits = period_qs.aggregate(s=Sum("debit_amount_bdt"))["s"] or MONEY_ZERO
        period_credits = period_qs.aggregate(s=Sum("credit_amount_bdt"))["s"] or MONEY_ZERO
        closing_balance = max(MONEY_ZERO, q2(opening_balance + period_debits - period_credits))

        # 3. Due aging analysis
        aging = cls.compute_due_aging(tenant, customer, as_of_date=parsed_end)

        # 4. Parity check: if end_date is today or unconstrained, closing balance matches customer current due
        is_reconciled = True
        if parsed_end >= today:
            is_reconciled = (closing_balance == customer.current_due_bdt)

        # 5. Format itemized transactions
        running_bal = opening_balance
        items: List[Dict[str, Any]] = []
        for e in period_qs:
            deb = q2(e.debit_amount_bdt)
            cred = q2(e.credit_amount_bdt)
            running_bal = max(MONEY_ZERO, q2(running_bal + deb - cred))
            items.append(
                {
                    "date": e.posted_at.strftime("%Y-%m-%d %H:%M"),
                    "reference": e.reference_number,
                    "type": e.entry_type,
                    "description": e.narration,
                    "debit_bdt": f"{deb:.2f}" if deb > MONEY_ZERO else "-",
                    "credit_bdt": f"{cred:.2f}" if cred > MONEY_ZERO else "-",
                    "balance_bdt": f"{running_bal:.2f}",
                }
            )

        # 6. Generate printable HTML and text
        statement_id = f"stmt-{uuid.uuid4().hex[:12]}"
        pharmacy_name = getattr(tenant, "name", "Pharmacy")
        branch_name = branch.name if branch else (customer.primary_branch.name if customer.primary_branch else "Main Branch")
        branch_phone = getattr(branch, "phone", "") or "+880-1700-000000"
        drug_license = getattr(branch, "drug_license_no", "") or "DGDA-PMS-VERIFIED"

        masked_phone = customer.phone
        if getattr(customer, "privacy_preferences", None) and getattr(customer.privacy_preferences, "mask_phone_on_receipts", False):
            if len(masked_phone) >= 8:
                masked_phone = f"{masked_phone[:4]}****{masked_phone[-3:]}"

        printable_html = cls._render_statement_html(
            statement_id=statement_id,
            pharmacy_name=pharmacy_name,
            branch_name=branch_name,
            branch_phone=branch_phone,
            drug_license=drug_license,
            customer_name=customer.full_name,
            customer_phone=masked_phone,
            credit_limit=f"{customer.credit_limit_bdt:.2f}",
            period_str=f"{parsed_start.strftime('%d-%b-%Y')} to {parsed_end.strftime('%d-%b-%Y')}",
            opening_balance=f"{opening_balance:.2f}",
            total_charges=f"{period_debits:.2f}",
            total_payments=f"{period_credits:.2f}",
            closing_balance=f"{closing_balance:.2f}",
            aging=aging,
            items=items,
            generated_at=timezone.now().strftime("%d-%b-%Y %I:%M %p"),
        )

        printable_text = cls._render_statement_text(
            pharmacy_name=pharmacy_name,
            branch_name=branch_name,
            customer_name=customer.full_name,
            period_str=f"{parsed_start} to {parsed_end}",
            opening_balance=f"{opening_balance:.2f}",
            charges=f"{period_debits:.2f}",
            payments=f"{period_credits:.2f}",
            closing=f"{closing_balance:.2f}",
            aging=aging,
        )

        # 7. Persist audit log
        audit_record: Optional[CustomerStatementAudit] = None
        if save_audit:
            audit_record = CustomerStatementAudit.objects.create(
                statement_id=statement_id,
                tenant=tenant,
                branch=branch or customer.primary_branch,
                customer=customer,
                period_start=parsed_start,
                period_end=parsed_end,
                opening_balance_bdt=opening_balance,
                total_charges_bdt=period_debits,
                total_payments_bdt=period_credits,
                closing_balance_bdt=closing_balance,
                aging_current_bdt=aging["current_bdt"],
                aging_31_60_bdt=aging["days_31_60_bdt"],
                aging_61_90_bdt=aging["days_61_90_bdt"],
                aging_91_plus_bdt=aging["days_91_plus_bdt"],
                is_parity_reconciled=is_reconciled,
                generated_by=user,
                print_count=1,
            )

        return {
            "statement_id": statement_id,
            "customer": {
                "customer_id": customer.customer_id,
                "full_name": customer.full_name,
                "phone": masked_phone,
                "credit_limit_bdt": f"{customer.credit_limit_bdt:.2f}",
                "current_due_bdt": f"{customer.current_due_bdt:.2f}",
            },
            "period": {
                "start_date": parsed_start.isoformat(),
                "end_date": parsed_end.isoformat(),
            },
            "summary": {
                "opening_balance_bdt": f"{opening_balance:.2f}",
                "total_charges_bdt": f"{period_debits:.2f}",
                "total_payments_bdt": f"{period_credits:.2f}",
                "closing_balance_bdt": f"{closing_balance:.2f}",
                "is_parity_reconciled": is_reconciled,
            },
            "aging": {
                "current_bdt": f"{aging['current_bdt']:.2f}",
                "days_31_60_bdt": f"{aging['days_31_60_bdt']:.2f}",
                "days_61_90_bdt": f"{aging['days_61_90_bdt']:.2f}",
                "days_91_plus_bdt": f"{aging['days_91_plus_bdt']:.2f}",
                "total_aging_bdt": f"{aging['total_aging_bdt']:.2f}",
            },
            "transactions_count": len(items),
            "transactions": items,
            "printable_html": printable_html,
            "printable_text": printable_text,
        }

    @classmethod
    @transaction.atomic
    def record_statement_print(cls, user: Any, statement_id: str) -> Dict[str, Any]:
        """
        Records that a statement was printed or reprinted, updating print count.
        """
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user, require_write=False, allowed_roles=PosDomainService.POS_READ_ROLES
        )
        audit = CustomerStatementAudit.objects.select_for_update().filter(
            tenant=tenant, statement_id=statement_id
        ).first()
        if not audit:
            raise NotFound({"statement_id": f"Statement '{statement_id}' not found."})

        audit.print_count += 1
        audit.last_printed_at = timezone.now()
        audit.save(update_fields=["print_count", "last_printed_at"])

        return {
            "statement_id": audit.statement_id,
            "print_count": audit.print_count,
            "last_printed_at": audit.last_printed_at.isoformat(),
            "is_reprint": audit.print_count > 1,
        }

    @staticmethod
    def _render_statement_html(
        *,
        statement_id: str,
        pharmacy_name: str,
        branch_name: str,
        branch_phone: str,
        drug_license: str,
        customer_name: str,
        customer_phone: str,
        credit_limit: str,
        period_str: str,
        opening_balance: str,
        total_charges: str,
        total_payments: str,
        closing_balance: str,
        aging: Dict[str, Decimal],
        items: List[Dict[str, Any]],
        generated_at: str,
    ) -> str:
        rows = []
        for it in items:
            rows.append(
                f"<tr>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #e2e8f0;'>{html.escape(it['date'])}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #e2e8f0;font-family:monospace;'>{html.escape(it['reference'])}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #e2e8f0;'>{html.escape(it['description'])}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;'>{html.escape(it['debit_bdt'])}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;color:#16a34a;'>{html.escape(it['credit_bdt'])}</td>"
                f"<td style='padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600;'>{html.escape(it['balance_bdt'])}</td>"
                f"</tr>"
            )
        table_rows = "".join(rows) if rows else "<tr><td colspan='6' style='padding:12px;text-align:center;color:#64748b;'>No activity in this period.</td></tr>"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Customer Statement - {html.escape(customer_name)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #1e293b; margin: 20px; line-height: 1.4; }}
  .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #0f172a; padding-bottom: 12px; margin-bottom: 16px; }}
  .title {{ font-size: 20px; font-weight: 700; color: #0f172a; }}
  .meta {{ font-size: 12px; color: #64748b; }}
  .summary-box {{ display: flex; gap: 12px; margin-bottom: 16px; }}
  .card {{ flex: 1; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; text-align: center; }}
  .card-label {{ font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 600; }}
  .card-val {{ font-size: 16px; font-weight: 700; color: #0f172a; margin-top: 4px; }}
  .aging-table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 12px; text-align: center; }}
  .aging-table th, .aging-table td {{ border: 1px solid #cbd5e1; padding: 6px; }}
  .aging-table th {{ background: #f1f5f9; font-weight: 600; }}
  .ledger-table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 20px; }}
  .ledger-table th {{ background: #0f172a; color: white; padding: 8px; text-align: left; }}
  .footer {{ display: flex; justify-content: space-between; font-size: 11px; color: #64748b; border-top: 1px solid #e2e8f0; padding-top: 12px; }}
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="title">{html.escape(pharmacy_name)}</div>
    <div class="meta">{html.escape(branch_name)} | Phone: {html.escape(branch_phone)}</div>
    <div class="meta">Drug License: {html.escape(drug_license)}</div>
  </div>
  <div style="text-align:right;">
    <div style="font-size:16px;font-weight:700;">STATEMENT OF ACCOUNT</div>
    <div class="meta">Statement ID: {html.escape(statement_id)}</div>
    <div class="meta">Period: {html.escape(period_str)}</div>
  </div>
</div>

<div style="display:flex;justify-content:space-between;margin-bottom:14px;font-size:13px;">
  <div>
    <strong>Customer:</strong> {html.escape(customer_name)}<br>
    <strong>Phone:</strong> {html.escape(customer_phone)}
  </div>
  <div style="text-align:right;">
    <strong>Credit Limit:</strong> BDT {html.escape(credit_limit)}<br>
    <strong>Generated:</strong> {html.escape(generated_at)}
  </div>
</div>

<div class="summary-box">
  <div class="card"><div class="card-label">Opening Balance</div><div class="card-val">BDT {html.escape(opening_balance)}</div></div>
  <div class="card"><div class="card-label">Charges (+)</div><div class="card-val">BDT {html.escape(total_charges)}</div></div>
  <div class="card"><div class="card-label">Payments (-)</div><div class="card-val" style="color:#16a34a;">BDT {html.escape(total_payments)}</div></div>
  <div class="card" style="background:#eff6ff;border-color:#bfdbfe;"><div class="card-label">Closing Due</div><div class="card-val" style="color:#1d4ed8;">BDT {html.escape(closing_balance)}</div></div>
</div>

<div style="font-weight:600;font-size:13px;margin-bottom:6px;">Due Aging Summary (বকেয়ার মেয়াদ ভিত্তিক বিবরণ)</div>
<table class="aging-table">
  <tr>
    <th>Current (0-30 Days)</th>
    <th>31 - 60 Days</th>
    <th>61 - 90 Days</th>
    <th>90+ Days</th>
    <th style="background:#eff6ff;color:#1d4ed8;">Total Due Balance</th>
  </tr>
  <tr>
    <td>BDT {aging['current_bdt']:.2f}</td>
    <td>BDT {aging['days_31_60_bdt']:.2f}</td>
    <td>BDT {aging['days_61_90_bdt']:.2f}</td>
    <td style="color:#dc2626;font-weight:600;">BDT {aging['days_91_plus_bdt']:.2f}</td>
    <td style="font-weight:700;color:#1d4ed8;">BDT {aging['total_aging_bdt']:.2f}</td>
  </tr>
</table>

<div style="font-weight:600;font-size:13px;margin-bottom:6px;">Itemized Activity (লেনদেনের বিস্তারিত তালিকা)</div>
<table class="ledger-table">
  <thead>
    <tr>
      <th style="width:140px;">Date & Time</th>
      <th style="width:130px;">Reference</th>
      <th>Description</th>
      <th style="text-align:right;width:90px;">Charge (Dr)</th>
      <th style="text-align:right;width:90px;">Payment (Cr)</th>
      <th style="text-align:right;width:100px;">Balance</th>
    </tr>
  </thead>
  <tbody>
    {table_rows}
  </tbody>
</table>

<div class="footer">
  <div>System Generated Official Statement | Bangladesh DGDA & PMS Compliant</div>
  <div>Authorised Signatory: _________________________</div>
</div>
</body>
</html>"""

    @staticmethod
    def _render_statement_text(
        *,
        pharmacy_name: str,
        branch_name: str,
        customer_name: str,
        period_str: str,
        opening_balance: str,
        charges: str,
        payments: str,
        closing: str,
        aging: Dict[str, Decimal],
    ) -> str:
        lines = [
            f"{pharmacy_name.upper()} - {branch_name}",
            "STATEMENT OF ACCOUNT / গ্রাহক হিসাব বিবরণী",
            f"Customer: {customer_name}",
            f"Period: {period_str}",
            "-" * 44,
            f"Opening Balance : BDT {opening_balance}",
            f"Total Charges   : BDT {charges}",
            f"Total Payments  : BDT {payments}",
            f"Closing Due     : BDT {closing}",
            "-" * 44,
            "DUE AGING (বকেয়ার বয়স):",
            f" Current (0-30d): BDT {aging['current_bdt']:.2f}",
            f" 31-60 Days     : BDT {aging['days_31_60_bdt']:.2f}",
            f" 61-90 Days     : BDT {aging['days_61_90_bdt']:.2f}",
            f" 90+ Days       : BDT {aging['days_91_plus_bdt']:.2f}",
            "=" * 44,
        ]
        return "\n".join(lines)
