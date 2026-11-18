"""
backend/tests/test_p293_profit_loss_report.py
Architecture Decision: ADR-0293 (P293).
Phase 15: General Ledger, Operating Expenses & Profitability.

Comprehensive targeted integration tests for Profit & Loss (Income Statement) reporting:
1. Owner generates consolidated P&L with accurate revenue, COGS, gross profit, OpEx & net operating profit.
2. Multi-branch profitability breakdown correctly isolates branch revenue and expenses.
3. Branch Manager can access assigned branch P&L, but is forbidden from accessing other branches.
4. POS Cashier is strictly denied access to P&L financial reports (403 Forbidden).
5. Staff Pharmacist is strictly denied access to P&L reports (403 Forbidden).
6. Date range filtering includes only transactions within the specified window.
7. Invalid date range (start_date > end_date) is rejected (400 Bad Request).
8. Period closing journal entries (source_module='CLOSING') are excluded from nominal P&L.
9. CSV export endpoint produces valid CSV headers, revenue/cogs/opex lines, and margins.
10. Cross-tenant branch access is forbidden (403 Forbidden).
11. Suspended tenant subscription blocks P&L generation (403 Forbidden).
12. Unauthenticated requests are rejected.
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
from apps.core.financial_period_service import FinancialPeriodService
from apps.core.models import (
    Branch,
    ChartOfAccount,
    GLAccountCategory,
    JournalEntry,
    JournalEntryLine,
    JournalEntryStatus,
    JournalSourceModule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestProfitLossReportIntegration:

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
            name="Dhanmondi Branch",
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name="Gulshan Branch",
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-b1-{self.suffix}",
            tenant=self.tenant_b,
            name="Uttara Branch",
        )

        # 2. Users for Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.finance_officer_a = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Finance Officer A",
            password="StrongPassword123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a1 = PharmacyUser.objects.create_user(
            email=f"bm1_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A1",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a2 = PharmacyUser.objects.create_user(
            email=f"bm2_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A2",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="POS Cashier A",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.staff_pharm_a = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@pharma.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist A",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 3. Foreign User for Tenant B
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@pharma.com",
            phone_number=f"+880177{random.randint(1000000, 9999999)}",
            full_name="Foreign Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        # 4. Initialize Chart of Accounts for Tenant A & B
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a
        )
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b
        )

    def _post_sample_financial_entries(self):
        """Helper posting balanced sales, discounts, lot COGS, and operating expenses."""
        acc_cash = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")
        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        acc_med_rev = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4010")
        acc_otc_rev = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4020")
        acc_disc = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4030")
        acc_cogs_lot = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="5010")
        acc_cogs_exp = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="5020")
        acc_rent = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6010")
        acc_utility = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6020")
        acc_salaries = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6030")
        acc_internet = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6040")

        # Entry 1: Branch A1 Sales (Medicine 150,000, OTC 50,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-10",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"sale-{self.suffix}-1",
            narration="Daily OTC & Medicine Sales Collection",
            lines=[
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("200000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_med_rev.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("150000.00")},
                {"account_id": str(acc_otc_rev.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("50000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # Entry 2: Branch A1 Contra-revenue Discounts Allowed (5,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-10",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"disc-{self.suffix}-1",
            narration="Patient Special Discounts Allowed",
            lines=[
                {"account_id": str(acc_disc.account_id), "debit_amount_bdt": Decimal("5000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("5000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # Entry 3: Branch A1 Lot-based COGS & Expiry Write-off (COGS 120,000, Expiry 2,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-10",
            source_module=JournalSourceModule.STOCK_ADJUSTMENT,
            source_reference=f"cogs-{self.suffix}-1",
            narration="Cost of Goods Sold & Expiry Loss Recognition",
            lines=[
                {"account_id": str(acc_cogs_lot.account_id), "debit_amount_bdt": Decimal("120000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_cogs_exp.account_id), "debit_amount_bdt": Decimal("2000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_inv.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("122000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # Entry 4: Branch A1 Operating Expenses (Rent 15,000, Electricity 4,500, Salaries 30,000, Internet 1,500 = 51,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-15",
            source_module=JournalSourceModule.OPERATING_EXPENSE,
            source_reference=f"opex-{self.suffix}-1",
            narration="Branch Monthly Operating Expenses",
            lines=[
                {"account_id": str(acc_rent.account_id), "debit_amount_bdt": Decimal("15000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_utility.account_id), "debit_amount_bdt": Decimal("4500.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_salaries.account_id), "debit_amount_bdt": Decimal("30000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_internet.account_id), "debit_amount_bdt": Decimal("1500.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("51000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # Entry 5: Branch A2 Sales (Medicine 80,000) and OpEx (Rent 10,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date="2026-11-12",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"sale-a2-{self.suffix}",
            narration="Gulshan Branch Medicine Sales",
            lines=[
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("80000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_med_rev.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("80000.00")},
            ],
            actor_user=self.finance_officer_a,
        )
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date="2026-11-12",
            source_module=JournalSourceModule.STOCK_ADJUSTMENT,
            source_reference=f"cogs-a2-{self.suffix}",
            narration="Gulshan Branch COGS",
            lines=[
                {"account_id": str(acc_cogs_lot.account_id), "debit_amount_bdt": Decimal("50000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_inv.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("50000.00")},
            ],
            actor_user=self.finance_officer_a,
        )
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date="2026-11-16",
            source_module=JournalSourceModule.OPERATING_EXPENSE,
            source_reference=f"opex-a2-{self.suffix}",
            narration="Gulshan Branch Rent",
            lines=[
                {"account_id": str(acc_rent.account_id), "debit_amount_bdt": Decimal("10000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("10000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

    def test_01_owner_generates_accurate_consolidated_profit_and_loss(self):
        """Owner generates consolidated P&L and verifies math across revenue, COGS, gross profit & OpEx."""
        self._post_sample_financial_entries()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        data = response.data

        # Consolidated figures:
        # Net Revenue: Branch A1 (150k + 50k - 5k = 195k) + Branch A2 (80k) = 275,000.00
        rev = data["revenue"]
        assert Decimal(rev["gross_revenue_bdt"]) == Decimal("280000.00")
        assert Decimal(rev["contra_discounts_bdt"]) == Decimal("5000.00")
        assert Decimal(rev["net_revenue_bdt"]) == Decimal("275000.00")

        # COGS: Branch A1 (120k + 2k) + Branch A2 (50k) = 172,000.00
        cogs = data["cogs"]
        assert Decimal(cogs["total_cogs_bdt"]) == Decimal("172000.00")

        # Gross Profit: 275,000.00 - 172,000.00 = 103,000.00
        gp = data["gross_profit"]
        assert Decimal(gp["gross_profit_bdt"]) == Decimal("103000.00")
        assert gp["is_profitable"] is True
        # Margin: (103,000 / 275,000) * 100 = 37.45%
        assert Decimal(gp["gross_margin_pct"]) == Decimal("37.45")

        # Operating Expenses: Branch A1 (51k) + Branch A2 (10k) = 61,000.00
        opex = data["operating_expenses"]
        assert Decimal(opex["total_operating_expenses_bdt"]) == Decimal("61000.00")

        # Net Operating Profit: 103,000 - 61,000 = 42,000.00
        np = data["net_operating_profit"]
        assert Decimal(np["net_operating_profit_bdt"]) == Decimal("42000.00")
        assert np["is_profitable"] is True
        # Margin: (42,000 / 275,000) * 100 = 15.27%
        assert Decimal(np["net_margin_pct"]) == Decimal("15.27")

    def test_02_multi_branch_profitability_breakdown(self):
        """Report provides accurate isolated profitability for Branch A1 and Branch A2."""
        self._post_sample_financial_entries()

        self.client.force_authenticate(user=self.finance_officer_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_200_OK
        branches = response.data["branch_breakdown"]
        assert len(branches) == 2

        b1_stat = next(b for b in branches if b["branch_id"] == str(self.branch_a1.branch_id))
        assert Decimal(b1_stat["net_revenue_bdt"]) == Decimal("195000.00")
        assert Decimal(b1_stat["total_cogs_bdt"]) == Decimal("122000.00")
        assert Decimal(b1_stat["gross_profit_bdt"]) == Decimal("73000.00")
        assert Decimal(b1_stat["operating_expenses_bdt"]) == Decimal("51000.00")
        assert Decimal(b1_stat["net_profit_bdt"]) == Decimal("22000.00")

        b2_stat = next(b for b in branches if b["branch_id"] == str(self.branch_a2.branch_id))
        assert Decimal(b2_stat["net_revenue_bdt"]) == Decimal("80000.00")
        assert Decimal(b2_stat["total_cogs_bdt"]) == Decimal("50000.00")
        assert Decimal(b2_stat["gross_profit_bdt"]) == Decimal("30000.00")
        assert Decimal(b2_stat["operating_expenses_bdt"]) == Decimal("10000.00")
        assert Decimal(b2_stat["net_profit_bdt"]) == Decimal("20000.00")

    def test_03_branch_manager_restricted_to_assigned_branch(self):
        """Branch Manager can access own branch, but is denied other branches."""
        self._post_sample_financial_entries()

        self.client.force_authenticate(user=self.bm_a1)

        # 1. Access own branch -> 200 OK
        resp_own = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a1.branch_id}"
        )
        assert resp_own.status_code == status.HTTP_200_OK
        assert resp_own.data["branch_id"] == str(self.branch_a1.branch_id)

        # 2. Access other branch -> 403 Forbidden
        resp_other = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a2.branch_id}"
        )
        assert resp_other.status_code == status.HTTP_403_FORBIDDEN
        assert resp_other.data.get("error_code") == "ERR_BRANCH_ACCESS_DENIED"

    def test_04_cashier_denied_access_to_profit_loss(self):
        """POS cashier is strictly blocked from seeing business profitability."""
        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_PL_REPORT_UNAUTHORIZED"

    def test_05_staff_pharmacist_denied_access(self):
        """Staff pharmacist is strictly blocked from seeing financial P&L."""
        self.client.force_authenticate(user=self.staff_pharm_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_PL_REPORT_UNAUTHORIZED"

    def test_06_date_range_filtering(self):
        """Date filtering includes only journals within the specified start and end date."""
        self._post_sample_financial_entries()

        acc_cash = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")
        acc_med_rev = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4010")

        # Post an entry in October 2026 (outside November window)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-10-15",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"sale-oct-{self.suffix}",
            narration="October Medicine Sales",
            lines=[
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("40000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_med_rev.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("40000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        self.client.force_authenticate(user=self.owner_a)

        # Query only November: 2026-11-01 to 2026-11-30
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-11-01&end_date=2026-11-30"
        )
        assert response.status_code == status.HTTP_200_OK
        # October's 40,000 is excluded; gross revenue remains 280,000
        assert Decimal(response.data["revenue"]["gross_revenue_bdt"]) == Decimal("280000.00")

    def test_07_invalid_date_range_validation(self):
        """Reject invalid date range where start_date is later than end_date."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-12-01&end_date=2026-11-01"
        )
        assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert response.data.get("error_code") == "ERR_INVALID_DATE_RANGE"

    def test_08_closing_journals_excluded_from_profit_and_loss(self):
        """Period close creates closing journals; verify P&L excludes them from nominal reporting."""
        self._post_sample_financial_entries()

        # Create and close period for November
        period = FinancialPeriodService.create_period(
            tenant_id=self.tenant_a.tenant_id,
            data={
                "period_code": f"2026-11-{self.suffix[:4]}",
                "period_name": "November 2026 Test Period",
                "start_date": "2026-11-01",
                "end_date": "2026-11-30",
            },
            actor_user=self.finance_officer_a,
        )

        # Close period
        FinancialPeriodService.close_period(
            period_id=str(period["period_id"]),
            tenant_id=self.tenant_a.tenant_id,
            closing_notes="November Books Sealing",
            actor_user=self.owner_a,
        )

        # Ensure closing entry exists
        closing_entry = JournalEntry.objects.filter(
            tenant=self.tenant_a,
            source_module=JournalSourceModule.CLOSING,
        ).first()
        assert closing_entry is not None

        # Fetch P&L statement
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_200_OK

        # Closing journals did not zero out nominal accounts:
        assert Decimal(response.data["revenue"]["net_revenue_bdt"]) == Decimal("275000.00")
        assert Decimal(response.data["gross_profit"]["gross_profit_bdt"]) == Decimal("103000.00")
        assert Decimal(response.data["net_operating_profit"]["net_operating_profit_bdt"]) == Decimal("42000.00")

    def test_09_csv_export_endpoint(self):
        """Test CSV export endpoint delivers proper headers and CSV formatting."""
        self._post_sample_financial_entries()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/export/?tenant_id={self.tenant_a.tenant_id}&format=csv"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response["Content-Type"].startswith("text/csv")
        assert "attachment; filename=" in response["Content-Disposition"]

        content = response.content.decode("utf-8")
        assert "PROFIT & LOSS STATEMENT" in content
        assert "REVENUE,4010" in content
        assert "COGS,5010" in content
        assert "SUMMARY,GROSS_PROFIT" in content
        assert "SUMMARY,NET_PROFIT" in content

    def test_10_cross_tenant_branch_isolation(self):
        """User from Tenant A cannot query Branch belonging to Tenant B."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_b1.branch_id}"
        )
        assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_11_suspended_tenant_subscription_blocks_report(self):
        """Suspended tenant subscription blocks financial reporting."""
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_TENANT_SUSPENDED"

    def test_12_unauthenticated_request_rejected(self):
        """Unauthenticated requests are rejected."""
        response = self.client.get(
            f"/api/v2/finance/reports/profit-and-loss/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
