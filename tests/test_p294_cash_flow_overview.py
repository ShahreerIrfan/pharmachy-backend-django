"""
backend/tests/test_p294_cash_flow_overview.py
Architecture Decision: ADR-0294 (P294).
Phase 15: General Ledger, Operating Expenses & Profitability.

Comprehensive targeted integration tests for Cash Flow Overview:
1. Owner generates consolidated Cash Flow Overview with real receipts, payments and balances.
2. Treasury transfers between cash and bank produce zero net impact on total liquid funds.
3. Accrual vs cash flow reconciliation highlights revenue and expense timing variances.
4. Multi-branch breakdown correctly isolates branch cash flows.
5. Branch Manager is restricted to assigned branch (403 on other branches).
6. POS Cashier is strictly denied access (403 Forbidden).
7. Staff Pharmacist is strictly denied access (403 Forbidden).
8. Date range filtering and opening balance computation prior to start_date.
9. Invalid date range (start_date > end_date) is rejected (400/422).
10. CSV export endpoint delivers proper headers and CSV formatting.
11. Cross-tenant branch access is forbidden (403/404).
12. Suspended tenant subscription blocks cash flow reporting (403 Forbidden).
13. Unauthenticated requests are rejected.
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
class TestCashFlowOverviewIntegration:

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

    def _post_sample_cash_flow_entries(self):
        """Helper posting balanced entries demonstrating opening balance, inflows, outflows, and transfers."""
        acc_cash = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")
        acc_bank = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1020")
        acc_ar = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1040")
        acc_ap = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        acc_equity = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="3010")
        acc_med_rev = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4010")
        acc_rent = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6010")
        acc_salaries = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6030")

        # 1. Opening Cash Float on 2026-10-31 (Prior to November period): BDT 50,000 (30,000 cash, 20,000 bank)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-10-31",
            source_module=JournalSourceModule.MANUAL,
            source_reference=f"float-{self.suffix}",
            narration="Initial Pharmacy Working Capital Float",
            lines=[
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("30000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_bank.account_id), "debit_amount_bdt": Decimal("20000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_equity.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("50000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 2. Inflow: Branch A1 POS Counter Cash Sales (BDT 200,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-05",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"pos-{self.suffix}",
            narration="Daily Counter Sales Cash Collection",
            lines=[
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("200000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_med_rev.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("200000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 3. Accrual Only: Branch A1 Credit Sale (Customer Due BDT 40,000) - No cash received yet
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-06",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"credit-{self.suffix}",
            narration="Hospital Patient Credit Dispensing",
            lines=[
                {"account_id": str(acc_ar.account_id), "debit_amount_bdt": Decimal("40000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_med_rev.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("40000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 4. Inflow: Branch A1 Customer AR Collection (BDT 25,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-08",
            source_module=JournalSourceModule.CUSTOMER_AR,
            source_reference=f"ar-col-{self.suffix}",
            narration="Customer Due Partial Cash Collection",
            lines=[
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("25000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_ar.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("25000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 5. Outflow: Branch A1 Operating Expenses Paid in Cash (Rent BDT 20,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-10",
            source_module=JournalSourceModule.OPERATING_EXPENSE,
            source_reference=f"rent-{self.suffix}",
            narration="Branch Rent Payment in Cash",
            lines=[
                {"account_id": str(acc_rent.account_id), "debit_amount_bdt": Decimal("20000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("20000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 6. Outflow: Branch A1 Supplier AP Payment via Bank (BDT 80,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-12",
            source_module=JournalSourceModule.SUPPLIER_AP,
            source_reference=f"sup-ap-{self.suffix}",
            narration="Beximco Pharma Supplier AP Bank Settlement",
            lines=[
                {"account_id": str(acc_ap.account_id), "debit_amount_bdt": Decimal("80000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_bank.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("80000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 7. Outflow: Branch A1 Staff Payroll Disbursed via Bank (BDT 30,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-15",
            source_module=JournalSourceModule.PAYROLL,
            source_reference=f"pay-{self.suffix}",
            narration="Staff Monthly Salaries Disbursement",
            lines=[
                {"account_id": str(acc_salaries.account_id), "debit_amount_bdt": Decimal("30000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_bank.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("30000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 8. Treasury Transfer: Deposit Excess Till Cash into Bank Account (BDT 50,000)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date="2026-11-18",
            source_module=JournalSourceModule.TREASURY_TRANSFER,
            source_reference=f"xfer-{self.suffix}",
            narration="Counter Till Cash Deposited into Bank Account",
            lines=[
                {"account_id": str(acc_bank.account_id), "debit_amount_bdt": Decimal("50000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("50000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # 9. Branch A2 Activity: Sales BDT 50,000 cash, Expenses BDT 10,000 cash
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date="2026-11-20",
            source_module=JournalSourceModule.POS_SALE,
            source_reference=f"pos-a2-{self.suffix}",
            narration="Gulshan Branch Cash Sales",
            lines=[
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("50000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_med_rev.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("50000.00")},
            ],
            actor_user=self.finance_officer_a,
        )
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date="2026-11-22",
            source_module=JournalSourceModule.OPERATING_EXPENSE,
            source_reference=f"exp-a2-{self.suffix}",
            narration="Gulshan Branch Maintenance in Cash",
            lines=[
                {"account_id": str(acc_rent.account_id), "debit_amount_bdt": Decimal("10000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_cash.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("10000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

    # =========================================================================
    # TESTS
    # =========================================================================

    def test_01_owner_generates_accurate_consolidated_cash_flow(self):
        """Owner generates consolidated cash flow statement and validates math."""
        self._post_sample_cash_flow_entries()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-11-01&end_date=2026-11-30"
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        data = response.data

        # 1. Opening balance prior to Nov 1 = BDT 50,000.00
        assert Decimal(data["opening_balance_bdt"]) == Decimal("50000.00")

        # 2. Operating Inflows:
        # Branch A1 (POS 200k + AR 25k) + Branch A2 (POS 50k) = 275,000.00
        inflows = data["operating_inflows"]
        assert Decimal(inflows["pos_sales_collections_bdt"]) == Decimal("250000.00")
        assert Decimal(inflows["customer_ar_collections_bdt"]) == Decimal("25000.00")
        assert Decimal(inflows["total_operating_inflows_bdt"]) == Decimal("275000.00")

        # 3. Operating Outflows:
        # Branch A1: Rent 20k, Supplier 80k, Payroll 30k = 130k
        # Branch A2: Maintenance 10k -> Total = 140,000.00
        outflows = data["operating_outflows"]
        assert Decimal(outflows["operating_expense_payments_bdt"]) == Decimal("30000.00")
        assert Decimal(outflows["supplier_ap_payments_bdt"]) == Decimal("80000.00")
        assert Decimal(outflows["payroll_disbursements_bdt"]) == Decimal("30000.00")
        assert Decimal(outflows["total_operating_outflows_bdt"]) == Decimal("140000.00")

        # 4. Net Operating Cash Flow: 275,000 - 140,000 = +135,000.00
        net_op = data["net_operating_cash_flow"]
        assert Decimal(net_op["net_operating_cash_flow_bdt"]) == Decimal("135000.00")
        assert net_op["is_positive"] is True

        # 5. Treasury Transfers: In 50k, Out 50k -> Net 0.00
        xfer = data["treasury_transfers"]
        assert Decimal(xfer["transfers_in_bdt"]) == Decimal("50000.00")
        assert Decimal(xfer["transfers_out_bdt"]) == Decimal("50000.00")
        assert Decimal(xfer["net_transfers_bdt"]) == Decimal("0.00")

        # 6. Overall Net Cash Flow: 135,000.00
        net_flow = data["net_cash_flow"]
        assert Decimal(net_flow["net_cash_flow_bdt"]) == Decimal("135000.00")
        assert net_flow["is_positive"] is True

        # 7. Closing Balance: Opening 50,000 + Net 135,000 = 185,000.00
        assert Decimal(data["closing_balance_bdt"]) == Decimal("185000.00")

    def test_02_treasury_transfer_zero_net_liquidity_impact(self):
        """Internal cash-to-bank transfers do not change total liquid funds."""
        self._post_sample_cash_flow_entries()

        self.client.force_authenticate(user=self.finance_officer_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-11-01&end_date=2026-11-30"
        )
        assert response.status_code == status.HTTP_200_OK
        xfer = response.data["treasury_transfers"]
        assert Decimal(xfer["net_transfers_bdt"]) == Decimal("0.00")

    def test_03_accrual_reconciliation_variance_detected(self):
        """Demonstrate that accrual revenue and expenses differ from real cash flow."""
        self._post_sample_cash_flow_entries()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-11-01&end_date=2026-11-30"
        )
        assert response.status_code == status.HTTP_200_OK
        rec = response.data["accrual_reconciliation"]

        # Accrual Revenue = POS 200k + Credit 40k + POS A2 50k = 290,000.00
        # Cash Inflows = POS 200k + AR 25k + POS A2 50k = 275,000.00
        # Variance = 290,000 - 275,000 = 15,000.00 (uncollected customer due)
        assert Decimal(rec["accrual_net_revenue_bdt"]) == Decimal("290000.00")
        assert Decimal(rec["cash_operating_inflows_bdt"]) == Decimal("275000.00")
        assert Decimal(rec["revenue_timing_variance_bdt"]) == Decimal("15000.00")

    def test_04_multi_branch_cash_flow_breakdown(self):
        """Report provides accurate isolated cash flow metrics per branch."""
        self._post_sample_cash_flow_entries()

        self.client.force_authenticate(user=self.finance_officer_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-11-01&end_date=2026-11-30"
        )
        assert response.status_code == status.HTTP_200_OK
        branches = response.data["branch_breakdown"]
        assert len(branches) == 2

        b1 = next(b for b in branches if b["branch_id"] == str(self.branch_a1.branch_id))
        assert Decimal(b1["operating_inflows_bdt"]) == Decimal("225000.00")
        assert Decimal(b1["operating_outflows_bdt"]) == Decimal("130000.00")
        assert Decimal(b1["net_operating_cash_flow_bdt"]) == Decimal("95000.00")

        b2 = next(b for b in branches if b["branch_id"] == str(self.branch_a2.branch_id))
        assert Decimal(b2["operating_inflows_bdt"]) == Decimal("50000.00")
        assert Decimal(b2["operating_outflows_bdt"]) == Decimal("10000.00")
        assert Decimal(b2["net_operating_cash_flow_bdt"]) == Decimal("40000.00")

    def test_05_branch_manager_restricted_to_assigned_branch(self):
        """Branch Manager can access own branch, but is forbidden from other branches."""
        self._post_sample_cash_flow_entries()

        self.client.force_authenticate(user=self.bm_a1)

        # 1. Own branch -> 200 OK
        resp_own = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a1.branch_id}"
        )
        assert resp_own.status_code == status.HTTP_200_OK
        assert resp_own.data["branch_id"] == str(self.branch_a1.branch_id)

        # 2. Other branch -> 403 Forbidden
        resp_other = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a2.branch_id}"
        )
        assert resp_other.status_code == status.HTTP_403_FORBIDDEN
        assert resp_other.data.get("error_code") == "ERR_BRANCH_ACCESS_DENIED"

    def test_06_cashier_denied_access_to_cash_flow(self):
        """POS cashier is strictly blocked from accessing business cash flow."""
        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_CASH_FLOW_UNAUTHORIZED"

    def test_07_staff_pharmacist_denied_access(self):
        """Staff pharmacist is strictly blocked from accessing cash flow."""
        self.client.force_authenticate(user=self.staff_pharm_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_CASH_FLOW_UNAUTHORIZED"

    def test_08_date_range_filtering_and_opening_balance(self):
        """Opening balance accurately sums transactions before start_date."""
        self._post_sample_cash_flow_entries()

        self.client.force_authenticate(user=self.owner_a)

        # Query November 1 to November 15 only
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-11-01&end_date=2026-11-15"
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.data

        # Opening balance remains 50,000 (from Oct 31)
        assert Decimal(data["opening_balance_bdt"]) == Decimal("50000.00")
        # Nov 20 and Nov 22 entries are excluded from period inflows
        assert Decimal(data["operating_inflows"]["pos_sales_collections_bdt"]) == Decimal("200000.00")

    def test_09_invalid_date_range_rejected(self):
        """Reject invalid date range where start_date is later than end_date."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-12-01&end_date=2026-11-01"
        )
        assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert response.data.get("error_code") == "ERR_INVALID_DATE_RANGE"

    def test_10_csv_export_endpoint(self):
        """Test CSV export endpoint delivers proper headers and CSV formatting."""
        self._post_sample_cash_flow_entries()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/export/?tenant_id={self.tenant_a.tenant_id}&format=csv"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response["Content-Type"].startswith("text/csv")
        assert "attachment; filename=" in response["Content-Disposition"]

        content = response.content.decode("utf-8")
        assert "CASH FLOW STATEMENT" in content
        assert "BALANCE,Opening Liquid Cash & Bank Balance" in content
        assert "OPERATING_INFLOW,POS Counter Sales Collections" in content
        assert "OPERATING_OUTFLOW,Supplier AP Payments" in content
        assert "SUMMARY,Net Operating Cash Flow" in content
        assert "TREASURY_TRANSFER,Internal Transfers In" in content
        assert "BALANCE,Closing Liquid Cash & Bank Balance" in content

    def test_11_cross_tenant_branch_isolation(self):
        """User from Tenant A cannot query Branch belonging to Tenant B."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_b1.branch_id}"
        )
        assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_subscription_blocks_report(self):
        """Suspended tenant subscription blocks cash flow report."""
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_TENANT_SUSPENDED"

    def test_13_unauthenticated_request_rejected(self):
        """Unauthenticated requests are rejected."""
        response = self.client.get(
            f"/api/v2/finance/reports/cash-flow/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
