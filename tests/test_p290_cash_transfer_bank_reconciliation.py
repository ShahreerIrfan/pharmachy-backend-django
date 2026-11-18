"""
backend/tests/test_p290_cash_transfer_bank_reconciliation.py
Architecture Decision: ADR-0290 (P290).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying Cash Transfer and Bank Reconciliation without treating internal transfers as revenue or expenses:
1. Cash-to-bank deposit transfer moves funds, posts balanced GL journal (Dr 1020, Cr 1010) and has ZERO P&L impact.
2. Bank-to-cash withdrawal transfer moves funds, posts balanced GL journal (Dr 1010, Cr 1020).
3. Same-account transfer is strictly rejected (ERR_SAME_ACCOUNT_TRANSFER, 400/422).
4. Negative or zero amount transfer is rejected (ERR_INVALID_TRANSFER_AMOUNT, 400/422).
5. Inactive account transfer is rejected (ERR_ACCOUNT_NOT_ACTIVE, 400/422).
6. Bank reconciliation with matching balance marked RECONCILED.
7. Bank reconciliation with variance marked DISCREPANCY.
8. Cash register account rejected for bank reconciliation (ERR_INVALID_ACCOUNT_TYPE, 400/422).
9. Cashier role strictly denied from executing transfers or reconciliations (403).
10. Cross-tenant account access strictly forbidden (403/404).
11. Suspended tenant blocked from treasury operations (403).
12. List transfers and filtering by transfer_type.
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
    BankReconciliationBatch,
    BankReconciliationStatus,
    Branch,
    CashBankTransfer,
    CashBankTransferStatus,
    CashBankTransferType,
    ChartOfAccount,
    FinancialAccount,
    FinancialAccountStatus,
    FinancialAccountType,
    GLAccountCategory,
    JournalEntry,
    JournalEntryLine,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestCashTransferBankReconciliationIntegration:

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
            name="Dhanmondi Central Branch",
        )

        # 2. Foreign Tenant & Branch
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

        # 3. Users for Tenant A
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

        # 4. Foreign User for Tenant B
        self.foreign_owner = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Foreign Owner B",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 5. Initialize Standard Chart of Accounts
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner
        )
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.foreign_owner
        )

        # 6. Create Financial Accounts in Tenant A
        self.till_a = FinancialAccount.objects.create(
            account_id=f"acc-till-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            account_type=FinancialAccountType.CASH_REGISTER,
            account_name="Cash Counter Till 1",
            account_code=f"TILL-01-{self.suffix}",
            current_balance=Decimal("35000.00"),
            status=FinancialAccountStatus.ACTIVE,
        )
        self.bank_a = FinancialAccount.objects.create(
            account_id=f"acc-bank-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            account_type=FinancialAccountType.BANK_ACCOUNT,
            account_name="Dutch-Bangla Bank Current A/C",
            account_code=f"DBBL-01-{self.suffix}",
            current_balance=Decimal("150000.00"),
            status=FinancialAccountStatus.ACTIVE,
        )
        self.mfs_a = FinancialAccount.objects.create(
            account_id=f"acc-mfs-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            account_type=FinancialAccountType.MFS_CLEARING,
            account_name="bKash Merchant Wallet",
            account_code=f"BKASH-01-{self.suffix}",
            current_balance=Decimal("20000.00"),
            status=FinancialAccountStatus.ACTIVE,
        )

        # Foreign Account in Tenant B
        self.foreign_bank = FinancialAccount.objects.create(
            account_id=f"acc-fbank-{self.suffix}",
            tenant=self.tenant_b,
            branch=self.branch_b,
            account_type=FinancialAccountType.BANK_ACCOUNT,
            account_name="Foreign City Bank A/C",
            account_code=f"CITY-01-{self.suffix}",
            current_balance=Decimal("50000.00"),
            status=FinancialAccountStatus.ACTIVE,
        )

    def test_01_cash_to_bank_deposit_transfer_success_and_zero_pl_impact(self):
        self.client.force_authenticate(user=self.manager)

        initial_till_bal = self.till_a.current_balance
        initial_bank_bal = self.bank_a.current_balance
        transfer_amount = Decimal("20000.00")

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_account_id": self.till_a.account_id,
            "destination_account_id": self.bank_a.account_id,
            "amount_bdt": str(transfer_amount),
            "reference_number": "DEP-SLIP-778811",
            "notes": "Daily evening cash deposit from till to DBBL.",
        }

        res = self.client.post("/api/v2/finance/treasury/transfers/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.json()["transfer"]
        assert data["transfer_type"] == CashBankTransferType.CASH_TO_BANK
        assert data["amount_bdt"] == "20000.00"
        assert data["status"] == CashBankTransferStatus.COMPLETED
        assert data["posted_journal_id"] is not None

        # Verify Balances updated
        self.till_a.refresh_from_db()
        self.bank_a.refresh_from_db()
        assert self.till_a.current_balance == initial_till_bal - transfer_amount
        assert self.bank_a.current_balance == initial_bank_bal + transfer_amount

        # Verify GL Journal Entry
        journal = JournalEntry.objects.get(entry_id=data["posted_journal_id"])
        assert journal.total_debit_bdt == journal.total_credit_bdt == transfer_amount

        # CRITICAL P&L INVARIANT VERIFICATION:
        # None of the journal lines may belong to REVENUE (4000s) or EXPENSE (6000s) accounts!
        lines = JournalEntryLine.objects.filter(journal_entry=journal).select_related("account")
        for line in lines:
            assert line.account.category == GLAccountCategory.ASSET
            assert line.account.account_code in ("1010", "1020")
            assert line.account.category not in (GLAccountCategory.REVENUE, GLAccountCategory.EXPENSE)

    def test_02_bank_to_cash_withdrawal_transfer_success(self):
        self.client.force_authenticate(user=self.owner)

        initial_till_bal = self.till_a.current_balance
        initial_bank_bal = self.bank_a.current_balance
        withdrawal_amount = Decimal("15000.00")

        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "source_account_id": self.bank_a.account_id,
            "destination_account_id": self.till_a.account_id,
            "amount_bdt": str(withdrawal_amount),
            "reference_number": "CHQ-WITHDRAW-9988",
            "notes": "Replenish drawer petty cash float.",
        }

        res = self.client.post("/api/v2/finance/treasury/transfers/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.json()["transfer"]
        assert data["transfer_type"] == CashBankTransferType.BANK_TO_CASH
        assert data["amount_bdt"] == "15000.00"

        self.till_a.refresh_from_db()
        self.bank_a.refresh_from_db()
        assert self.till_a.current_balance == initial_till_bal + withdrawal_amount
        assert self.bank_a.current_balance == initial_bank_bal - withdrawal_amount

    def test_03_same_account_transfer_rejected(self):
        self.client.force_authenticate(user=self.manager)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "source_account_id": self.till_a.account_id,
            "destination_account_id": self.till_a.account_id,
            "amount_bdt": "5000.00",
        }
        res = self.client.post("/api/v2/finance/treasury/transfers/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res.json().get("error_code") == "ERR_SAME_ACCOUNT_TRANSFER"

    def test_04_negative_or_zero_amount_transfer_rejected(self):
        self.client.force_authenticate(user=self.manager)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "source_account_id": self.till_a.account_id,
            "destination_account_id": self.bank_a.account_id,
            "amount_bdt": "-1000.00",
        }
        res = self.client.post("/api/v2/finance/treasury/transfers/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res.json().get("error_code") == "ERR_INVALID_TRANSFER_AMOUNT"

    def test_05_inactive_account_transfer_rejected(self):
        self.till_a.status = FinancialAccountStatus.FROZEN
        self.till_a.save()

        self.client.force_authenticate(user=self.manager)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "source_account_id": self.till_a.account_id,
            "destination_account_id": self.bank_a.account_id,
            "amount_bdt": "1000.00",
        }
        res = self.client.post("/api/v2/finance/treasury/transfers/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res.json().get("error_code") == "ERR_ACCOUNT_NOT_ACTIVE"

    def test_06_bank_reconciliation_zero_difference_reconciled(self):
        self.client.force_authenticate(user=self.finance)
        # Bank account balance is 150000.00
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "bank_account_id": self.bank_a.account_id,
            "statement_starting_balance": "100000.00",
            "statement_ending_balance": "150000.00",
            "notes": "November monthly bank statement reconciliation.",
        }
        res = self.client.post("/api/v2/finance/treasury/reconciliations/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.json()["reconciliation"]
        assert data["status"] == BankReconciliationStatus.RECONCILED
        assert data["difference_bdt"] == "0.00"
        assert data["reconciled_by"] == self.finance.email

    def test_07_bank_reconciliation_variance_detected_discrepancy(self):
        self.client.force_authenticate(user=self.finance)
        # Statement has 148,000 but book balance is 150,000 (diff: -2,000)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "bank_account_id": self.bank_a.account_id,
            "statement_ending_balance": "148000.00",
            "notes": "Statement with uncredited bank charges.",
        }
        res = self.client.post("/api/v2/finance/treasury/reconciliations/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.json()["reconciliation"]
        assert data["status"] == BankReconciliationStatus.DISCREPANCY
        assert data["difference_bdt"] == "-2000.00"
        assert data["reconciled_by"] is None

    def test_08_cash_till_cannot_undergo_bank_reconciliation(self):
        self.client.force_authenticate(user=self.owner)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "bank_account_id": self.till_a.account_id, # Till is not a bank account!
            "statement_ending_balance": "35000.00",
        }
        res = self.client.post("/api/v2/finance/treasury/reconciliations/", payload, format="json")
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res.json().get("error_code") == "ERR_INVALID_ACCOUNT_TYPE"

    def test_09_cashier_role_strictly_denied_from_transfer_and_reconciliation(self):
        self.client.force_authenticate(user=self.cashier)

        # Cashier tries transfer -> 403
        t_res = self.client.post(
            "/api/v2/finance/treasury/transfers/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "source_account_id": self.till_a.account_id,
                "destination_account_id": self.bank_a.account_id,
                "amount_bdt": "100.00",
            },
            format="json",
        )
        assert t_res.status_code == status.HTTP_403_FORBIDDEN

        # Cashier tries reconciliation -> 403
        r_res = self.client.post(
            "/api/v2/finance/treasury/reconciliations/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "bank_account_id": self.bank_a.account_id,
                "statement_ending_balance": "100.00",
            },
            format="json",
        )
        assert r_res.status_code == status.HTTP_403_FORBIDDEN

    def test_10_cross_tenant_transfer_forbidden(self):
        self.client.force_authenticate(user=self.owner)
        # Attempt transfer from Tenant A Till to Tenant B Foreign Bank
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "source_account_id": self.till_a.account_id,
            "destination_account_id": self.foreign_bank.account_id,
            "amount_bdt": "5000.00",
        }
        res = self.client.post("/api/v2/finance/treasury/transfers/", payload, format="json")
        assert res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_11_suspended_tenant_blocked(self):
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "source_account_id": self.till_a.account_id,
            "destination_account_id": self.bank_a.account_id,
            "amount_bdt": "5000.00",
        }
        res = self.client.post("/api/v2/finance/treasury/transfers/", payload, format="json")
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert res.json().get("error_code") in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")

    def test_12_list_transfers_and_filter_by_type(self):
        self.client.force_authenticate(user=self.manager)
        # Create a transfer
        self.client.post(
            "/api/v2/finance/treasury/transfers/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "source_account_id": self.till_a.account_id,
                "destination_account_id": self.mfs_a.account_id,
                "amount_bdt": "3000.00",
            },
            format="json",
        )

        res = self.client.get(
            "/api/v2/finance/treasury/transfers/",
            {"transfer_type": "CASH_TO_BANK"},
            HTTP_X_TENANT_ID=self.tenant_a.tenant_id,
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data["count"] >= 1
        for item in data["transfers"]:
            assert item["transfer_type"] == "CASH_TO_BANK"
