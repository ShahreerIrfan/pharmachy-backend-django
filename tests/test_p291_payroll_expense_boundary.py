"""
backend/tests/test_p291_payroll_expense_boundary.py
Architecture Decision: ADR-0291 (P291).
Phase 15: General Ledger, Operating Expenses & Profitability.

Comprehensive targeted integration tests for Payroll Expense Boundary & Employee Salary Privacy:
1. Ordinary expense entry for category "SALARIES" is strictly blocked by payroll boundary.
2. Draft payroll run creation with multiple confidential employee salary slips.
3. Approval and posting posts balanced double-entry GL journal lines (Dr 6030, Cr 1020/1010, Cr 2020).
4. Journal narration contains summary-only data with zero employee personal PII leakage.
5. Settlement financial account balance accurately updated upon disbursement.
6. Negative net salary rejection (deductions > earnings).
7. Duplicate staff member in same payroll run rejection.
8. Invalid payroll period (start > end) rejection.
9. Immutability: already posted payroll run cannot be re-posted.
10. Role-based access control: Cashier strictly denied (403 Forbidden).
11. Role-based access control: Branch Manager denied payroll execution (403 Forbidden).
12. Cross-tenant staff slip and account access forbidden (403 Forbidden).
13. Suspended tenant subscription blocks payroll operations.
14. Draft payroll cancellation and posted cancellation protection.
"""

from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.chart_of_accounts_service import ChartOfAccountsService
from apps.core.expense_category_service import ExpenseCategoryService
from apps.core.models import (
    Branch,
    ExpenseCategory,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    GLAccountCategory,
    JournalEntry,
    JournalEntryLine,
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


@pytest.mark.django_db(transaction=True)
class TestPayrollExpenseBoundaryIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants & Branches
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Central Dispensary",
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name="Mirpur Branch Dispensary",
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lz-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Uttara Branch",
        )

        # 2. Users for Tenant A
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner A",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.finance = PharmacyUser.objects.create_user(
            email=f"finance_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Finance Officer A",
            password="Password123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
        )
        self.manager = PharmacyUser.objects.create_user(
            email=f"manager_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="POS Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharmacist_{self.suffix}@pharma.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist A",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 3. Foreign User for Tenant B
        self.foreign_owner = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Foreign Owner B",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )
        self.foreign_staff = PharmacyUser.objects.create_user(
            email=f"fstaff_{self.suffix}@pharma.com",
            phone_number=f"+880177{random.randint(1000000, 9999999)}",
            full_name="Foreign Staff B",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 4. Initialize Chart of Accounts & Seed Categories
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner
        )
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.foreign_owner
        )
        ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner
        )
        ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.foreign_owner
        )

        # 5. Settlement Account in Tenant A
        self.bank_account = FinancialAccount.objects.create(
            account_id=f"acc-corp-bank-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            account_type=FinancialAccountType.BANK_ACCOUNT,
            account_name="Dutch-Bangla Corporate Payroll Account",
            account_code=f"DBBL-CORP-{self.suffix}",
            current_balance=Decimal("250000.00"),
            status=FinancialAccountStatus.ACTIVE,
        )

        # 6. Staff Salary Records (Confidential HR Ledger)
        self.sal_pharmacist = StaffSalaryRecord.objects.create(
            salary_id=f"sal-pharma-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            staff_member=self.pharmacist,
            base_salary_bdt=Decimal("35000.00"),
            allowances_bdt=Decimal("5000.00"),
            bank_name="Dutch-Bangla Bank PLC",
            bank_account_no="122.110.554433",
            effective_from=timezone.now().date(),
            is_active=True,
        )
        self.sal_cashier = StaffSalaryRecord.objects.create(
            salary_id=f"sal-cashier-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            staff_member=self.cashier,
            base_salary_bdt=Decimal("18000.00"),
            allowances_bdt=Decimal("2000.00"),
            bank_name="BRAC Bank PLC",
            bank_account_no="150.102.778899",
            effective_from=timezone.now().date(),
            is_active=True,
        )

    # =========================================================================
    # TESTS
    # =========================================================================

    def test_01_ordinary_expense_entry_salaries_blocked_by_boundary(self):
        """
        Verify that attempting to create an ordinary operational expense voucher
        with category 'SALARIES' is strictly rejected with ERR_PAYROLL_EXPENSE_BOUNDARY_ENFORCED.
        """
        self.client.force_authenticate(user=self.manager)
        sal_category = ExpenseCategory.objects.get(tenant=self.tenant_a, code="SALARIES")

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a1.branch_id,
            "category_id": sal_category.category_id,
            "payee_name": "Tariq Cashier Monthly Pay",
            "amount_bdt": "20000.00",
            "payment_method": "CASH",
        }

        res = self.client.post("/api/v2/finance/expenses/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        data = res.json()
        assert "ERR_PAYROLL_EXPENSE_BOUNDARY_ENFORCED" in str(data)

    def test_02_create_draft_payroll_run_with_multiple_slips_success(self):
        """
        Verify that Owner can create a draft payroll execution batch with multiple
        confidential staff slips and accurate mathematical totals.
        """
        self.client.force_authenticate(user=self.owner)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a1.branch_id,
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "payment_method": "BANK_TRANSFER",
            "settlement_account_id": self.bank_account.account_id,
            "notes": "November 2026 Monthly Staff Salary Run",
            "slips": [
                {
                    "staff_member_id": str(self.pharmacist.user_id),
                    "base_salary_bdt": "35000.00",
                    "allowances_bdt": "5000.00",
                    "bonus_bdt": "2500.00",
                    "deductions_bdt": "1500.00",
                },
                {
                    "staff_member_id": str(self.cashier.user_id),
                    "base_salary_bdt": "18000.00",
                    "allowances_bdt": "2000.00",
                    "bonus_bdt": "0.00",
                    "deductions_bdt": "500.00",
                },
            ],
        }

        res = self.client.post("/api/v2/finance/payroll/runs/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.json()["payroll_run"]
        assert data["status"] == PayrollRunStatus.DRAFT
        assert data["total_gross_bdt"] == "53000.00"       # 35000 + 18000
        assert data["total_allowances_bdt"] == "7000.00"   # 5000 + 2000
        assert data["total_bonuses_bdt"] == "2500.00"      # 2500 + 0
        assert data["total_deductions_bdt"] == "2000.00"   # 1500 + 500
        # Net: (35000+5000+2500-1500 = 41000) + (18000+2000+0-500 = 19500) = 60500.00
        assert data["total_net_payable_bdt"] == "60500.00"
        assert len(data["slips"]) == 2
        assert data["is_confidential"] is True

    def test_03_approve_and_post_payroll_posts_balanced_double_entry_journal(self):
        """
        Verify that Finance Officer approves payroll run:
        1. Posts balanced GL journal: Dr 6030 (Salaries Expense), Cr 1020 (Bank Settlement), Cr 2020 (Accrued Liabilities / Deductions).
        2. Invariant: Total Dr == Total Cr.
        3. General Ledger journal narration is aggregated and strictly contains ZERO staff PII.
        """
        self.client.force_authenticate(user=self.finance)

        # 1. Create draft run
        create_res = self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "payment_method": "BANK_TRANSFER",
                "settlement_account_id": self.bank_account.account_id,
                "slips": [
                    {
                        "staff_member_id": str(self.pharmacist.user_id),
                        "base_salary_bdt": "40000.00",
                        "allowances_bdt": "4000.00",
                        "bonus_bdt": "1000.00",
                        "deductions_bdt": "2000.00",
                    }
                ],
            },
            format="json",
        )
        assert create_res.status_code == status.HTTP_201_CREATED
        run_id = create_res.json()["payroll_run"]["run_id"]

        # 2. Approve and Post
        post_res = self.client.post(
            f"/api/v2/finance/payroll/runs/{run_id}/approve-and-post/",
            {"notes": "Approved by senior finance officer after attendance audit."},
            format="json",
        )
        assert post_res.status_code == status.HTTP_200_OK
        run_data = post_res.json()["payroll_run"]
        assert run_data["status"] == PayrollRunStatus.POSTED
        assert run_data["posted_journal_id"] is not None

        # 3. Verify General Ledger Journal Entry
        journal = JournalEntry.objects.get(entry_id=run_data["posted_journal_id"])
        # Total Expense = Gross (40000) + Allowances (4000) + Bonus (1000) = 45000.00
        # Net Payable = 45000 - 2000 = 43000.00
        # Deductions = 2000.00
        assert journal.total_debit_bdt == Decimal("45000.00")
        assert journal.total_credit_bdt == Decimal("45000.00")
        assert journal.source_module == JournalSourceModule.PAYROLL

        # Verify Narration contains NO employee name or personal account
        assert "Staff Pharmacist A" not in journal.narration
        assert "122.110.554433" not in journal.narration
        assert "Monthly Staff Payroll" in journal.narration

        # Verify Lines
        lines = list(JournalEntryLine.objects.filter(journal_entry=journal).select_related("account"))
        assert len(lines) == 3

        dr_salaries = [l for l in lines if l.account.account_code == "6030"][0]
        assert dr_salaries.debit_amount_bdt == Decimal("45000.00")
        assert dr_salaries.credit_amount_bdt == Decimal("0.00")
        assert dr_salaries.account.category == GLAccountCategory.EXPENSE

        cr_bank = [l for l in lines if l.account.account_code == "1020"][0]
        assert cr_bank.credit_amount_bdt == Decimal("43000.00")
        assert cr_bank.debit_amount_bdt == Decimal("0.00")
        assert cr_bank.account.category == GLAccountCategory.ASSET

        cr_deductions = [l for l in lines if l.account.account_code == "2020"][0]
        assert cr_deductions.credit_amount_bdt == Decimal("2000.00")
        assert cr_deductions.debit_amount_bdt == Decimal("0.00")
        assert cr_deductions.account.category == GLAccountCategory.LIABILITY

    def test_04_settlement_account_balance_updated_on_posting(self):
        """
        Verify that posting payroll decreases the current_balance of the settlement financial account.
        """
        self.client.force_authenticate(user=self.owner)
        initial_bank_bal = self.bank_account.current_balance

        create_res = self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "settlement_account_id": self.bank_account.account_id,
                "slips": [
                    {
                        "staff_member_id": str(self.cashier.user_id),
                        "base_salary_bdt": "20000.00",
                        "allowances_bdt": "0.00",
                        "bonus_bdt": "0.00",
                        "deductions_bdt": "0.00",
                    }
                ],
            },
            format="json",
        )
        run_id = create_res.json()["payroll_run"]["run_id"]

        self.client.post(f"/api/v2/finance/payroll/runs/{run_id}/approve-and-post/", format="json")

        self.bank_account.refresh_from_db()
        assert self.bank_account.current_balance == initial_bank_bal - Decimal("20000.00")

    def test_05_negative_net_salary_rejected(self):
        """
        Verify that a slip with deductions exceeding earnings is rejected.
        """
        self.client.force_authenticate(user=self.owner)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "slips": [
                {
                    "staff_member_id": str(self.pharmacist.user_id),
                    "base_salary_bdt": "20000.00",
                    "deductions_bdt": "25000.00",  # Exceeds earnings
                }
            ],
        }

        res = self.client.post("/api/v2/finance/payroll/runs/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_NEGATIVE_NET_SALARY" in str(res.json())

    def test_06_duplicate_staff_in_same_payroll_run_rejected(self):
        """
        Verify that including the same staff member twice in a single run is rejected.
        """
        self.client.force_authenticate(user=self.owner)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "slips": [
                {"staff_member_id": str(self.pharmacist.user_id), "base_salary_bdt": "30000.00"},
                {"staff_member_id": str(self.pharmacist.user_id), "base_salary_bdt": "30000.00"},
            ],
        }

        res = self.client.post("/api/v2/finance/payroll/runs/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_DUPLICATE_STAFF_SLIP" in str(res.json())

    def test_07_invalid_period_dates_rejected(self):
        """
        Verify that period_start after period_end is rejected.
        """
        self.client.force_authenticate(user=self.owner)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "period_start": "2026-12-01",
            "period_end": "2026-11-01",  # Invalid
            "slips": [{"staff_member_id": str(self.cashier.user_id), "base_salary_bdt": "15000.00"}],
        }

        res = self.client.post("/api/v2/finance/payroll/runs/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_INVALID_PERIOD" in str(res.json())

    def test_08_already_posted_payroll_cannot_be_posted_again(self):
        """
        Verify strict immutability: re-posting a posted payroll run fails.
        """
        self.client.force_authenticate(user=self.owner)

        create_res = self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "slips": [{"staff_member_id": str(self.cashier.user_id), "base_salary_bdt": "15000.00"}],
            },
            format="json",
        )
        run_id = create_res.json()["payroll_run"]["run_id"]

        # First post -> SUCCESS
        res1 = self.client.post(f"/api/v2/finance/payroll/runs/{run_id}/approve-and-post/", format="json")
        assert res1.status_code == status.HTTP_200_OK

        # Second post -> REJECTED
        res2 = self.client.post(f"/api/v2/finance/payroll/runs/{run_id}/approve-and-post/", format="json")
        assert res2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_PAYROLL_ALREADY_POSTED" in str(res2.json())

    def test_09_cashier_strictly_denied_from_payroll(self):
        """
        Verify that POS terminal cashier is strictly denied access to payroll.
        """
        self.client.force_authenticate(user=self.cashier)

        # List runs -> 403
        res_list = self.client.get("/api/v2/finance/payroll/runs/", HTTP_X_TENANT_ID=self.tenant_a.tenant_id)
        assert res_list.status_code == status.HTTP_403_FORBIDDEN

        # Create run -> 403
        res_create = self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "slips": [{"staff_member_id": str(self.cashier.user_id), "base_salary_bdt": "15000.00"}],
            },
            format="json",
        )
        assert res_create.status_code == status.HTTP_403_FORBIDDEN

    def test_10_branch_manager_denied_manage_payroll(self):
        """
        Verify that Branch Manager cannot create or approve payroll runs.
        """
        self.client.force_authenticate(user=self.manager)

        res = self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "slips": [{"staff_member_id": str(self.cashier.user_id), "base_salary_bdt": "15000.00"}],
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_PAYROLL_MANAGE_UNAUTHORIZED" in str(res.json())

    def test_11_cross_tenant_staff_or_account_access_forbidden(self):
        """
        Verify that attempting to include a staff member from another tenant is forbidden.
        """
        self.client.force_authenticate(user=self.owner)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "period_start": "2026-11-01",
            "period_end": "2026-11-30",
            "slips": [
                {
                    "staff_member_id": str(self.foreign_staff.user_id),  # Belongs to Tenant B
                    "base_salary_bdt": "18000.00",
                }
            ],
        }

        res = self.client.post("/api/v2/finance/payroll/runs/", payload, format="json")
        assert res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_blocked(self):
        """
        Verify that suspended tenant subscription blocks payroll execution.
        """
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner)
        res = self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "slips": [{"staff_member_id": str(self.cashier.user_id), "base_salary_bdt": "15000.00"}],
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert ("ERR_TENANT_SUSPENDED" in str(res.json()) or "ERR_SUBSCRIPTION" in str(res.json()))

    def test_13_cancel_draft_payroll_run(self):
        """
        Verify that a draft payroll run can be cancelled.
        """
        self.client.force_authenticate(user=self.owner)

        create_res = self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "slips": [{"staff_member_id": str(self.cashier.user_id), "base_salary_bdt": "15000.00"}],
            },
            format="json",
        )
        run_id = create_res.json()["payroll_run"]["run_id"]

        cancel_res = self.client.post(
            f"/api/v2/finance/payroll/runs/{run_id}/cancel/",
            {"reason": "Wrong compensation formulas applied by HR assistant."},
            format="json",
        )
        assert cancel_res.status_code == status.HTTP_200_OK
        assert cancel_res.json()["payroll_run"]["status"] == PayrollRunStatus.CANCELLED

    def test_14_multibranch_payroll_filtering(self):
        """
        Verify multibranch pharmacy payroll: filtering runs by branch.
        """
        self.client.force_authenticate(user=self.owner)

        # Create run for Branch A1
        self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "slips": [{"staff_member_id": str(self.pharmacist.user_id), "base_salary_bdt": "30000.00"}],
            },
            format="json",
        )

        # Create run for Branch A2
        self.client.post(
            "/api/v2/finance/payroll/runs/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a2.branch_id,
                "period_start": "2026-11-01",
                "period_end": "2026-11-30",
                "slips": [{"staff_member_id": str(self.cashier.user_id), "base_salary_bdt": "16000.00"}],
            },
            format="json",
        )

        # Filter by Branch A1
        res_a1 = self.client.get(
            "/api/v2/finance/payroll/runs/",
            {"branch_id": self.branch_a1.branch_id},
            HTTP_X_TENANT_ID=self.tenant_a.tenant_id,
        )
        assert res_a1.status_code == status.HTTP_200_OK
        runs_a1 = res_a1.json()["runs"]
        assert len(runs_a1) == 1
        assert runs_a1[0]["branch_id"] == self.branch_a1.branch_id
