"""
backend/tests/test_p287_operating_expenses.py
Architecture Decision: ADR-0287 (P287).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying Operating Expense Entry Form, Receipt Attachment and Approval Engine:
1. Create draft operating expense with payee, branch, category, and receipt attachment.
2. Approve cash expense: posts balanced double-entry GL journal (Dr 6020 Electricity, Cr 1010 Cash).
3. Approve bank transfer expense: posts balanced double-entry GL journal (Dr 6030 Salaries, Cr 1020 Bank/MFS).
4. Approve accrual expense: posts balanced double-entry GL journal (Dr 6010 Rent, Cr 2010 AP Liability).
5. Reject expense draft with mandatory explanatory reason (status=REJECTED).
6. Missing rejection reason rejected (ERR_MISSING_REJECTION_REASON, 400/422).
7. Modifying or deleting approved expense rejected (ERR_EXPENSE_ALREADY_APPROVED / ERR_CANNOT_DELETE_APPROVED_EXPENSE, 400/422).
8. Re-approving already approved expense rejected (ERR_EXPENSE_ALREADY_APPROVED, 400/422).
9. Missing payee name or negative amount rejected (ERR_MISSING_PAYEE / ERR_INVALID_EXPENSE_AMOUNT, 400/422).
10. Cashier role strictly denied from creating or approving expenses (ERR_EXPENSE_CREATE_UNAUTHORIZED / ERR_EXPENSE_APPROVE_UNAUTHORIZED, 403).
11. Cross-tenant isolation prevents accessing, modifying, or approving another tenant's expense (403/404).
12. Suspended tenant protection blocks expense voucher operations (ERR_TENANT_SUSPENDED, 403).
"""

from __future__ import annotations

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
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestOperatingExpensesIntegration:

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
        self.manager_a = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="POS Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. User in Tenant B
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner B",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 5. Initialize Chart of Accounts and Seed Expense Categories
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

        ExpenseCategoryService.seed_default_expense_categories(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ExpenseCategoryService.seed_default_expense_categories(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

        self.rent_cat_a = ExpenseCategory.objects.get(tenant=self.tenant_a, code="RENT")
        self.util_cat_a = ExpenseCategory.objects.get(tenant=self.tenant_a, code="UTILITIES")
        self.sal_cat_a = ExpenseCategory.objects.get(tenant=self.tenant_a, code="SALARIES")

    def test_01_create_draft_operating_expense_success(self):
        self.client.force_authenticate(user=self.manager_a)
        payload = {
            "branch_id": self.branch_a.branch_id,
            "category_id": self.rent_cat_a.category_id,
            "payee_name": "Al-Haj Latif (Landlord)",
            "payee_phone": "+8801711223344",
            "amount_bdt": "35000.00",
            "payment_method": "BANK_TRANSFER",
            "payment_reference": "CHEQUE-998811",
            "receipt_file_url": "https://storage.pharma.bd/receipts/rent_nov.pdf",
            "notes": "Advance store premises rental for Dhanmondi",
            "tax_deductible": True,
        }
        response = self.client.post("/api/v2/finance/expenses/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert data["status"] == OperatingExpenseStatus.DRAFT
        assert data["voucher_number"].startswith("EXP-")
        assert Decimal(data["amount_bdt"]) == Decimal("35000.00")
        assert data["category_code"] == "RENT"
        assert data["payee_name"] == "Al-Haj Latif (Landlord)"
        assert "rent_nov.pdf" in data["receipt_file_url"]

    def test_02_approve_cash_expense_posts_balanced_gl_journal(self):
        # 1. Manager creates electricity draft (12,000 BDT Cash)
        self.client.force_authenticate(user=self.manager_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "DPDC Ltd",
                "amount_bdt": "12000.00",
                "payment_method": "CASH",
                "notes": "Electricity bill November 2026",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        # 2. Owner approves expense
        self.client.force_authenticate(user=self.owner_a)
        approve_res = self.client.post(
            f"/api/v2/finance/expenses/{expense_id}/approve/",
            {"approval_notes": "Verified DPDC electric meter receipt."},
            format="json",
        )
        assert approve_res.status_code == status.HTTP_200_OK, approve_res.data
        app_data = approve_res.data["expense"]
        assert app_data["status"] == OperatingExpenseStatus.APPROVED
        assert app_data["posted_journal_number"] is not None

        # 3. Verify double-entry GL journal
        journal = JournalEntry.objects.get(entry_id=app_data["posted_journal_id"])
        assert journal.is_balanced is True
        assert journal.total_debit_bdt == Decimal("12000.00")
        assert journal.total_credit_bdt == Decimal("12000.00")

        lines = list(journal.lines.all())
        assert len(lines) == 2
        debit_line = [l for l in lines if l.debit_amount_bdt > Decimal("0.00")][0]
        credit_line = [l for l in lines if l.credit_amount_bdt > Decimal("0.00")][0]

        assert debit_line.account.account_code == "6020"  # Electricity & Utilities
        assert credit_line.account.account_code == "1010"  # Cash in Hand

    def test_03_approve_bank_transfer_expense_posts_balanced_gl_journal(self):
        # 1. Create payroll expense draft (65,000 BDT Bank Transfer)
        self.client.force_authenticate(user=self.manager_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.sal_cat_a.category_id,
                "payee_name": "Pharmacist Staff November Payroll",
                "amount_bdt": "65000.00",
                "payment_method": "BANK_TRANSFER",
                "payment_reference": "NPSB-PAYROLL-001",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        # 2. Finance Officer approves expense
        self.client.force_authenticate(user=self.finance_a)
        approve_res = self.client.post(
            f"/api/v2/finance/expenses/{expense_id}/approve/",
            {"approval_notes": "Payroll disbursement authorized via bank transfer."},
            format="json",
        )
        assert approve_res.status_code == status.HTTP_200_OK, approve_res.data
        app_data = approve_res.data["expense"]
        assert app_data["status"] == OperatingExpenseStatus.APPROVED

        # 3. Verify double-entry GL journal
        journal = JournalEntry.objects.get(entry_id=app_data["posted_journal_id"])
        assert journal.is_balanced is True
        lines = list(journal.lines.all())
        debit_line = [l for l in lines if l.debit_amount_bdt > Decimal("0.00")][0]
        credit_line = [l for l in lines if l.credit_amount_bdt > Decimal("0.00")][0]

        assert debit_line.account.account_code == "6030"  # Salaries
        assert credit_line.account.account_code == "1020"  # Bank & MFS

    def test_04_approve_accrual_expense_posts_accounts_payable_liability(self):
        # Accrual expense: Dr Rent 6010, Cr Accounts Payable 2010
        self.client.force_authenticate(user=self.owner_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Premises Landlord",
                "amount_bdt": "35000.00",
                "payment_method": "CREDIT_ACCRUAL",
                "notes": "Accrued rent payable next month",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        approve_res = self.client.post(
            f"/api/v2/finance/expenses/{expense_id}/approve/",
            {"approval_notes": "Recognize rent liability accrual."},
            format="json",
        )
        assert approve_res.status_code == status.HTTP_200_OK
        journal = JournalEntry.objects.get(entry_id=approve_res.data["expense"]["posted_journal_id"])
        assert journal.is_balanced is True
        lines = list(journal.lines.all())
        debit_line = [l for l in lines if l.debit_amount_bdt > Decimal("0.00")][0]
        credit_line = [l for l in lines if l.credit_amount_bdt > Decimal("0.00")][0]

        assert debit_line.account.account_code == "6010"  # Rent
        assert credit_line.account.account_code == "2010"  # Accounts Payable liability

    def test_05_reject_expense_with_mandatory_reason_success(self):
        self.client.force_authenticate(user=self.manager_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "Dubious Billing Co",
                "amount_bdt": "8000.00",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        self.client.force_authenticate(user=self.finance_a)
        reject_res = self.client.post(
            f"/api/v2/finance/expenses/{expense_id}/reject/",
            {"rejection_reason": "No bill invoice attached and vendor signature missing."},
            format="json",
        )
        assert reject_res.status_code == status.HTTP_200_OK, reject_res.data
        data = reject_res.data["expense"]
        assert data["status"] == OperatingExpenseStatus.REJECTED
        assert "vendor signature missing" in data["rejection_reason"]

    def test_06_reject_without_reason_rejected(self):
        self.client.force_authenticate(user=self.manager_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "Test Payee",
                "amount_bdt": "5000.00",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        self.client.force_authenticate(user=self.finance_a)
        reject_res = self.client.post(
            f"/api/v2/finance/expenses/{expense_id}/reject/",
            {"rejection_reason": "   "},
            format="json",
        )
        assert reject_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert reject_res.data["error_code"] == "ERR_MISSING_REJECTION_REASON"

    def test_07_modifying_approved_expense_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "DPDC",
                "amount_bdt": "10000.00",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        self.client.post(f"/api/v2/finance/expenses/{expense_id}/approve/", format="json")

        # Attempt to modify
        patch_res = self.client.patch(
            f"/api/v2/finance/expenses/{expense_id}/",
            {"amount_bdt": "15000.00"},
            format="json",
        )
        assert patch_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert patch_res.data["error_code"] == "ERR_EXPENSE_ALREADY_APPROVED"

        # Attempt to delete
        del_res = self.client.delete(f"/api/v2/finance/expenses/{expense_id}/")
        assert del_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert del_res.data["error_code"] == "ERR_CANNOT_DELETE_APPROVED_EXPENSE"

    def test_08_re_approving_approved_expense_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "DPDC",
                "amount_bdt": "4000.00",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        self.client.post(f"/api/v2/finance/expenses/{expense_id}/approve/", format="json")

        re_res = self.client.post(f"/api/v2/finance/expenses/{expense_id}/approve/", format="json")
        assert re_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert re_res.data["error_code"] == "ERR_EXPENSE_ALREADY_APPROVED"

    def test_09_missing_payee_or_negative_amount_rejected(self):
        self.client.force_authenticate(user=self.manager_a)
        res_payee = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "   ",
                "amount_bdt": "1000.00",
            },
            format="json",
        )
        assert res_payee.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res_payee.data["error_code"] == "ERR_MISSING_PAYEE"

        res_amt = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "Valid Payee",
                "amount_bdt": "-500.00",
            },
            format="json",
        )
        assert res_amt.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res_amt.data["error_code"] == "ERR_INVALID_EXPENSE_AMOUNT"

    def test_10_cashier_role_strictly_denied(self):
        self.client.force_authenticate(user=self.cashier_a)
        post_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.util_cat_a.category_id,
                "payee_name": "Cashier Payee",
                "amount_bdt": "200.00",
            },
            format="json",
        )
        assert post_res.status_code == status.HTTP_403_FORBIDDEN
        assert post_res.data["error_code"] == "ERR_EXPENSE_CREATE_UNAUTHORIZED"

        app_res = self.client.post("/api/v2/finance/expenses/fake-exp/approve/", format="json")
        assert app_res.status_code == status.HTTP_403_FORBIDDEN
        assert app_res.data["error_code"] == "ERR_EXPENSE_APPROVE_UNAUTHORIZED"

    def test_11_cross_tenant_isolation_enforced(self):
        self.client.force_authenticate(user=self.owner_a)
        draft_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "branch_id": self.branch_a.branch_id,
                "category_id": self.rent_cat_a.category_id,
                "payee_name": "Tenant A Secret Payee",
                "amount_bdt": "15000.00",
            },
            format="json",
        )
        expense_id = draft_res.data["expense_id"]

        # Tenant B attempts access or approve
        self.client.force_authenticate(user=self.owner_b)
        get_res = self.client.get(f"/api/v2/finance/expenses/{expense_id}/")
        assert get_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        app_res = self.client.post(f"/api/v2/finance/expenses/{expense_id}/approve/", format="json")
        assert app_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_blocked(self):
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/finance/expenses/")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.json() if hasattr(response, "json") else getattr(response, "data", {})
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(response.content).lower()
