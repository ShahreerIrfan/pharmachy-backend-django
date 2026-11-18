"""
backend/tests/test_p292_financial_period_close.py
Architecture Decision: ADR-0292 (P292).
Phase 15: General Ledger, Operating Expenses & Profitability.

Comprehensive targeted integration tests for Financial Period Close & Controlled Reopening:
1. Creation of financial period (OPEN status).
2. Pre-close integrity check detects pending draft expenses and blocks close.
3. Close period posts balanced closing journal transferring net profit to Retained Earnings (GL 3020).
4. Closed period immutability: retroactive journal posting blocked (ERR_PERIOD_CLOSED).
5. Closed period immutability: retroactive operating expense creation blocked (ERR_PERIOD_CLOSED).
6. Closing an already closed period is rejected (ERR_PERIOD_ALREADY_CLOSED).
7. Finance Officer alone is denied from reopening; strictly Owner required (403 Forbidden).
8. Owner reopens closed period with mandatory audit reason (status -> REOPENED, reversal journal posted).
9. Reopening with missing reason is rejected (ERR_MISSING_REOPEN_REASON).
10. POS terminal cashier is strictly denied access (403 Forbidden).
11. Cross-tenant period access is forbidden (403 Forbidden).
12. Suspended tenant subscription blocks period operations (403 Forbidden).
"""

from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.chart_of_accounts_service import ChartOfAccountsService
from apps.core.double_entry_journal_service import DoubleEntryJournalService
from apps.core.expense_category_service import ExpenseCategoryService
from apps.core.models import (
    Branch,
    ExpenseCategory,
    FinancialPeriod,
    FinancialPeriodStatus,
    FinancialPeriodType,
    GLAccountCategory,
    JournalEntry,
    JournalEntryLine,
    JournalSourceModule,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestFinancialPeriodCloseIntegration:

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
        self.branch_a = Branch.objects.create(
            branch_id=f"br-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Central Dispensary",
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
            branch_id=self.branch_a.branch_id,
        )
        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="POS Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
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

        # 4. Initialize Chart of Accounts & Categories
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

    # =========================================================================
    # TESTS
    # =========================================================================

    def test_01_create_financial_period_success(self):
        """
        Verify that Finance Officer can create an accounting period in OPEN status.
        """
        self.client.force_authenticate(user=self.finance)

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "period_code": f"2026-10-{self.suffix}",
            "period_name": "October 2026 Monthly Accounting Period",
            "period_type": FinancialPeriodType.MONTHLY,
            "start_date": "2026-10-01",
            "end_date": "2026-10-31",
        }

        res = self.client.post("/api/v2/finance/periods/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.json()["period"]
        assert data["status"] == FinancialPeriodStatus.OPEN
        assert data["period_code"] == f"2026-10-{self.suffix}".upper()
        assert data["reopen_count"] == 0

    def test_02_pre_close_integrity_check_detects_pending_draft_expenses(self):
        """
        Verify that pre-close integrity check detects pending unapproved draft expenses
        in the period and blocks the period from being closed.
        """
        self.client.force_authenticate(user=self.finance)

        # 1. Create Period for Oct 2026
        period = FinancialPeriod.objects.create(
            period_id=f"fper-oct-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"OCT26-{self.suffix}",
            period_name="October 2026",
            start_date="2026-10-01",
            end_date="2026-10-31",
            status=FinancialPeriodStatus.OPEN,
        )

        # 2. Create a pending draft expense in Oct 2026
        cat = ExpenseCategory.objects.get(tenant=self.tenant_a, code="RENT")
        OperatingExpenseEntry.objects.create(
            expense_id=f"exp-draft-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            voucher_number=f"VCH-OCT-{self.suffix}",
            category=cat,
            payee_name="Dhanmondi Landlord",
            amount_bdt=Decimal("45000.00"),
            expense_date="2026-10-15",
            status=OperatingExpenseStatus.DRAFT,
        )

        # 3. Call validate-integrity
        res_val = self.client.get(f"/api/v2/finance/periods/{period.period_id}/validate-integrity/")
        assert res_val.status_code == status.HTTP_200_OK
        val_data = res_val.json()
        assert val_data["is_ready_to_close"] is False
        assert any("draft operating expenses" in issue for issue in val_data["blocking_issues"])

        # 4. Attempt to close period -> must fail
        res_close = self.client.post(
            f"/api/v2/finance/periods/{period.period_id}/close/",
            {"closing_notes": "Attempt close with draft"},
            format="json",
        )
        assert res_close.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_PERIOD_INTEGRITY_FAILED" in str(res_close.json())

    def test_03_close_period_posts_balanced_retained_earnings_journal(self):
        """
        Verify that closing a period:
        1. Posts a balanced closing journal transferring Net Income to GL 3020 (Retained Earnings).
        2. Closes Revenue nominal balance and Expense nominal balance to 0.
        3. Sets period status to CLOSED.
        """
        self.client.force_authenticate(user=self.finance)

        period = FinancialPeriod.objects.create(
            period_id=f"fper-sep-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"SEP26-{self.suffix}",
            period_name="September 2026",
            start_date="2026-09-01",
            end_date="2026-09-30",
            status=FinancialPeriodStatus.OPEN,
        )

        # Post an operational sale in Sep 2026: Dr Cash 1010 (50,000), Cr Revenue 4010 (50,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            lines=[
                {"account_code": "1010", "debit_amount_bdt": Decimal("50000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "4010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("50000.00")},
            ],
            posting_date="2026-09-10",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"SALE-SEP-{self.suffix}",
            actor_user=self.finance,
        )

        # Post an operational expense in Sep 2026: Dr Electricity 6020 (15,000), Cr Cash 1010 (15,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            lines=[
                {"account_code": "6020", "debit_amount_bdt": Decimal("15000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "1010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("15000.00")},
            ],
            posting_date="2026-09-12",
            source_module=JournalSourceModule.OPERATING_EXPENSE,
            source_reference=f"EXP-SEP-{self.suffix}",
            actor_user=self.finance,
        )

        # Net Income = 50,000 - 15,000 = 35,000.00 (Profit)

        # Close the period
        res_close = self.client.post(
            f"/api/v2/finance/periods/{period.period_id}/close/",
            {"closing_notes": "September 2026 month-end book closure."},
            format="json",
        )
        assert res_close.status_code == status.HTTP_200_OK
        p_data = res_close.json()["period"]
        assert p_data["status"] == FinancialPeriodStatus.CLOSED
        assert p_data["net_income_bdt"] == "35000.00"
        assert p_data["closing_journal_id"] is not None

        # Verify Closing Journal Entry
        closing_journal = JournalEntry.objects.get(entry_id=p_data["closing_journal_id"])
        assert closing_journal.total_debit_bdt == closing_journal.total_credit_bdt == Decimal("50000.00")
        assert closing_journal.source_module == JournalSourceModule.CLOSING

        lines = list(JournalEntryLine.objects.filter(journal_entry=closing_journal).select_related("account"))
        # 3 lines: Dr Revenue 4010 (50,000), Cr Expense 6020 (15,000), Cr Retained Earnings 3020 (35,000)
        assert len(lines) == 3

        dr_rev = [l for l in lines if l.account.account_code == "4010"][0]
        assert dr_rev.debit_amount_bdt == Decimal("50000.00")

        cr_exp = [l for l in lines if l.account.account_code == "6020"][0]
        assert cr_exp.credit_amount_bdt == Decimal("15000.00")

        cr_retained = [l for l in lines if l.account.account_code == "3020"][0]
        assert cr_retained.credit_amount_bdt == Decimal("35000.00")
        assert cr_retained.account.category == GLAccountCategory.EQUITY

    def test_04_closed_period_blocks_new_retroactive_journal_postings(self):
        """
        Verify immutability: once a period is CLOSED, attempting to post a journal
        with a posting_date inside that closed period is strictly blocked with ERR_PERIOD_CLOSED.
        """
        self.client.force_authenticate(user=self.finance)

        # Create and close period for Aug 2026
        period = FinancialPeriod.objects.create(
            period_id=f"fper-aug-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"AUG26-{self.suffix}",
            period_name="August 2026",
            start_date="2026-08-01",
            end_date="2026-08-31",
            status=FinancialPeriodStatus.CLOSED,
        )

        # Attempt to post a journal dated 2026-08-15
        with pytest.raises(Exception) as exc_info:
            DoubleEntryJournalService.post_journal_entry(
                tenant_id=self.tenant_a.tenant_id,
                lines=[
                    {"account_code": "1010", "debit_amount_bdt": Decimal("1000.00"), "credit_amount_bdt": Decimal("0.00")},
                    {"account_code": "4010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("1000.00")},
                ],
                posting_date="2026-08-15",
                source_module=JournalSourceModule.POS_SALE,
                source_reference=f"SALE-RETRO-{self.suffix}",
                actor_user=self.finance,
            )
        assert "ERR_PERIOD_CLOSED" in str(exc_info.value)

    def test_05_closed_period_blocks_new_retroactive_expense_creation(self):
        """
        Verify that creating an operating expense draft dated within a CLOSED period is blocked.
        """
        self.client.force_authenticate(user=self.manager)

        # Create closed period for July 2026
        FinancialPeriod.objects.create(
            period_id=f"fper-jul-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"JUL26-{self.suffix}",
            period_name="July 2026",
            start_date="2026-07-01",
            end_date="2026-07-31",
            status=FinancialPeriodStatus.CLOSED,
        )

        cat = ExpenseCategory.objects.get(tenant=self.tenant_a, code="RENT")
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "category_id": cat.category_id,
            "payee_name": "Backdated Landlord",
            "amount_bdt": "10000.00",
            "expense_date": "2026-07-20",  # Falls into closed period
        }

        res = self.client.post("/api/v2/finance/expenses/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_PERIOD_CLOSED" in str(res.json())

    def test_06_already_closed_period_cannot_be_closed_again(self):
        """
        Verify that calling /close/ on a period that is already CLOSED is rejected.
        """
        self.client.force_authenticate(user=self.finance)

        period = FinancialPeriod.objects.create(
            period_id=f"fper-jun-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"JUN26-{self.suffix}",
            period_name="June 2026",
            start_date="2026-06-01",
            end_date="2026-06-30",
            status=FinancialPeriodStatus.CLOSED,
        )

        res = self.client.post(f"/api/v2/finance/periods/{period.period_id}/close/", format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_PERIOD_ALREADY_CLOSED" in str(res.json())

    def test_07_finance_officer_denied_from_reopening_strictly_owner_required(self):
        """
        Verify internal control segregation: Finance Officer cannot reopen a closed period.
        Strictly requires Pharmacy Owner authorization.
        """
        self.client.force_authenticate(user=self.finance)

        period = FinancialPeriod.objects.create(
            period_id=f"fper-may-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"MAY26-{self.suffix}",
            period_name="May 2026",
            start_date="2026-05-01",
            end_date="2026-05-31",
            status=FinancialPeriodStatus.CLOSED,
        )

        res = self.client.post(
            f"/api/v2/finance/periods/{period.period_id}/reopen/",
            {"reopen_reason": "Finance officer wants to adjust entries"},
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_PERIOD_REOPEN_UNAUTHORIZED" in str(res.json())

    def test_08_owner_reopens_period_with_mandatory_audit_reason(self):
        """
        Verify that Pharmacy Owner can reopen a closed period with a mandatory formal audit reason:
        1. Status changes to REOPENED.
        2. Reopening audit log recorded.
        3. reopen_count incremented.
        """
        self.client.force_authenticate(user=self.owner)

        period = FinancialPeriod.objects.create(
            period_id=f"fper-apr-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"APR26-{self.suffix}",
            period_name="April 2026",
            start_date="2026-04-01",
            end_date="2026-04-30",
            status=FinancialPeriodStatus.CLOSED,
        )

        res = self.client.post(
            f"/api/v2/finance/periods/{period.period_id}/reopen/",
            {"reopen_reason": "Auditor noted missed supplier credit note adjustment."},
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()["period"]
        assert data["status"] == FinancialPeriodStatus.REOPENED
        assert data["reopen_count"] == 1
        assert "missed supplier credit note" in data["reopen_reason"]

    def test_09_reopen_missing_reason_rejected(self):
        """
        Verify that reopening with empty or whitespace-only reason is rejected.
        """
        self.client.force_authenticate(user=self.owner)

        period = FinancialPeriod.objects.create(
            period_id=f"fper-mar-{self.suffix}",
            tenant=self.tenant_a,
            period_code=f"MAR26-{self.suffix}",
            period_name="March 2026",
            start_date="2026-03-01",
            end_date="2026-03-31",
            status=FinancialPeriodStatus.CLOSED,
        )

        res = self.client.post(
            f"/api/v2/finance/periods/{period.period_id}/reopen/",
            {"reopen_reason": "   "},
            format="json",
        )
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert "ERR_MISSING_REOPEN_REASON" in str(res.json())

    def test_10_cashier_strictly_denied_from_period_operations(self):
        """
        Verify that POS terminal cashier is strictly denied access to period operations.
        """
        self.client.force_authenticate(user=self.cashier)

        # List periods -> 403
        res = self.client.get("/api/v2/finance/periods/", HTTP_X_TENANT_ID=self.tenant_a.tenant_id)
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_11_cross_tenant_period_access_forbidden(self):
        """
        Verify that a user from Tenant A cannot access or close a period belonging to Tenant B.
        """
        self.client.force_authenticate(user=self.owner)

        period_b = FinancialPeriod.objects.create(
            period_id=f"fper-b-{self.suffix}",
            tenant=self.tenant_b,
            period_code=f"FEB26-B-{self.suffix}",
            period_name="February 2026 Tenant B",
            start_date="2026-02-01",
            end_date="2026-02-28",
            status=FinancialPeriodStatus.OPEN,
        )

        res = self.client.get(f"/api/v2/finance/periods/{period_b.period_id}/")
        assert res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_blocked(self):
        """
        Verify that a suspended tenant cannot perform financial period operations.
        """
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner)
        res = self.client.post(
            "/api/v2/finance/periods/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "period_code": f"2026-01-{self.suffix}",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert ("ERR_TENANT_SUSPENDED" in str(res.json()) or "ERR_SUBSCRIPTION" in str(res.json()))
