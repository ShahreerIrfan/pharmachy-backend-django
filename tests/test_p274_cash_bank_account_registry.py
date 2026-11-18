"""
backend/tests/test_p274_cash_bank_account_registry.py
Architecture Decision: ADR-0274 (P274).
Phase 14: Customers, Suppliers, Dues & Payments.

Integration tests verifying Cash & Bank Account Registry:
1. Valid registration of cash registers, bank accounts, and MFS clearing accounts.
2. Incomplete and invalid input rejection (missing name, missing bank details, duplicate codes).
3. Fine-grained RBAC matrix (Cashier/Pharmacist cannot register or freeze accounts, Owner/Manager authorized).
4. Cashier view restriction (Cashier can view assigned branch registers only, not bank accounts).
5. Cross-tenant isolation strictly enforced (foreign account access rejected with HTTP 403).
6. Lifecycle state machine: Active -> Frozen -> Unfrozen -> Closed (closed accounts locked).
7. Branch-level primary default clearing account switching.
8. Suspended tenant account write blocking.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    Branch,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestCashBankAccountRegistryIntegration:

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

        self.branch_manager = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@square.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Branch Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="POS Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.pharmacist = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@square.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        self.suspended_user = PharmacyUser.objects.create_user(
            email=f"susp_{self.suffix}@square.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Suspended Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.suspended_tenant.tenant_id,
        )

        self.foreign_user = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@health.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
        )

    def test_01_valid_cash_register_and_bank_account_registration(self):
        """Branch manager registers a physical cash register and commercial bank account."""
        self.client.force_authenticate(user=self.branch_manager)

        # 1. Register Cash Drawer
        url = "/api/v2/finance/accounts/"
        payload_cr = {
            "account_type": FinancialAccountType.CASH_REGISTER,
            "account_name": "Counter 1 POS Till",
            "account_code": f"CR-01-{self.suffix}",
            "branch_id": self.branch_a.branch_id,
            "opening_balance": "5000.0000",
            "is_default_for_type": True,
            "notes": "Front desk POS cashier till",
        }
        res_cr = self.client.post(url, payload_cr, format="json")
        assert res_cr.status_code == status.HTTP_201_CREATED, res_cr.data
        data_cr = res_cr.data

        assert data_cr["account_name"] == "Counter 1 POS Till"
        assert data_cr["account_type"] == FinancialAccountType.CASH_REGISTER
        assert Decimal(data_cr["opening_balance"]) == Decimal("5000.0000")
        assert Decimal(data_cr["current_balance"]) == Decimal("5000.0000")
        assert data_cr["is_default_for_type"] is True
        assert data_cr["status"] == FinancialAccountStatus.ACTIVE

        # 2. Register Bank Account
        payload_ba = {
            "account_type": FinancialAccountType.BANK_ACCOUNT,
            "account_name": "DBBL Principal Operating Current Account",
            "account_code": f"BA-DBBL-{self.suffix}",
            "account_number": "110.120.998877",
            "bank_name": "Dutch-Bangla Bank Limited",
            "branch_name": "Dhanmondi Branch",
            "routing_number": "090271234",
            "branch_id": self.branch_a.branch_id,
            "opening_balance": "250000.0000",
            "is_default_for_type": True,
        }
        res_ba = self.client.post(url, payload_ba, format="json")
        assert res_ba.status_code == status.HTTP_201_CREATED, res_ba.data
        data_ba = res_ba.data

        assert data_ba["bank_name"] == "Dutch-Bangla Bank Limited"
        assert data_ba["account_number"] == "110.120.998877"
        assert data_ba["routing_number"] == "090271234"
        assert Decimal(data_ba["current_balance"]) == Decimal("250000.0000")

        # Verify Audit Log
        assert AuditLogEntry.objects.filter(
            action="FINANCIAL_ACCOUNT_REGISTERED",
            resource_id=data_ba["account_id"],
        ).exists()

    def test_02_invalid_inputs_rejected(self):
        """Validation rules: missing name, missing bank details for bank account, and duplicate code."""
        self.client.force_authenticate(user=self.branch_manager)
        url = "/api/v2/finance/accounts/"

        # 1. Missing account_name
        res1 = self.client.post(url, {"account_type": "CASH_REGISTER"}, format="json")
        assert res1.status_code == status.HTTP_400_BAD_REQUEST
        assert res1.data["error_code"] == "ERR_MISSING_ACCOUNT_NAME"

        # 2. Bank account missing bank details
        res2 = self.client.post(url, {"account_name": "Incomplete Bank A/C", "account_type": "BANK_ACCOUNT"}, format="json")
        assert res2.status_code == status.HTTP_400_BAD_REQUEST
        assert res2.data["error_code"] == "ERR_MISSING_BANK_DETAILS"

        # 3. Duplicate account_code
        code = f"DUP-CODE-{self.suffix}"
        self.client.post(url, {"account_name": "Till 1", "account_code": code}, format="json")
        res3 = self.client.post(url, {"account_name": "Till 2", "account_code": code}, format="json")
        assert res3.status_code == status.HTTP_400_BAD_REQUEST
        assert res3.data["error_code"] == "ERR_DUPLICATE_ACCOUNT_CODE"

    def test_03_role_authorization_cashier_and_pharmacist_denied(self):
        """Cashier and Pharmacist cannot create, modify, or freeze financial accounts."""
        url = "/api/v2/finance/accounts/"
        payload = {"account_name": "Unauthorized Attempt", "account_type": "CASH_REGISTER"}

        # 1. Cashier creation attempt -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        res_cashier = self.client.post(url, payload, format="json")
        assert res_cashier.status_code == status.HTTP_403_FORBIDDEN
        assert res_cashier.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Staff Pharmacist freeze attempt -> 403 Forbidden
        # First, owner creates an account
        self.client.force_authenticate(user=self.owner)
        res_acc = self.client.post(url, {"account_name": "Safe Account"}, format="json")
        acc_id = res_acc.data["account_id"]

        self.client.force_authenticate(user=self.pharmacist)
        res_freeze = self.client.post(f"/api/v2/finance/accounts/{acc_id}/freeze/", format="json")
        assert res_freeze.status_code == status.HTTP_403_FORBIDDEN
        assert res_freeze.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_04_cashier_restricted_listing(self):
        """Cashiers can only view CASH_REGISTER accounts for their assigned branch, not commercial bank accounts."""
        # Owner registers 1 Bank Account and 1 Cash Register
        self.client.force_authenticate(user=self.owner)
        url = "/api/v2/finance/accounts/"
        self.client.post(url, {
            "account_name": "Secret Bank Account",
            "account_type": "BANK_ACCOUNT",
            "bank_name": "BRAC Bank",
            "account_number": "55443322",
            "branch_id": self.branch_a.branch_id,
        }, format="json")

        self.client.post(url, {
            "account_name": "Front Desk Cashier Drawer",
            "account_type": "CASH_REGISTER",
            "branch_id": self.branch_a.branch_id,
        }, format="json")

        # Cashier lists accounts
        self.client.force_authenticate(user=self.cashier)
        res_cashier = self.client.get(url)
        assert res_cashier.status_code == status.HTTP_200_OK
        accounts = res_cashier.data["accounts"]

        assert len(accounts) == 1
        assert accounts[0]["account_type"] == FinancialAccountType.CASH_REGISTER
        assert accounts[0]["account_name"] == "Front Desk Cashier Drawer"

    def test_05_cross_tenant_isolation(self):
        """Tenant A manager cannot access, mutate or register accounts for Tenant B."""
        # 1. Tenant B creates an account
        self.client.force_authenticate(user=self.foreign_user)
        res_b = self.client.post("/api/v2/finance/accounts/", {
            "account_name": "Foreign Till",
            "account_type": "CASH_REGISTER",
            "branch_id": self.branch_b.branch_id,
        }, format="json")
        acc_b_id = res_b.data["account_id"]

        # 2. Tenant A attempts to retrieve Tenant B account -> 404 / 403
        self.client.force_authenticate(user=self.branch_manager)
        res_get = self.client.get(f"/api/v2/finance/accounts/{acc_b_id}/")
        assert res_get.status_code in [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN]

        # 3. Tenant A attempts to freeze Tenant B account -> 404 / 403
        res_freeze = self.client.post(f"/api/v2/finance/accounts/{acc_b_id}/freeze/", format="json")
        assert res_freeze.status_code in [status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN]

        # 4. Tenant A attempts to register with Tenant B's branch -> 403 Forbidden
        res_cross_branch = self.client.post("/api/v2/finance/accounts/", {
            "account_name": "Spoofed Branch Account",
            "account_type": "CASH_REGISTER",
            "branch_id": self.branch_b.branch_id,
        }, format="json")
        assert res_cross_branch.status_code == status.HTTP_403_FORBIDDEN
        assert res_cross_branch.data["error_code"] == "ERR_CROSS_TENANT_ACCESS"

    def test_06_lifecycle_transitions_freeze_unfreeze_close(self):
        """Lifecycle transitions: Active -> Frozen -> Active -> Closed."""
        self.client.force_authenticate(user=self.branch_manager)
        url = "/api/v2/finance/accounts/"

        # 1. Register active account
        res_reg = self.client.post(url, {
            "account_name": "MFS bKash Merchant Wallet",
            "account_type": "MFS_CLEARING",
            "account_number": "01700000000",
        }, format="json")
        acc_id = res_reg.data["account_id"]
        assert res_reg.data["status"] == FinancialAccountStatus.ACTIVE

        # 2. Freeze account
        res_freeze = self.client.post(f"/api/v2/finance/accounts/{acc_id}/freeze/", {
            "reason": "Reconciliation variance investigation",
        }, format="json")
        assert res_freeze.status_code == status.HTTP_200_OK
        assert res_freeze.data["status"] == FinancialAccountStatus.FROZEN

        # 3. Unfreeze account
        res_unfreeze = self.client.post(f"/api/v2/finance/accounts/{acc_id}/unfreeze/", {
            "reason": "Audit cleared successfully",
        }, format="json")
        assert res_unfreeze.status_code == status.HTTP_200_OK
        assert res_unfreeze.data["status"] == FinancialAccountStatus.ACTIVE

        # 4. Close account
        res_close = self.client.post(f"/api/v2/finance/accounts/{acc_id}/close/", {
            "reason": "Merchant wallet deactivated",
        }, format="json")
        assert res_close.status_code == status.HTTP_200_OK
        assert res_close.data["status"] == FinancialAccountStatus.CLOSED
        assert res_close.data["is_active"] is False

        # 5. Modifying or freezing a closed account is rejected
        res_post_close = self.client.post(f"/api/v2/finance/accounts/{acc_id}/freeze/", format="json")
        assert res_post_close.status_code == status.HTTP_400_BAD_REQUEST
        assert res_post_close.data["error_code"] == "ERR_ACCOUNT_CLOSED"

    def test_07_default_clearing_account_reset_per_branch(self):
        """Setting a new default clearing account unsets previous defaults of same type in the branch."""
        self.client.force_authenticate(user=self.branch_manager)
        url = "/api/v2/finance/accounts/"

        # 1. Register Till 1 as default
        res1 = self.client.post(url, {
            "account_name": "Till 1",
            "account_type": "CASH_REGISTER",
            "branch_id": self.branch_a.branch_id,
            "is_default_for_type": True,
        }, format="json")
        acc1_id = res1.data["account_id"]
        assert res1.data["is_default_for_type"] is True

        # 2. Register Till 2 as default
        res2 = self.client.post(url, {
            "account_name": "Till 2",
            "account_type": "CASH_REGISTER",
            "branch_id": self.branch_a.branch_id,
            "is_default_for_type": True,
        }, format="json")
        assert res2.data["is_default_for_type"] is True

        # Verify Till 1 is no longer default
        res1_updated = self.client.get(f"/api/v2/finance/accounts/{acc1_id}/")
        assert res1_updated.data["is_default_for_type"] is False

    def test_08_suspended_tenant_blocked(self):
        """Suspended tenant is barred from creating or modifying financial accounts."""
        self.client.force_authenticate(user=self.suspended_user)
        url = "/api/v2/finance/accounts/"
        res = self.client.post(url, {"account_name": "Suspended Attempt"}, format="json")
        data = res.data if hasattr(res, "data") else res.json()
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert data.get("error_code") in ["ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"]
