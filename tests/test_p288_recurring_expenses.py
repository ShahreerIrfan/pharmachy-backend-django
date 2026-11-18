"""
backend/tests/test_p288_recurring_expenses.py
Architecture Decision: ADR-0288 (P288).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying Recurring Operating Expense Schedules and Generation Engine:
1. Create recurring expense template (Rent, auto_approve=False).
2. Create recurring expense template with auto_approve=True (Broadband Internet).
3. Generate due expenses in review mode (generates DRAFT expense voucher and advances next_run_date).
4. Generate due expenses in auto-approve mode (generates APPROVED expense and posts balanced GL journal).
5. On-demand trigger single template run endpoint (/api/v2/finance/recurring-expenses/<template_id>/run/).
6. Partial update (PATCH) recurring template estimated amount and frequency.
7. Soft-archive (DELETE) recurring template marks is_active=False and excludes from future generation.
8. Duplicate template name within tenant rejected (ERR_DUPLICATE_TEMPLATE_NAME, 400/422).
9. Missing template name or negative amount rejected (ERR_MISSING_TEMPLATE_NAME / ERR_INVALID_EXPENSE_AMOUNT, 400/422).
10. Cashier role strictly denied from viewing, creating, or triggering recurring expenses (ERR_STAFF_ROLE_DENIED / ERR_RECURRING_MANAGE_UNAUTHORIZED, 403).
11. Cross-tenant isolation prevents accessing, modifying, or running another tenant's template (403/404).
12. Suspended tenant protection blocks recurring expense operations (ERR_TENANT_SUSPENDED, 403).
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.chart_of_accounts_service import ChartOfAccountsService
from apps.core.expense_category_service import ExpenseCategoryService
from apps.core.models import (
    Branch,
    ExpenseCategory,
    JournalEntry,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    RecurringExpenseFrequency,
    RecurringExpenseTemplate,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestRecurringExpensesIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
        )

        # 2. Foreign Tenant & Branch
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Foreign Pharma {self.suffix}",
            subdomain=f"for-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Banani Branch",
        )

        # 3. Users in Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner A",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.finance_a = PharmacyUser.objects.create_user(
            email=f"finance_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Finance Officer A",
            password="Password123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. User in Tenant B
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner B",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 5. Initialize Charts of Accounts and Default Categories
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

        ExpenseCategoryService.seed_default_expense_categories(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ExpenseCategoryService.seed_default_expense_categories(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

        self.rent_cat_a = ExpenseCategory.objects.get(tenant=self.tenant_a, code="RENT")
        self.int_cat_a = ExpenseCategory.objects.get(tenant=self.tenant_a, code="INTERNET")

    def test_01_create_recurring_template_success(self):
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "template_name": "Dhanmondi Shop Rent Monthly",
            "branch_id": self.branch_a.branch_id,
            "category_id": self.rent_cat_a.category_id,
            "payee_name": "Al-Haj Abdul Latif",
            "estimated_amount_bdt": "35000.00",
            "frequency": "MONTHLY",
            "day_of_month": 1,
            "payment_method": "BANK_TRANSFER",
            "auto_approve": False,
            "description": "Monthly retail store advance lease",
        }
        response = self.client.post("/api/v2/finance/recurring-expenses/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert data["template_name"] == "Dhanmondi Shop Rent Monthly"
        assert Decimal(data["estimated_amount_bdt"]) == Decimal("35000.00")
        assert data["frequency"] == RecurringExpenseFrequency.MONTHLY
        assert data["auto_approve"] is False
        assert data["is_active"] is True
        assert data["next_run_date"] is not None

    def test_02_create_auto_approve_recurring_template(self):
        self.client.force_authenticate(user=self.finance_a)
        payload = {
            "template_name": "AmberIT Broadband Optical Fiber",
            "branch_id": self.branch_a.branch_id,
            "category_id": self.int_cat_a.category_id,
            "payee_name": "AmberIT Ltd",
            "estimated_amount_bdt": "3500.00",
            "frequency": "MONTHLY",
            "day_of_month": 5,
            "payment_method": "BKASH",
            "auto_approve": True,
        }
        response = self.client.post("/api/v2/finance/recurring-expenses/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["auto_approve"] is True
        assert response.data["payment_method"] == "BKASH"

    def test_03_generate_due_expenses_review_mode(self):
        # 1. Create template due today with auto_approve=False
        today = datetime.date.today()
        tpl = RecurringExpenseTemplate.objects.create(
            template_id=f"rectpl-{uuid.uuid4().hex[:12]}",
            template_name="Premises Rent Schedule",
            tenant=self.tenant_a,
            branch=self.branch_a,
            category=self.rent_cat_a,
            payee_name="Landlord Latif",
            estimated_amount_bdt=Decimal("35000.00"),
            frequency=RecurringExpenseFrequency.MONTHLY,
            day_of_month=today.day,
            start_date=today,
            next_run_date=today,
            payment_method="BANK_TRANSFER",
            auto_approve=False,
            is_active=True,
        )

        # 2. Trigger generation
        self.client.force_authenticate(user=self.owner_a)
        gen_res = self.client.post(
            "/api/v2/finance/recurring-expenses/generate/",
            {"as_of_date": today.isoformat()},
            format="json",
        )
        assert gen_res.status_code == status.HTTP_200_OK, gen_res.data
        data = gen_res.data
        assert data["count"] >= 1

        # 3. Verify created expense is DRAFT
        exp = OperatingExpenseEntry.objects.filter(tenant=self.tenant_a, payee_name="Landlord Latif").first()
        assert exp is not None
        assert exp.status == OperatingExpenseStatus.DRAFT
        assert exp.amount_bdt == Decimal("35000.00")
        assert exp.posted_journal is None

        # 4. Verify template advanced to next month
        tpl.refresh_from_db()
        assert tpl.last_run_date == today
        assert tpl.next_run_date > today

    def test_04_generate_due_expenses_auto_approve_mode(self):
        today = datetime.date.today()
        tpl = RecurringExpenseTemplate.objects.create(
            template_id=f"rectpl-{uuid.uuid4().hex[:12]}",
            template_name="Auto Broadband Schedule",
            tenant=self.tenant_a,
            branch=self.branch_a,
            category=self.int_cat_a,
            payee_name="AmberIT",
            estimated_amount_bdt=Decimal("3500.00"),
            frequency=RecurringExpenseFrequency.MONTHLY,
            day_of_month=today.day,
            start_date=today,
            next_run_date=today,
            payment_method="CASH",
            auto_approve=True,
            is_active=True,
        )

        self.client.force_authenticate(user=self.finance_a)
        gen_res = self.client.post(
            "/api/v2/finance/recurring-expenses/generate/",
            {"as_of_date": today.isoformat()},
            format="json",
        )
        assert gen_res.status_code == status.HTTP_200_OK, gen_res.data

        # Verify created expense is APPROVED and has balanced GL journal
        exp = OperatingExpenseEntry.objects.filter(tenant=self.tenant_a, payee_name="AmberIT").first()
        assert exp is not None
        assert exp.status == OperatingExpenseStatus.APPROVED
        assert exp.posted_journal is not None

        journal = exp.posted_journal
        assert journal.is_balanced is True
        assert journal.total_debit_bdt == Decimal("3500.00")

    def test_05_run_template_on_demand_endpoint(self):
        today = datetime.date.today()
        future_date = today + datetime.timedelta(days=15)
        tpl = RecurringExpenseTemplate.objects.create(
            template_id=f"rectpl-{uuid.uuid4().hex[:12]}",
            template_name="Special Security Schedule",
            tenant=self.tenant_a,
            branch=self.branch_a,
            category=self.rent_cat_a,
            payee_name="Security Co",
            estimated_amount_bdt=Decimal("12000.00"),
            frequency=RecurringExpenseFrequency.MONTHLY,
            start_date=today,
            next_run_date=future_date,
            auto_approve=False,
            is_active=True,
        )

        self.client.force_authenticate(user=self.owner_a)
        run_res = self.client.post(f"/api/v2/finance/recurring-expenses/{tpl.template_id}/run/", format="json")
        assert run_res.status_code == status.HTTP_200_OK, run_res.data
        assert run_res.data["count"] == 1

        tpl.refresh_from_db()
        assert tpl.last_run_date == future_date

    def test_06_update_recurring_template_success(self):
        self.client.force_authenticate(user=self.owner_a)
        create_res = self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "Editable Utility Schedule",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Utility Co",
                "estimated_amount_bdt": "6000.00",
                "frequency": "MONTHLY",
            },
            format="json",
        )
        tpl_id = create_res.data["template_id"]

        patch_res = self.client.patch(
            f"/api/v2/finance/recurring-expenses/{tpl_id}/",
            {"estimated_amount_bdt": "7500.00", "description": "Adjusted for rate hike"},
            format="json",
        )
        assert patch_res.status_code == status.HTTP_200_OK, patch_res.data
        assert Decimal(patch_res.data["estimated_amount_bdt"]) == Decimal("7500.00")
        assert patch_res.data["description"] == "Adjusted for rate hike"

    def test_07_archive_recurring_template_success(self):
        self.client.force_authenticate(user=self.owner_a)
        create_res = self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "Temporary Contract Schedule",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Old Contractor",
                "estimated_amount_bdt": "4000.00",
            },
            format="json",
        )
        tpl_id = create_res.data["template_id"]

        del_res = self.client.delete(f"/api/v2/finance/recurring-expenses/{tpl_id}/")
        assert del_res.status_code == status.HTTP_200_OK, del_res.data
        assert del_res.data["template"]["is_active"] is False

        # Confirm from DB
        tpl = RecurringExpenseTemplate.objects.get(template_id=tpl_id)
        assert tpl.is_active is False

    def test_08_duplicate_template_name_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "Unique Recurring Name",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Payee 1",
                "estimated_amount_bdt": "1000.00",
            },
            format="json",
        )
        dup_res = self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "Unique Recurring Name",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Payee 2",
                "estimated_amount_bdt": "2000.00",
            },
            format="json",
        )
        assert dup_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert dup_res.data["error_code"] == "ERR_DUPLICATE_TEMPLATE_NAME"

    def test_09_negative_amount_or_missing_name_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        res_name = self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "   ",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Payee",
                "estimated_amount_bdt": "1000.00",
            },
            format="json",
        )
        assert res_name.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res_name.data["error_code"] == "ERR_MISSING_TEMPLATE_NAME"

        res_amt = self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "Negative Amount Template",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Payee",
                "estimated_amount_bdt": "-500.00",
            },
            format="json",
        )
        assert res_amt.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res_amt.data["error_code"] == "ERR_INVALID_EXPENSE_AMOUNT"

    def test_10_cashier_role_strictly_denied(self):
        self.client.force_authenticate(user=self.cashier_a)
        list_res = self.client.get("/api/v2/finance/recurring-expenses/")
        assert list_res.status_code == status.HTTP_403_FORBIDDEN
        assert list_res.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        post_res = self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "Cashier Attempt",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Payee",
                "estimated_amount_bdt": "1000.00",
            },
            format="json",
        )
        assert post_res.status_code == status.HTTP_403_FORBIDDEN
        assert post_res.data["error_code"] == "ERR_RECURRING_MANAGE_UNAUTHORIZED"

        gen_res = self.client.post("/api/v2/finance/recurring-expenses/generate/", format="json")
        assert gen_res.status_code == status.HTTP_403_FORBIDDEN
        assert gen_res.data["error_code"] == "ERR_RECURRING_MANAGE_UNAUTHORIZED"

    def test_11_cross_tenant_isolation_enforced(self):
        self.client.force_authenticate(user=self.owner_a)
        create_res = self.client.post(
            "/api/v2/finance/recurring-expenses/",
            {
                "template_name": "Tenant A Secret Template",
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Payee A",
                "estimated_amount_bdt": "10000.00",
            },
            format="json",
        )
        tpl_id = create_res.data["template_id"]

        # Tenant B attempts access or run
        self.client.force_authenticate(user=self.owner_b)
        get_res = self.client.get(f"/api/v2/finance/recurring-expenses/{tpl_id}/")
        assert get_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        run_res = self.client.post(f"/api/v2/finance/recurring-expenses/{tpl_id}/run/", format="json")
        assert run_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_blocked(self):
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/finance/recurring-expenses/")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.json() if hasattr(response, "json") else getattr(response, "data", {})
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(response.content).lower()
