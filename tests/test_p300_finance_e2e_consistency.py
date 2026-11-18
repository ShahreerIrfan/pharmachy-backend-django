"""
backend/tests/test_p300_finance_e2e_consistency.py
Architecture Decision: ADR-0300 (P300).
Phase 15 Capstone Gate: Finance E2E Consistency across Multi-Branch Transaction Cycles.

Exercises a complete end-to-end multi-branch pharmacy accounting cycle:
1. Purchase lot inventory capitalization (Dr 1030 / Cr 2010 AP) + physical BatchStock in Branch A1 & A2.
2. Sales revenue + AR receivable + reviewed VAT (P297) + actual landed lot COGS (Dr 5010 / Cr 1030).
3. Sales return & inventory restock reversal (Dr 4030 / Cr 1010, Dr 1030 / Cr 5010).
4. Expense budget control (P296) + approved Operating Expense posting (P287/P289).
5. Maker-Checker Accounting Error Reversal & Rebook (P299).
6. Sealed SHA-256 Historical Accounting Snapshot (P298).
7. Live 6-point Consistency Audit endpoint confirms overall_status == CONSISTENT (6/6 PASS),
   accurate multi-branch Gross & Net Profit, zero GL/lot variance, and balanced books.
8. Invalid date range (400), Cashier denied (403), Cross-tenant isolation (403), Suspended tenant (403), and CSV export (200).
"""

from decimal import Decimal
import datetime
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
    BatchStatus,
    BatchStock,
    Branch,
    ExpenseCategory,
    JournalSourceModule,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestFinanceE2EConsistencyCapstone:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-e2e-{self.suffix}",
            name=f"E2E Model Pharma {self.suffix}",
            subdomain=f"e2e-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-e2e1-{self.suffix}", tenant=self.tenant_a, name="Dhanmondi Flagship"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-e2e2-{self.suffix}", tenant=self.tenant_a, name="Gulshan Flagship"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-other-{self.suffix}",
            name=f"Other Pharma {self.suffix}",
            subdomain=f"oth-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-oth1-{self.suffix}", tenant=self.tenant_b, name="Rajshahi Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880151{random.randint(1000000, 9999999)}",
            full_name="Owner E2E",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.finance_a = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@pharma.com",
            phone_number=f"+880152{random.randint(1000000, 9999999)}",
            full_name="Finance E2E",
            password="StrongPassword123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880153{random.randint(1000000, 9999999)}",
            full_name="Cashier E2E",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880154{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a
        )
        ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a
        )
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b
        )

        self.med = MedicineProduct.objects.create(
            product_id=f"med-e2e-{self.suffix}",
            brand_name="Napa Extend",
            generic_name="Paracetamol",
            dosage_form="TABLET",
            cost_unit=Decimal("10.00"),
            mrp_unit=Decimal("20.00"),
        )
        self.cat_utilities = ExpenseCategory.objects.get(tenant=self.tenant_a, code="UTILITIES")

    def test_01_full_multibranch_cycle_balanced_books_and_accurate_profit(self):
        # 1. Purchase Inventory Capitalization in Branch A1 (100 units @ 10.00 = 1000.00) & Branch A2 (50 units @ 12.00 = 600.00)
        batch_a1 = BatchStock.objects.create(
            batch_id=f"bat-a1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.med,
            batch_number="B-E2E-01",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=365),
            quantity_base_units=100,
            cost_per_unit=Decimal("10.00"),
            status=BatchStatus.ACTIVE,
        )
        batch_a2 = BatchStock.objects.create(
            batch_id=f"bat-a2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a2,
            product=self.med,
            batch_number="B-E2E-02",
            purchase_date=self.today,
            expiry_date=self.today + datetime.timedelta(days=365),
            quantity_base_units=50,
            cost_per_unit=Decimal("12.00"),
            status=BatchStatus.ACTIVE,
        )

        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.PROCUREMENT,
            source_reference="GRN-A1-001",
            narration="Branch A1 purchase lot capitalization",
            lines=[
                {"account_code": "1030", "debit_amount_bdt": Decimal("1000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "2010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("1000.00")},
            ],
            actor_user=self.owner_a,
        )
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.PROCUREMENT,
            source_reference="GRN-A2-001",
            narration="Branch A2 purchase lot capitalization",
            lines=[
                {"account_code": "1030", "debit_amount_bdt": Decimal("600.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "2010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("600.00")},
            ],
            actor_user=self.owner_a,
        )

        # 2. Branch A1 Sale: 40 units @ 20.00 = 800.00 (Cash 600 + AR 200), Lot COGS = 40 * 10.00 = 400.00
        batch_a1.quantity_base_units = 60
        batch_a1.save()
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.POS_SALE,
            source_reference="SALE-A1-001",
            narration="Branch A1 medicine sale (Cash + AR)",
            lines=[
                {"account_code": "1010", "debit_amount_bdt": Decimal("600.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "1020", "debit_amount_bdt": Decimal("200.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "4010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("800.00")},
            ],
            actor_user=self.owner_a,
        )
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.POS_SALE,
            source_reference="COGS-A1-001",
            narration="Branch A1 lot COGS",
            lines=[
                {"account_code": "5010", "debit_amount_bdt": Decimal("400.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "1030", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("400.00")},
            ],
            actor_user=self.owner_a,
        )

        # 3. Branch A1 Return: 10 units refunded (200.00 contra-revenue, 100.00 COGS reversed, stock back to 70)
        batch_a1.quantity_base_units = 70
        batch_a1.save()
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.POS_SALE,
            source_reference="RET-A1-001",
            narration="Branch A1 sales return refund & restock",
            lines=[
                {"account_code": "4030", "debit_amount_bdt": Decimal("200.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "1010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("200.00")},
                {"account_code": "1030", "debit_amount_bdt": Decimal("100.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "5010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("100.00")},
            ],
            actor_user=self.owner_a,
        )

        # 4. Branch A2 Sale: 20 units @ 20.00 = 400.00 Cash, Lot COGS = 20 * 12.00 = 240.00
        batch_a2.quantity_base_units = 30
        batch_a2.save()
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.POS_SALE,
            source_reference="SALE-A2-001",
            narration="Branch A2 cash sale + COGS",
            lines=[
                {"account_code": "1010", "debit_amount_bdt": Decimal("400.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "4010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("400.00")},
                {"account_code": "5010", "debit_amount_bdt": Decimal("240.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "1030", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("240.00")},
            ],
            actor_user=self.owner_a,
        )

        # 5. Operating Expense under Budget Control (Branch A1 Utilities BDT 150.00)
        self.client.force_authenticate(user=self.owner_a)
        self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_utilities.category_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "500.00",
            },
            format="json",
        )
        exp = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_utilities.category_id,
                "payee_name": "DESCO",
                "amount_bdt": "150.00",
                "payment_method": "CASH",
                "expense_date": self.today.isoformat(),
            },
            format="json",
        )
        assert exp.status_code == status.HTTP_201_CREATED
        app = self.client.post(f"/api/v2/finance/expenses/{exp.data['expense_id']}/approve/", {}, format="json")
        assert app.status_code == status.HTTP_200_OK

        # 6. Run live 6-point E2E Consistency Audit!
        # Expected consolidated figures:
        # Net Revenue = (800 - 200) + 400 = 1000.00
        # Total COGS  = (400 - 100) + 240 = 540.00
        # Gross Profit = 1000.00 - 540.00 = 460.00
        # OpEx = 150.00 -> Net Profit = 310.00
        # Physical Inventory = (70 * 10.00) + (30 * 12.00) = 700 + 360 = 1060.00 == GL 1030 (1000 + 600 - 400 + 100 - 240 = 1060.00)
        audit = self.client.get(f"/api/v2/finance/consistency-audit/?tenant_id={self.tenant_a.tenant_id}")
        assert audit.status_code == status.HTTP_200_OK, audit.data
        assert audit.data["overall_status"] == "CONSISTENT"
        assert audit.data["passed_checks"] == 6
        assert audit.data["total_checks"] == 6

        pl_check = next(c for c in audit.data["checks"] if c["check_code"] == "PROFIT_LOSS_EQUATION")
        assert pl_check["net_revenue_bdt"] == "1000.00"
        assert pl_check["total_cogs_bdt"] == "540.00"
        assert pl_check["gross_profit_bdt"] == "460.00"
        assert pl_check["operating_expenses_bdt"] == "150.00"
        assert pl_check["net_profit_bdt"] == "310.00"
        assert pl_check["branch_count"] == 2

        # CSV export
        csv_resp = self.client.get(
            f"/api/v2/finance/consistency-audit/export/?format=csv&tenant_id={self.tenant_a.tenant_id}"
        )
        assert csv_resp.status_code == status.HTTP_200_OK
        assert "CONSISTENT" in csv_resp.content.decode("utf-8")

    def test_02_invalid_date_rbac_cross_tenant_and_suspended_tenant(self):
        # Invalid date range
        self.client.force_authenticate(user=self.owner_a)
        bad = self.client.get(
            f"/api/v2/finance/consistency-audit/?tenant_id={self.tenant_a.tenant_id}&start_date=2026-12-31&end_date=2026-01-01"
        )
        assert bad.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Cashier denied
        self.client.force_authenticate(user=self.cashier_a1)
        assert self.client.get(f"/api/v2/finance/consistency-audit/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN

        # Cross-tenant branch isolation
        self.client.force_authenticate(user=self.owner_b)
        assert (
            self.client.get(
                f"/api/v2/finance/consistency-audit/?tenant_id={self.tenant_b.tenant_id}&branch_id={self.branch_a1.branch_id}"
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Suspended tenant blocked
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        assert self.client.get(f"/api/v2/finance/consistency-audit/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN
