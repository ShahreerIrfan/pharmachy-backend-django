"""
backend/tests/test_p286_expense_categories.py
Architecture Decision: ADR-0286 (P286).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying Owner-Managed Operating Expense Categories and CRUD:
1. Seed default operating expense categories (Rent, Utilities, Salaries, Transport, Internet, Maintenance, Misc).
2. Create custom operating expense category with budget and GL mapping.
3. Retrieve single expense category detail.
4. Partial update (PATCH) category budget and description.
5. Soft-archive (DELETE) category marks is_active=False.
6. List categories with is_active query filter.
7. Missing category name rejected (ERR_MISSING_CATEGORY_NAME, 400/422).
8. Duplicate category name within tenant rejected (ERR_DUPLICATE_CATEGORY_NAME, 400/422).
9. Negative monthly budget rejected (ERR_INVALID_BUDGET, 400/422).
10. Cashier role strictly denied from viewing or managing categories (ERR_STAFF_ROLE_DENIED, 403).
11. Cross-tenant isolation prevents accessing or modifying another tenant's category (403/404).
12. Suspended tenant protection blocks expense category operations (ERR_TENANT_SUSPENDED, 403).
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.chart_of_accounts_service import ChartOfAccountsService
from apps.core.models import (
    Branch,
    ExpenseCategory,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestExpenseCategoriesIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-{self.suffix}",
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

        # 5. Initialize Charts of Accounts
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

    def test_01_seed_default_expense_categories_success(self):
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.post("/api/v2/finance/expense-categories/seed/", format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert data["status"] == "SUCCESS"
        assert data["count"] == 7
        codes = [c["code"] for c in data["categories"]]
        assert "RENT" in codes
        assert "UTILITIES" in codes
        assert "SALARIES" in codes
        assert "TRANSPORT" in codes
        assert "INTERNET" in codes
        assert "MAINTENANCE" in codes
        assert "MISCELLANEOUS" in codes

    def test_02_create_custom_expense_category_success(self):
        self.client.force_authenticate(user=self.finance_a)
        payload = {
            "name": "Social Media Marketing & Signage",
            "name_bn": "প্রচার ও সাইনবোর্ড খরচ",
            "code": "MARKETING",
            "default_gl_code": "6060",
            "monthly_budget_bdt": "8500.00",
            "tax_deductible": True,
            "description": "Facebook pharmacy ads and neon sign board maintenance",
        }
        response = self.client.post("/api/v2/finance/expense-categories/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert data["code"] == "MARKETING"
        assert data["name"] == "Social Media Marketing & Signage"
        assert Decimal(data["monthly_budget_bdt"]) == Decimal("8500.00")
        assert data["default_gl_code"] == "6060"
        assert data["is_active"] is True

    def test_03_retrieve_expense_category_detail(self):
        self.client.force_authenticate(user=self.owner_a)
        create_res = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Security Guard Services", "code": "SECURITY", "monthly_budget_bdt": "15000.00"},
            format="json",
        )
        cat_id = create_res.data["category_id"]

        response = self.client.get(f"/api/v2/finance/expense-categories/{cat_id}/")
        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["category_id"] == cat_id
        assert response.data["name"] == "Security Guard Services"

    def test_04_partial_update_expense_category(self):
        self.client.force_authenticate(user=self.owner_a)
        create_res = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Air Conditioner Maintenance", "code": "AC_MAINT", "monthly_budget_bdt": "4000.00"},
            format="json",
        )
        cat_id = create_res.data["category_id"]

        update_res = self.client.patch(
            f"/api/v2/finance/expense-categories/{cat_id}/",
            {"monthly_budget_bdt": "6500.00", "description": "Summer servicing included"},
            format="json",
        )
        assert update_res.status_code == status.HTTP_200_OK, update_res.data
        assert Decimal(update_res.data["monthly_budget_bdt"]) == Decimal("6500.00")
        assert update_res.data["description"] == "Summer servicing included"

    def test_05_archive_expense_category(self):
        self.client.force_authenticate(user=self.owner_a)
        create_res = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Old Renovation Project", "code": "OLD_RENOVATION", "monthly_budget_bdt": "20000.00"},
            format="json",
        )
        cat_id = create_res.data["category_id"]

        del_res = self.client.delete(f"/api/v2/finance/expense-categories/{cat_id}/")
        assert del_res.status_code == status.HTTP_200_OK, del_res.data
        assert del_res.data["category"]["is_active"] is False

        # Confirm from DB
        cat = ExpenseCategory.objects.get(category_id=cat_id)
        assert cat.is_active is False

    def test_06_list_active_expense_categories_filter(self):
        self.client.force_authenticate(user=self.owner_a)
        c1 = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Active Category 1", "code": "ACT_1"},
            format="json",
        ).data["category_id"]

        c2 = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Archived Category 2", "code": "ARCH_2"},
            format="json",
        ).data["category_id"]

        self.client.delete(f"/api/v2/finance/expense-categories/{c2}/")

        res_active = self.client.get("/api/v2/finance/expense-categories/?is_active=true")
        assert res_active.status_code == status.HTTP_200_OK
        active_ids = [c["category_id"] for c in res_active.data["categories"]]
        assert c1 in active_ids
        assert c2 not in active_ids

    def test_07_missing_category_name_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "   ", "code": "NO_NAME"},
            format="json",
        )
        assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert response.data["error_code"] == "ERR_MISSING_CATEGORY_NAME"

    def test_08_duplicate_category_name_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Unique Name Test", "code": "CODE_A"},
            format="json",
        )
        dup_res = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Unique Name Test", "code": "CODE_B"},
            format="json",
        )
        assert dup_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert dup_res.data["error_code"] == "ERR_DUPLICATE_CATEGORY_NAME"

    def test_09_negative_monthly_budget_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Negative Budget Category", "code": "NEG_BUDGET", "monthly_budget_bdt": "-500.00"},
            format="json",
        )
        assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert response.data["error_code"] == "ERR_INVALID_BUDGET"

    def test_10_cashier_role_strictly_denied(self):
        self.client.force_authenticate(user=self.cashier_a)
        list_res = self.client.get("/api/v2/finance/expense-categories/")
        assert list_res.status_code == status.HTTP_403_FORBIDDEN
        assert list_res.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        post_res = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Cashier Unauth Category", "code": "CASH_FAIL"},
            format="json",
        )
        assert post_res.status_code == status.HTTP_403_FORBIDDEN
        assert post_res.data["error_code"] == "ERR_EXPENSE_MANAGE_UNAUTHORIZED"

    def test_11_cross_tenant_isolation_enforced(self):
        self.client.force_authenticate(user=self.owner_a)
        cat_id = self.client.post(
            "/api/v2/finance/expense-categories/",
            {"name": "Tenant A Secret Expense", "code": "SECRET_A"},
            format="json",
        ).data["category_id"]

        # Tenant B attempts access
        self.client.force_authenticate(user=self.owner_b)
        get_res = self.client.get(f"/api/v2/finance/expense-categories/{cat_id}/")
        assert get_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        patch_res = self.client.patch(
            f"/api/v2/finance/expense-categories/{cat_id}/",
            {"name": "Malicious Tampering"},
            format="json",
        )
        assert patch_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_blocked(self):
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/finance/expense-categories/")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        data = response.json() if hasattr(response, "json") else getattr(response, "data", {})
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(response.content).lower()
