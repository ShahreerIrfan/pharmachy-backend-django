"""
backend/apps/core/expense_budget_service.py
Architecture Decision: ADR-0296 (P296).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Operating Expense Budget Controls:
- Planned budgets scoped by tenant, optional branch, optional expense category, and fiscal period (MONTHLY/QUARTERLY/ANNUAL).
- Alert thresholds (warning_threshold_pct, critical_threshold_pct) calculated with strict Decimal arithmetic against real APPROVED OperatingExpenseEntry postings.
- Three enforcement modes:
  1. ADVISORY_ALERT: Allows posting and records immutable ExpenseBudgetAlertEvent.
  2. REQUIRE_APPROVAL: Blocks Branch Managers on overrun (403 ERR_BUDGET_APPROVAL_REQUIRED); requires Pharmacy Owner / Finance Officer with explicit budget_override=True and audited reason.
  3. HARD_STOP: Blocks any posting that exceeds planned budget limit (400 ERR_BUDGET_HARD_LIMIT_EXCEEDED) and records HARD_STOP_BLOCKED event.
- Direct integration with OperatingExpenseService.approve_and_post_expense (zero parallel disconnected ledger).
- Multi-tenant isolation, branch scoping, subscription enforcement, AuditLogEntry, and EN/BN localized responses.
"""

from __future__ import annotations

import calendar
import csv
import datetime
import io
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
from apps.core.models import (
    Branch,
    ExpenseBudget,
    ExpenseBudgetAlertEvent,
    ExpenseBudgetAlertType,
    ExpenseBudgetEnforcementMode,
    ExpenseBudgetPeriodType,
    ExpenseBudgetStatus,
    ExpenseCategory,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")
DECIMAL_HUNDRED = Decimal("100.00")

BUDGET_VIEW_ROLES = {
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

BUDGET_MANAGE_ROLES = {
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


class ExpenseBudgetService:
    """
    Domain service governing planned operating expense budgets, threshold alerting,
    and real-time spend limit enforcement integrated into OperatingExpenseService.
    """

    # =========================================================================
    # RBAC & TENANT GUARDS
    # =========================================================================

    @classmethod
    def assert_view_authorized(cls, actor_user: Any, branch_id: Optional[str] = None) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in BUDGET_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and counter staff are not authorized to access expense budgets.",
                    "message_en": "Cashier and counter staff are not authorized to access expense budgets.",
                    "message_bn": "ক্যাশিয়ার বা কাউন্টার স্টাফ খরচের বাজেট দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER") and branch_id:
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch and str(branch_id) != user_branch:
                raise PermissionDenied(
                    {
                        "error_code": "ERR_BRANCH_SCOPE_DENIED",
                        "detail": "Branch Manager can only access budgets for their assigned branch.",
                        "message_en": "Branch Manager can only access budgets for their assigned branch.",
                        "message_bn": "ব্রাঞ্চ ম্যানেজার শুধুমাত্র নিজস্ব শাখার বাজেট দেখতে পারবেন।",
                    }
                )

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in BUDGET_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_BUDGET_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can create, update, or archive expense budgets.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can create, update, or archive expense budgets.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার খরচের বাজেট তৈরি, পরিবর্তন বা আর্কাইভ করতে পারেন।",
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
                    "detail": "Tenant account subscription is suspended. Expense budget controls blocked.",
                    "message_en": "Tenant account subscription is suspended. Expense budget controls blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। বাজেট নিয়ন্ত্রণ অ্যাক্সেস বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    # =========================================================================
    # PERIOD & UTILIZATION HELPERS
    # =========================================================================

    @classmethod
    def _period_date_bounds(
        cls, period_type: str, fiscal_year: int, fiscal_month: Optional[int]
    ) -> Tuple[datetime.date, datetime.date]:
        if period_type == ExpenseBudgetPeriodType.MONTHLY:
            m = int(fiscal_month or 1)
            last_day = calendar.monthrange(fiscal_year, m)[1]
            return datetime.date(fiscal_year, m, 1), datetime.date(fiscal_year, m, last_day)
        if period_type == ExpenseBudgetPeriodType.QUARTERLY:
            q = int(fiscal_month or 1)
            start_m = (q - 1) * 3 + 1
            end_m = start_m + 2
            last_day = calendar.monthrange(fiscal_year, end_m)[1]
            return datetime.date(fiscal_year, start_m, 1), datetime.date(fiscal_year, end_m, last_day)
        # ANNUAL
        return datetime.date(fiscal_year, 1, 1), datetime.date(fiscal_year, 12, 31)

    @classmethod
    def compute_actual_spent_bdt(
        cls,
        budget: ExpenseBudget,
        exclude_expense_id: Optional[str] = None,
    ) -> Decimal:
        start_d, end_d = cls._period_date_bounds(budget.period_type, budget.fiscal_year, budget.fiscal_month)
        qs = OperatingExpenseEntry.objects.filter(
            tenant=budget.tenant,
            status=OperatingExpenseStatus.APPROVED,
            expense_date__gte=start_d,
            expense_date__lte=end_d,
        )
        if budget.branch_id:
            qs = qs.filter(branch_id=budget.branch_id)
        if budget.category_id:
            qs = qs.filter(category_id=budget.category_id)
        if exclude_expense_id:
            qs = qs.exclude(expense_id=exclude_expense_id)

        agg = qs.aggregate(total=Sum("amount_bdt"))["total"]
        return q2(agg if agg is not None else DECIMAL_ZERO)

    @classmethod
    def _budget_health_status(
        cls,
        utilization_pct: Decimal,
        warning_pct: Decimal,
        critical_pct: Decimal,
    ) -> str:
        if utilization_pct > critical_pct:
            return "EXCEEDED"
        if utilization_pct >= warning_pct:
            return "WARNING"
        return "HEALTHY"

    @classmethod
    def _serialize_budget(cls, budget: ExpenseBudget) -> Dict[str, Any]:
        planned = q2(budget.planned_amount_bdt)
        actual = cls.compute_actual_spent_bdt(budget)
        remaining = q2(planned - actual)
        utilization_pct = q2((actual / planned) * DECIMAL_HUNDRED) if planned > DECIMAL_ZERO else DECIMAL_ZERO
        health = cls._budget_health_status(utilization_pct, budget.warning_threshold_pct, budget.critical_threshold_pct)
        start_d, end_d = cls._period_date_bounds(budget.period_type, budget.fiscal_year, budget.fiscal_month)

        return {
            "budget_id": budget.budget_id,
            "tenant_id": str(budget.tenant_id),
            "branch_id": str(budget.branch_id) if budget.branch_id else None,
            "branch_name": budget.branch.name if budget.branch else "All Branches (Consolidated)",
            "category_id": str(budget.category_id) if budget.category_id else None,
            "category_code": budget.category.code if budget.category else "ALL_CATEGORIES",
            "category_name": budget.category.name if budget.category else "All Operating Categories",
            "period_type": budget.period_type,
            "fiscal_year": budget.fiscal_year,
            "fiscal_month": budget.fiscal_month,
            "period_start": start_d.isoformat(),
            "period_end": end_d.isoformat(),
            "planned_amount_bdt": str(planned),
            "actual_spent_bdt": str(actual),
            "remaining_bdt": str(remaining),
            "utilization_pct": str(utilization_pct),
            "warning_threshold_pct": str(q2(budget.warning_threshold_pct)),
            "critical_threshold_pct": str(q2(budget.critical_threshold_pct)),
            "enforcement_mode": budget.enforcement_mode,
            "require_manager_approval_on_exceed": budget.require_manager_approval_on_exceed,
            "health_status": health,
            "status": budget.status,
            "notes": budget.notes,
            "created_at": budget.created_at.isoformat(),
            "updated_at": budget.updated_at.isoformat(),
        }

    # =========================================================================
    # VALIDATION HELPERS
    # =========================================================================

    @classmethod
    def _validate_period_fields(cls, period_type: str, fiscal_year: Any, fiscal_month: Any) -> Tuple[str, int, Optional[int]]:
        p_type = str(period_type or ExpenseBudgetPeriodType.MONTHLY).upper().strip()
        if p_type not in ExpenseBudgetPeriodType.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_BUDGET_PERIOD",
                    "detail": f"Invalid period_type '{p_type}'. Allowed: MONTHLY, QUARTERLY, ANNUAL.",
                    "message_en": f"Invalid period_type '{p_type}'.",
                    "message_bn": "ভুল বাজেট পিরিয়ড ধরন। MONTHLY, QUARTERLY অথবা ANNUAL দিন।",
                }
            )

        try:
            year = int(fiscal_year)
            if year < 2020 or year > 2100:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_FISCAL_YEAR",
                    "detail": "fiscal_year must be a valid integer year between 2020 and 2100.",
                }
            )

        month_val: Optional[int] = None
        if p_type == ExpenseBudgetPeriodType.MONTHLY:
            try:
                month_val = int(fiscal_month)
                if month_val < 1 or month_val > 12:
                    raise ValueError()
            except Exception:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_FISCAL_MONTH",
                        "detail": "MONTHLY budgets require fiscal_month between 1 and 12.",
                    }
                )
        elif p_type == ExpenseBudgetPeriodType.QUARTERLY:
            try:
                month_val = int(fiscal_month)
                if month_val < 1 or month_val > 4:
                    raise ValueError()
            except Exception:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_FISCAL_QUARTER",
                        "detail": "QUARTERLY budgets require fiscal_month (quarter number) between 1 and 4.",
                    }
                )
        else:
            month_val = None

        return p_type, year, month_val

    @classmethod
    def _validate_thresholds(cls, warning_raw: Any, critical_raw: Any) -> Tuple[Decimal, Decimal]:
        try:
            warn_pct = q2(Decimal(str(warning_raw if warning_raw is not None else "80.00")))
            crit_pct = q2(Decimal(str(critical_raw if critical_raw is not None else "100.00")))
        except Exception:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_THRESHOLD",
                    "detail": "Threshold percentages must be valid decimals.",
                }
            )

        if warn_pct < Decimal("1.00") or warn_pct > DECIMAL_HUNDRED:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_WARNING_THRESHOLD",
                    "detail": "warning_threshold_pct must be between 1.00 and 100.00.",
                }
            )
        if crit_pct < warn_pct or crit_pct > Decimal("200.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_CRITICAL_THRESHOLD",
                    "detail": "critical_threshold_pct must be >= warning_threshold_pct and <= 200.00.",
                }
            )
        return warn_pct, crit_pct

    # =========================================================================
    # CRUD & LIFECYCLE
    # =========================================================================

    @classmethod
    @transaction.atomic
    def create_budget(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        # Planned amount validation
        planned_raw = data.get("planned_amount_bdt")
        if planned_raw is None:
            raise ValidationError({"error_code": "ERR_MISSING_PLANNED_AMOUNT", "detail": "planned_amount_bdt is required."})
        try:
            planned = q2(Decimal(str(planned_raw)))
            if planned <= DECIMAL_ZERO:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PLANNED_AMOUNT",
                    "detail": "planned_amount_bdt must be a positive decimal amount.",
                    "message_en": "Planned budget amount must be greater than zero.",
                    "message_bn": "পরিকল্পিত বাজেটের পরিমাণ শূন্যের চেয়ে বেশি হতে হবে।",
                }
            )

        # Period validation
        now = timezone.now().date()
        period_type, fiscal_year, fiscal_month = cls._validate_period_fields(
            data.get("period_type", ExpenseBudgetPeriodType.MONTHLY),
            data.get("fiscal_year", now.year),
            data.get("fiscal_month", now.month if str(data.get("period_type", "MONTHLY")).upper() == "MONTHLY" else None),
        )

        # Threshold validation
        warn_pct, crit_pct = cls._validate_thresholds(
            data.get("warning_threshold_pct", "80.00"),
            data.get("critical_threshold_pct", "100.00"),
        )

        # Enforcement mode validation
        enforcement_mode = str(
            data.get("enforcement_mode", ExpenseBudgetEnforcementMode.REQUIRE_APPROVAL)
        ).upper().strip()
        if enforcement_mode not in ExpenseBudgetEnforcementMode.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_ENFORCEMENT_MODE",
                    "detail": f"Invalid enforcement_mode '{enforcement_mode}'. Allowed: ADVISORY_ALERT, REQUIRE_APPROVAL, HARD_STOP.",
                }
            )

        # Optional Branch scope (with cross-tenant guard)
        branch = None
        branch_id = data.get("branch_id")
        if branch_id:
            branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first()
            if not branch:
                foreign = Branch.objects.filter(branch_id=branch_id).first()
                if foreign and str(foreign.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant branch access forbidden."})
                raise NotFound({"error_code": "ERR_BRANCH_NOT_FOUND", "detail": f"Branch '{branch_id}' not found."})

        # Optional Category scope (with cross-tenant guard)
        category = None
        category_id = data.get("category_id")
        if category_id:
            category = ExpenseCategory.objects.filter(tenant=tenant, category_id=category_id).first()
            if not category:
                foreign_cat = ExpenseCategory.objects.filter(category_id=category_id).first()
                if foreign_cat and str(foreign_cat.tenant_id) != str(tenant_id):
                    raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant category access forbidden."})
                raise NotFound({"error_code": "ERR_CATEGORY_NOT_FOUND", "detail": f"Expense category '{category_id}' not found."})

        # Uniqueness guard for active budgets of identical scope & period
        existing = ExpenseBudget.objects.filter(
            tenant=tenant,
            branch=branch,
            category=category,
            period_type=period_type,
            fiscal_year=fiscal_year,
            fiscal_month=fiscal_month,
            status=ExpenseBudgetStatus.ACTIVE,
        ).first()
        if existing:
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_ACTIVE_BUDGET",
                    "detail": f"An active budget ({existing.budget_id}) already exists for this scope and fiscal period.",
                    "message_en": "An active budget already exists for this branch, category, and fiscal period.",
                    "message_bn": "এই শাখা, ক্যাটাগরি এবং অর্থবছর/মাসের জন্য ইতোমধ্যে একটি সক্রিয় বাজেট রয়েছে।",
                }
            )

        req_approval = bool(
            data.get(
                "require_manager_approval_on_exceed",
                enforcement_mode == ExpenseBudgetEnforcementMode.REQUIRE_APPROVAL,
            )
        )

        budget = ExpenseBudget.objects.create(
            tenant=tenant,
            branch=branch,
            category=category,
            period_type=period_type,
            fiscal_year=fiscal_year,
            fiscal_month=fiscal_month,
            planned_amount_bdt=planned,
            warning_threshold_pct=warn_pct,
            critical_threshold_pct=crit_pct,
            enforcement_mode=enforcement_mode,
            require_manager_approval_on_exceed=req_approval,
            status=ExpenseBudgetStatus.ACTIVE,
            notes=str(data.get("notes", "")).strip(),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
            updated_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="EXPENSE_BUDGET_CREATE",
            resource_type="ExpenseBudget",
            resource_id=budget.budget_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "planned_amount_bdt": str(planned),
                "enforcement_mode": enforcement_mode,
                "branch_id": str(branch.branch_id) if branch else "ALL",
                "category_id": str(category.category_id) if category else "ALL",
            },
        )

        serialized = cls._serialize_budget(budget)
        serialized["message_en"] = f"Expense budget ({budget.budget_id}) created with limit BDT {planned}."
        serialized["message_bn"] = f"খরচের বাজেট ({budget.budget_id}) ৳{planned} সীমাসহ সফলভাবে তৈরি হয়েছে।"
        return serialized

    @classmethod
    def list_budgets(
        cls,
        *,
        tenant_id: str,
        fiscal_year: Optional[int] = None,
        fiscal_month: Optional[int] = None,
        branch_id: Optional[str] = None,
        category_id: Optional[str] = None,
        include_archived: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user, branch_id=branch_id)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = ExpenseBudget.objects.filter(tenant=tenant).select_related("branch", "category")
        if not include_archived:
            qs = qs.filter(status=ExpenseBudgetStatus.ACTIVE)

        role = getattr(actor_user, "role", None)
        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch:
                qs = qs.filter(branch_id=user_branch)
        elif branch_id:
            qs = qs.filter(branch_id=branch_id)

        if category_id:
            qs = qs.filter(category_id=category_id)
        if fiscal_year is not None:
            qs = qs.filter(fiscal_year=int(fiscal_year))
        if fiscal_month is not None:
            qs = qs.filter(fiscal_month=int(fiscal_month))

        items = [cls._serialize_budget(b) for b in qs]
        total_planned = sum((Decimal(i["planned_amount_bdt"]) for i in items), DECIMAL_ZERO)
        total_actual = sum((Decimal(i["actual_spent_bdt"]) for i in items), DECIMAL_ZERO)
        exceeded_count = sum(1 for i in items if i["health_status"] == "EXCEEDED")
        warning_count = sum(1 for i in items if i["health_status"] == "WARNING")

        return {
            "tenant_id": str(tenant.tenant_id),
            "count": len(items),
            "summary": {
                "total_planned_bdt": str(q2(total_planned)),
                "total_actual_spent_bdt": str(q2(total_actual)),
                "total_remaining_bdt": str(q2(total_planned - total_actual)),
                "exceeded_count": exceeded_count,
                "warning_count": warning_count,
            },
            "budgets": items,
        }

    @classmethod
    def get_budget(
        cls,
        *,
        tenant_id: str,
        budget_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        tenant = cls.assert_tenant_active(tenant_id)
        budget = ExpenseBudget.objects.select_related("branch", "category").filter(tenant=tenant, budget_id=budget_id).first()
        if not budget:
            foreign = ExpenseBudget.objects.filter(budget_id=budget_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant budget access forbidden."})
            raise NotFound({"error_code": "ERR_BUDGET_NOT_FOUND", "detail": f"Budget '{budget_id}' not found."})

        cls.assert_view_authorized(actor_user, branch_id=str(budget.branch_id) if budget.branch_id else None)
        return cls._serialize_budget(budget)

    @classmethod
    @transaction.atomic
    def update_budget(
        cls,
        *,
        tenant_id: str,
        budget_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        budget = ExpenseBudget.objects.select_for_update().filter(tenant=tenant, budget_id=budget_id).first()
        if not budget:
            foreign = ExpenseBudget.objects.filter(budget_id=budget_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant budget update forbidden."})
            raise NotFound({"error_code": "ERR_BUDGET_NOT_FOUND", "detail": f"Budget '{budget_id}' not found."})

        if budget.status == ExpenseBudgetStatus.ARCHIVED:
            raise ValidationError(
                {
                    "error_code": "ERR_BUDGET_ARCHIVED",
                    "detail": "Archived expense budgets cannot be modified.",
                    "message_en": "Archived expense budgets cannot be modified.",
                    "message_bn": "আর্কাইভ করা খরচের বাজেট পরিবর্তন করা যাবে না।",
                }
            )

        if "planned_amount_bdt" in data and data["planned_amount_bdt"] is not None:
            try:
                planned = q2(Decimal(str(data["planned_amount_bdt"])))
                if planned <= DECIMAL_ZERO:
                    raise ValueError()
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_PLANNED_AMOUNT", "detail": "planned_amount_bdt must be positive."})
            budget.planned_amount_bdt = planned

        if "warning_threshold_pct" in data or "critical_threshold_pct" in data:
            warn_pct, crit_pct = cls._validate_thresholds(
                data.get("warning_threshold_pct", budget.warning_threshold_pct),
                data.get("critical_threshold_pct", budget.critical_threshold_pct),
            )
            budget.warning_threshold_pct = warn_pct
            budget.critical_threshold_pct = crit_pct

        if "enforcement_mode" in data and data["enforcement_mode"] is not None:
            mode = str(data["enforcement_mode"]).upper().strip()
            if mode not in ExpenseBudgetEnforcementMode.values:
                raise ValidationError({"error_code": "ERR_INVALID_ENFORCEMENT_MODE", "detail": f"Invalid enforcement_mode '{mode}'."})
            budget.enforcement_mode = mode

        if "require_manager_approval_on_exceed" in data:
            budget.require_manager_approval_on_exceed = bool(data["require_manager_approval_on_exceed"])

        if "notes" in data:
            budget.notes = str(data["notes"] or "").strip()

        budget.updated_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        budget.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="EXPENSE_BUDGET_UPDATE",
            resource_type="ExpenseBudget",
            resource_id=budget.budget_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"planned_amount_bdt": str(budget.planned_amount_bdt), "enforcement_mode": budget.enforcement_mode},
        )

        serialized = cls._serialize_budget(budget)
        serialized["message_en"] = f"Expense budget ({budget.budget_id}) updated successfully."
        serialized["message_bn"] = f"খরচের বাজেট ({budget.budget_id}) সফলভাবে আপডেট হয়েছে।"
        return serialized

    @classmethod
    @transaction.atomic
    def archive_budget(
        cls,
        *,
        tenant_id: str,
        budget_id: str,
        reason: str = "",
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        budget = ExpenseBudget.objects.select_for_update().filter(tenant=tenant, budget_id=budget_id).first()
        if not budget:
            foreign = ExpenseBudget.objects.filter(budget_id=budget_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant budget archive forbidden."})
            raise NotFound({"error_code": "ERR_BUDGET_NOT_FOUND", "detail": f"Budget '{budget_id}' not found."})

        budget.status = ExpenseBudgetStatus.ARCHIVED
        if reason:
            budget.notes = f"{budget.notes} [Archived: {reason.strip()}]".strip()
        budget.updated_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        budget.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="EXPENSE_BUDGET_ARCHIVE",
            resource_type="ExpenseBudget",
            resource_id=budget.budget_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"reason": reason.strip()},
        )

        return {
            "success": True,
            "budget_id": budget.budget_id,
            "status": budget.status,
            "message_en": f"Expense budget '{budget.budget_id}' archived.",
            "message_bn": f"খরচের বাজেট '{budget.budget_id}' আর্কাইভ করা হয়েছে।",
        }

    # =========================================================================
    # REAL-TIME BUDGET EVALUATION & ENFORCEMENT (INTEGRATED WITH P287/P289)
    # =========================================================================

    @classmethod
    def _find_matching_budgets(
        cls,
        tenant: Tenant,
        branch_id: Optional[str],
        category_id: Optional[str],
        expense_date: datetime.date,
    ) -> List[ExpenseBudget]:
        year = expense_date.year
        month = expense_date.month
        quarter = ((month - 1) // 3) + 1

        candidates = ExpenseBudget.objects.filter(
            tenant=tenant,
            status=ExpenseBudgetStatus.ACTIVE,
            fiscal_year=year,
        ).select_related("branch", "category")

        matched: List[ExpenseBudget] = []
        for b in candidates:
            # Period match
            if b.period_type == ExpenseBudgetPeriodType.MONTHLY and b.fiscal_month != month:
                continue
            if b.period_type == ExpenseBudgetPeriodType.QUARTERLY and b.fiscal_month != quarter:
                continue
            # Scope match (branch None means all branches; category None means all categories)
            if b.branch_id and branch_id and str(b.branch_id) != str(branch_id):
                continue
            if b.branch_id and not branch_id:
                continue
            if b.category_id and category_id and str(b.category_id) != str(category_id):
                continue
            if b.category_id and not category_id:
                continue
            matched.append(b)
        return matched

    @classmethod
    def evaluate_proposed_expense(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str],
        category_id: Optional[str],
        amount_bdt: Any,
        expense_date: Optional[Any] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Read-only pre-flight evaluation of a proposed expense against all matching active budgets.
        """
        cls.assert_view_authorized(actor_user, branch_id=branch_id)
        tenant = cls.assert_tenant_active(tenant_id)

        try:
            amt = q2(Decimal(str(amount_bdt)))
            if amt <= DECIMAL_ZERO:
                raise ValueError()
        except Exception:
            raise ValidationError({"error_code": "ERR_INVALID_AMOUNT", "detail": "amount_bdt must be a positive decimal."})

        exp_date = timezone.now().date()
        if expense_date:
            if isinstance(expense_date, str):
                exp_date = datetime.date.fromisoformat(expense_date)
            elif isinstance(expense_date, datetime.date):
                exp_date = expense_date

        budgets = cls._find_matching_budgets(tenant, branch_id, category_id, exp_date)
        evaluations: List[Dict[str, Any]] = []
        overall_status = "WITHIN_BUDGET"
        requires_senior_override = False
        hard_blocked = False

        for b in budgets:
            planned = q2(b.planned_amount_bdt)
            spent_before = cls.compute_actual_spent_bdt(b)
            projected = q2(spent_before + amt)
            util_pct = q2((projected / planned) * DECIMAL_HUNDRED) if planned > DECIMAL_ZERO else DECIMAL_ZERO
            exceeds = projected > planned or util_pct > b.critical_threshold_pct
            warns = util_pct >= b.warning_threshold_pct

            decision = "ALLOW"
            if exceeds:
                if b.enforcement_mode == ExpenseBudgetEnforcementMode.HARD_STOP:
                    decision = "HARD_STOP_BLOCKED"
                    hard_blocked = True
                    overall_status = "HARD_STOP_BLOCKED"
                elif b.enforcement_mode == ExpenseBudgetEnforcementMode.REQUIRE_APPROVAL or b.require_manager_approval_on_exceed:
                    decision = "REQUIRES_SENIOR_APPROVAL"
                    requires_senior_override = True
                    if not hard_blocked:
                        overall_status = "REQUIRES_SENIOR_APPROVAL"
                else:
                    decision = "ADVISORY_EXCEEDED"
                    if overall_status == "WITHIN_BUDGET":
                        overall_status = "ADVISORY_EXCEEDED"
            elif warns:
                decision = "WARNING_THRESHOLD"
                if overall_status == "WITHIN_BUDGET":
                    overall_status = "WARNING_THRESHOLD"

            evaluations.append(
                {
                    "budget_id": b.budget_id,
                    "scope_branch": b.branch.name if b.branch else "ALL_BRANCHES",
                    "scope_category": b.category.code if b.category else "ALL_CATEGORIES",
                    "enforcement_mode": b.enforcement_mode,
                    "planned_amount_bdt": str(planned),
                    "spent_before_bdt": str(spent_before),
                    "projected_spent_bdt": str(projected),
                    "utilization_pct": str(util_pct),
                    "decision": decision,
                }
            )

        return {
            "tenant_id": str(tenant.tenant_id),
            "proposed_amount_bdt": str(amt),
            "expense_date": exp_date.isoformat(),
            "overall_status": overall_status,
            "hard_blocked": hard_blocked,
            "requires_senior_override": requires_senior_override,
            "matched_budget_count": len(evaluations),
            "evaluations": evaluations,
        }

    @classmethod
    def enforce_budgets_for_approval(
        cls,
        *,
        tenant: Tenant,
        expense: OperatingExpenseEntry,
        actor_user: Any,
        budget_override: bool = False,
        budget_override_reason: str = "",
    ) -> Dict[str, Any]:
        """
        Called directly inside OperatingExpenseService.approve_and_post_expense before GL posting.
        Enforces HARD_STOP, REQUIRE_APPROVAL (senior override), and logs immutable ExpenseBudgetAlertEvent records.
        """
        amount = q2(expense.amount_bdt)
        matched = cls._find_matching_budgets(
            tenant=tenant,
            branch_id=str(expense.branch_id) if expense.branch_id else None,
            category_id=str(expense.category_id) if expense.category_id else None,
            expense_date=expense.expense_date,
        )

        role = getattr(actor_user, "role", None)
        is_senior = bool(getattr(actor_user, "is_superuser", False) or role in BUDGET_MANAGE_ROLES)

        alerts_created: List[Dict[str, Any]] = []
        status_summary = "WITHIN_BUDGET"

        for b in matched:
            planned = q2(b.planned_amount_bdt)
            spent_before = cls.compute_actual_spent_bdt(b, exclude_expense_id=expense.expense_id)
            projected = q2(spent_before + amount)
            util_pct = q2((projected / planned) * DECIMAL_HUNDRED) if planned > DECIMAL_ZERO else DECIMAL_ZERO

            exceeds = projected > planned or util_pct > b.critical_threshold_pct
            warns = util_pct >= b.warning_threshold_pct

            if exceeds:
                # 1. HARD_STOP: Always block posting beyond limit
                if b.enforcement_mode == ExpenseBudgetEnforcementMode.HARD_STOP:
                    # Record audit alert outside rollback only if desired; inside atomic block raise ValidationError
                    raise ValidationError(
                        {
                            "error_code": "ERR_BUDGET_HARD_LIMIT_EXCEEDED",
                            "detail": (
                                f"Expense BDT {amount} exceeds HARD_STOP budget '{b.budget_id}' "
                                f"(Planned: BDT {planned}, Projected: BDT {projected}, Utilization: {util_pct}%)."
                            ),
                            "message_en": f"Budget hard limit exceeded ({util_pct}% of BDT {planned}). Increase budget before approving.",
                            "message_bn": f"বাজেটের কঠোর সীমা অতিক্রম হয়েছে (৳{planned}-এর {util_pct}%)। অনুমোদনের আগে বাজেট বাড়ান।",
                            "budget_id": b.budget_id,
                            "projected_spent_bdt": str(projected),
                            "planned_amount_bdt": str(planned),
                        }
                    )

                # 2. REQUIRE_APPROVAL: Branch Managers blocked; Senior roles require explicit override flag
                if b.enforcement_mode == ExpenseBudgetEnforcementMode.REQUIRE_APPROVAL or b.require_manager_approval_on_exceed:
                    if not is_senior:
                        raise PermissionDenied(
                            {
                                "error_code": "ERR_BUDGET_APPROVAL_REQUIRED",
                                "detail": (
                                    f"Expense BDT {amount} exceeds budget '{b.budget_id}' ({util_pct}%). "
                                    "Pharmacy Owner or Finance Officer override approval is required."
                                ),
                                "message_en": "Expense exceeds planned budget. Pharmacy Owner or Finance Officer override required.",
                                "message_bn": "খরচ পরিকল্পিত বাজেট অতিক্রম করেছে। ফার্মেসি ওনার বা ফিন্যান্স অফিসারের অনুমোদন আবশ্যক।",
                                "budget_id": b.budget_id,
                            }
                        )
                    if not budget_override:
                        raise ValidationError(
                            {
                                "error_code": "ERR_BUDGET_OVERRIDE_CONFIRMATION_REQUIRED",
                                "detail": (
                                    f"Expense exceeds budget '{b.budget_id}' (Projected BDT {projected} > Planned BDT {planned}). "
                                    "Pass budget_override=true with budget_override_reason to approve."
                                ),
                                "message_en": "Explicit budget override confirmation is required to approve over-budget expense.",
                                "message_bn": "বাজেট অতিক্রম করা খরচ অনুমোদনের জন্য ওভাররাইড নিশ্চিতকরণ ও কারণ উল্লেখ আবশ্যক।",
                                "budget_id": b.budget_id,
                            }
                        )
                    evt = ExpenseBudgetAlertEvent.objects.create(
                        tenant=tenant,
                        budget=b,
                        expense_entry=expense,
                        alert_type=ExpenseBudgetAlertType.APPROVAL_OVERRIDE,
                        planned_amount_bdt=planned,
                        spent_before_bdt=spent_before,
                        projected_spent_bdt=projected,
                        utilization_pct=util_pct,
                        override_granted=True,
                        override_reason=budget_override_reason.strip() or "Senior override approved",
                        triggered_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                    )
                    alerts_created.append({"alert_id": evt.alert_id, "alert_type": evt.alert_type, "budget_id": b.budget_id})
                    status_summary = "OVERRIDE_APPROVED"
                else:
                    # 3. ADVISORY_ALERT: Allow posting and log CRITICAL_EXCEEDED alert
                    evt = ExpenseBudgetAlertEvent.objects.create(
                        tenant=tenant,
                        budget=b,
                        expense_entry=expense,
                        alert_type=ExpenseBudgetAlertType.CRITICAL_EXCEEDED,
                        planned_amount_bdt=planned,
                        spent_before_bdt=spent_before,
                        projected_spent_bdt=projected,
                        utilization_pct=util_pct,
                        override_granted=False,
                        override_reason="",
                        triggered_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                    )
                    alerts_created.append({"alert_id": evt.alert_id, "alert_type": evt.alert_type, "budget_id": b.budget_id})
                    if status_summary == "WITHIN_BUDGET":
                        status_summary = "ADVISORY_EXCEEDED"
            elif warns:
                evt = ExpenseBudgetAlertEvent.objects.create(
                    tenant=tenant,
                    budget=b,
                    expense_entry=expense,
                    alert_type=ExpenseBudgetAlertType.WARNING_THRESHOLD,
                    planned_amount_bdt=planned,
                    spent_before_bdt=spent_before,
                    projected_spent_bdt=projected,
                    utilization_pct=util_pct,
                    override_granted=False,
                    triggered_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
                )
                alerts_created.append({"alert_id": evt.alert_id, "alert_type": evt.alert_type, "budget_id": b.budget_id})
                if status_summary == "WITHIN_BUDGET":
                    status_summary = "WARNING_THRESHOLD"

        return {
            "status": status_summary,
            "checked_budgets": len(matched),
            "alerts_triggered": alerts_created,
        }

    # =========================================================================
    # ALERT HISTORY & CSV EXPORT
    # =========================================================================

    @classmethod
    def list_budget_alerts(
        cls,
        *,
        tenant_id: str,
        budget_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        cls.assert_view_authorized(actor_user, branch_id=branch_id)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = ExpenseBudgetAlertEvent.objects.filter(tenant=tenant).select_related("budget", "budget__branch", "expense_entry")
        if budget_id:
            qs = qs.filter(budget_id=budget_id)

        role = getattr(actor_user, "role", None)
        if role in (UserRole.BRANCH_MANAGER, "BRANCH_MANAGER"):
            user_branch = str(getattr(actor_user, "branch_id", "") or "")
            if user_branch:
                qs = qs.filter(budget__branch_id=user_branch)
        elif branch_id:
            qs = qs.filter(budget__branch_id=branch_id)

        alerts = [
            {
                "alert_id": a.alert_id,
                "budget_id": a.budget_id,
                "expense_id": a.expense_entry_id,
                "alert_type": a.alert_type,
                "planned_amount_bdt": str(q2(a.planned_amount_bdt)),
                "spent_before_bdt": str(q2(a.spent_before_bdt)),
                "projected_spent_bdt": str(q2(a.projected_spent_bdt)),
                "utilization_pct": str(q2(a.utilization_pct)),
                "override_granted": a.override_granted,
                "override_reason": a.override_reason,
                "created_at": a.created_at.isoformat(),
            }
            for a in qs[:100]
        ]
        return {"tenant_id": str(tenant.tenant_id), "count": len(alerts), "alerts": alerts}

    @classmethod
    def export_budgets_csv(
        cls,
        *,
        tenant_id: str,
        fiscal_year: Optional[int] = None,
        fiscal_month: Optional[int] = None,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> str:
        report = cls.list_budgets(
            tenant_id=tenant_id,
            fiscal_year=fiscal_year,
            fiscal_month=fiscal_month,
            branch_id=branch_id,
            include_archived=False,
            actor_user=actor_user,
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Budget ID",
                "Branch",
                "Category",
                "Period",
                "Fiscal Year",
                "Fiscal Month/Quarter",
                "Planned (BDT)",
                "Actual Spent (BDT)",
                "Remaining (BDT)",
                "Utilization (%)",
                "Warning (%)",
                "Critical (%)",
                "Enforcement Mode",
                "Health Status",
            ]
        )
        for b in report["budgets"]:
            writer.writerow(
                [
                    b["budget_id"],
                    b["branch_name"],
                    b["category_name"],
                    b["period_type"],
                    b["fiscal_year"],
                    b["fiscal_month"] or "",
                    b["planned_amount_bdt"],
                    b["actual_spent_bdt"],
                    b["remaining_bdt"],
                    b["utilization_pct"],
                    b["warning_threshold_pct"],
                    b["critical_threshold_pct"],
                    b["enforcement_mode"],
                    b["health_status"],
                ]
            )
        return buf.getvalue()
