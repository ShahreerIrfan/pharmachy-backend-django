"""
backend/apps/core/chart_of_accounts_service.py
Architecture Decision: ADR-0281 (P281).
Phase 15: General Ledger, Operating Expenses & Profitability.

Authoritative domain service governing the General Ledger (GL) Chart of Accounts (COA):
- Standard DGDA & NBR compliant pharmacy chart of accounts initialization.
- Full double-entry categorization: Asset, Liability, Equity, Revenue, COGS, and Operating Expense.
- Tenant isolation, fine-grained RBAC, and trial balance verification.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    ChartOfAccount,
    GLAccountCategory,
    GLAccountStatus,
    GLNormalBalance,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger(__name__)

DECIMAL_ZERO = Decimal("0.00")

COA_VIEW_ROLES = {
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

COA_MANAGE_ROLES = {
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

DEFAULT_PHARMACY_COA_SEED: List[Dict[str, Any]] = [
    # 1. Assets (1000s)
    {
        "account_code": "1010",
        "account_name": "Cash on Hand / Till Cash",
        "account_name_bn": "হাতে নগদ ক্যাশ ও ড্রয়ার",
        "category": GLAccountCategory.ASSET,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": True,
        "description": "Physical cash in branch cash drawers, tills, and petty cash float.",
    },
    {
        "account_code": "1020",
        "account_name": "Bank & MFS Clearing Accounts",
        "account_name_bn": "ব্যাংক ও এমএফএস ক্লিয়ারিং হিসাব",
        "category": GLAccountCategory.ASSET,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": True,
        "description": "Commercial bank accounts and bKash/Nagad/Rocket merchant settlement wallets.",
    },
    {
        "account_code": "1030",
        "account_name": "Accounts Receivable (Customer Dues)",
        "account_name_bn": "গ্রাহক দেনা / প্রাপ্য হিসাব (AR)",
        "category": GLAccountCategory.ASSET,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": True,
        "description": "Total outstanding customer credit sales and due receivables.",
    },
    {
        "account_code": "1040",
        "account_name": "Merchandise Inventory Asset",
        "account_name_bn": "ফার্মেসি মজুদ ওষুধ সম্পদ",
        "category": GLAccountCategory.ASSET,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": True,
        "description": "Current valuation of pharmaceutical and OTC retail stocks in warehouse/branches.",
    },
    {
        "account_code": "1510",
        "account_name": "Pharmacy Fixtures & Cold Chain Equipment",
        "account_name_bn": "ফার্মেসি ও কোল্ড চেইন যন্ত্রপাতি",
        "category": GLAccountCategory.ASSET,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": False,
        "description": "Non-current capital assets including medical refrigerators, POS hardware, and racks.",
    },
    # 2. Liabilities (2000s)
    {
        "account_code": "2010",
        "account_name": "Accounts Payable (Supplier Dues)",
        "account_name_bn": "সরবরাহকারী পাওনা হিসাব (AP)",
        "category": GLAccountCategory.LIABILITY,
        "normal_balance": GLNormalBalance.CREDIT,
        "is_system_account": True,
        "description": "Unsettled purchase invoices and liabilities owed to pharmaceutical distributors.",
    },
    {
        "account_code": "2020",
        "account_name": "Accrued Operating Liabilities",
        "account_name_bn": "বকেয়া পরিচালন ব্যয় দায়",
        "category": GLAccountCategory.LIABILITY,
        "normal_balance": GLNormalBalance.CREDIT,
        "is_system_account": True,
        "description": "Accrued unpaid staff salaries, utilities, and branch liabilities.",
    },
    {
        "account_code": "2030",
        "account_name": "NBR VAT & Tax Payable",
        "account_name_bn": "এনবিআর মূসক / ভ্যাট প্রদেয়",
        "category": GLAccountCategory.LIABILITY,
        "normal_balance": GLNormalBalance.CREDIT,
        "is_system_account": True,
        "description": "Sales VAT and regulatory withholding taxes collected on behalf of NBR.",
    },
    # 3. Equity (3000s)
    {
        "account_code": "3010",
        "account_name": "Owner Equity & Paid-in Capital",
        "account_name_bn": "মালিকের মূলধন ও ইকুইটি",
        "category": GLAccountCategory.EQUITY,
        "normal_balance": GLNormalBalance.CREDIT,
        "is_system_account": True,
        "description": "Initial and subsequent capital invested by pharmacy proprietors/partners.",
    },
    {
        "account_code": "3020",
        "account_name": "Retained Earnings",
        "account_name_bn": "পুঞ্জীভূত মুনাফা হিসাব",
        "category": GLAccountCategory.EQUITY,
        "normal_balance": GLNormalBalance.CREDIT,
        "is_system_account": True,
        "description": "Accumulated net operating profits retained in the business across fiscal periods.",
    },
    # 4. Revenue (4000s)
    {
        "account_code": "4010",
        "account_name": "Medicine Sales Revenue",
        "account_name_bn": "প্রেসক্রিপশন ও ওষুধ বিক্রয় আয়",
        "category": GLAccountCategory.REVENUE,
        "normal_balance": GLNormalBalance.CREDIT,
        "is_system_account": True,
        "description": "Gross sales revenue earned from prescription and generic medicine dispensing.",
    },
    {
        "account_code": "4020",
        "account_name": "OTC & Retail Health Sales Revenue",
        "account_name_bn": "ওটিসি ও স্বাস্থ্যপণ্য বিক্রয় আয়",
        "category": GLAccountCategory.REVENUE,
        "normal_balance": GLNormalBalance.CREDIT,
        "is_system_account": True,
        "description": "Sales revenue from OTC vitamins, baby food, and consumer healthcare products.",
    },
    {
        "account_code": "4030",
        "account_name": "Sales Discounts & Allowances",
        "account_name_bn": "বিক্রয় বাট্টা ও ডিসকাউন্ট (কনট্রা)",
        "category": GLAccountCategory.REVENUE,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": True,
        "description": "Contra-revenue account recording POS discounts, promotion concessions, and price breaks.",
    },
    # 5. Cost of Goods Sold (5000s)
    {
        "account_code": "5010",
        "account_name": "Cost of Goods Sold (COGS - Medicine Lots)",
        "account_name_bn": "বিক্রীত ওষুধের ক্রয় ব্যয় (COGS)",
        "category": GLAccountCategory.COGS,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": True,
        "description": "Direct landed cost lot valuation of medicines dispensed to customers.",
    },
    {
        "account_code": "5020",
        "account_name": "Inventory Expiry & Wastage Expense",
        "account_name_bn": "মেয়াদোত্তীর্ণ ও নষ্ট ওষুধ ক্ষতি",
        "category": GLAccountCategory.COGS,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": True,
        "description": "Cost of expired, damaged, recalled, or cold-chain excursion spoiled medicine write-offs.",
    },
    # 6. Operating Expenses (6000s)
    {
        "account_code": "6010",
        "account_name": "Branch Rent Expense",
        "account_name_bn": "দোকান বা শাখা ভাড়া খরচ",
        "category": GLAccountCategory.EXPENSE,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": False,
        "description": "Monthly pharmacy retail store lease and branch rental fees.",
    },
    {
        "account_code": "6020",
        "account_name": "Electricity & Cold Chain Utilities",
        "account_name_bn": "বিদ্যুৎ ও কোল্ড-চেইন ইউটিলিটি খরচ",
        "category": GLAccountCategory.EXPENSE,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": False,
        "description": "Electric utility bills, generator fuel, and backup power for refrigeration.",
    },
    {
        "account_code": "6030",
        "account_name": "Staff Salaries & Pharmacist Compensation",
        "account_name_bn": "স্টাফ ও ফার্মাসিস্ট বেতন-ভাতা",
        "category": GLAccountCategory.EXPENSE,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": False,
        "description": "Monthly payroll compensation for registered A-grade pharmacists and cashier staff.",
    },
    {
        "account_code": "6040",
        "account_name": "Packaging & Freight Delivery Expense",
        "account_name_bn": "প্যাকেজিং ও ডেলিভারি পরিবহন খরচ",
        "category": GLAccountCategory.EXPENSE,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": False,
        "description": "Medicine carry bags, packaging boxes, courier fees, and customer home deliveries.",
    },
    {
        "account_code": "6050",
        "account_name": "Software License & SaaS Subscription",
        "account_name_bn": "সফটওয়্যার ও সাবস্ক্রিপশন ফি",
        "category": GLAccountCategory.EXPENSE,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": False,
        "description": "Pharmacy ERP cloud platform monthly subscription and SMS gateway charges.",
    },
    {
        "account_code": "6060",
        "account_name": "General Office & Administrative Expense",
        "account_name_bn": "সাধারণ অফিস ও প্রশাসনিক ব্যয়",
        "category": GLAccountCategory.EXPENSE,
        "normal_balance": GLNormalBalance.DEBIT,
        "is_system_account": False,
        "description": "Stationery, receipt paper rolls, sanitation chemicals, and general office expenses.",
    },
]


class ChartOfAccountsService:
    """
    Authoritative domain service governing General Ledger Chart of Accounts.
    """

    @classmethod
    def assert_view_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role in STAFF_DENIED_ROLES or role not in COA_VIEW_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Cashier and staff pharmacist are not authorized to access Chart of Accounts.",
                    "message_en": "Cashier and staff pharmacist are not authorized to access Chart of Accounts.",
                    "message_bn": "ক্যাশিয়ার বা স্টাফ ফার্মাসিস্ট চার্ট অফ অ্যাকাউন্টস দেখার অনুমতিপ্রাপ্ত নন।",
                }
            )

    @classmethod
    def assert_manage_authorized(cls, actor_user: Any) -> None:
        if not actor_user or not getattr(actor_user, "is_authenticated", False):
            raise PermissionDenied({"error_code": "ERR_UNAUTHENTICATED", "detail": "Authentication required."})
        if getattr(actor_user, "is_superuser", False):
            return

        role = getattr(actor_user, "role", None)
        if role not in COA_MANAGE_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_COA_MANAGE_UNAUTHORIZED",
                    "detail": "Only Pharmacy Owner or Finance Officer can create or modify Chart of Accounts.",
                    "message_en": "Only Pharmacy Owner or Finance Officer can create or modify Chart of Accounts.",
                    "message_bn": "শুধুমাত্র ফার্মেসি ওনার বা ফিন্যান্স অফিসার চার্ট অফ অ্যাকাউন্টস তৈরি বা পরিবর্তন করতে পারেন।",
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
                    "detail": "Tenant account subscription is suspended. Chart of Accounts operations blocked.",
                    "message_en": "Tenant account subscription is suspended. Chart of Accounts operations blocked.",
                    "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। চার্ট অফ অ্যাকাউন্টস অপারেশন বন্ধ করা হয়েছে।",
                }
            )
        return tenant

    @classmethod
    @transaction.atomic
    def initialize_default_chart_of_accounts(
        cls,
        *,
        tenant_id: str,
        actor_user: Any = None,
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Provisions the standard 21 pharmacy GL accounts for a tenant idempotently.
        """
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first() if branch_id else None

        created_count = 0
        existing_count = 0

        for seed in DEFAULT_PHARMACY_COA_SEED:
            acc, created = ChartOfAccount.objects.get_or_create(
                tenant=tenant,
                account_code=seed["account_code"],
                defaults={
                    "account_name": seed["account_name"],
                    "account_name_bn": seed["account_name_bn"],
                    "category": seed["category"],
                    "normal_balance": seed["normal_balance"],
                    "is_system_account": seed["is_system_account"],
                    "description": seed["description"],
                    "branch": branch,
                    "created_by": actor_user if getattr(actor_user, "is_authenticated", False) else None,
                },
            )
            if created:
                created_count += 1
            else:
                existing_count += 1

        return {
            "success": True,
            "tenant_id": str(tenant.tenant_id),
            "accounts_created": created_count,
            "accounts_existing": existing_count,
            "total_accounts": created_count + existing_count,
            "message_en": f"Successfully initialized {created_count} new GL accounts ({existing_count} existing).",
            "message_bn": f"সফলভাবে {created_count}টি নতুন জিএল অ্যাকাউন্ট প্রস্তুত করা হয়েছে ({existing_count}টি পূর্বেই বিদ্যমান)।",
        }

    @classmethod
    @transaction.atomic
    def create_account(
        cls,
        *,
        tenant_id: str,
        account_code: str,
        account_name: str,
        category: str,
        account_name_bn: str = "",
        normal_balance: Optional[str] = None,
        parent_account_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        description: str = "",
        is_system_account: bool = False,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """Creates a custom GL account with code uniqueness validation."""
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        clean_code = str(account_code or "").strip()
        if not clean_code:
            raise ValidationError({"error_code": "ERR_MISSING_ACCOUNT_CODE", "detail": "Account code is required."})

        clean_name = str(account_name or "").strip()
        if not clean_name:
            raise ValidationError({"error_code": "ERR_MISSING_ACCOUNT_NAME", "detail": "Account name is required."})

        clean_cat = str(category or "").strip().upper()
        if clean_cat not in GLAccountCategory.values:
            raise ValidationError(
                {"error_code": "ERR_INVALID_GL_CATEGORY", "detail": f"Invalid GL category '{category}'."}
            )

        if ChartOfAccount.objects.filter(tenant=tenant, account_code=clean_code).exists():
            raise ValidationError(
                {
                    "error_code": "ERR_DUPLICATE_ACCOUNT_CODE",
                    "detail": f"Account code '{clean_code}' already exists for this pharmacy tenant.",
                    "message_en": f"Account code '{clean_code}' already exists.",
                    "message_bn": f"হিসাব কোড '{clean_code}' এই ফার্মেসিতে পূর্বেই ব্যবহৃত হয়েছে।",
                }
            )

        parent = None
        if parent_account_id:
            parent = ChartOfAccount.objects.filter(tenant=tenant, account_id=parent_account_id).first()

        branch = Branch.objects.filter(tenant=tenant, branch_id=branch_id).first() if branch_id else None

        # Determine normal balance
        norm_bal = normal_balance
        if not norm_bal:
            norm_bal = (
                GLNormalBalance.DEBIT
                if clean_cat in (GLAccountCategory.ASSET, GLAccountCategory.COGS, GLAccountCategory.EXPENSE)
                else GLNormalBalance.CREDIT
            )

        acc = ChartOfAccount.objects.create(
            tenant=tenant,
            branch=branch,
            account_code=clean_code,
            account_name=clean_name,
            account_name_bn=account_name_bn.strip(),
            category=clean_cat,
            normal_balance=norm_bal,
            parent_account=parent,
            is_system_account=is_system_account,
            description=description.strip(),
            created_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        )

        AuditLogEntry.objects.create(
            request_id=f"req-coa-create",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "owner@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "PHARMACY_OWNER")),
            action="GL_ACCOUNT_CREATE",
            resource_type="ChartOfAccount",
            resource_id=acc.account_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={"code": acc.account_code, "name": acc.account_name, "category": acc.category},
        )

        return {
            "success": True,
            "account_id": acc.account_id,
            "account_code": acc.account_code,
            "account_name": acc.account_name,
            "category": acc.category,
            "normal_balance": acc.normal_balance,
            "current_balance_bdt": str(acc.current_balance_bdt),
            "status": acc.status,
            "message_en": "GL account created successfully.",
            "message_bn": "জিএল হিসাব সফলভাবে তৈরি হয়েছে।",
        }

    @classmethod
    @transaction.atomic
    def update_account(
        cls,
        *,
        tenant_id: str,
        account_id: str,
        account_name: Optional[str] = None,
        account_name_bn: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """Updates mutable metadata of a GL account."""
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        acc = ChartOfAccount.objects.select_for_update().filter(tenant=tenant, account_id=account_id).first()
        if not acc:
            foreign = ChartOfAccount.objects.filter(account_id=account_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Account '{account_id}' not found."})

        if account_name:
            acc.account_name = str(account_name).strip()
        if account_name_bn is not None:
            acc.account_name_bn = str(account_name_bn).strip()
        if description is not None:
            acc.description = str(description).strip()
        if status and status.upper() in GLAccountStatus.values:
            acc.status = status.upper()
        acc.save()

        return {
            "success": True,
            "account_id": acc.account_id,
            "account_code": acc.account_code,
            "account_name": acc.account_name,
            "status": acc.status,
            "message_en": "GL account updated successfully.",
            "message_bn": "জিএল হিসাব সফলভাবে আপডেট করা হয়েছে।",
        }

    @classmethod
    @transaction.atomic
    def archive_or_delete_account(
        cls,
        *,
        tenant_id: str,
        account_id: str,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """Deletes a custom empty account or archives it if balance is zero."""
        cls.assert_manage_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        acc = ChartOfAccount.objects.select_for_update().filter(tenant=tenant, account_id=account_id).first()
        if not acc:
            foreign = ChartOfAccount.objects.filter(account_id=account_id).first()
            if foreign and str(foreign.tenant_id) != str(tenant_id):
                raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_ACCESS", "detail": "Cross-tenant access forbidden."})
            raise NotFound({"error_code": "ERR_ACCOUNT_NOT_FOUND", "detail": f"Account '{account_id}' not found."})

        if acc.is_system_account:
            raise ValidationError(
                {
                    "error_code": "ERR_SYSTEM_ACCOUNT_IMMUTABLE",
                    "detail": "Standard system GL accounts cannot be deleted.",
                    "message_en": "Standard system GL accounts cannot be deleted.",
                    "message_bn": "স্ট্যান্ডার্ড সিস্টেম জিএল হিসাব মুছে ফেলা নিষিদ্ধ।",
                }
            )

        if acc.current_balance_bdt != DECIMAL_ZERO:
            raise ValidationError(
                {
                    "error_code": "ERR_NONZERO_BALANCE_ACCOUNT",
                    "detail": f"Cannot delete or archive GL account with non-zero balance ({acc.current_balance_bdt} BDT).",
                    "message_en": "Cannot delete GL account with non-zero balance.",
                    "message_bn": "ব্যালেন্স থাকা অবস্থায় জিএল হিসাব মুছে ফেলা বা আর্কাইভ করা নিষিদ্ধ।",
                }
            )

        acc.status = GLAccountStatus.ARCHIVED
        acc.save()

        return {
            "success": True,
            "account_id": acc.account_id,
            "account_code": acc.account_code,
            "status": GLAccountStatus.ARCHIVED,
            "message_en": "GL account archived successfully.",
            "message_bn": "জিএল হিসাব সফলভাবে আর্কাইভ করা হয়েছে।",
        }

    @classmethod
    def list_chart_of_accounts(
        cls,
        *,
        tenant_id: str,
        category: Optional[str] = None,
        status: Optional[str] = None,
        branch_id: Optional[str] = None,
        search: Optional[str] = None,
        actor_user: Any = None,
    ) -> List[Dict[str, Any]]:
        """Lists tenant chart of accounts with optional filters."""
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        # Auto-seed if tenant has zero accounts
        if not ChartOfAccount.objects.filter(tenant=tenant).exists():
            cls.initialize_default_chart_of_accounts(tenant_id=tenant_id, actor_user=actor_user)

        qs = ChartOfAccount.objects.filter(tenant=tenant)
        if category:
            qs = qs.filter(category=category.upper())
        if status:
            qs = qs.filter(status=status.upper())
        if branch_id:
            qs = qs.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        if search:
            s = search.strip()
            qs = qs.filter(Q(account_code__icontains=s) | Q(account_name__icontains=s) | Q(account_name_bn__icontains=s))

        return [
            {
                "account_id": a.account_id,
                "account_code": a.account_code,
                "account_name": a.account_name,
                "account_name_bn": a.account_name_bn,
                "category": a.category,
                "normal_balance": a.normal_balance,
                "is_system_account": a.is_system_account,
                "current_balance_bdt": str(a.current_balance_bdt),
                "status": a.status,
                "description": a.description,
            }
            for a in qs.order_by("account_code")
        ]

    @classmethod
    def get_trial_balance_summary(
        cls,
        *,
        tenant_id: str,
        branch_id: Optional[str] = None,
        actor_user: Any = None,
    ) -> Dict[str, Any]:
        """Calculates Trial Balance across active GL accounts."""
        cls.assert_view_authorized(actor_user)
        tenant = cls.assert_tenant_active(tenant_id)

        accounts = ChartOfAccount.objects.filter(tenant=tenant, status=GLAccountStatus.ACTIVE)
        total_debits = DECIMAL_ZERO
        total_credits = DECIMAL_ZERO

        account_balances = []
        for a in accounts:
            bal = a.current_balance_bdt
            debit_val = bal if a.normal_balance == GLNormalBalance.DEBIT else DECIMAL_ZERO
            credit_val = bal if a.normal_balance == GLNormalBalance.CREDIT else DECIMAL_ZERO

            total_debits += debit_val
            total_credits += credit_val

            account_balances.append({
                "account_code": a.account_code,
                "account_name": a.account_name,
                "category": a.category,
                "debit_bdt": str(debit_val),
                "credit_bdt": str(credit_val),
            })

        variance = abs(total_debits - total_credits)

        return {
            "tenant_id": str(tenant.tenant_id),
            "total_accounts_count": accounts.count(),
            "total_debit_bdt": str(total_debits),
            "total_credit_bdt": str(total_credits),
            "variance_bdt": str(variance),
            "is_trial_balance_balanced": (variance == DECIMAL_ZERO),
            "accounts": account_balances,
            "generated_at": timezone.now().isoformat(),
        }
