"""
backend/apps/core/recurring_expense_service.py
Architecture Decision: ADR-0288 (P288).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Recurring Operating Expenses:
- Template management for recurring rent, utilities, internet broadband, security, and administrative overheads.
- Automated or on-demand voucher generation with configurable auto-approval into General Ledger.
- Frequency scheduling (Daily, Weekly, Monthly, Quarterly, Yearly) with next run date calculation.
- Multi-branch scoping, tax deductibility, and strict multi-tenant isolation.
"""

from __future__ import annotations

import calendar
import datetime
from decimal import Decimal
import logging
import uuid
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    Branch,
    ExpenseCategory,
    ExpensePaymentMethod,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    RecurringExpenseFrequency,
    RecurringExpenseTemplate,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.operating_expense_service import OperatingExpenseService
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

RECURRING_VIEW_ROLES = {
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

RECURRING_MANAGE_ROLES = {
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


def calculate_next_run_date(
    current_date: datetime.date,
    frequency: str,
    day_of_month: int = 1,
    day_of_week: int = 0,
) -> datetime.date:
    """
    Computes the next scheduled run date following the recurrence rules.
    """
    freq = frequency.upper().strip()
    if freq == RecurringExpenseFrequency.DAILY:
        return current_date + datetime.timedelta(days=1)
    elif freq == RecurringExpenseFrequency.WEEKLY:
        return current_date + datetime.timedelta(days=7)
    elif freq == RecurringExpenseFrequency.MONTHLY:
        # Move forward 1 month
        year = current_date.year + (1 if current_date.month == 12 else 0)
        month = 1 if current_date.month == 12 else current_date.month + 1
        max_days = calendar.monthrange(year, month)[1]
        target_day = min(day_of_month, max_days)
        return datetime.date(year, month, target_day)
    elif freq == RecurringExpenseFrequency.QUARTERLY:
        month = current_date.month + 3
        year = current_date.year + ((month - 1) // 12)
        month = ((month - 1) % 12) + 1
        max_days = calendar.monthrange(year, month)[1]
        target_day = min(day_of_month, max_days)
        return datetime.date(year, month, target_day)
    elif freq == RecurringExpenseFrequency.YEARLY:
        year = current_date.year + 1
        month = current_date.month
        max_days = calendar.monthrange(year, month)[1]
        target_day = min(day_of_month, max_days)
        return datetime.date(year, month, target_day)
    return current_date + datetime.timedelta(days=30)


class RecurringExpenseService:
    """
    Authoritative domain service governing Recurring Operating Expenses.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in RECURRING_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and counter staff are not authorized to access recurring expenses.",
                    "message_en": "Cashier and counter staff are not authorized to access recurring expenses.",
                    "message_bn": "ক্যাশিয়ার বা কাউন্টার স্টাফ রিকারিং খরচ অ্যাক্সেস করার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in RECURRING_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_RECURRING_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can create, modify, or trigger recurring expenses.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can create, modify, or trigger recurring expenses.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার রিকারিং খরচ পরিচালনা করতে পারেন।",
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
                    "detail": "Tenant account subscription is suspended. Recurring expense operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Recurring expense operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। রিকারিং খরচ অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def create_recurring_template(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Creates a new recurring operating expense template & schedule.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        template_name = str(data.get("template_name", "")).strip()
        if not template_name:
            raise ValidationError({"error_code": "ERR_MISSING_TEMPLATE_NAME", "detail": "Template name is required."})

        if RecurringExpenseTemplate.objects.filter(tenant=tenant, template_name__iexact=template_name).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_TEMPLATE_NAME",
                    "detail": f"A recurring expense template with name '{template_name}' already exists.",
                    "message_en": f"A recurring expense template with name '{template_name}' already exists.",
                    "message_bn": f"'{template_name}' নামের একটি রিকারিং টেমপ্লেট ইতিমধ্যে বিদ্যমান।",
                }
            )

        branch_id = data.get("branch_id")
        if not branch_id:
            branch = Branch.objects.filter(tenant=tenant).first()
            if not branch:
                raise ValidationError({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": "Branch required."})
        else:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        category_id = data.get("category_id")
        if not category_id:
            raise ValidationError({"error_code": "ERR_MISSING_CATEGORY", "detail": "Expense category_id is required."})
        category = ExpenseCategory.objects.filter(tenant=tenant, category_id=category_id).first()
        if not category:
            foreign = ExpenseCategory.objects.filter(category_id=category_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant category forbidden."})
            raise NotFound({"error_code": "ERR_CATEGORY_NOT_FOUND", "detail": f"Category '{category_id}' not found."})

        payee_name = str(data.get("payee_name", "")).strip()
        if not payee_name:
            raise ValidationError({"error_code": "ERR_MISSING_PAYEE", "detail": "Payee name is required."})

        amt_raw = data.get("estimated_amount_bdt")
        if amt_raw is None:
            raise ValidationError({"error_code": "ERR_MISSING_AMOUNT", "detail": "Estimated amount is required."})
        try:
            estimated_amount = q2(Decimal(str(amt_raw)))
            if estimated_amount <= DECIMAL_ZERO:
                raise ValueError()
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_EXPENSE_AMOUNT", "detail": "Amount must be positive."})

        freq = str(data.get("frequency", RecurringExpenseFrequency.MONTHLY)).upper().strip()
        if freq not in RecurringExpenseFrequency.values:
            freq = RecurringExpenseFrequency.MONTHLY

        day_of_month = int(data.get("day_of_month", 1))
        if not 1 <= day_of_month <= 31:
            day_of_month = 1

        day_of_week = int(data.get("day_of_week", 0))

        start_date = timezone.now().date()
        if data.get("start_date"):
            try:
                start_date = datetime.date.fromisoformat(str(data["start_date"]))
            except Exception:
                pass

        end_date = None
        if data.get("end_date"):
            try:
                end_date = datetime.date.fromisoformat(str(data["end_date"]))
            except Exception:
                pass

        next_run = start_date
        if data.get("next_run_date"):
            try:
                next_run = datetime.date.fromisoformat(str(data["next_run_date"]))
            except Exception:
                pass

        method = str(data.get("payment_method", ExpensePaymentMethod.CASH)).upper().strip()
        if method not in ExpensePaymentMethod.values:
            method = ExpensePaymentMethod.CASH

        template = RecurringExpenseTemplate.objects.create(
            template_id=f"rectpl-{uuid.uuid4().hex[:12]}",
            template_name=template_name,
            tenant=tenant,
            branch=branch,
            category=category,
            payee_name=payee_name,
            payee_phone=str(data.get("payee_phone", "")).strip(),
            estimated_amount_bdt=estimated_amount,
            frequency=freq,
            day_of_month=day_of_month,
            day_of_week=day_of_week,
            start_date=start_date,
            end_date=end_date,
            next_run_date=next_run,
            payment_method=method,
            payment_reference=str(data.get("payment_reference", "")).strip(),
            auto_approve=bool(data.get("auto_approve", False)),
            is_active=True,
            description=str(data.get("description", "")).strip(),
            tax_deductible=bool(data.get("tax_deductible", True)),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="RECURRING_EXPENSE_CREATE",
            resource_type="RecurringExpenseTemplate",
            resource_id=template.template_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "template_name": template.template_name,
                "frequency": template.frequency,
                "amount": str(template.estimated_amount_bdt),
            },
        )

        return cls._serialize_template(template)

    @classmethod
    @transaction.atomic
    def update_recurring_template(
        cls,
        *,
        tenant_id: str,
        template_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Updates an existing recurring operating expense template.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        template = RecurringExpenseTemplate.objects.select_for_update().filter(template_id=template_id, tenant=tenant).first()
        if not template:
            foreign = RecurringExpenseTemplate.objects.filter(template_id=template_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_TEMPLATE_NOT_FOUND", "detail": f"Template '{template_id}' not found."})

        if "template_name" in data:
            new_name = str(data["template_name"]).strip()
            if not new_name:
                raise ValidationError({"error_code": "ERR_MISSING_TEMPLATE_NAME", "detail": "Name cannot be empty."})
            if RecurringExpenseTemplate.objects.filter(tenant=tenant, template_name__iexact=new_name).exclude(template_id=template.template_id).exists():
                raise ValidationError({"error_code": "ERR_DUPLICATE_TEMPLATE_NAME", "detail": f"Name '{new_name}' exists."})
            template.template_name = new_name

        if "payee_name" in data:
            template.payee_name = str(data["payee_name"]).strip()
        if "payee_phone" in data:
            template.payee_phone = str(data["payee_phone"]).strip()

        if "estimated_amount_bdt" in data:
            try:
                amt = q2(Decimal(str(data["estimated_amount_bdt"])))
                if amt <= DECIMAL_ZERO:
                    raise ValueError()
                template.estimated_amount_bdt = amt
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_EXPENSE_AMOUNT", "detail": "Amount must be positive."})

        if "frequency" in data:
            f = str(data["frequency"]).upper().strip()
            if f in RecurringExpenseFrequency.values:
                template.frequency = f

        if "day_of_month" in data:
            dom = int(data["day_of_month"])
            if 1 <= dom <= 31:
                template.day_of_month = dom

        if "auto_approve" in data:
            template.auto_approve = bool(data["auto_approve"])
        if "is_active" in data:
            template.is_active = bool(data["is_active"])
        if "description" in data:
            template.description = str(data["description"]).strip()
        if "tax_deductible" in data:
            template.tax_deductible = bool(data["tax_deductible"])

        if "payment_method" in data:
            pm = str(data["payment_method"]).upper().strip()
            if pm in ExpensePaymentMethod.values:
                template.payment_method = pm

        if "next_run_date" in data:
            try:
                template.next_run_date = datetime.date.fromisoformat(str(data["next_run_date"]))
            except Exception:
                pass

        template.updated_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        template.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="RECURRING_EXPENSE_UPDATE",
            resource_type="RecurringExpenseTemplate",
            resource_id=template.template_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"template_name": template.template_name, "is_active": template.is_active},
        )

        return cls._serialize_template(template)

    @classmethod
    @transaction.atomic
    def archive_recurring_template(
        cls,
        *,
        tenant_id: str,
        template_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Soft-archives a recurring expense template (sets is_active=False).
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        template = RecurringExpenseTemplate.objects.select_for_update().filter(template_id=template_id, tenant=tenant).first()
        if not template:
            foreign = RecurringExpenseTemplate.objects.filter(template_id=template_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_TEMPLATE_NOT_FOUND", "detail": f"Template '{template_id}' not found."})

        template.is_active = False
        template.updated_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        template.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="RECURRING_EXPENSE_ARCHIVE",
            resource_type="RecurringExpenseTemplate",
            resource_id=template.template_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"template_id": template.template_id, "is_active": False},
        )

        return cls._serialize_template(template)

    @classmethod
    def list_recurring_templates(
        cls,
        *,
        tenant_id: str,
        is_active: Optional[bool] = None,
        branch_id: Optional[str] = None,
        category_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = RecurringExpenseTemplate.objects.filter(tenant=tenant).select_related("branch", "category")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)
        if category_id:
            qs = qs.filter(category__category_id=category_id)

        return [cls._serialize_template(t) for t in qs]

    @classmethod
    def get_recurring_template(
        cls,
        *,
        tenant_id: str,
        template_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        template = RecurringExpenseTemplate.objects.filter(template_id=template_id, tenant=tenant).select_related("branch", "category").first()
        if not template:
            foreign = RecurringExpenseTemplate.objects.filter(template_id=template_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_TEMPLATE_NOT_FOUND", "detail": f"Template '{template_id}' not found."})

        return cls._serialize_template(template)

    @classmethod
    @transaction.atomic
    def generate_due_expenses(
        cls,
        *,
        tenant_id: str,
        as_of_date: Optional[datetime.date] = None,
        template_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Generates OperatingExpenseEntry vouchers for active recurring templates whose next_run_date <= as_of_date.
        If auto_approve is True, automatically posts the balanced General Ledger journal entry!
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        cutoff_date = as_of_date or timezone.now().date()

        qs = RecurringExpenseTemplate.objects.select_for_update().filter(
            tenant=tenant,
            is_active=True,
            next_run_date__lte=cutoff_date,
        )
        if template_id:
            qs = qs.filter(template_id=template_id)

        generated_expenses: List[Dict[str, Any]] = []

        for template in qs:
            # Check end_date
            if template.end_date and template.next_run_date > template.end_date:
                template.is_active = False
                template.save()
                continue

            # 1. Create Operating Expense Entry Draft
            voucher_data = {
                "branch_id": template.branch.branch_id,
                "category_id": template.category.category_id,
                "payee_name": template.payee_name,
                "payee_phone": template.payee_phone,
                "amount_bdt": template.estimated_amount_bdt,
                "payment_method": template.payment_method,
                "payment_reference": template.payment_reference,
                "expense_date": template.next_run_date,
                "notes": f"Generated from recurring schedule: {template.template_name}",
                "tax_deductible": template.tax_deductible,
            }

            expense_res = OperatingExpenseService.create_expense_draft(
                tenant_id=str(tenant.tenant_id),
                data=voucher_data,
                actor_user=actor_user,
            )

            # 2. Auto-approve and post to GL if requested
            if template.auto_approve:
                expense_res = OperatingExpenseService.approve_and_post_expense(
                    tenant_id=str(tenant.tenant_id),
                    expense_id=expense_res["expense_id"],
                    approval_notes=f"Auto-approved via recurring schedule '{template.template_name}'",
                    actor_user=actor_user,
                )

            generated_expenses.append(expense_res)

            # 3. Advance next_run_date and record last_run_date
            template.last_run_date = template.next_run_date
            template.next_run_date = calculate_next_run_date(
                current_date=template.next_run_date,
                frequency=template.frequency,
                day_of_month=template.day_of_month,
                day_of_week=template.day_of_week,
            )
            template.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="RECURRING_EXPENSE_GENERATE_DUE",
            resource_type="OperatingExpenseEntry",
            resource_id=f"gen-{len(generated_expenses)}",
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"count": len(generated_expenses), "cutoff_date": cutoff_date.isoformat()},
        )

        return {
            "status": "SUCCESS",
            "count": len(generated_expenses),
            "generated_expenses": generated_expenses,
            "message_en": f"Generated {len(generated_expenses)} recurring operating expense vouchers.",
            "message_bn": f"{len(generated_expenses)}টি রিকারিং অপারেটিং খরচ ভাউচার সফলভাবে তৈরি করা হয়েছে।",
        }

    @classmethod
    def _serialize_template(cls, t: RecurringExpenseTemplate) -> Dict[str, Any]:
        return {
            "template_id": t.template_id,
            "template_name": t.template_name,
            "tenant_id": str(t.tenant_id),
            "branch_id": str(t.branch_id),
            "branch_name": t.branch.name if t.branch else "",
            "category_id": str(t.category_id),
            "category_name": t.category.name if t.category else "",
            "category_code": t.category.code if t.category else "",
            "payee_name": t.payee_name,
            "payee_phone": t.payee_phone,
            "estimated_amount_bdt": str(t.estimated_amount_bdt),
            "frequency": t.frequency,
            "day_of_month": t.day_of_month,
            "day_of_week": t.day_of_week,
            "start_date": t.start_date.isoformat(),
            "end_date": t.end_date.isoformat() if t.end_date else None,
            "next_run_date": t.next_run_date.isoformat(),
            "last_run_date": t.last_run_date.isoformat() if t.last_run_date else None,
            "payment_method": t.payment_method,
            "payment_reference": t.payment_reference,
            "auto_approve": t.auto_approve,
            "is_active": t.is_active,
            "description": t.description,
            "tax_deductible": t.tax_deductible,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
