"""
backend/tests/test_p281_chart_of_accounts.py
Architecture Decision: ADR-0281 (P281).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying General Ledger Chart of Accounts (COA):
1. Default pharmacy COA initialization provisions 21 standard accounts.
2. Idempotent initialization ensures zero duplicate creation.
3. Owner / Finance Officer can create custom operating expense accounts.
4. Duplicate account code in the same tenant is rejected (ERR_DUPLICATE_ACCOUNT_CODE, 422/400).
5. Missing account code or name rejected with validation error (422/400).
6. Account metadata update succeeds for authorized roles.
7. Zero-balance custom account can be archived.
8. System default accounts are immutable against deletion (ERR_SYSTEM_ACCOUNT_IMMUTABLE, 422/400).
9. Trial balance endpoint compiles total debits, credits and balance verification.
10. Branch Manager can view accounts but is denied creation (ERR_COA_MANAGE_UNAUTHORIZED, 403).
11. Cashier and staff pharmacist strictly denied all COA endpoints (ERR_STAFF_ROLE_DENIED, 403).
12. Cross-tenant isolation and suspended tenant account write protection.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    ChartOfAccount,
    GLAccountCategory,
    GLAccountStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestChartOfAccountsIntegration:

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
            name=f"Foreign Health {self.suffix}",
            subdomain=f"for-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Foreign Branch",
        )

        # 3. Suspended Tenant
        self.suspended_tenant = Tenant.objects.create(
            tenant_id=f"ten-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-{self.suffix}",
            status=TenantLifecycleStatus.SUSPENDED,
        )

        # 4. Users
        self.owner = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.finance = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Finance Officer",
            password="StrongPassword123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@square.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@square.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@square.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Suspended Owner",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

    def test_01_initialize_default_chart_of_accounts(self):
        """Standard 21 pharmacy GL accounts are initialized for the tenant."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post("/api/v2/finance/chart-of-accounts/initialize/", format="json")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["success"] is True
        assert data["total_accounts"] >= 21

        # Verify key pharmacy accounts exist
        codes = set(ChartOfAccount.objects.filter(tenant=self.tenant_a).values_list("account_code", flat=True))
        assert "1010" in codes  # Cash
        assert "1020" in codes  # Bank
        assert "1030" in codes  # AR
        assert "1040" in codes  # Inventory
        assert "2010" in codes  # AP
        assert "4010" in codes  # Medicine Sales
        assert "5010" in codes  # COGS
        assert "6010" in codes  # Rent

    def test_02_idempotent_initialization(self):
        """Second call to initialize provisions 0 new accounts and reports existing."""
        self.client.force_authenticate(user=self.finance)
        r1 = self.client.post("/api/v2/finance/chart-of-accounts/initialize/", format="json")
        assert r1.status_code == status.HTTP_200_OK
        assert r1.json()["accounts_created"] >= 21

        r2 = self.client.post("/api/v2/finance/chart-of-accounts/initialize/", format="json")
        assert r2.status_code == status.HTTP_200_OK
        assert r2.json()["accounts_created"] == 0
        assert r2.json()["accounts_existing"] >= 21

    def test_03_create_custom_operating_expense_account(self):
        """Owner creates custom operating expense account."""
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            "/api/v2/finance/chart-of-accounts/",
            {
                "account_code": "6070",
                "account_name": "Generator Diesel & Fuel Expense",
                "account_name_bn": "জেনারেটর ডিজেল ও জ্বালানি খরচ",
                "category": "EXPENSE",
                "description": "Cold chain backup power fuel cost",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()
        assert data["success"] is True
        assert data["account_code"] == "6070"
        assert data["category"] == "EXPENSE"
        assert data["normal_balance"] == "DEBIT"

        # Verify DB
        assert ChartOfAccount.objects.filter(tenant=self.tenant_a, account_code="6070").exists()

    def test_04_duplicate_account_code_rejected(self):
        """Creating an account with duplicate code is rejected with ERR_DUPLICATE_ACCOUNT_CODE."""
        ChartOfAccount.objects.create(
            tenant=self.tenant_a,
            account_code="6080",
            account_name="Existing Account",
            category=GLAccountCategory.EXPENSE,
        )

        self.client.force_authenticate(user=self.finance)
        resp = self.client.post(
            "/api/v2/finance/chart-of-accounts/",
            {
                "account_code": "6080",
                "account_name": "Duplicate Code Account",
                "category": "EXPENSE",
            },
            format="json",
        )
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.json()["error_code"] == "ERR_DUPLICATE_ACCOUNT_CODE"

    def test_05_missing_code_or_name_rejected(self):
        """Missing required code or name is rejected with validation error."""
        self.client.force_authenticate(user=self.owner)
        r1 = self.client.post(
            "/api/v2/finance/chart-of-accounts/",
            {"account_code": "", "account_name": "No Code", "category": "EXPENSE"},
            format="json",
        )
        assert r1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert r1.json()["error_code"] == "ERR_MISSING_ACCOUNT_CODE"

        r2 = self.client.post(
            "/api/v2/finance/chart-of-accounts/",
            {"account_code": "6090", "account_name": "", "category": "EXPENSE"},
            format="json",
        )
        assert r2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert r2.json()["error_code"] == "ERR_MISSING_ACCOUNT_NAME"

    def test_06_update_account_metadata(self):
        """Finance Officer updates mutable metadata of a custom account."""
        acc = ChartOfAccount.objects.create(
            tenant=self.tenant_a,
            account_code="6095",
            account_name="Old Office Name",
            category=GLAccountCategory.EXPENSE,
        )

        self.client.force_authenticate(user=self.finance)
        resp = self.client.patch(
            f"/api/v2/finance/chart-of-accounts/{acc.account_id}/",
            {"account_name": "Updated Administrative Expense", "description": "New description"},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["success"] is True
        assert data["account_name"] == "Updated Administrative Expense"

        acc.refresh_from_db()
        assert acc.account_name == "Updated Administrative Expense"
        assert acc.description == "New description"

    def test_07_archive_custom_account_success(self):
        """Zero-balance custom account can be archived."""
        acc = ChartOfAccount.objects.create(
            tenant=self.tenant_a,
            account_code="6099",
            account_name="Temporary Expense",
            category=GLAccountCategory.EXPENSE,
            is_system_account=False,
            current_balance_bdt=Decimal("0.00"),
        )

        self.client.force_authenticate(user=self.owner)
        resp = self.client.delete(f"/api/v2/finance/chart-of-accounts/{acc.account_id}/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["status"] == GLAccountStatus.ARCHIVED

        acc.refresh_from_db()
        assert acc.status == GLAccountStatus.ARCHIVED

    def test_08_system_account_deletion_blocked(self):
        """Attempting to archive or delete standard system account is blocked."""
        sys_acc = ChartOfAccount.objects.create(
            tenant=self.tenant_a,
            account_code="1010",
            account_name="Till Cash",
            category=GLAccountCategory.ASSET,
            is_system_account=True,
            current_balance_bdt=Decimal("0.00"),
        )

        self.client.force_authenticate(user=self.owner)
        resp = self.client.delete(f"/api/v2/finance/chart-of-accounts/{sys_acc.account_id}/")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.json()["error_code"] == "ERR_SYSTEM_ACCOUNT_IMMUTABLE"

    def test_09_trial_balance_summary(self):
        """Trial balance endpoint compiles total debits, credits and balance verification."""
        # Provision accounts with sample balances
        ChartOfAccount.objects.create(
            tenant=self.tenant_a,
            account_code="1010",
            account_name="Cash",
            category=GLAccountCategory.ASSET,
            normal_balance="DEBIT",
            current_balance_bdt=Decimal("15000.00"),
        )
        ChartOfAccount.objects.create(
            tenant=self.tenant_a,
            account_code="3010",
            account_name="Capital",
            category=GLAccountCategory.EQUITY,
            normal_balance="CREDIT",
            current_balance_bdt=Decimal("15000.00"),
        )

        self.client.force_authenticate(user=self.manager)
        resp = self.client.get("/api/v2/finance/chart-of-accounts/trial-balance/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["total_debit_bdt"] == "15000.00"
        assert data["total_credit_bdt"] == "15000.00"
        assert data["variance_bdt"] == "0.00"
        assert data["is_trial_balance_balanced"] is True

    def test_10_branch_manager_view_allowed_but_create_denied(self):
        """Branch Manager can view accounts but is blocked from creating accounts."""
        self.client.force_authenticate(user=self.manager)
        r_list = self.client.get("/api/v2/finance/chart-of-accounts/")
        assert r_list.status_code == status.HTTP_200_OK

        r_create = self.client.post(
            "/api/v2/finance/chart-of-accounts/",
            {"account_code": "6100", "account_name": "Test", "category": "EXPENSE"},
            format="json",
        )
        assert r_create.status_code == status.HTTP_403_FORBIDDEN
        assert r_create.json()["error_code"] == "ERR_COA_MANAGE_UNAUTHORIZED"

    def test_11_staff_roles_strictly_denied(self):
        """Cashier and staff pharmacist are strictly forbidden from COA endpoints."""
        for user in [self.cashier, self.pharmacist]:
            self.client.force_authenticate(user=user)
            resp = self.client.get("/api/v2/finance/chart-of-accounts/")
            assert resp.status_code == status.HTTP_403_FORBIDDEN
            assert resp.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_12_cross_tenant_isolation_and_suspended_tenant(self):
        """Cross-tenant access forbidden; suspended tenant account write blocked."""
        foreign_acc = ChartOfAccount.objects.create(
            tenant=self.tenant_b,
            account_code="6200",
            account_name="Foreign Account",
            category=GLAccountCategory.EXPENSE,
        )

        self.client.force_authenticate(user=self.owner)
        r_foreign = self.client.patch(
            f"/api/v2/finance/chart-of-accounts/{foreign_acc.account_id}/",
            {"account_name": "Hacked Name"},
            format="json",
        )
        assert r_foreign.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        if r_foreign.status_code == status.HTTP_403_FORBIDDEN:
            assert r_foreign.json()["error_code"] == "ERR_CROSS_TENANT_ACCESS"

        # Suspended tenant write blocked
        self.client.force_authenticate(user=self.suspended_user)
        r_susp = self.client.post(
            "/api/v2/finance/chart-of-accounts/",
            {"account_code": "6300", "account_name": "Suspended Create", "category": "EXPENSE"},
            format="json",
        )
        assert r_susp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_402_PAYMENT_REQUIRED)
        assert r_susp.json()["error_code"] in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")
