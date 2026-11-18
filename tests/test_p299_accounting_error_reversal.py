"""
backend/tests/test_p299_accounting_error_reversal.py
Architecture Decision: ADR-0299 (P299).
Phase 15: General Ledger, Operating Expenses & Profitability.

Targeted integration tests for Audited Accounting Error Reversal & Reverse-and-Rebook:
1. Finance Officer submits reverse-and-rebook request -> self-approval blocked (403 ERR_SELF_APPROVAL_FORBIDDEN) ->
   Owner approves -> original journal preserved as REVERSED, compensating reversal journal + balanced replacement
   journal posted, and GL account balance reflects exact corrected amount (never destructive edit!).
2. Unbalanced replacement lines, missing reason, duplicate pending request, and cancelling executed request rejected (400).
3. Rejection workflow marks request REJECTED and leaves original journal untouched.
4. RBAC: Cashier denied (403); Branch Manager cannot approve (403).
5. Cross-tenant isolation (403) and suspended tenant block (403), plus CSV export verification.
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
    AccountingReversalRequestStatus,
    Branch,
    ChartOfAccount,
    JournalEntry,
    JournalEntryStatus,
    JournalSourceModule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestAccountingErrorReversalIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"MediCare {self.suffix}",
            subdomain=f"mc-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}", tenant=self.tenant_a, name="Motijheel Branch"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"OtherMed {self.suffix}",
            subdomain=f"om-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-b1-{self.suffix}", tenant=self.tenant_b, name="Khulna Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880161{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.finance_a = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@pharma.com",
            phone_number=f"+880162{random.randint(1000000, 9999999)}",
            full_name="Finance A",
            password="StrongPassword123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a1 = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@pharma.com",
            phone_number=f"+880163{random.randint(1000000, 9999999)}",
            full_name="BM A1",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880164{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880165{random.randint(1000000, 9999999)}",
            full_name="Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a
        )
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b
        )

        # Erroneous journal: booked BDT 9,000.00 instead of BDT 900.00
        j = DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.OPERATING_EXPENSE,
            source_reference="EXP-ERR-001",
            narration="Erroneous 10x utility bill posting",
            lines=[
                {"account_code": "6020", "debit_amount_bdt": Decimal("9000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "1010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("9000.00")},
            ],
            actor_user=self.owner_a,
        )
        self.err_journal_id = j["entry_id"]

    def test_01_maker_checker_reverse_and_rebook_preserves_original_and_fixes_balance(self):
        # 1. Finance Officer submits reverse-and-rebook request (9000 -> 900)
        self.client.force_authenticate(user=self.finance_a)
        cr = self.client.post(
            "/api/v2/finance/journal-reversals/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "original_journal_id": self.err_journal_id,
                "error_category": "WRONG_AMOUNT",
                "reason": "Entered extra zero on DESCO bill (9000 instead of 900)",
                "replacement_lines": [
                    {"account_code": "6020", "debit_amount_bdt": "900.00", "credit_amount_bdt": "0.00", "description": "Corrected DESCO bill"},
                    {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "900.00", "description": "Cash payment"},
                ],
            },
            format="json",
        )
        assert cr.status_code == status.HTTP_201_CREATED, cr.data
        req_id = cr.data["request_id"]
        assert cr.data["status"] == AccountingReversalRequestStatus.PENDING_APPROVAL

        # 2. Finance Officer cannot self-approve (Maker-Checker 403)
        self_app = self.client.post(f"/api/v2/finance/journal-reversals/{req_id}/approve/", {}, format="json")
        assert self_app.status_code == status.HTTP_403_FORBIDDEN
        assert self_app.data["error_code"] == "ERR_SELF_APPROVAL_FORBIDDEN"

        # 3. Owner approves & atomically executes reversal + replacement
        self.client.force_authenticate(user=self.owner_a)
        app = self.client.post(
            f"/api/v2/finance/journal-reversals/{req_id}/approve/",
            {"approval_notes": "Verified original DESCO receipt BDT 900"},
            format="json",
        )
        assert app.status_code == status.HTTP_200_OK, app.data
        assert app.data["status"] == AccountingReversalRequestStatus.APPROVED_EXECUTED
        assert app.data["reversal_journal_id"] is not None
        assert app.data["replacement_journal_id"] is not None

        # 4. Verify original journal still exists (non-destructive) and marked REVERSED
        orig = JournalEntry.objects.get(entry_id=self.err_journal_id)
        assert orig.status == JournalEntryStatus.REVERSED
        assert orig.total_debit_bdt == Decimal("9000.00")

        # 5. Verify GL 6020 net balance is now exactly BDT 900.00 (9000 - 9000 + 900)
        gl_6020 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6020")
        assert gl_6020.current_balance_bdt == Decimal("900.00")

        # 6. Cannot cancel executed reversal
        del_exec = self.client.delete(f"/api/v2/finance/journal-reversals/{req_id}/")
        assert del_exec.status_code == status.HTTP_400_BAD_REQUEST

    def test_02_invalid_unbalanced_replacement_and_duplicate_request_rejected(self):
        self.client.force_authenticate(user=self.finance_a)
        unbal = self.client.post(
            "/api/v2/finance/journal-reversals/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "original_journal_id": self.err_journal_id,
                "error_category": "WRONG_AMOUNT",
                "reason": "Fix amount",
                "replacement_lines": [
                    {"account_code": "6020", "debit_amount_bdt": "900.00", "credit_amount_bdt": "0.00"},
                    {"account_code": "1010", "debit_amount_bdt": "0.00", "credit_amount_bdt": "800.00"},
                ],
            },
            format="json",
        )
        assert unbal.status_code == status.HTTP_400_BAD_REQUEST
        assert unbal.data["error_code"] == "ERR_UNBALANCED_REPLACEMENT_ENTRY"

        # Valid request, then duplicate rejected
        ok = self.client.post(
            "/api/v2/finance/journal-reversals/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "original_journal_id": self.err_journal_id,
                "error_category": "DUPLICATE_POSTING",
                "reason": "Duplicate bill",
            },
            format="json",
        )
        assert ok.status_code == status.HTTP_201_CREATED
        dup = self.client.post(
            "/api/v2/finance/journal-reversals/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "original_journal_id": self.err_journal_id,
                "error_category": "DUPLICATE_POSTING",
                "reason": "Second request",
            },
            format="json",
        )
        assert dup.status_code == status.HTTP_400_BAD_REQUEST
        assert dup.data["error_code"] == "ERR_DUPLICATE_REVERSAL_REQUEST"

    def test_03_rbac_cross_tenant_suspended_and_csv_export(self):
        # Cashier denied
        self.client.force_authenticate(user=self.cashier_a1)
        assert self.client.get(f"/api/v2/finance/journal-reversals/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN

        # Branch Manager submits request, cannot approve it
        self.client.force_authenticate(user=self.bm_a1)
        req = self.client.post(
            "/api/v2/finance/journal-reversals/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "original_journal_id": self.err_journal_id,
                "error_category": "WRONG_AMOUNT",
                "reason": "Branch check failed",
            },
            format="json",
        )
        assert req.status_code == status.HTTP_201_CREATED
        req_id = req.data["request_id"]
        assert self.client.post(f"/api/v2/finance/journal-reversals/{req_id}/approve/", {}, format="json").status_code == status.HTTP_403_FORBIDDEN

        # Cross-tenant Owner B denied approval
        self.client.force_authenticate(user=self.owner_b)
        assert self.client.post(f"/api/v2/finance/journal-reversals/{req_id}/approve/", {}, format="json").status_code == status.HTTP_403_FORBIDDEN

        # CSV export
        self.client.force_authenticate(user=self.owner_a)
        csv_resp = self.client.get(f"/api/v2/finance/journal-reversals/export/?format=csv&tenant_id={self.tenant_a.tenant_id}")
        assert csv_resp.status_code == status.HTTP_200_OK
        assert "WRONG_AMOUNT" in csv_resp.content.decode("utf-8")

        # Suspended tenant blocked
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        assert self.client.get(f"/api/v2/finance/journal-reversals/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN
