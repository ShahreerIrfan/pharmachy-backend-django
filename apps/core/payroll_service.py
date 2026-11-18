"""
backend/apps/core/payroll_service.py
Architecture Decision: ADR-0291 (P291).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service for Payroll Expense Boundary & Employee Salary Privacy:
1. Strict segregation of payroll from ordinary operating expenses (protects employee salary privacy).
2. Confidential staff salary calculation (base, allowances, bonuses, deductions, net).
3. Double-entry General Ledger posting:
   - Debit: 6030 (Staff Salaries & Pharmacist Compensation) = Gross + Allowances + Bonuses
   - Credit: 1010/1020 (Settlement Account: Till Cash / Corporate Bank) = Total Net Payable
   - Credit: 2020 (Accrued Liabilities / Payroll Deductions) = Total Deductions
   - Balanced Invariant: Total Debits == Total Credits.
4. Summary-only journal narration ensuring zero personal employee PII leakage to general ledger viewers.
"""

from __future__ import annotations

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
    Branch,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    JournalEntry,
    JournalSourceModule,
    PayrollPaymentMethod,
    PayrollRunBatch,
    PayrollRunStatus,
    PayrollSlipItem,
    StaffSalaryRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

PAYROLL_SENIOR_ROLES = {
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


def mask_bank_account(acc_no: str) -> str:
    """Masks all but the last 4 characters of a bank account number."""
    if not acc_no:
        return ""
    clean = str(acc_no).strip()
    if len(clean) <= 4:
        return "****"
    return "*" * (len(clean) - 4) + clean[-4:]


class PayrollExpenseService:
    """
    Authoritative domain service managing Payroll Execution, Staff Privacy boundaries,
    and balanced Double-Entry General Ledger journal postings.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PAYROLL_UNAUTHORIZED",
                    "detail": "Cashier and dispensary staff are not authorized to access payroll runs or employee salary data.",
                    "message_en": "Cashier and dispensary staff are not authorized to access payroll runs or employee salary data.",
                    "message_bn": "ক্যাশিয়ার এবং সাধারণ স্টাফদের পেরোল রান বা বেতন দেখার অনুমতি নেই।",
                }
            )

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in PAYROLL_SENIOR_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PAYROLL_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can create, approve, or post payroll runs.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can create, approve, or post payroll runs.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার পেরোল প্রস্তুত, অনুমোদন বা পোস্ট করতে পারেন।",
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
                    "detail": "Tenant account subscription is suspended. Payroll operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Payroll operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পেরোল কার্যক্রম বন্ধ রাখা হয়েছে।",
                }
            )
        return tenant

    # =========================================================================
    # 1. CREATE PAYROLL RUN BATCH
    # =========================================================================

    @classmethod
    @transaction.atomic
    def create_payroll_run(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Creates a draft payroll execution batch with individual confidential staff slips.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        # 1. Branch Validation (Optional, null = all branches)
        branch_id = data.get("branch_id")
        branch = None
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        # 2. Period Validation
        start_date = data.get("period_start")
        end_date = data.get("period_end")
        if not start_date or not end_date:
            raise ValidationError(
                {"error_code": "ERR_MISSING_PERIOD", "detail": "Both period_start and period_end are required."}
            )
        try:
            p_start = timezone.datetime.fromisoformat(str(start_date)).date()
            p_end = timezone.datetime.fromisoformat(str(end_date)).date()
            if p_start > p_end:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {"error_code": "ERR_INVALID_PERIOD", "detail": "period_start must be before or equal to period_end."}
            )

        # 3. Payment Method & Settlement Account
        payment_method = str(data.get("payment_method", PayrollPaymentMethod.BANK_TRANSFER)).upper().strip()
        if payment_method not in PayrollPaymentMethod.values:
            payment_method = PayrollPaymentMethod.BANK_TRANSFER

        settlement_account = None
        settlement_acc_id = data.get("settlement_account_id")
        if settlement_acc_id:
            settlement_account = FinancialAccount.objects.filter(account_id=settlement_acc_id, tenant=tenant).first()
            if not settlement_account:
                foreign_acc = FinancialAccount.objects.filter(account_id=settlement_acc_id).first()
                if foreign_acc and str(foreign_acc.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant settlement account access forbidden."})
                raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Settlement account '{settlement_acc_id}' not found."})

        # 4. Slips Processing
        raw_slips = data.get("slips", [])
        if not raw_slips or not isinstance(raw_slips, list):
            raise ValidationError(
                {"error_code": "ERR_EMPTY_PAYROLL_SLIPS", "detail": "Payroll run requires at least one staff salary slip."}
            )

        # Generate Run Number
        now_dt = timezone.now()
        prefix = f"PAY-{p_start.strftime('%Y%m')}"
        random_code = uuid.uuid4().hex[:6].upper()
        run_number = f"{prefix}-{random_code}"

        run_batch = PayrollRunBatch(
            run_id=f"payrun-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            run_number=run_number,
            period_start=p_start,
            period_end=p_end,
            payment_date=data.get("payment_date") or now_dt.date(),
            status=PayrollRunStatus.DRAFT,
            payment_method=payment_method,
            settlement_account=settlement_account,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            notes=str(data.get("notes", "")).strip(),
            is_confidential=True,
        )

        total_gross = DECIMAL_ZERO
        total_allowances = DECIMAL_ZERO
        total_bonuses = DECIMAL_ZERO
        total_deductions = DECIMAL_ZERO
        total_net_payable = DECIMAL_ZERO

        slip_objects: List[PayrollSlipItem] = []
        seen_staff = set()

        for idx, item in enumerate(raw_slips, start=1):
            staff_id = item.get("staff_member_id")
            if not staff_id:
                raise ValidationError({"error_code": "ERR_MISSING_STAFF_ID", "detail": f"Slip {idx} requires staff_member_id."})

            if staff_id in seen_staff:
                raise ValidationError(
                    {"error_code": "ERR_DUPLICATE_STAFF_SLIP", "detail": f"Staff '{staff_id}' appears multiple times in this payroll run."}
                )
            seen_staff.add(staff_id)

            staff_user = PharmacyUser.objects.filter(user_id=staff_id, tenant_id=tenant.tenant_id).first()
            if not staff_user:
                foreign_user = PharmacyUser.objects.filter(user_id=staff_id).first()
                if foreign_user and str(foreign_user.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant staff payroll access forbidden."})
                raise NotFound({"error_code": "ERR_STAFF_NOT_FOUND", "detail": f"Staff user '{staff_id}' not found in tenant."})

            # Base salary
            base_salary = q2(Decimal(str(item.get("base_salary_bdt", 0))))
            if base_salary <= DECIMAL_ZERO:
                # Try to pull from StaffSalaryRecord
                record = StaffSalaryRecord.objects.filter(staff_member=staff_user, tenant=tenant, is_active=True).first()
                if record and record.base_salary_bdt > DECIMAL_ZERO:
                    base_salary = q2(record.base_salary_bdt)
                else:
                    raise ValidationError(
                        {"error_code": "ERR_INVALID_BASE_SALARY", "detail": f"Base salary for '{staff_user.full_name}' must be positive."}
                    )

            allowances = q2(Decimal(str(item.get("allowances_bdt", 0))))
            bonus = q2(Decimal(str(item.get("bonus_bdt", 0))))
            deductions = q2(Decimal(str(item.get("deductions_bdt", 0))))

            net_salary = base_salary + allowances + bonus - deductions
            if net_salary < DECIMAL_ZERO:
                raise ValidationError(
                    {
                        "error_code": "ERR_NEGATIVE_NET_SALARY",
                        "detail": f"Net salary for '{staff_user.full_name}' cannot be negative (Deductions exceed earnings).",
                    }
                )

            total_gross += base_salary
            total_allowances += allowances
            total_bonuses += bonus
            total_deductions += deductions
            total_net_payable += net_salary

            bank_name = str(item.get("bank_name", "")).strip()
            bank_acc = str(item.get("bank_account_no", "")).strip()
            if not bank_name or not bank_acc:
                record = StaffSalaryRecord.objects.filter(staff_member=staff_user, tenant=tenant, is_active=True).first()
                if record:
                    bank_name = bank_name or record.bank_name
                    bank_acc = bank_acc or record.bank_account_no

            slip = PayrollSlipItem(
                slip_id=f"pslip-{uuid.uuid4().hex[:12]}",
                payroll_run=run_batch,
                tenant=tenant,
                staff_member=staff_user,
                staff_name=staff_user.full_name or staff_user.email,
                staff_role=str(staff_user.role),
                base_salary_bdt=base_salary,
                allowances_bdt=allowances,
                bonus_bdt=bonus,
                deductions_bdt=deductions,
                net_salary_bdt=net_salary,
                bank_name=bank_name,
                bank_account_no=bank_acc,
                payment_status="PENDING",
            )
            slip_objects.append(slip)

        run_batch.total_gross_bdt = total_gross
        run_batch.total_allowances_bdt = total_allowances
        run_batch.total_bonuses_bdt = total_bonuses
        run_batch.total_deductions_bdt = total_deductions
        run_batch.total_net_payable_bdt = total_net_payable
        run_batch.save()

        PayrollSlipItem.objects.bulk_create(slip_objects)

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="PAYROLL_RUN_CREATE_DRAFT",
            resource_type="PayrollRunBatch",
            resource_id=run_batch.run_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"run_number": run_number, "total_net_payable": str(total_net_payable), "slips_count": len(slip_objects)},
        )

        return cls._serialize_run(run_batch, include_slips=True, actor_user=actor_user)

    # =========================================================================
    # 2. APPROVE & POST PAYROLL RUN TO GENERAL LEDGER
    # =========================================================================

    @classmethod
    @transaction.atomic
    def approve_and_post_payroll(
        cls,
        *,
        tenant_id: str,
        run_id: str,
        notes: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Approves draft payroll and posts balanced double-entry General Ledger journal entries:
        - Dr: GL 6030 (Staff Salaries & Pharmacist Compensation) = Gross + Allowances + Bonuses
        - Cr: GL 1010/1020 (Settlement Asset Account: Cash/Bank) = Net Payable
        - Cr: GL 2020 (Accrued Liabilities / Payroll Deductions Payable) = Total Deductions
        Invariant: Total Dr == Total Cr.
        General ledger narration is aggregated and strictly contains ZERO employee personal PII.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        run = PayrollRunBatch.objects.select_for_update().filter(run_id=run_id, tenant=tenant).first()
        if not run:
            foreign = PayrollRunBatch.objects.filter(run_id=run_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant payroll access forbidden."})
            raise NotFound({"error_code": "ERR_PAYROLL_RUN_NOT_FOUND", "detail": f"Payroll run '{run_id}' not found."})

        if run.status == PayrollRunStatus.POSTED:
            raise ValidationError(
                {
                    "error_code": "ERR_PAYROLL_ALREADY_POSTED",
                    "detail": f"Payroll run '{run.run_number}' has already been approved and posted to the General Ledger.",
                    "message_en": "Payroll run has already been approved and posted.",
                    "message_bn": "পেরোল রান ইতোমধ্যে অনুমোদিত এবং জেনারেল লেজারে পোস্ট করা হয়েছে।",
                }
            )

        if run.status == PayrollRunStatus.CANCELLED:
            raise ValidationError(
                {"error_code": "ERR_PAYROLL_CANCELLED", "detail": "Cancelled payroll runs cannot be posted."}
            )

        total_expense = q2(run.total_gross_bdt + run.total_allowances_bdt + run.total_bonuses_bdt)
        net_payable = q2(run.total_net_payable_bdt)
        deductions = q2(run.total_deductions_bdt)

        # Invariant check: expense must equal net_payable + deductions
        if total_expense != (net_payable + deductions):
            raise ValidationError(
                {"error_code": "ERR_PAYROLL_MATH_MISMATCH", "detail": "Gross earnings do not equal Net Payable plus Deductions."}
            )

        # Resolve Settlement GL Account (Asset: 1010 Cash or 1020 Bank)
        settlement_gl_code = "1020"  # default Bank & MFS Clearing
        if run.settlement_account:
            if run.settlement_account.account_type == FinancialAccountType.CASH_REGISTER:
                settlement_gl_code = "1010"
            else:
                settlement_gl_code = "1020"
        elif run.payment_method == PayrollPaymentMethod.CASH:
            settlement_gl_code = "1010"

        # Construct Balanced Double-Entry Journal Lines
        # 1. Debit Salaries Expense
        journal_lines = [
            {
                "account_code": "6030",
                "debit_amount_bdt": total_expense,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"Staff Salaries & Compensation for period {run.period_start} to {run.period_end}",
            }
        ]

        # 2. Credit Settlement Account for Net Payable
        if net_payable > DECIMAL_ZERO:
            journal_lines.append(
                {
                    "account_code": settlement_gl_code,
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": net_payable,
                    "description": f"Salary disbursement ({run.payment_method}) via {run.run_number}",
                }
            )

        # 3. Credit Accrued Liabilities for Deductions (Tax / Provident / Advances)
        if deductions > DECIMAL_ZERO:
            journal_lines.append(
                {
                    "account_code": "2020",
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": deductions,
                    "description": f"Payroll statutory/advance deductions for {run.run_number}",
                }
            )

        # Post Journal Entry via DoubleEntryJournalService
        # CRITICAL PRIVACY INVARIANT: Summary narration only, no employee names or personal bank accounts
        slips_count = run.slips.count()
        journal_res = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=journal_lines,
            posting_date=run.payment_date,
            source_module=JournalSourceModule.PAYROLL,
            source_reference=run.run_number,
            narration=f"Monthly Staff Payroll {run.run_number}: {run.period_start} to {run.period_end} ({slips_count} staff)",
            branch_id=str(run.branch.branch_id) if run.branch else None,
            actor_user=actor_user,
        )

        journal = JournalEntry.objects.get(entry_id=journal_res["entry_id"])

        # Update run status
        run.status = PayrollRunStatus.POSTED
        run.approved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        run.approved_at = timezone.now()
        run.posted_journal = journal
        if notes:
            run.notes = f"{run.notes}\n[Approved] {notes}".strip()
        run.save()

        # Update individual slips status
        run.slips.update(payment_status="PAID")

        # Update settlement financial account balance if present
        if run.settlement_account:
            run.settlement_account.current_balance = q2(run.settlement_account.current_balance - net_payable)
            run.settlement_account.save(update_fields=["current_balance", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="PAYROLL_RUN_APPROVE_AND_POST",
            resource_type="PayrollRunBatch",
            resource_id=run.run_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "run_number": run.run_number,
                "posted_journal_id": journal.entry_id,
                "total_expense_bdt": str(total_expense),
                "total_net_payable_bdt": str(net_payable),
            },
        )

        return cls._serialize_run(run, include_slips=True, actor_user=actor_user)

    # =========================================================================
    # 3. CANCEL PAYROLL RUN
    # =========================================================================

    @classmethod
    @transaction.atomic
    def cancel_payroll_run(
        cls,
        *,
        tenant_id: str,
        run_id: str,
        reason: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Cancels draft payroll run. If already posted to GL, raises validation error
        requiring explicit audited reversal.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        run = PayrollRunBatch.objects.select_for_update().filter(run_id=run_id, tenant=tenant).first()
        if not run:
            raise NotFound({"error_code": "ERR_PAYROLL_RUN_NOT_FOUND", "detail": f"Payroll run '{run_id}' not found."})

        if run.status == PayrollRunStatus.POSTED:
            raise ValidationError(
                {
                    "error_code": "ERR_POSTED_PAYROLL_CANNOT_CANCEL_DIRECTLY",
                    "detail": "Posted payroll run has posted journal lines. Cancellation requires formal financial reversal.",
                    "message_en": "Posted payroll cannot be cancelled directly without reversing the posted journal.",
                    "message_bn": "পোস্ট করা পেরোল সরাসরি বাতিল করা যাবে না; আর্থিক রিভার্সাল জার্নাল প্রয়োজন।",
                }
            )

        run.status = PayrollRunStatus.CANCELLED
        run.notes = f"{run.notes}\n[Cancelled] {reason}".strip()
        run.save(update_fields=["status", "notes", "updated_at"])

        run.slips.update(payment_status="CANCELLED")

        return cls._serialize_run(run, include_slips=False, actor_user=actor_user)

    # =========================================================================
    # 4. QUERY & RETRIEVAL
    # =========================================================================

    @classmethod
    def list_payroll_runs(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        status: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = PayrollRunBatch.objects.filter(tenant=tenant).select_related("branch", "settlement_account", "posted_journal")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if status:
            qs = qs.filter(status=status.upper())

        return [cls._serialize_run(r, include_slips=False, actor_user=actor_user) for r in qs]

    @classmethod
    def get_payroll_run(
        cls,
        *,
        tenant_id: str,
        run_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        run = PayrollRunBatch.objects.filter(run_id=run_id, tenant=tenant).select_related("branch", "settlement_account", "posted_journal").first()
        if not run:
            foreign = PayrollRunBatch.objects.filter(run_id=run_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant payroll access forbidden."})
            raise NotFound({"error_code": "ERR_PAYROLL_RUN_NOT_FOUND", "detail": f"Payroll run '{run_id}' not found."})

        return cls._serialize_run(run, include_slips=True, actor_user=actor_user)

    # =========================================================================
    # 5. SERIALIZATION WITH SALARY PRIVACY MASKING
    # =========================================================================

    @classmethod
    def _serialize_run(
        cls,
        run: PayrollRunBatch,
        include_slips: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        is_senior = False
        if actor_user and (getattr(actor_user, "is_superuser", False) or getattr(actor_user, "role", None) in PAYROLL_SENIOR_ROLES):
            is_senior = True

        data: Dict[str, Any] = {
            "run_id": run.run_id,
            "tenant_id": str(run.tenant_id),
            "branch_id": str(run.branch_id) if run.branch_id else None,
            "branch_name": run.branch.name if run.branch else "All Branches (Consolidated)",
            "run_number": run.run_number,
            "period_start": run.period_start.isoformat(),
            "period_end": run.period_end.isoformat(),
            "payment_date": run.payment_date.isoformat(),
            "status": run.status,
            "payment_method": run.payment_method,
            "total_gross_bdt": str(run.total_gross_bdt),
            "total_allowances_bdt": str(run.total_allowances_bdt),
            "total_bonuses_bdt": str(run.total_bonuses_bdt),
            "total_deductions_bdt": str(run.total_deductions_bdt),
            "total_net_payable_bdt": str(run.total_net_payable_bdt),
            "settlement_account_id": str(run.settlement_account_id) if run.settlement_account_id else None,
            "settlement_account_name": run.settlement_account.account_name if run.settlement_account else None,
            "posted_journal_id": str(run.posted_journal_id) if run.posted_journal_id else None,
            "approved_by_id": str(run.approved_by_id) if run.approved_by_id else None,
            "approved_at": run.approved_at.isoformat() if run.approved_at else None,
            "notes": run.notes,
            "is_confidential": run.is_confidential,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

        if include_slips:
            slips_data = []
            for slip in run.slips.all():
                # Enforce employee salary privacy:
                # If actor is not a senior role, bank details are strictly masked
                bank_acc = slip.bank_account_no if is_senior else mask_bank_account(slip.bank_account_no)

                slips_data.append(
                    {
                        "slip_id": slip.slip_id,
                        "staff_member_id": str(slip.staff_member_id),
                        "staff_name": slip.staff_name,
                        "staff_role": slip.staff_role,
                        "base_salary_bdt": str(slip.base_salary_bdt),
                        "allowances_bdt": str(slip.allowances_bdt),
                        "bonus_bdt": str(slip.bonus_bdt),
                        "deductions_bdt": str(slip.deductions_bdt),
                        "net_salary_bdt": str(slip.net_salary_bdt),
                        "bank_name": slip.bank_name,
                        "bank_account_no": bank_acc,
                        "payment_status": slip.payment_status,
                    }
                )
            data["slips"] = slips_data
            data["slips_count"] = len(slips_data)

        return data
