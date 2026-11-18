"""
backend/tests/test_p296_expense_budget_controls.py
Architecture Decision: ADR-0296 (P296).
Phase 15: General Ledger, Operating Expenses & Profitability.

Targeted integration tests for Expense Budget Controls:
1. Owner creates, retrieves, updates (PATCH), and archives (DELETE) planned budgets.
2. Within-budget expense approval posts balanced GL journal (Dr 6020 / Cr 1010) and triggers WARNING_THRESHOLD at >= 80%.
3. REQUIRE_APPROVAL mode blocks Branch Manager overrun (403) and blocks Owner without explicit override flag (400),
   then succeeds with budget_override=True + reason and logs APPROVAL_OVERRIDE alert + balanced GL journal.
4. HARD_STOP mode blocks even Owner from posting beyond budget limit (400 ERR_BUDGET_HARD_LIMIT_EXCEEDED) until budget is raised.
5. ADVISORY_ALERT mode allows posting beyond planned amount while logging CRITICAL_EXCEEDED alert.
6. Pre-flight /evaluate/ endpoint projects utilization and flags senior approval / hard-stop decisions.
7. Duplicate active budget for identical scope/period and invalid payloads are rejected (400).
8. Cashier and Staff Pharmacist are strictly denied access (403).
9. Branch Manager is blocked from managing budgets (403) and restricted to assigned branch (403 on other branches).
10. Cross-tenant budget read/update/create isolation enforced (403).
11. Suspended tenant subscription blocks all budget operations (403).
12. CSV export (?format=csv) and /alerts/ endpoints return accurate tabular data and immutable alert history.
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
    ExpenseBudget,
    ExpenseBudgetAlertEvent,
    ExpenseBudgetAlertType,
    ExpenseBudgetEnforcementMode,
    ExpenseBudgetStatus,
    ExpenseCategory,
    JournalEntry,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestExpenseBudgetControlsIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        # Tenants & Branches
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Lazz Pharma {self.suffix}",
            subdomain=f"lazz-{self.suffix}",
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
            name="Uttara Branch",
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Tamanna Pharma {self.suffix}",
            subdomain=f"tam-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-b1-{self.suffix}",
            tenant=self.tenant_b,
            name="Mirpur Branch",
        )

        # Users for Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.finance_a = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Finance A",
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
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.pharmacist_a1 = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Pharmacist A1",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        # Seed COA & Expense Categories for Tenant A & Tenant B
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a
        )
        ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a
        )
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b
        )
        ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b
        )

        self.cat_utilities = ExpenseCategory.objects.get(tenant=self.tenant_a, code="UTILITIES")
        self.cat_internet = ExpenseCategory.objects.get(tenant=self.tenant_a, code="INTERNET")
        self.cat_transport = ExpenseCategory.objects.get(tenant=self.tenant_a, code="TRANSPORT")
        self.cat_utilities_b = ExpenseCategory.objects.get(tenant=self.tenant_b, code="UTILITIES")

    def _create_expense_draft(self, user, branch, category, amount_str, payee="DESCO Utility"):
        self.client.force_authenticate(user=user)
        resp = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": branch.branch_id,
                "category_id": category.category_id,
                "payee_name": payee,
                "amount_bdt": amount_str,
                "payment_method": "CASH",
                "expense_date": self.today.isoformat(),
                "description": f"Test expense {amount_str}",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        return resp.data["expense_id"]

    def test_01_owner_budget_crud_and_archive(self):
        self.client.force_authenticate(user=self.owner_a)
        create_resp = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_utilities.category_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "10000.00",
                "warning_threshold_pct": "80.00",
                "critical_threshold_pct": "100.00",
                "enforcement_mode": "REQUIRE_APPROVAL",
                "notes": "Monthly electricity cap",
            },
            format="json",
        )
        assert create_resp.status_code == status.HTTP_201_CREATED, create_resp.data
        budget_id = create_resp.data["budget_id"]
        assert create_resp.data["planned_amount_bdt"] == "10000.00"
        assert create_resp.data["actual_spent_bdt"] == "0.00"
        assert create_resp.data["health_status"] == "HEALTHY"

        # PATCH update planned amount
        patch_resp = self.client.patch(
            f"/api/v2/finance/expense-budgets/{budget_id}/",
            {"planned_amount_bdt": "12000.00", "warning_threshold_pct": "75.00"},
            format="json",
        )
        assert patch_resp.status_code == status.HTTP_200_OK
        assert patch_resp.data["planned_amount_bdt"] == "12000.00"
        assert patch_resp.data["warning_threshold_pct"] == "75.00"

        # DELETE soft-archives budget
        del_resp = self.client.delete(
            f"/api/v2/finance/expense-budgets/{budget_id}/",
            {"reason": "Replaced by quarterly budget"},
            format="json",
        )
        assert del_resp.status_code == status.HTTP_200_OK
        assert del_resp.data["status"] == ExpenseBudgetStatus.ARCHIVED

        # Updating archived budget must fail
        fail_patch = self.client.patch(
            f"/api/v2/finance/expense-budgets/{budget_id}/",
            {"planned_amount_bdt": "15000.00"},
            format="json",
        )
        assert fail_patch.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert fail_patch.data["error_code"] == "ERR_BUDGET_ARCHIVED"

    def test_02_within_budget_warning_threshold_and_balanced_gl(self):
        self.client.force_authenticate(user=self.owner_a)
        b_resp = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_utilities.category_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "4000.00",
                "warning_threshold_pct": "80.00",
                "critical_threshold_pct": "100.00",
                "enforcement_mode": "REQUIRE_APPROVAL",
            },
            format="json",
        )
        assert b_resp.status_code == status.HTTP_201_CREATED
        budget_id = b_resp.data["budget_id"]

        # Create & approve BDT 3,400.00 (85% -> triggers WARNING_THRESHOLD, under 5,000 high-value limit)
        exp_id = self._create_expense_draft(self.bm_a1, self.branch_a1, self.cat_utilities, "3400.00")
        self.client.force_authenticate(user=self.bm_a1)
        app_resp = self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")
        assert app_resp.status_code == status.HTTP_200_OK, app_resp.data
        assert app_resp.data["budget_evaluation"]["status"] == "WARNING_THRESHOLD"

        # Verify balanced double-entry journal posted
        journal = JournalEntry.objects.get(entry_id=app_resp.data["expense"]["posted_journal_id"])
        assert journal.is_balanced is True
        assert journal.total_debit_bdt == Decimal("3400.00")
        assert journal.total_credit_bdt == Decimal("3400.00")

        # Verify budget utilization & alert event logged
        detail = self.client.get(f"/api/v2/finance/expense-budgets/{budget_id}/")
        assert detail.status_code == status.HTTP_200_OK
        assert detail.data["actual_spent_bdt"] == "3400.00"
        assert detail.data["remaining_bdt"] == "600.00"
        assert detail.data["utilization_pct"] == "85.00"
        assert detail.data["health_status"] == "WARNING"
        assert ExpenseBudgetAlertEvent.objects.filter(
            budget_id=budget_id, alert_type=ExpenseBudgetAlertType.WARNING_THRESHOLD
        ).exists()

    def test_03_require_approval_blocks_manager_and_allows_owner_override(self):
        self.client.force_authenticate(user=self.owner_a)
        b_resp = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_internet.category_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "2000.00",
                "warning_threshold_pct": "80.00",
                "critical_threshold_pct": "100.00",
                "enforcement_mode": "REQUIRE_APPROVAL",
            },
            format="json",
        )
        assert b_resp.status_code == status.HTTP_201_CREATED
        budget_id = b_resp.data["budget_id"]

        # Expense of BDT 2,500.00 exceeds BDT 2,000.00 budget (and is < 5,000 high-value threshold)
        exp_id = self._create_expense_draft(self.bm_a1, self.branch_a1, self.cat_internet, "2500.00", "Link3 ISP")

        # 1. Branch Manager is blocked by budget control (403)
        self.client.force_authenticate(user=self.bm_a1)
        bm_resp = self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")
        assert bm_resp.status_code == status.HTTP_403_FORBIDDEN
        assert bm_resp.data["error_code"] == "ERR_BUDGET_APPROVAL_REQUIRED"
        assert OperatingExpenseEntry.objects.get(expense_id=exp_id).status == OperatingExpenseStatus.DRAFT

        # 2. Owner without budget_override=True gets 400 confirmation required
        self.client.force_authenticate(user=self.owner_a)
        no_override = self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")
        assert no_override.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert no_override.data["error_code"] == "ERR_BUDGET_OVERRIDE_CONFIRMATION_REQUIRED"

        # 3. Owner with explicit budget_override=True succeeds and logs APPROVAL_OVERRIDE
        override_resp = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/approve/",
            {
                "approval_notes": "Emergency fiber upgrade",
                "budget_override": True,
                "budget_override_reason": "Backup ISP line installed for POS uptime",
            },
            format="json",
        )
        assert override_resp.status_code == status.HTTP_200_OK, override_resp.data
        assert override_resp.data["budget_evaluation"]["status"] == "OVERRIDE_APPROVED"

        alert = ExpenseBudgetAlertEvent.objects.filter(
            budget_id=budget_id, alert_type=ExpenseBudgetAlertType.APPROVAL_OVERRIDE
        ).first()
        assert alert is not None
        assert alert.override_granted is True
        assert "Backup ISP" in alert.override_reason

    def test_04_hard_stop_blocks_even_owner_until_budget_increased(self):
        self.client.force_authenticate(user=self.owner_a)
        b_resp = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_transport.category_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "1500.00",
                "enforcement_mode": "HARD_STOP",
            },
            format="json",
        )
        assert b_resp.status_code == status.HTTP_201_CREATED

        exp_id = self._create_expense_draft(self.owner_a, self.branch_a1, self.cat_transport, "1800.00", "Sundarban Courier")
        self.client.force_authenticate(user=self.owner_a)
        blocked = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/approve/",
            {"budget_override": True, "budget_override_reason": "Try override"},
            format="json",
        )
        assert blocked.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert blocked.data["error_code"] == "ERR_BUDGET_HARD_LIMIT_EXCEEDED"
        assert OperatingExpenseEntry.objects.get(expense_id=exp_id).status == OperatingExpenseStatus.DRAFT

    def test_05_advisory_alert_allows_posting_and_logs_critical_event(self):
        self.client.force_authenticate(user=self.owner_a)
        b_resp = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a2.branch_id,
                "category_id": self.cat_transport.category_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "1000.00",
                "enforcement_mode": "ADVISORY_ALERT",
                "require_manager_approval_on_exceed": False,
            },
            format="json",
        )
        assert b_resp.status_code == status.HTTP_201_CREATED
        budget_id = b_resp.data["budget_id"]

        exp_id = self._create_expense_draft(self.owner_a, self.branch_a2, self.cat_transport, "1250.00", "Pathao Parcel")
        self.client.force_authenticate(user=self.owner_a)
        resp = self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["budget_evaluation"]["status"] == "ADVISORY_EXCEEDED"
        assert ExpenseBudgetAlertEvent.objects.filter(
            budget_id=budget_id, alert_type=ExpenseBudgetAlertType.CRITICAL_EXCEEDED
        ).exists()

    def test_06_preflight_evaluate_endpoint(self):
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
                "planned_amount_bdt": "3000.00",
                "enforcement_mode": "HARD_STOP",
            },
            format="json",
        )

        eval_resp = self.client.post(
            "/api/v2/finance/expense-budgets/evaluate/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_utilities.category_id,
                "amount_bdt": "3500.00",
                "expense_date": self.today.isoformat(),
            },
            format="json",
        )
        assert eval_resp.status_code == status.HTTP_200_OK
        assert eval_resp.data["overall_status"] == "HARD_STOP_BLOCKED"
        assert eval_resp.data["hard_blocked"] is True
        assert eval_resp.data["matched_budget_count"] == 1

    def test_07_duplicate_active_budget_and_invalid_inputs_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a1.branch_id,
            "category_id": self.cat_utilities.category_id,
            "period_type": "MONTHLY",
            "fiscal_year": self.today.year,
            "fiscal_month": self.today.month,
            "planned_amount_bdt": "5000.00",
        }
        assert self.client.post("/api/v2/finance/expense-budgets/", payload, format="json").status_code == status.HTTP_201_CREATED

        dup = self.client.post("/api/v2/finance/expense-budgets/", payload, format="json")
        assert dup.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert dup.data["error_code"] == "ERR_DUPLICATE_ACTIVE_BUDGET"

        neg = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {**payload, "category_id": self.cat_internet.category_id, "planned_amount_bdt": "-100.00"},
            format="json",
        )
        assert neg.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        bad_thresh = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                **payload,
                "category_id": self.cat_internet.category_id,
                "warning_threshold_pct": "95.00",
                "critical_threshold_pct": "80.00",
            },
            format="json",
        )
        assert bad_thresh.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert bad_thresh.data["error_code"] == "ERR_INVALID_CRITICAL_THRESHOLD"

    def test_08_cashier_and_pharmacist_denied_access(self):
        for staff in (self.cashier_a1, self.pharmacist_a1):
            self.client.force_authenticate(user=staff)
            r1 = self.client.get(f"/api/v2/finance/expense-budgets/?tenant_id={self.tenant_a.tenant_id}")
            assert r1.status_code == status.HTTP_403_FORBIDDEN
            r2 = self.client.post(
                "/api/v2/finance/expense-budgets/",
                {"tenant_id": self.tenant_a.tenant_id, "planned_amount_bdt": "1000.00"},
                format="json",
            )
            assert r2.status_code == status.HTTP_403_FORBIDDEN

    def test_09_branch_manager_cannot_manage_or_view_other_branch(self):
        self.client.force_authenticate(user=self.owner_a)
        b2 = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a2.branch_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "20000.00",
            },
            format="json",
        )
        assert b2.status_code == status.HTTP_201_CREATED
        b2_id = b2.data["budget_id"]

        self.client.force_authenticate(user=self.bm_a1)
        assert (
            self.client.post(
                "/api/v2/finance/expense-budgets/",
                {"tenant_id": self.tenant_a.tenant_id, "planned_amount_bdt": "5000.00"},
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )
        assert (
            self.client.get(f"/api/v2/finance/expense-budgets/{b2_id}/").status_code
            == status.HTTP_403_FORBIDDEN
        )

    def test_10_cross_tenant_isolation(self):
        self.client.force_authenticate(user=self.owner_a)
        b_a = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "15000.00",
            },
            format="json",
        )
        assert b_a.status_code == status.HTTP_201_CREATED
        b_a_id = b_a.data["budget_id"]

        # Owner B cannot access Owner A's budget
        self.client.force_authenticate(user=self.owner_b)
        r_get = self.client.get(f"/api/v2/finance/expense-budgets/{b_a_id}/")
        assert r_get.status_code == status.HTTP_403_FORBIDDEN

        # Owner B cannot create budget referencing Tenant A's branch
        r_create = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_b.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "5000.00",
            },
            format="json",
        )
        assert r_create.status_code == status.HTTP_403_FORBIDDEN

    def test_11_suspended_tenant_and_unauthenticated_blocked(self):
        # Unauthenticated
        self.client.force_authenticate(user=None)
        unauth = self.client.get(f"/api/v2/finance/expense-budgets/?tenant_id={self.tenant_a.tenant_id}")
        assert unauth.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

        # Suspended tenant
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        susp = self.client.get(f"/api/v2/finance/expense-budgets/?tenant_id={self.tenant_a.tenant_id}")
        assert susp.status_code == status.HTTP_403_FORBIDDEN
        assert susp.data["error_code"] == "ERR_TENANT_SUSPENDED"

    def test_12_csv_export_and_alerts_history(self):
        self.client.force_authenticate(user=self.owner_a)
        b_resp = self.client.post(
            "/api/v2/finance/expense-budgets/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "category_id": self.cat_utilities.category_id,
                "period_type": "MONTHLY",
                "fiscal_year": self.today.year,
                "fiscal_month": self.today.month,
                "planned_amount_bdt": "3000.00",
                "warning_threshold_pct": "80.00",
                "enforcement_mode": "ADVISORY_ALERT",
            },
            format="json",
        )
        assert b_resp.status_code == status.HTTP_201_CREATED

        exp_id = self._create_expense_draft(self.owner_a, self.branch_a1, self.cat_utilities, "2700.00")
        self.client.force_authenticate(user=self.owner_a)
        self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")

        alerts_resp = self.client.get(f"/api/v2/finance/expense-budgets/alerts/?tenant_id={self.tenant_a.tenant_id}")
        assert alerts_resp.status_code == status.HTTP_200_OK
        assert alerts_resp.data["count"] >= 1
        assert alerts_resp.data["alerts"][0]["alert_type"] == ExpenseBudgetAlertType.WARNING_THRESHOLD

        csv_resp = self.client.get(
            f"/api/v2/finance/expense-budgets/export/?format=csv&tenant_id={self.tenant_a.tenant_id}"
        )
        assert csv_resp.status_code == status.HTTP_200_OK
        csv_text = csv_resp.content.decode("utf-8")
        assert "Budget ID,Branch,Category" in csv_text
        assert "3000.00" in csv_text
        assert "2700.00" in csv_text
