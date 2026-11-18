"""
backend/apps/core/operating_expense_service.py
Architecture Decision: ADR-0287 (P287) & ADR-0289 (P289).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Operational Expense Entries:
- Draft creation, receipt upload attachment, payment method, payee details, and approval lifecycle.
- Approval triggers balanced double-entry General Ledger journal entry (Dr OpEx GL 6000s, Cr Payment Asset 1000s/2010).
- High-value (>= 5,000 BDT) and reimbursable expense governance requiring elevated Owner/Finance sign-off.
- Self-approval protection preventing claimants from approving their own reimbursement requests.
- Reimbursement payout and settlement workflow.
- Strict immutability of approved vouchers and audited correction events (Full Reversal, Amount Adjustment, Reclassification).
- Multi-branch scoping, tax deductibility, and strict multi-tenant isolation.
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
    Branch,
    ChartOfAccount,
    ExpenseCategory,
    ExpenseCorrectionEvent,
    ExpenseCorrectionType,
    ExpensePaymentMethod,
    JournalEntry,
    JournalSourceModule,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    ReimbursementStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")
HIGH_VALUE_THRESHOLD = Decimal("5000.00")

EXPENSE_VIEW_ROLES = {
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

EXPENSE_CREATE_ROLES = {
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

EXPENSE_APPROVE_ROLES = {
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

SENIOR_APPROVE_ROLES = {
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

PAYMENT_METHOD_CREDIT_ACCOUNT_MAP = {
    ExpensePaymentMethod.CASH: "1010",
    ExpensePaymentMethod.BANK_TRANSFER: "1020",
    ExpensePaymentMethod.BKASH: "1020",
    ExpensePaymentMethod.NAGAD: "1020",
    ExpensePaymentMethod.ROCKET: "1020",
    ExpensePaymentMethod.CHEQUE: "1020",
    ExpensePaymentMethod.CREDIT_ACCRUAL: "2010",
    "CASH": "1010",
    "BANK_TRANSFER": "1020",
    "BKASH": "1020",
    "NAGAD": "1020",
    "ROCKET": "1020",
    "CHEQUE": "1020",
    "CREDIT_ACCRUAL": "2010",
}


class OperatingExpenseService:
    """
    Authoritative domain service governing Operational Expense entries, approvals, reimbursements, and corrections.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in EXPENSE_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and counter staff are not authorized to access operating expenses.",
                    "message_en": "Cashier and counter staff are not authorized to access operating expenses.",
                    "message_bn": "ক্যাশিয়ার বা কাউন্টার স্টাফ অপারেটিং খরচ অ্যাক্সেস করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_create_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in EXPENSE_CREATE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_EXPENSE_CREATE_UNAUTHORIZED",
                    "detail": "Cashier is not authorized to create operating expense entries.",
                    "message_en": "Cashier is not authorized to create operating expense entries.",
                    "message_bn": "ক্যাশিয়ার অপারেটিং খরচ তৈরি করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_approve_authorized(cls, actor_user: Any, expense: Optional[OperatingExpenseEntry] = None) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in EXPENSE_APPROVE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_EXPENSE_APPROVE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner, Finance Officer or Branch Manager can approve operating expenses.",
                    "message_en": "Only Pharmacy Owner, Finance Officer or Branch Manager can approve operating expenses.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার, ফিন্যান্স অফিসার বা ব্রাঞ্চ ম্যানেজার খরচ অনুমোদন করতে পারেন।",
                }
            )

        if expense:
            # High-value or Reimbursable check requires Senior Approver
            is_high_val = expense.is_high_value or (q2(expense.amount_bdt) >= HIGH_VALUE_THRESHOLD)
            if (is_high_val or expense.is_reimbursable) and role not in SENIOR_APPROVE_ROLES:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_HIGH_VALUE_APPROVAL_REQUIRED",
                        "detail": "High-value (>= 5,000 BDT) or reimbursable expenses require Pharmacy Owner or Finance Officer approval.",
                        "message_en": "High-value (>= 5,000 BDT) or reimbursable expenses require Pharmacy Owner or Finance Officer approval.",
                        "message_bn": "উচ্চমূল্যের (>= ৫,০০০ টাকা) বা রিইম্বার্সমেন্ট খরচ অনুমোদনের জন্য ওনার বা ফিন্যান্স অফিসারের অনুমোদন আবশ্যক।",
                    }
                )

            # Self-approval guard: Claimant cannot approve their own reimbursable expense
            if expense.is_reimbursable and expense.claimant_user:
                actor_id = getattr(actor_user, "id", None) or getattr(actor_user, "user_id", None)
                claimant_id = getattr(expense.claimant_user, "id", None) or getattr(expense.claimant_user, "user_id", None)
                if actor_id and claimant_id and str(actor_id) == str(claimant_id):
                    raise PermissionDenied(
                        {
                            "error_code": "ERR_SELF_APPROVAL_FORBIDDEN",
                            "detail": "Claimants cannot approve their own reimbursement claims.",
                            "message_en": "Claimants cannot approve their own reimbursement claims.",
                            "message_bn": "দাবিকারক নিজে নিজের রিইম্বার্সমেন্ট ক্লেইম অনুমোদন করতে পারবেন না।",
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
                    "detail": "Tenant account subscription is suspended. Operating expense operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Operating expense operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। খরচ অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def create_expense_draft(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Creates a new draft operating expense voucher.
        """
        cls.assert_create_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        # 1. Branch validation
        branch_id = data.get("branch_id")
        if not branch_id:
            branch = Branch.objects.filter(tenant=tenant).first()
            if not branch:
                raise ValidationError({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": "Active branch required."})
        else:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found in tenant."})

        # 2. Category validation
        category_id = data.get("category_id")
        if not category_id:
            raise ValidationError({"error_code": "ERR_MISSING_CATEGORY", "detail": "Expense category_id is required."})
        category = ExpenseCategory.objects.filter(tenant=tenant, category_id=category_id).first()
        if not category:
            foreign_cat = ExpenseCategory.objects.filter(category_id=category_id).first()
            if foreign_cat and str(foreign_cat.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant category access forbidden."})
            raise NotFound({"error_code": "ERR_CATEGORY_NOT_FOUND", "detail": f"Expense category '{category_id}' not found."})

        # ADR-0291 (P291): Payroll Expense Boundary Enforcement
        # Individual staff salary payments must be processed via the dedicated Payroll Run module
        # to safeguard employee salary privacy and prevent confidential salary leaks in general expense lists.
        if category.code == "SALARIES" and not data.get("is_payroll_authorized"):
            raise ValidationError(
                {
                    "error_code": "ERR_PAYROLL_EXPENSE_BOUNDARY_ENFORCED",
                    "detail": "Individual employee payroll cannot be entered as ordinary operational expenses. Use the dedicated Payroll & Salary Management module to ensure employee privacy and proper statutory accounting.",
                    "message_en": "Individual employee payroll cannot be entered as ordinary operational expenses. Use the dedicated Payroll & Salary Management module to ensure employee privacy and proper statutory accounting.",
                    "message_bn": "কর্মচারীদের বেতন সাধারণ পরিচালন ব্যয় হিসেবে এন্ট্রি করা যাবে না। বেতনের তথ্যের গোপনীয়তা নিশ্চিত করতে ডেডিকেটেড পেরোল মডিউল ব্যবহার করুন।",
                }
            )

        # 3. Payee validation
        payee_name = str(data.get("payee_name", "")).strip()
        if not payee_name:
            raise ValidationError({"error_code": "ERR_MISSING_PAYEE", "detail": "Payee name is required."})

        # 4. Amount validation
        amount_raw = data.get("amount_bdt")
        if amount_raw is None:
            raise ValidationError({"error_code": "ERR_MISSING_AMOUNT", "detail": "Expense amount is required."})
        try:
            amount = q2(Decimal(str(amount_raw)))
            if amount <= DECIMAL_ZERO:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {"error_code": "ERR_INVALID_EXPENSE_AMOUNT", "detail": "Expense amount must be a positive decimal number."}
            )

        # 5. Payment method
        method = str(data.get("payment_method", ExpensePaymentMethod.CASH)).upper().strip()
        if method not in ExpensePaymentMethod.values:
            method = ExpensePaymentMethod.CASH

        # 6. Expense date
        expense_date_raw = data.get("expense_date")
        expense_date = timezone.now().date()
        if expense_date_raw:
            try:
                if isinstance(expense_date_raw, str):
                    expense_date = datetime.date.fromisoformat(expense_date_raw)
                elif isinstance(expense_date_raw, datetime.date):
                    expense_date = expense_date_raw
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_DATE", "detail": "Invalid expense date format (YYYY-MM-DD)."})

        # ADR-0292: Enforce Financial Period lock on expense draft creation
        DoubleEntryJournalService.assert_period_open(tenant, expense_date)

        # 7. Reimbursable & High-Value Attributes
        is_reimbursable = bool(data.get("is_reimbursable", False))
        claimant_name = str(data.get("claimant_name", "")).strip()
        if is_reimbursable and not claimant_name:
            claimant_name = getattr(actor_user, "get_full_name", lambda: "")() or str(getattr(actor_user, "email", "Staff Claimant"))

        claimant_user = None
        if is_reimbursable:
            if getattr(actor_user, "is_authenticated", False):
                claimant_user = actor_user

        is_high_value = bool(data.get("is_high_value", False)) or (amount >= HIGH_VALUE_THRESHOLD)
        reimbursement_status = ReimbursementStatus.PENDING_REVIEW if is_reimbursable else ReimbursementStatus.NOT_APPLICABLE

        # 8. Generate Voucher Number
        now_dt = timezone.now()
        prefix = f"EXP-{now_dt.strftime('%Y%m')}"
        random_code = uuid.uuid4().hex[:6].upper()
        voucher_number = f"{prefix}-{random_code}"

        expense = OperatingExpenseEntry.objects.create(
            expense_id=f"opexp-{uuid.uuid4().hex[:12]}",
            voucher_number=voucher_number,
            tenant=tenant,
            branch=branch,
            category=category,
            payee_name=payee_name,
            payee_phone=str(data.get("payee_phone", "")).strip(),
            expense_date=expense_date,
            amount_bdt=amount,
            payment_method=method,
            payment_reference=str(data.get("payment_reference", "")).strip(),
            status=OperatingExpenseStatus.DRAFT,
            receipt_file_url=str(data.get("receipt_file_url", "")).strip(),
            receipt_document_id=str(data.get("receipt_document_id", "")).strip(),
            notes=str(data.get("notes", "")).strip(),
            tax_deductible=bool(data.get("tax_deductible", True)),
            is_reimbursable=is_reimbursable,
            claimant_name=claimant_name,
            claimant_user=claimant_user,
            reimbursement_status=reimbursement_status,
            is_high_value=is_high_value,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="OPERATING_EXPENSE_CREATE_DRAFT",
            resource_type="OperatingExpenseEntry",
            resource_id=expense.expense_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "voucher_number": expense.voucher_number,
                "amount": str(expense.amount_bdt),
                "is_reimbursable": is_reimbursable,
                "is_high_value": is_high_value,
            },
        )

        return cls._serialize_expense(expense)

    @classmethod
    @transaction.atomic
    def update_expense_draft(
        cls,
        *,
        tenant_id: str,
        expense_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Updates an existing draft operating expense voucher.
        Guarantees that approved and reversed vouchers cannot be modified directly.
        """
        cls.assert_create_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        expense = OperatingExpenseEntry.objects.select_for_update().filter(expense_id=expense_id, tenant=tenant).first()
        if not expense:
            foreign = OperatingExpenseEntry.objects.filter(expense_id=expense_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_EXPENSE_NOT_FOUND", "detail": f"Expense voucher '{expense_id}' not found."})

        if expense.status in (OperatingExpenseStatus.APPROVED, OperatingExpenseStatus.REVERSED):
            raise ValidationError(
                {
                    "error_code": "ERR_EXPENSE_IMMUTABLE",
                    "detail": "Approved or reversed expense vouchers are strictly immutable. Use correction events for audited modifications.",
                    "message_en": "Approved or reversed expense vouchers are strictly immutable.",
                    "message_bn": "অনুমোদিত বা বাতিল খরচ ভাউচার সরাসরি পরিবর্তন করা সম্ভব নয়। অডিট সংশোধনের জন্য কারেকশন ইভেন্ট ব্যবহার করুন।",
                }
            )

        if "payee_name" in data:
            new_payee = str(data["payee_name"]).strip()
            if not new_payee:
                raise ValidationError({"error_code": "ERR_MISSING_PAYEE", "detail": "Payee name cannot be empty."})
            expense.payee_name = new_payee

        if "payee_phone" in data:
            expense.payee_phone = str(data["payee_phone"]).strip()

        if "amount_bdt" in data:
            try:
                amt = q2(Decimal(str(data["amount_bdt"])))
                if amt <= DECIMAL_ZERO:
                    raise ValueError()
                expense.amount_bdt = amt
                expense.is_high_value = bool(data.get("is_high_value", False)) or (amt >= HIGH_VALUE_THRESHOLD)
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_EXPENSE_AMOUNT", "detail": "Amount must be a positive decimal."})

        if "payment_method" in data:
            m = str(data["payment_method"]).upper().strip()
            if m in ExpensePaymentMethod.values:
                expense.payment_method = m

        if "payment_reference" in data:
            expense.payment_reference = str(data["payment_reference"]).strip()
        if "notes" in data:
            expense.notes = str(data["notes"]).strip()
        if "receipt_file_url" in data:
            expense.receipt_file_url = str(data["receipt_file_url"]).strip()
        if "receipt_document_id" in data:
            expense.receipt_document_id = str(data["receipt_document_id"]).strip()
        if "tax_deductible" in data:
            expense.tax_deductible = bool(data["tax_deductible"])

        if "is_reimbursable" in data:
            expense.is_reimbursable = bool(data["is_reimbursable"])
            if expense.is_reimbursable and expense.reimbursement_status == ReimbursementStatus.NOT_APPLICABLE:
                expense.reimbursement_status = ReimbursementStatus.PENDING_REVIEW

        if "claimant_name" in data:
            expense.claimant_name = str(data["claimant_name"]).strip()

        if "category_id" in data:
            new_cat = ExpenseCategory.objects.filter(tenant=tenant, category_id=data["category_id"]).first()
            if new_cat:
                if new_cat.code == "SALARIES" and not data.get("is_payroll_authorized"):
                    raise ValidationError(
                        {
                            "error_code": "ERR_PAYROLL_EXPENSE_BOUNDARY_ENFORCED",
                            "detail": "Individual employee payroll cannot be entered as ordinary operational expenses. Use the dedicated Payroll & Salary Management module to ensure employee privacy and proper statutory accounting.",
                            "message_en": "Individual employee payroll cannot be entered as ordinary operational expenses. Use the dedicated Payroll & Salary Management module to ensure employee privacy and proper statutory accounting.",
                            "message_bn": "কর্মচারীদের বেতন সাধারণ পরিচালন ব্যয় হিসেবে এন্ট্রি করা যাবে না। বেতনের তথ্যের গোপনীয়তা নিশ্চিত করতে ডেডিকেটেড পেরোল মডিউল ব্যবহার করুন।",
                        }
                    )
                expense.category = new_cat

        expense.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="OPERATING_EXPENSE_UPDATE_DRAFT",
            resource_type="OperatingExpenseEntry",
            resource_id=expense.expense_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"voucher_number": expense.voucher_number, "new_amount": str(expense.amount_bdt)},
        )

        return cls._serialize_expense(expense)

    @classmethod
    @transaction.atomic
    def delete_expense_draft(
        cls,
        *,
        tenant_id: str,
        expense_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Deletes a draft or rejected expense voucher.
        Strictly forbids deleting posted/approved vouchers.
        """
        cls.assert_create_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        expense = OperatingExpenseEntry.objects.select_for_update().filter(expense_id=expense_id, tenant=tenant).first()
        if not expense:
            foreign = OperatingExpenseEntry.objects.filter(expense_id=expense_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_EXPENSE_NOT_FOUND", "detail": f"Expense voucher '{expense_id}' not found."})

        if expense.status in (OperatingExpenseStatus.APPROVED, OperatingExpenseStatus.REVERSED):
            raise ValidationError(
                {
                    "error_code": "ERR_EXPENSE_IMMUTABLE",
                    "detail": "Approved or reversed expense vouchers cannot be deleted. Use correction events for audited reversals.",
                    "message_en": "Approved or reversed expense vouchers cannot be deleted.",
                    "message_bn": "অনুমোদিত বা বাতিল খরচ ভাউচার মুছে ফেলা সম্ভব নয়।",
                }
            )

        voucher_no = expense.voucher_number
        expense.delete()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="OPERATING_EXPENSE_DELETE_DRAFT",
            resource_type="OperatingExpenseEntry",
            resource_id=expense_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"voucher_number": voucher_no},
        )

        return {"success": True, "message": f"Draft expense voucher '{voucher_no}' deleted successfully."}

    @classmethod
    @transaction.atomic
    def approve_and_post_expense(
        cls,
        *,
        tenant_id: str,
        expense_id: str,
        approval_notes: str = "",
        budget_override: bool = False,
        budget_override_reason: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Approves an expense voucher and atomically posts balanced double-entry General Ledger journal lines:
        - Debit: Operating Expense GL Account (e.g. 6010 Rent, 6020 Electricity, 6030 Salaries)
        - Credit: Payment Account (1010 Cash, 1020 Bank/MFS, or 2010 Accrual)
        Enforces higher-tier approval for high-value or reimbursable expenses, prevents self-approval,
        and enforces active P296 Expense Budget Controls before posting.
        """
        tenant = cls.assert_tenant_active(tenant_id)

        expense = OperatingExpenseEntry.objects.select_for_update().filter(expense_id=expense_id, tenant=tenant).first()
        if not expense:
            foreign = OperatingExpenseEntry.objects.filter(expense_id=expense_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_EXPENSE_NOT_FOUND", "detail": f"Expense voucher '{expense_id}' not found."})

        # Role & Rule verification against this specific expense
        cls.assert_approve_authorized(actor_user, expense)

        if expense.status in (OperatingExpenseStatus.APPROVED, OperatingExpenseStatus.REVERSED):
            raise ValidationError(
                {
                    "error_code": "ERR_EXPENSE_ALREADY_APPROVED",
                    "detail": "This expense voucher has already been approved and posted.",
                    "message_en": "This expense voucher has already been approved and posted.",
                    "message_bn": "এই খরচ ভাউচারটি ইতোমধ্যে অনুমোদিত এবং পোস্ট করা হয়েছে।",
                }
            )

        # ADR-0296 (P296): Enforce active planned expense budgets, thresholds, and override governance
        from apps.core.expense_budget_service import ExpenseBudgetService

        budget_evaluation = ExpenseBudgetService.enforce_budgets_for_approval(
            tenant=tenant,
            expense=expense,
            actor_user=actor_user,
            budget_override=bool(budget_override),
            budget_override_reason=str(budget_override_reason or ""),
        )

        amount = q2(expense.amount_bdt)
        category = expense.category

        # 1. Determine Debit GL Account (Expense Account)
        debit_gl_code = category.gl_account.account_code if category.gl_account else category.default_gl_code

        # 2. Determine Credit GL Account (Cash 1010, Bank 1020, Accrual 2010)
        credit_gl_code = PAYMENT_METHOD_CREDIT_ACCOUNT_MAP.get(expense.payment_method, "1010")

        # 3. Post Balanced Double-Entry Journal Entry
        journal_lines = [
            {
                "account_code": debit_gl_code,
                "debit_amount_bdt": amount,
                "credit_amount_bdt": DECIMAL_ZERO,
                "description": f"OpEx {category.name} ({expense.voucher_number}) paid to {expense.payee_name}",
            },
            {
                "account_code": credit_gl_code,
                "debit_amount_bdt": DECIMAL_ZERO,
                "credit_amount_bdt": amount,
                "description": f"Settlement via {expense.payment_method} for {expense.voucher_number}",
            },
        ]

        journal_res = DoubleEntryJournalService.post_journal_entry(
            tenant_id=str(tenant.tenant_id),
            lines=journal_lines,
            posting_date=expense.expense_date,
            source_module=JournalSourceModule.OPERATING_EXPENSE,
            source_reference=expense.voucher_number,
            narration=f"Operating Expense Voucher {expense.voucher_number}: {category.name} [{expense.payee_name}]",
            branch_id=str(expense.branch_id),
            actor_user=actor_user,
        )

        journal = JournalEntry.objects.get(entry_id=journal_res["entry_id"])

        expense.status = OperatingExpenseStatus.APPROVED
        expense.approved_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        expense.approved_at = timezone.now()
        expense.approval_notes = approval_notes.strip()
        expense.posted_journal = journal

        # Advance reimbursement status if reimbursable
        if expense.is_reimbursable:
            expense.reimbursement_status = ReimbursementStatus.APPROVED_PENDING_PAYOUT

        expense.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="OPERATING_EXPENSE_APPROVE",
            resource_type="OperatingExpenseEntry",
            resource_id=expense.expense_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "voucher_number": expense.voucher_number,
                "amount": str(amount),
                "journal_number": journal.journal_number,
                "is_reimbursable": expense.is_reimbursable,
            },
        )

        return {
            "success": True,
            "expense_id": expense.expense_id,
            "voucher_number": expense.voucher_number,
            "status": expense.status,
            "posted_journal_id": str(journal.entry_id),
            "posted_journal_number": journal.journal_number,
            "reimbursement_status": expense.reimbursement_status,
            "budget_evaluation": budget_evaluation,
            "expense": cls._serialize_expense(expense),
        }

    @classmethod
    @transaction.atomic
    def reject_expense(
        cls,
        *,
        tenant_id: str,
        expense_id: str,
        rejection_reason: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Rejects an unposted expense voucher with mandatory audit reason.
        """
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(rejection_reason).strip()
        if not clean_reason:
            raise ValidationError({"error_code": "ERR_MISSING_REJECTION_REASON", "detail": "Mandatory rejection reason required."})

        expense = OperatingExpenseEntry.objects.select_for_update().filter(expense_id=expense_id, tenant=tenant).first()
        if not expense:
            foreign = OperatingExpenseEntry.objects.filter(expense_id=expense_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_EXPENSE_NOT_FOUND", "detail": f"Expense voucher '{expense_id}' not found."})

        cls.assert_approve_authorized(actor_user, expense)

        if expense.status in (OperatingExpenseStatus.APPROVED, OperatingExpenseStatus.REVERSED):
            raise ValidationError({"error_code": "ERR_CANNOT_REJECT_POSTED", "detail": "Cannot reject an already approved or posted expense."})

        expense.status = OperatingExpenseStatus.REJECTED
        expense.rejection_reason = clean_reason
        if expense.is_reimbursable:
            expense.reimbursement_status = ReimbursementStatus.REJECTED
        expense.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="OPERATING_EXPENSE_REJECT",
            resource_type="OperatingExpenseEntry",
            resource_id=expense.expense_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"voucher_number": expense.voucher_number, "reason": clean_reason},
        )

        return {"success": True, "expense_id": expense.expense_id, "status": expense.status, "reason": clean_reason}

    @classmethod
    @transaction.atomic
    def reimburse_expense(
        cls,
        *,
        tenant_id: str,
        expense_id: str,
        payout_method: str = "CASH",
        reference: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Disburses payout for an approved reimbursable operating expense.
        """
        cls.assert_approve_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        expense = OperatingExpenseEntry.objects.select_for_update().filter(expense_id=expense_id, tenant=tenant).first()
        if not expense:
            foreign = OperatingExpenseEntry.objects.filter(expense_id=expense_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_EXPENSE_NOT_FOUND", "detail": f"Expense voucher '{expense_id}' not found."})

        if not expense.is_reimbursable:
            raise ValidationError({"error_code": "ERR_NOT_REIMBURSABLE", "detail": "This expense is not marked as reimbursable."})

        if expense.status != OperatingExpenseStatus.APPROVED:
            raise ValidationError({"error_code": "ERR_EXPENSE_NOT_APPROVED", "detail": "Only approved expenses can be reimbursed."})

        if expense.reimbursement_status == ReimbursementStatus.PAID_OUT:
            raise ValidationError({"error_code": "ERR_ALREADY_REIMBURSED", "detail": "This expense claim has already been reimbursed."})

        method = str(payout_method).upper().strip()
        if method not in ExpensePaymentMethod.values:
            method = ExpensePaymentMethod.CASH

        expense.reimbursement_status = ReimbursementStatus.PAID_OUT
        expense.reimbursed_at = timezone.now()
        expense.reimbursed_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        expense.reimbursement_reference = reference.strip()
        expense.reimbursement_payment_method = method
        expense.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="OPERATING_EXPENSE_REIMBURSE",
            resource_type="OperatingExpenseEntry",
            resource_id=expense.expense_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"voucher_number": expense.voucher_number, "payout_method": method, "amount": str(expense.amount_bdt)},
        )

        return cls._serialize_expense(expense)

    @classmethod
    @transaction.atomic
    def correct_expense(
        cls,
        *,
        tenant_id: str,
        expense_id: str,
        correction_type: str,
        reason: str,
        corrected_amount_bdt: Optional[Decimal] = None,
        corrected_category_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Executes an audited correction or reversal on an approved expense voucher:
        - FULL_REVERSAL: Reverses original journal entry (Dr Asset/Payable, Cr OpEx 6000s), marks voucher REVERSED.
        - AMOUNT_ADJUSTMENT: Adjusts voucher amount by delta and posts adjusting journal.
        - CATEGORY_RECLASSIFICATION: Reclassifies expense category and posts Dr New OpEx, Cr Old OpEx.
        """
        cls.assert_approve_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_reason = str(reason).strip()
        if len(clean_reason) < 5:
            raise ValidationError(
                {"error_code": "ERR_MANDATORY_CORRECTION_REASON", "detail": "A meaningful correction justification of at least 5 characters is required."}
            )

        expense = OperatingExpenseEntry.objects.select_for_update().filter(expense_id=expense_id, tenant=tenant).first()
        if not expense:
            foreign = OperatingExpenseEntry.objects.filter(expense_id=expense_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_EXPENSE_NOT_FOUND", "detail": f"Expense voucher '{expense_id}' not found."})

        cls.assert_approve_authorized(actor_user, expense)

        if expense.status != OperatingExpenseStatus.APPROVED:
            raise ValidationError({"error_code": "ERR_EXPENSE_NOT_APPROVED", "detail": "Only approved and posted expenses can undergo correction."})

        corr_type = str(correction_type).upper().strip()
        if corr_type not in ExpenseCorrectionType.values:
            raise ValidationError(
                {"error_code": "ERR_INVALID_CORRECTION_TYPE", "detail": f"Correction type must be one of {ExpenseCorrectionType.values}."}
            )

        original_amount = q2(expense.amount_bdt)
        original_category = expense.category
        diff = DECIMAL_ZERO
        reversal_journal = None

        if corr_type == ExpenseCorrectionType.FULL_REVERSAL:
            # Reversal Journal: Dr Payment Asset / Payable, Cr OpEx GL (6000s)
            debit_account = PAYMENT_METHOD_CREDIT_ACCOUNT_MAP.get(expense.payment_method, "1010")
            credit_account = original_category.gl_account.account_code if original_category.gl_account else original_category.default_gl_code

            journal_lines = [
                {
                    "account_code": debit_account,
                    "debit_amount_bdt": original_amount,
                    "credit_amount_bdt": DECIMAL_ZERO,
                    "description": f"Reversal of payment for voided OpEx {expense.voucher_number}",
                },
                {
                    "account_code": credit_account,
                    "debit_amount_bdt": DECIMAL_ZERO,
                    "credit_amount_bdt": original_amount,
                    "description": f"Credit reversal of OpEx {original_category.name} ({expense.voucher_number})",
                },
            ]

            j_res = DoubleEntryJournalService.post_journal_entry(
                tenant_id=str(tenant.tenant_id),
                lines=journal_lines,
                posting_date=timezone.now().date(),
                source_module=JournalSourceModule.OPERATING_EXPENSE,
                source_reference=f"REV-{expense.voucher_number}",
                narration=f"Full Reversal & Void of Operating Expense {expense.voucher_number}: {clean_reason}",
                branch_id=str(expense.branch_id),
                actor_user=actor_user,
            )
            reversal_journal = JournalEntry.objects.get(entry_id=j_res["entry_id"])

            expense.status = OperatingExpenseStatus.REVERSED
            if expense.is_reimbursable and expense.reimbursement_status != ReimbursementStatus.PAID_OUT:
                expense.reimbursement_status = ReimbursementStatus.REJECTED
            expense.notes = f"{expense.notes} [REVERSED: {clean_reason}]".strip()
            expense.save()
            new_amount = original_amount
            new_category = original_category

        elif corr_type == ExpenseCorrectionType.AMOUNT_ADJUSTMENT:
            if corrected_amount_bdt is None:
                raise ValidationError({"error_code": "ERR_MISSING_CORRECTED_AMOUNT", "detail": "Corrected amount is required for AMOUNT_ADJUSTMENT."})
            new_amount = q2(Decimal(str(corrected_amount_bdt)))
            if new_amount <= DECIMAL_ZERO:
                raise ValidationError({"error_code": "ERR_INVALID_EXPENSE_AMOUNT", "detail": "Corrected amount must be positive."})
            if new_amount == original_amount:
                raise ValidationError({"error_code": "ERR_AMOUNT_UNCHANGED", "detail": "Corrected amount must differ from original amount."})

            diff = q2(new_amount - original_amount)
            expense_gl = original_category.gl_account.account_code if original_category.gl_account else original_category.default_gl_code
            payment_gl = PAYMENT_METHOD_CREDIT_ACCOUNT_MAP.get(expense.payment_method, "1010")

            if diff > DECIMAL_ZERO:
                # Additional expense: Dr OpEx GL, Cr Payment Asset
                journal_lines = [
                    {"account_code": expense_gl, "debit_amount_bdt": diff, "credit_amount_bdt": DECIMAL_ZERO, "description": f"Upward adjustment for {expense.voucher_number}"},
                    {"account_code": payment_gl, "debit_amount_bdt": DECIMAL_ZERO, "credit_amount_bdt": diff, "description": f"Adjustment payout for {expense.voucher_number}"},
                ]
            else:
                # Downward expense reduction: Dr Payment Asset, Cr OpEx GL
                abs_diff = abs(diff)
                journal_lines = [
                    {"account_code": payment_gl, "debit_amount_bdt": abs_diff, "credit_amount_bdt": DECIMAL_ZERO, "description": f"Downward adjustment refund for {expense.voucher_number}"},
                    {"account_code": expense_gl, "debit_amount_bdt": DECIMAL_ZERO, "credit_amount_bdt": abs_diff, "description": f"OpEx reduction for {expense.voucher_number}"},
                ]

            j_res = DoubleEntryJournalService.post_journal_entry(
                tenant_id=str(tenant.tenant_id),
                lines=journal_lines,
                posting_date=timezone.now().date(),
                source_module=JournalSourceModule.OPERATING_EXPENSE,
                source_reference=f"ADJ-{expense.voucher_number}",
                narration=f"Amount Adjustment for OpEx {expense.voucher_number} (Delta: {diff} BDT): {clean_reason}",
                branch_id=str(expense.branch_id),
                actor_user=actor_user,
            )
            reversal_journal = JournalEntry.objects.get(entry_id=j_res["entry_id"])

            expense.amount_bdt = new_amount
            expense.notes = f"{expense.notes} [ADJUSTED: {original_amount} -> {new_amount} ({clean_reason})]".strip()
            expense.save()
            new_category = original_category

        elif corr_type == ExpenseCorrectionType.CATEGORY_RECLASSIFICATION:
            if not corrected_category_id:
                raise ValidationError({"error_code": "ERR_MISSING_CORRECTED_CATEGORY", "detail": "Corrected category is required for CATEGORY_RECLASSIFICATION."})
            new_cat = ExpenseCategory.objects.filter(tenant=tenant, category_id=corrected_category_id).first()
            if not new_cat:
                raise ValidationError({"error_code": "ERR_CATEGORY_NOT_FOUND", "detail": "Target reclassification category not found."})
            if new_cat.category_id == original_category.category_id:
                raise ValidationError({"error_code": "ERR_CATEGORY_UNCHANGED", "detail": "New category must differ from original category."})

            old_gl = original_category.gl_account.account_code if original_category.gl_account else original_category.default_gl_code
            new_gl = new_cat.gl_account.account_code if new_cat.gl_account else new_cat.default_gl_code

            # Reclassification journal: Dr New OpEx GL, Cr Old OpEx GL
            journal_lines = [
                {"account_code": new_gl, "debit_amount_bdt": original_amount, "credit_amount_bdt": DECIMAL_ZERO, "description": f"Reclassified OpEx to {new_cat.name} ({expense.voucher_number})"},
                {"account_code": old_gl, "debit_amount_bdt": DECIMAL_ZERO, "credit_amount_bdt": original_amount, "description": f"Reclassified OpEx from {original_category.name} ({expense.voucher_number})"},
            ]

            j_res = DoubleEntryJournalService.post_journal_entry(
                tenant_id=str(tenant.tenant_id),
                lines=journal_lines,
                posting_date=timezone.now().date(),
                source_module=JournalSourceModule.OPERATING_EXPENSE,
                source_reference=f"RECLASS-{expense.voucher_number}",
                narration=f"Category Reclassification of OpEx {expense.voucher_number} ({original_category.name} -> {new_cat.name}): {clean_reason}",
                branch_id=str(expense.branch_id),
                actor_user=actor_user,
            )
            reversal_journal = JournalEntry.objects.get(entry_id=j_res["entry_id"])

            expense.category = new_cat
            expense.notes = f"{expense.notes} [RECLASSIFIED: {original_category.name} -> {new_cat.name} ({clean_reason})]".strip()
            expense.save()
            new_amount = original_amount
            new_category = new_cat

        event = ExpenseCorrectionEvent.objects.create(
            tenant=tenant,
            branch=expense.branch,
            expense=expense,
            correction_type=corr_type,
            original_amount_bdt=original_amount,
            corrected_amount_bdt=new_amount,
            difference_bdt=diff,
            original_category=original_category,
            corrected_category=new_category,
            reason=clean_reason,
            reversal_journal=reversal_journal,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="OPERATING_EXPENSE_CORRECTION",
            resource_type="ExpenseCorrectionEvent",
            resource_id=event.correction_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"voucher_number": expense.voucher_number, "type": corr_type, "diff": str(diff)},
        )

        return {
            "success": True,
            "correction_id": event.correction_id,
            "correction_type": event.correction_type,
            "original_amount_bdt": str(event.original_amount_bdt),
            "corrected_amount_bdt": str(event.corrected_amount_bdt),
            "difference_bdt": str(event.difference_bdt),
            "reason": event.reason,
            "reversal_journal_id": reversal_journal.entry_id if reversal_journal else None,
            "expense": cls._serialize_expense(expense),
        }

    @classmethod
    def list_expenses(
        cls,
        *,
        tenant_id: str,
        status_param: Optional[str] = None,
        category_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        is_reimbursable: Optional[bool] = None,
        is_high_value: Optional[bool] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Lists expenses filtered by status, category, branch, reimbursable and high-value flags.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = OperatingExpenseEntry.objects.filter(tenant=tenant).select_related(
            "branch", "category", "posted_journal", "claimant_user"
        )
        if status_param:
            qs = qs.filter(status=status_param.upper().strip())
        if category_id:
            qs = qs.filter(category__category_id=category_id)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if is_reimbursable is not None:
            qs = qs.filter(is_reimbursable=is_reimbursable)
        if is_high_value is not None:
            qs = qs.filter(is_high_value=is_high_value)

        return [cls._serialize_expense(e) for e in qs[:200]]

    @classmethod
    def get_expense(
        cls,
        *,
        tenant_id: str,
        expense_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Retrieves single operating expense voucher.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        expense = OperatingExpenseEntry.objects.filter(expense_id=expense_id, tenant=tenant).select_related(
            "branch", "category", "posted_journal", "claimant_user"
        ).first()
        if not expense:
            foreign = OperatingExpenseEntry.objects.filter(expense_id=expense_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_EXPENSE_NOT_FOUND", "detail": f"Expense voucher '{expense_id}' not found."})

        return cls._serialize_expense(expense)

    @classmethod
    def _serialize_expense(cls, e: OperatingExpenseEntry) -> Dict[str, Any]:
        return {
            "expense_id": e.expense_id,
            "voucher_number": e.voucher_number,
            "tenant_id": str(e.tenant_id),
            "branch_id": str(e.branch_id),
            "branch_name": e.branch.name if e.branch else "",
            "category_id": str(e.category_id),
            "category_name": e.category.name if e.category else "",
            "category_code": e.category.code if e.category else "",
            "payee_name": e.payee_name,
            "payee_phone": e.payee_phone,
            "expense_date": e.expense_date.isoformat(),
            "amount_bdt": str(e.amount_bdt),
            "payment_method": e.payment_method,
            "payment_reference": e.payment_reference,
            "status": e.status,
            "receipt_file_url": e.receipt_file_url,
            "receipt_document_id": e.receipt_document_id,
            "notes": e.notes,
            "tax_deductible": e.tax_deductible,
            "is_reimbursable": e.is_reimbursable,
            "claimant_name": e.claimant_name,
            "claimant_user_id": str(e.claimant_user_id) if e.claimant_user_id else None,
            "reimbursement_status": e.reimbursement_status,
            "reimbursed_at": e.reimbursed_at.isoformat() if e.reimbursed_at else None,
            "reimbursed_by": str(e.reimbursed_by.email) if e.reimbursed_by else None,
            "reimbursement_reference": e.reimbursement_reference,
            "reimbursement_payment_method": e.reimbursement_payment_method,
            "is_high_value": e.is_high_value,
            "approved_by": str(e.approved_by.email) if e.approved_by else None,
            "approved_at": e.approved_at.isoformat() if e.approved_at else None,
            "approval_notes": e.approval_notes,
            "rejection_reason": e.rejection_reason,
            "posted_journal_id": str(e.posted_journal_id) if e.posted_journal_id else None,
            "posted_journal_number": e.posted_journal.journal_number if e.posted_journal else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        }
