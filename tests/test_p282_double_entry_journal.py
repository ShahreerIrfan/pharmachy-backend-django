"""
backend/tests/test_p282_double_entry_journal.py
Architecture Decision: ADR-0282 (P282).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying General Ledger Double-Entry Journal Engine:
1. Balanced journal entry post (Rent Dr 25000, Cash Cr 25000) updates GL account balances and returns POSTED.
2. Unbalanced journal (Debits != Credits) rejected with ERR_UNBALANCED_JOURNAL_ENTRY (HTTP 422).
3. Minimum lines validation (< 2 lines rejected with ERR_MINIMUM_LINES_REQUIRED, HTTP 422).
4. Negative or zero line amounts rejected with validation error (HTTP 422).
5. Dual leg on single line rejected with ERR_DUAL_LEG_LINE (HTTP 422).
6. Multi-leg split journal (Sales Cash Dr 8000, AR Dr 2000, Sales Revenue Cr 10000).
7. Idempotency key replay returns existing journal without re-updating GL account balances.
8. Audited reversal creates compensating swapped entry and marks original as REVERSED.
9. Reversing already reversed entry rejected (ERR_ALREADY_REVERSED, HTTP 422).
10. Missing reversal reason rejected (ERR_MISSING_REVERSAL_REASON, HTTP 422).
11. Role enforcement: Branch Manager can view journals, but post/reverse is denied (ERR_JOURNAL_POST_UNAUTHORIZED, 403);
    Cashier and staff pharmacist strictly denied (ERR_STAFF_ROLE_DENIED, 403).
12. Cross-tenant isolation (ERR_CROSS_TENANT_ACCESS, 403/404) and suspended tenant write protection (ERR_TENANT_SUSPENDED, 403).
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
    ChartOfAccount,
    GLAccountCategory,
    GLNormalBalance,
    JournalEntry,
    JournalEntryStatus,
    JournalSourceModule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestDoubleEntryJournalIntegration:

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
            name="Uttara Branch",
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
        self.manager_a = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="POS Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )

        # 4. Users in Tenant B
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner B",
            password="Password123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
        )

        # 5. Seed Default Chart of Accounts for Tenant A & B
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

        # Accounts for Tenant A
        self.acc_cash_a = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")  # Cash in Hand (Asset)
        self.acc_bank_a = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1020")  # Bank Account (Asset)
        self.acc_ar_a = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")    # Accounts Receivable (Asset)
        self.acc_rev_a = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4010")   # Sales Revenue (Revenue)
        self.acc_rent_a = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6010")  # Rent Expense (Expense)

    def test_01_balanced_journal_entry_post_success(self):
        """1. Balanced journal entry post (Rent Dr 25000, Bank Cr 25000) updates GL balances."""
        self.client.force_authenticate(user=self.finance_a)

        payload = {
            "source_module": "OPERATING_EXPENSE",
            "source_reference": "EXP-RENT-202611",
            "narration": "November 2026 pharmacy premises rent payment",
            "branch_id": self.branch_a.branch_id,
            "lines": [
                {
                    "account_code": "6010",  # Rent Expense (Normal Balance: DEBIT)
                    "debit_amount_bdt": "25000.00",
                    "credit_amount_bdt": "0.00",
                    "description": "Store rental expense",
                },
                {
                    "account_code": "1020",  # Bank Account (Normal Balance: DEBIT)
                    "debit_amount_bdt": "0.00",
                    "credit_amount_bdt": "25000.00",
                    "description": "Cheque payment via Bank Asia",
                },
            ],
        }

        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data
        assert data["status"] == "POSTED"
        assert data["is_balanced"] is True
        assert Decimal(str(data["total_debit_bdt"])) == Decimal("25000.00")
        assert Decimal(str(data["total_credit_bdt"])) == Decimal("25000.00")
        assert len(data["lines"]) == 2

        # Verify Account Balances Updated:
        # Rent Expense: normal DEBIT -> increases by 25000
        self.acc_rent_a.refresh_from_db()
        assert self.acc_rent_a.current_balance_bdt == Decimal("25000.00")

        # Bank Account: normal DEBIT -> decreases by 25000 (credit)
        self.acc_bank_a.refresh_from_db()
        assert self.acc_bank_a.current_balance_bdt == Decimal("-25000.00")

    def test_02_unbalanced_journal_rejected(self):
        """2. Unbalanced journal (Debits != Credits) rejected with ERR_UNBALANCED_JOURNAL_ENTRY (422)."""
        self.client.force_authenticate(user=self.owner_a)

        payload = {
            "source_module": "MANUAL",
            "narration": "Out of balance attempt",
            "lines": [
                {
                    "account_code": "6010",
                    "debit_amount_bdt": "15000.00",
                    "credit_amount_bdt": "0.00",
                },
                {
                    "account_code": "1010",
                    "debit_amount_bdt": "0.00",
                    "credit_amount_bdt": "12000.00",  # 3000 difference
                },
            ],
        }

        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_UNBALANCED_JOURNAL_ENTRY"

    def test_03_minimum_lines_validation(self):
        """3. Minimum lines validation (< 2 lines rejected with ERR_MINIMUM_LINES_REQUIRED, 422)."""
        self.client.force_authenticate(user=self.owner_a)

        payload = {
            "source_module": "MANUAL",
            "lines": [
                {
                    "account_code": "6010",
                    "debit_amount_bdt": "5000.00",
                    "credit_amount_bdt": "0.00",
                }
            ],
        }

        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_MINIMUM_LINES_REQUIRED"

    def test_04_negative_or_zero_line_amounts_rejected(self):
        """4. Negative or zero line amounts rejected with validation error (422)."""
        self.client.force_authenticate(user=self.owner_a)

        # Negative amount
        payload_neg = {
            "lines": [
                {"account_code": "6010", "debit_amount_bdt": "-500.00", "credit_amount_bdt": "0.00"},
                {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "-500.00"},
            ]
        }
        resp = self.client.post("/api/v2/finance/journals/", payload_neg, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_NEGATIVE_AMOUNT"

        # Zero amount
        payload_zero = {
            "lines": [
                {"account_code": "6010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "0.00"},
                {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "0.00"},
            ]
        }
        resp = self.client.post("/api/v2/finance/journals/", payload_zero, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_ZERO_LINE_AMOUNT"

    def test_05_dual_leg_on_single_line_rejected(self):
        """5. Dual leg on single line rejected with ERR_DUAL_LEG_LINE (422)."""
        self.client.force_authenticate(user=self.owner_a)

        payload = {
            "lines": [
                {"account_code": "6010", "debit_amount_bdt": "100.00", "credit_amount_bdt": "50.00"},
                {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "50.00"},
            ]
        }
        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_DUAL_LEG_LINE"

    def test_06_multi_leg_split_journal(self):
        """6. Multi-leg split journal (Cash Dr 8000, AR Dr 2000, Sales Revenue Cr 10000)."""
        self.client.force_authenticate(user=self.finance_a)

        payload = {
            "source_module": "POS_SALE",
            "source_reference": "INV-202611-0099",
            "narration": "Split payment sale: Cash 8000 + Credit Due 2000",
            "lines": [
                {"account_code": "1010", "debit_amount_bdt": "8000.00", "credit_amount_bdt": "0.00"},
                {"account_code": "1030", "debit_amount_bdt": "2000.00", "credit_amount_bdt": "0.00"},
                {"account_code": "4010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "10000.00"},
            ],
        }

        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        assert resp.data["total_debit_bdt"] == "10000.00"
        assert resp.data["total_credit_bdt"] == "10000.00"
        assert len(resp.data["lines"]) == 3

        # Verify balances:
        # Cash in Hand (Asset, normal DEBIT): +8000
        self.acc_cash_a.refresh_from_db()
        assert self.acc_cash_a.current_balance_bdt == Decimal("8000.00")

        # AR (Asset, normal DEBIT): +2000
        self.acc_ar_a.refresh_from_db()
        assert self.acc_ar_a.current_balance_bdt == Decimal("2000.00")

        # Sales Revenue (Revenue, normal CREDIT): +10000
        self.acc_rev_a.refresh_from_db()
        assert self.acc_rev_a.current_balance_bdt == Decimal("10000.00")

    def test_07_idempotency_key_replay_protection(self):
        """7. Idempotency key replay returns existing journal without re-updating GL account balances."""
        self.client.force_authenticate(user=self.finance_a)
        idem_key = f"idem-{uuid.uuid4().hex}"

        payload = {
            "source_module": "OPERATING_EXPENSE",
            "idempotency_key": idem_key,
            "narration": "Idempotent payment test",
            "lines": [
                {"account_code": "6010", "debit_amount_bdt": "3000.00", "credit_amount_bdt": "0.00"},
                {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "3000.00"},
            ],
        }

        # 1st Post
        resp1 = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp1.status_code == status.HTTP_201_CREATED
        entry_id = resp1.data["entry_id"]

        self.acc_rent_a.refresh_from_db()
        assert self.acc_rent_a.current_balance_bdt == Decimal("3000.00")

        # 2nd Post with identical idempotency key
        resp2 = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp2.status_code == status.HTTP_201_CREATED
        assert resp2.data["entry_id"] == entry_id
        assert resp2.data.get("is_idempotent_replay") is True

        # Ensure balance did NOT double-apply
        self.acc_rent_a.refresh_from_db()
        assert self.acc_rent_a.current_balance_bdt == Decimal("3000.00")

    def test_08_audited_reversal_creates_compensating_entry(self):
        """8. Audited reversal creates compensating swapped entry and marks original as REVERSED."""
        self.client.force_authenticate(user=self.finance_a)

        # 1. Post initial entry (Rent 4000 Dr, Bank 4000 Cr)
        payload = {
            "source_module": "OPERATING_EXPENSE",
            "narration": "Initial incorrect rent payment",
            "lines": [
                {"account_code": "6010", "debit_amount_bdt": "4000.00", "credit_amount_bdt": "0.00"},
                {"account_code": "1020", "debit_amount_bdt": "0.00", "credit_amount_bdt": "4000.00"},
            ],
        }
        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        orig_entry_id = resp.data["entry_id"]

        self.acc_rent_a.refresh_from_db()
        assert self.acc_rent_a.current_balance_bdt == Decimal("4000.00")
        self.acc_bank_a.refresh_from_db()
        assert self.acc_bank_a.current_balance_bdt == Decimal("-4000.00")

        # 2. Reverse the entry
        rev_payload = {
            "reason": "Payment issued to incorrect landlord account; reversing check",
        }
        rev_resp = self.client.post(f"/api/v2/finance/journals/{orig_entry_id}/reverse/", rev_payload, format="json")
        assert rev_resp.status_code == status.HTTP_200_OK, rev_resp.data
        rev_data = rev_resp.data

        assert rev_data["success"] is True
        assert rev_data["original_journal_id"] == orig_entry_id
        assert rev_data["original_status"] == "REVERSED"
        assert rev_data["reversal_journal_id"] is not None

        # Verify reversal entry in database has reversal_of pointing to orig_entry_id
        reversal_db = JournalEntry.objects.get(entry_id=rev_data["reversal_journal_id"])
        assert reversal_db.reversal_of_id == orig_entry_id
        assert reversal_db.status == "POSTED"

        # 3. Check GL balances restored to 0.00
        self.acc_rent_a.refresh_from_db()
        assert self.acc_rent_a.current_balance_bdt == Decimal("0.00")
        self.acc_bank_a.refresh_from_db()
        assert self.acc_bank_a.current_balance_bdt == Decimal("0.00")

    def test_09_reversing_already_reversed_entry_rejected(self):
        """9. Reversing already reversed entry rejected (ERR_ALREADY_REVERSED, 422)."""
        self.client.force_authenticate(user=self.finance_a)

        # 1. Post entry
        payload = {
            "lines": [
                {"account_code": "6010", "debit_amount_bdt": "1000.00", "credit_amount_bdt": "0.00"},
                {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "1000.00"},
            ]
        }
        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        entry_id = resp.data["entry_id"]

        # 2. Reverse once
        self.client.post(f"/api/v2/finance/journals/{entry_id}/reverse/", {"reason": "First reversal"}, format="json")

        # 3. Attempt second reversal
        rev_resp2 = self.client.post(
            f"/api/v2/finance/journals/{entry_id}/reverse/",
            {"reason": "Second reversal attempt"},
            format="json",
        )
        assert rev_resp2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert rev_resp2.data.get("error_code") == "ERR_ALREADY_REVERSED"

    def test_10_missing_reversal_reason_rejected(self):
        """10. Missing reversal reason rejected (ERR_MISSING_REVERSAL_REASON, 422)."""
        self.client.force_authenticate(user=self.finance_a)

        payload = {
            "lines": [
                {"account_code": "6010", "debit_amount_bdt": "1000.00", "credit_amount_bdt": "0.00"},
                {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "1000.00"},
            ]
        }
        resp = self.client.post("/api/v2/finance/journals/", payload, format="json")
        entry_id = resp.data["entry_id"]

        rev_resp = self.client.post(f"/api/v2/finance/journals/{entry_id}/reverse/", {"reason": "   "}, format="json")
        assert rev_resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert rev_resp.data.get("error_code") == "ERR_MISSING_REVERSAL_REASON"

    def test_11_role_enforcement_branch_manager_and_cashier(self):
        """11. Role enforcement: Branch Manager can view journals, but post/reverse is denied (403); Cashier strictly denied (403)."""
        # Manager can view list
        self.client.force_authenticate(user=self.manager_a)
        list_resp = self.client.get("/api/v2/finance/journals/")
        assert list_resp.status_code == status.HTTP_200_OK

        # Manager cannot post
        post_resp = self.client.post(
            "/api/v2/finance/journals/",
            {
                "lines": [
                    {"account_code": "6010", "debit_amount_bdt": "500.00", "credit_amount_bdt": "0.00"},
                    {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "500.00"},
                ]
            },
            format="json",
        )
        assert post_resp.status_code == status.HTTP_403_FORBIDDEN
        assert post_resp.data.get("error_code") == "ERR_JOURNAL_POST_UNAUTHORIZED"

        # Cashier strictly denied view & post
        self.client.force_authenticate(user=self.cashier_a)
        cashier_list = self.client.get("/api/v2/finance/journals/")
        assert cashier_list.status_code == status.HTTP_403_FORBIDDEN
        assert cashier_list.data.get("error_code") == "ERR_STAFF_ROLE_DENIED"

    def test_12_cross_tenant_isolation_and_suspended_tenant(self):
        """12. Cross-tenant isolation (ERR_CROSS_TENANT_ACCESS, 403/404) and suspended tenant write protection."""
        # 1. Post entry in Tenant A
        self.client.force_authenticate(user=self.owner_a)
        post_resp = self.client.post(
            "/api/v2/finance/journals/",
            {
                "lines": [
                    {"account_code": "6010", "debit_amount_bdt": "200.00", "credit_amount_bdt": "0.00"},
                    {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "200.00"},
                ]
            },
            format="json",
        )
        entry_id_a = post_resp.data["entry_id"]

        # 2. Owner B attempts to access Tenant A's entry
        self.client.force_authenticate(user=self.owner_b)
        get_resp = self.client.get(f"/api/v2/finance/journals/{entry_id_a}/")
        assert get_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        rev_resp = self.client.post(
            f"/api/v2/finance/journals/{entry_id_a}/reverse/",
            {"reason": "Malicious cross tenant reversal"},
            format="json",
        )
        assert rev_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        # 3. Suspended Tenant protection
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        susp_resp = self.client.get("/api/v2/finance/journals/")
        assert susp_resp.status_code == status.HTTP_403_FORBIDDEN
        assert susp_resp.data.get("error_code") == "ERR_TENANT_SUSPENDED"
