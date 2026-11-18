"""
backend/apps/core/expense_category_service.py
Architecture Decision: ADR-0286 (P286).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing Owner-Managed Operating Expense Categories:
- Rent, electricity, staff salaries, internet, transport, maintenance, and miscellaneous costs.
- Linked directly to General Ledger Chart of Accounts (6000-series operating expenses).
- CRUD operations with monthly budget allocation and tax deductibility tracking.
- Multi-branch scoping and default category initialization.
- Multi-tenant isolation and strict role-based access control.
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
from apps.core.double_entry_journal_service import q2
from apps.core.models import (
    Branch,
    ChartOfAccount,
    ExpenseCategory,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

EXPENSE_CATEGORY_VIEW_ROLES = {
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

EXPENSE_CATEGORY_MANAGE_ROLES = {
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

DEFAULT_EXPENSE_CATEGORIES = [
    {
        "code": "RENT",
        "name": "Branch Rent / Shop Lease",
        "name_bn": "দোকান বা শাখা ভাড়া",
        "default_gl_code": "6010",
        "description": "Monthly retail shop lease, branch premises rent, and advance amortizations.",
        "monthly_budget_bdt": Decimal("35000.00"),
        "tax_deductible": True,
    },
    {
        "code": "UTILITIES",
        "name": "Electricity & Generator Utilities",
        "name_bn": "বিদ্যুৎ ও জেনারেটর ইউটিলিটি",
        "default_gl_code": "6020",
        "description": "DESCO/DPDC electrical billing, generator diesel fuel, and vaccine cooling power.",
        "monthly_budget_bdt": Decimal("12000.00"),
        "tax_deductible": True,
    },
    {
        "code": "SALARIES",
        "name": "Staff Salaries & Pharmacist Allowances",
        "name_bn": "স্টাফ ও ফার্মাসিস্ট বেতন-ভাতা",
        "default_gl_code": "6030",
        "description": "Monthly payroll for pharmacist staff, cashiers, dispensers, and overtime pay.",
        "monthly_budget_bdt": Decimal("65000.00"),
        "tax_deductible": True,
    },
    {
        "code": "TRANSPORT",
        "name": "Transport & Logistics Delivery",
        "name_bn": "যাতায়াত ও পণ্য পরিবহন খরচ",
        "default_gl_code": "6040",
        "description": "Courier freight, home delivery logistics, van hire, and distributor errand fares.",
        "monthly_budget_bdt": Decimal("6000.00"),
        "tax_deductible": True,
    },
    {
        "code": "INTERNET",
        "name": "Internet & Software Subscriptions",
        "name_bn": "সফটওয়্যার ও ইন্টারনেট ফি",
        "default_gl_code": "6050",
        "description": "Broadband optical fiber, cloud POS SaaS licenses, and SMS gateway notifications.",
        "monthly_budget_bdt": Decimal("3500.00"),
        "tax_deductible": True,
    },
    {
        "code": "MAINTENANCE",
        "name": "Asset Repairs & Maintenance",
        "name_bn": "মেরামত ও রক্ষণাবেক্ষণ",
        "default_gl_code": "6060",
        "description": "Air conditioner servicing, refrigerator repairs, shelving, and POS printer upkeep.",
        "monthly_budget_bdt": Decimal("5000.00"),
        "tax_deductible": True,
    },
    {
        "code": "MISCELLANEOUS",
        "name": "Miscellaneous Office & Admin Costs",
        "name_bn": "বিবিধ অফিস ও সাধারণ খরচ",
        "default_gl_code": "6060",
        "description": "Cleaning chemicals, thermal receipt rolls, refreshment tea, and general sundry items.",
        "monthly_budget_bdt": Decimal("4000.00"),
        "tax_deductible": True,
    },
]


class ExpenseCategoryService:
    """
    Authoritative domain service governing Operating Expense Categories.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in EXPENSE_CATEGORY_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to view operating expense categories.",
                    "message_en": "Cashier and staff pharmacist are not authorized to view operating expense categories.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট অপারেটিং খরচ ক্যাটাগরি দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in EXPENSE_CATEGORY_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_EXPENSE_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can create, modify or archive expense categories.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can create, modify or archive expense categories.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার খরচ ক্যাটাগরি তৈরি, পরিবর্তন বা আর্কাইভ করতে পারেন।",
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
                    "detail": "Tenant account subscription is suspended. Expense category operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Expense category operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। খরচ ক্যাটাগরি অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def seed_default_expense_categories(
        cls,
        *,
        tenant_id: str,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Seeds standard operating expense categories (Rent, Electricity, Salaries, Transport, Internet, Maintenance, Misc)
        linked directly to the tenant's General Ledger chart of accounts.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        created_list: List[ExpenseCategory] = []
        for default_cat in DEFAULT_EXPENSE_CATEGORIES:
            gl = ChartOfAccount.objects.filter(tenant=tenant, account_code=default_cat["default_gl_code"]).first()
            cat, created = ExpenseCategory.objects.get_or_create(
                tenant=tenant,
                code=default_cat["code"],
                defaults={
                    "category_id": f"expcat-{uuid.uuid4().hex[:12]}",
                    "name": default_cat["name"],
                    "name_bn": default_cat["name_bn"],
                    "gl_account": gl,
                    "default_gl_code": default_cat["default_gl_code"],
                    "description": default_cat["description"],
                    "monthly_budget_bdt": default_cat["monthly_budget_bdt"],
                    "tax_deductible": default_cat["tax_deductible"],
                    "is_predefined": True,
                    "is_active": True,
                    "created_by": actor_user if getattr(actor_user, "is_authenticated", False) else None,
                },
            )
            created_list.append(cat)

        return [cls._serialize_category(c) for c in created_list]

    @classmethod
    def list_expense_categories(
        cls,
        *,
        tenant_id: str,
        is_active: Optional[bool] = None,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Returns authorized list of expense categories for the tenant.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        qs = ExpenseCategory.objects.filter(tenant=tenant).select_related("gl_account", "branch")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        if branch_id:
            qs = qs.filter(branch__branch_id=branch_id)

        return [cls._serialize_category(c) for c in qs]

    @classmethod
    def get_expense_category(
        cls,
        *,
        tenant_id: str,
        category_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Retrieves single operating expense category by ID with strict cross-tenant verification.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        cat = ExpenseCategory.objects.filter(category_id=category_id, tenant=tenant).select_related("gl_account", "branch").first()
        if not cat:
            foreign = ExpenseCategory.objects.filter(category_id=category_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_CATEGORY_NOT_FOUND", "detail": f"Expense category '{category_id}' not found."})

        return cls._serialize_category(cat)

    @classmethod
    @transaction.atomic
    def create_expense_category(
        cls,
        *,
        tenant_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Creates a new owner-managed operating expense category.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        name = str(data.get("name", "")).strip()
        if not name:
            raise ValidationError({"error_code": "ERR_MISSING_CATEGORY_NAME", "detail": "Expense category name is required."})

        # Name uniqueness within tenant
        if ExpenseCategory.objects.filter(tenant=tenant, name__iexact=name).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_CATEGORY_NAME",
                    "detail": f"An expense category with name '{name}' already exists for this pharmacy.",
                    "message_en": f"An expense category with name '{name}' already exists for this pharmacy.",
                    "message_bn": f"'{name}' নামের একটি খরচ ক্যাটাগরি ইতিমধ্যে এই ফার্মেসিতে বিদ্যমান।",
                }
            )

        code = str(data.get("code", "")).strip().upper()
        if not code:
            code = "".join(c for c in name.upper() if c.isalnum() or c == "_")[:16]
            if not code:
                code = f"CAT_{uuid.uuid4().hex[:6].upper()}"

        if ExpenseCategory.objects.filter(tenant=tenant, code=code).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_CATEGORY_CODE",
                    "detail": f"An expense category with code '{code}' already exists for this pharmacy.",
                    "message_en": f"An expense category with code '{code}' already exists for this pharmacy.",
                    "message_bn": f"'{code}' কোডের একটি খরচ ক্যাটাগরি ইতিমধ্যে বিদ্যমান।",
                }
            )

        budget_raw = data.get("monthly_budget_bdt", "0.00")
        try:
            budget = q2(Decimal(str(budget_raw)))
            if budget < DECIMAL_ZERO:
                raise ValueError()
        except Exception:
            raise ValidationError(
                {"error_code": "ERR_INVALID_BUDGET", "detail": "Monthly budget must be a non-negative decimal number."}
            )

        default_gl = str(data.get("default_gl_code", "6060")).strip()
        gl_acc = None
        if data.get("gl_account_id"):
            gl_acc = ChartOfAccount.objects.filter(tenant=tenant, account_id=data["gl_account_id"]).first()
        elif default_gl:
            gl_acc = ChartOfAccount.objects.filter(tenant=tenant, account_code=default_gl).first()

        branch = None
        if data.get("branch_id"):
            branch = Branch.objects.filter(tenant=tenant, branch_id=data["branch_id"]).first()

        category_id = f"expcat-{uuid.uuid4().hex[:12]}"
        cat = ExpenseCategory.objects.create(
            category_id=category_id,
            tenant=tenant,
            branch=branch,
            name=name,
            name_bn=str(data.get("name_bn", "")).strip(),
            code=code,
            gl_account=gl_acc,
            default_gl_code=gl_acc.account_code if gl_acc else default_gl,
            description=str(data.get("description", "")).strip(),
            monthly_budget_bdt=budget,
            tax_deductible=bool(data.get("tax_deductible", True)),
            is_predefined=False,
            is_active=True,
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="EXPENSE_CATEGORY_CREATE",
            resource_type="ExpenseCategory",
            resource_id=cat.category_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"code": cat.code, "name": cat.name, "default_gl_code": cat.default_gl_code},
        )

        return cls._serialize_category(cat)

    @classmethod
    @transaction.atomic
    def update_expense_category(
        cls,
        *,
        tenant_id: str,
        category_id: str,
        data: Dict[str, Any],
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Updates an existing operating expense category.
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        cat = ExpenseCategory.objects.select_for_update().filter(category_id=category_id, tenant=tenant).first()
        if not cat:
            foreign = ExpenseCategory.objects.filter(category_id=category_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_CATEGORY_NOT_FOUND", "detail": f"Expense category '{category_id}' not found."})

        if "name" in data:
            new_name = str(data["name"]).strip()
            if not new_name:
                raise ValidationError({"error_code": "ERR_MISSING_CATEGORY_NAME", "detail": "Name cannot be empty."})
            if ExpenseCategory.objects.filter(tenant=tenant, name__iexact=new_name).exclude(category_id=cat.category_id).exists():
                raise ValidationError({"error_code": "ERR_DUPLICATE_CATEGORY_NAME", "detail": f"Name '{new_name}' already exists."})
            cat.name = new_name

        if "code" in data:
            new_code = str(data["code"]).strip().upper()
            if not new_code:
                raise ValidationError({"error_code": "ERR_MISSING_CATEGORY_CODE", "detail": "Code cannot be empty."})
            if ExpenseCategory.objects.filter(tenant=tenant, code=new_code).exclude(category_id=cat.category_id).exists():
                raise ValidationError({"error_code": "ERR_DUPLICATE_CATEGORY_CODE", "detail": f"Code '{new_code}' already exists."})
            cat.code = new_code

        if "name_bn" in data:
            cat.name_bn = str(data["name_bn"]).strip()
        if "description" in data:
            cat.description = str(data["description"]).strip()
        if "tax_deductible" in data:
            cat.tax_deductible = bool(data["tax_deductible"])
        if "is_active" in data:
            cat.is_active = bool(data["is_active"])

        if "monthly_budget_bdt" in data:
            try:
                b = q2(Decimal(str(data["monthly_budget_bdt"])))
                if b < DECIMAL_ZERO:
                    raise ValueError()
                cat.monthly_budget_bdt = b
            except Exception:
                raise ValidationError({"error_code": "ERR_INVALID_BUDGET", "detail": "Invalid budget value."})

        if "default_gl_code" in data:
            gl_code = str(data["default_gl_code"]).strip()
            gl_acc = ChartOfAccount.objects.filter(tenant=tenant, account_code=gl_code).first()
            if gl_acc:
                cat.gl_account = gl_acc
                cat.default_gl_code = gl_acc.account_code
            else:
                cat.default_gl_code = gl_code

        cat.updated_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        cat.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="EXPENSE_CATEGORY_UPDATE",
            resource_type="ExpenseCategory",
            resource_id=cat.category_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"code": cat.code, "name": cat.name, "is_active": cat.is_active},
        )

        return cls._serialize_category(cat)

    @classmethod
    @transaction.atomic
    def archive_expense_category(
        cls,
        *,
        tenant_id: str,
        category_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """
        Soft-archives an operating expense category (sets is_active=False).
        """
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        cat = ExpenseCategory.objects.select_for_update().filter(category_id=category_id, tenant=tenant).first()
        if not cat:
            foreign = ExpenseCategory.objects.filter(category_id=category_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_CATEGORY_NOT_FOUND", "detail": f"Expense category '{category_id}' not found."})

        cat.is_active = False
        cat.updated_by = actor_user if getattr(actor_user, "is_authenticated", False) else None
        cat.save()

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="EXPENSE_CATEGORY_ARCHIVE",
            resource_type="ExpenseCategory",
            resource_id=cat.category_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"category_id": cat.category_id, "is_active": False},
        )

        return cls._serialize_category(cat)

    @classmethod
    def _serialize_category(cls, c: ExpenseCategory) -> Dict[str, Any]:
        return {
            "category_id": c.category_id,
            "tenant_id": str(c.tenant_id),
            "branch_id": str(c.branch_id) if c.branch_id else None,
            "name": c.name,
            "name_bn": c.name_bn,
            "code": c.code,
            "gl_account_id": str(c.gl_account_id) if c.gl_account_id else None,
            "gl_account_code": c.gl_account.account_code if c.gl_account else c.default_gl_code,
            "gl_account_name": c.gl_account.account_name if c.gl_account else "Operating Expense",
            "default_gl_code": c.default_gl_code,
            "description": c.description,
            "monthly_budget_bdt": str(c.monthly_budget_bdt),
            "tax_deductible": c.tax_deductible,
            "is_predefined": c.is_predefined,
            "is_active": c.is_active,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
