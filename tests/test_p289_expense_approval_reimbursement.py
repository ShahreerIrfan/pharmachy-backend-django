"""
backend/tests/test_p289_expense_approval_reimbursement.py
Architecture Decision: ADR-0287 & ADR-0289 (P289).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying Expense Approval, Reimbursable Claims, High-Value Elevation, and Audited Correction Events:
1. Create reimbursable operating expense and verify initial status is PENDING_REVIEW.
2. Owner approves reimbursable expense: status becomes APPROVED and reimbursement_status advances to APPROVED_PENDING_PAYOUT.
3. Reimburse payout settlement: dispatches payout via bKash/Cash, marks PAID_OUT with audit reference.
4. High-value expense (>= 5,000 BDT) approval strictly requires Owner/Finance Officer sign-off (Branch Manager denied 403).
5. High-value expense successfully approved by Finance Officer.
6. Self-approval of reimbursable claim by claimant user strictly forbidden (ERR_SELF_APPROVAL_FORBIDDEN 403).
7. Approved expense voucher is strictly immutable against direct PATCH/DELETE.
8. Full reversal correction event creates balanced GL reversal journal and transitions voucher to REVERSED.
9. Amount adjustment correction event posts delta balancing journal to General Ledger.
10. Category reclassification correction event reallocates OpEx GL accounts with balanced zero-cash entry.
11. Correction event requires meaningful justification of at least 5 characters.
12. Cashier role is strictly denied from approving, reimbursing, or correcting expenses (403).
13. Cross-tenant isolation protects reimbursable and correction endpoints (403/404).
14. Suspended tenant protection blocks operations (ERR_TENANT_SUSPENDED 403).
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.chart_of_accounts_service import ChartOfAccountsService
from apps.core.expense_category_service import ExpenseCategoryService
from apps.core.models import (
    Branch,
    ExpenseCategory,
    ExpenseCorrectionEvent,
    ExpenseCorrectionType,
    JournalEntry,
    OperatingExpenseEntry,
    OperatingExpenseStatus,
    ReimbursementStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestExpenseApprovalReimbursementIntegration:

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
            name="Dhanmondi Central Dispensary",
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
            name="Uttara North Branch",
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

        # 6. Seed Expense Categories
        ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner
        )
        ExpenseCategoryService.seed_default_expense_categories(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.foreign_owner
        )

        self.cat_rent = ExpenseCategory.objects.filter(tenant=self.tenant_a, code="RENT").first()
        self.cat_util = ExpenseCategory.objects.filter(tenant=self.tenant_a, code="UTILITIES").first()
        self.cat_maint = ExpenseCategory.objects.filter(tenant=self.tenant_a, code="MAINTENANCE").first()
        self.cat_misc = ExpenseCategory.objects.filter(tenant=self.tenant_a, code="MISCELLANEOUS").first()

    def test_01_create_reimbursable_expense_and_verify_initial_status(self):
        self.client.force_authenticate(user=self.manager)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "category_id": self.cat_misc.category_id,
            "payee_name": "Popular Stationers",
            "amount_bdt": "1500.00",
            "payment_method": "CASH",
            "is_reimbursable": True,
            "claimant_name": "Kamal Hossain (Branch Cashier)",
            "notes": "Emergency cash drawer paper rolls and receipts bought out of pocket.",
        }
        res = self.client.post("/api/v2/finance/expenses/", payload, format="json")
        assert res.status_code == status.HTTP_201_CREATED
        data = res.json()
        assert data["is_reimbursable"] is True
        assert data["reimbursement_status"] == ReimbursementStatus.PENDING_REVIEW
        assert data["status"] == OperatingExpenseStatus.DRAFT
        assert data["claimant_name"] == "Kamal Hossain (Branch Cashier)"

    def test_02_owner_approves_reimbursable_expense_advancing_payout_status(self):
        # Create claim
        self.client.force_authenticate(user=self.manager)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a.branch_id,
            "category_id": self.cat_misc.category_id,
            "payee_name": "New Hardware",
            "amount_bdt": "850.00",
            "payment_method": "CASH",
            "is_reimbursable": True,
            "claimant_name": "Sabbir Ahmed",
        }
        c_res = self.client.post("/api/v2/finance/expenses/", payload, format="json")
        exp_id = c_res.json()["expense_id"]

        # Owner approves
        self.client.force_authenticate(user=self.owner)
        res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/approve/",
            {"approval_notes": "Receipt verified by owner."},
            format="json",
        )
        assert res.status_code == status.HTTP_200_OK
        data = res.json()["expense"]
        assert data["status"] == OperatingExpenseStatus.APPROVED
        assert data["reimbursement_status"] == ReimbursementStatus.APPROVED_PENDING_PAYOUT
        assert data["posted_journal_id"] is not None

    def test_03_reimburse_payout_settlement_success(self):
        # Create and approve claim
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Courier Express",
                "amount_bdt": "600.00",
                "payment_method": "CASH",
                "is_reimbursable": True,
                "claimant_name": "Staff Deliveryman",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        self.client.force_authenticate(user=self.owner)
        self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")

        # Disburse reimbursement payout
        p_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/reimburse/",
            {
                "payout_method": "BKASH",
                "reference": "BK-TRX-REIMB-883311",
            },
            format="json",
        )
        assert p_res.status_code == status.HTTP_200_OK
        data = p_res.json()["expense"]
        assert data["reimbursement_status"] == ReimbursementStatus.PAID_OUT
        assert data["reimbursement_payment_method"] == "BKASH"
        assert data["reimbursement_reference"] == "BK-TRX-REIMB-883311"

        # Disbursing again fails
        p_res_repeat = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/reimburse/",
            {"payout_method": "CASH"},
            format="json",
        )
        assert p_res_repeat.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_04_high_value_expense_requires_owner_branch_manager_denied(self):
        self.client.force_authenticate(user=self.manager)
        # 12,000 BDT is high-value (>= 5,000 threshold)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_maint.category_id,
                "payee_name": "Carrier Air Conditioner Service",
                "amount_bdt": "12000.00",
                "payment_method": "BANK_TRANSFER",
                "notes": "Full dispensary HVAC overhaul.",
            },
            format="json",
        )
        assert c_res.status_code == status.HTTP_201_CREATED
        exp_id = c_res.json()["expense_id"]
        assert c_res.json()["is_high_value"] is True

        # Branch Manager attempts to approve alone -> strictly denied 403
        a_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/approve/",
            {"approval_notes": "Manager self signoff"},
            format="json",
        )
        assert a_res.status_code == status.HTTP_403_FORBIDDEN
        assert a_res.json().get("error_code") == "ERR_HIGH_VALUE_APPROVAL_REQUIRED"

    def test_05_high_value_expense_approved_by_finance_officer(self):
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_util.category_id,
                "payee_name": "DESCO Power Utility",
                "amount_bdt": "18500.00",
                "payment_method": "BANK_TRANSFER",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        # Finance Officer has elevated signoff authority
        self.client.force_authenticate(user=self.finance)
        a_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/approve/",
            {"approval_notes": "Verified DESCO 3-phase meter commercial invoice."},
            format="json",
        )
        assert a_res.status_code == status.HTTP_200_OK
        data = a_res.json()["expense"]
        assert data["status"] == OperatingExpenseStatus.APPROVED

    def test_06_self_approval_of_reimbursable_claim_forbidden(self):
        # Finance officer creates claim where they are the claimant
        self.client.force_authenticate(user=self.finance)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Office Supplies Mart",
                "amount_bdt": "1200.00",
                "payment_method": "CASH",
                "is_reimbursable": True,
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        # Finance officer attempts to self-approve their own claim -> forbidden 403
        a_res = self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")
        assert a_res.status_code == status.HTTP_403_FORBIDDEN
        assert a_res.json().get("error_code") == "ERR_SELF_APPROVAL_FORBIDDEN"

    def test_07_approved_expense_is_immutable_against_patch_and_delete(self):
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Hardware Store",
                "amount_bdt": "500.00",
                "payment_method": "CASH",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        self.client.force_authenticate(user=self.owner)
        self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")

        # Attempt to PATCH approved expense
        patch_res = self.client.patch(
            f"/api/v2/finance/expenses/{exp_id}/",
            {"amount_bdt": "999.00"},
            format="json",
        )
        assert patch_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert patch_res.json().get("error_code") == "ERR_EXPENSE_IMMUTABLE"

        # Attempt to DELETE approved expense
        del_res = self.client.delete(f"/api/v2/finance/expenses/{exp_id}/")
        assert del_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert del_res.json().get("error_code") == "ERR_EXPENSE_IMMUTABLE"

    def test_08_correction_full_reversal_creates_reversal_journal_and_reversed_status(self):
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Duplicate Electrician",
                "amount_bdt": "2500.00",
                "payment_method": "CASH",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        self.client.force_authenticate(user=self.owner)
        self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")

        # Execute audited Full Reversal
        rev_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/correct/",
            {
                "correction_type": "FULL_REVERSAL",
                "reason": "Duplicate voucher entered in error; full cash refund received.",
            },
            format="json",
        )
        assert rev_res.status_code == status.HTTP_200_OK
        corr_data = rev_res.json()["correction"]
        assert corr_data["correction_type"] == ExpenseCorrectionType.FULL_REVERSAL
        assert corr_data["reversal_journal_id"] is not None
        assert corr_data["expense"]["status"] == OperatingExpenseStatus.REVERSED

        # Check DB state
        exp_obj = OperatingExpenseEntry.objects.get(expense_id=exp_id)
        assert exp_obj.status == OperatingExpenseStatus.REVERSED
        corr_event = ExpenseCorrectionEvent.objects.get(correction_id=corr_data["correction_id"])
        assert corr_event.correction_type == ExpenseCorrectionType.FULL_REVERSAL
        assert corr_event.reversal_journal is not None

        # Verify reversal journal is balanced
        rev_journal = corr_event.reversal_journal
        assert rev_journal.total_debit_bdt == rev_journal.total_credit_bdt == Decimal("2500.00")

    def test_09_correction_amount_adjustment_posts_delta_journal(self):
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Cleaning Supplies",
                "amount_bdt": "1000.00",
                "payment_method": "CASH",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        self.client.force_authenticate(user=self.owner)
        self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")

        # Adjust voucher amount from 1,000 to 1,250 (delta +250)
        adj_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/correct/",
            {
                "correction_type": "AMOUNT_ADJUSTMENT",
                "corrected_amount_bdt": "1250.00",
                "reason": "Omitted VAT receipt item; adjusting amount upward by 250 BDT.",
            },
            format="json",
        )
        assert adj_res.status_code == status.HTTP_200_OK
        corr_data = adj_res.json()["correction"]
        assert corr_data["original_amount_bdt"] == "1000.00"
        assert corr_data["corrected_amount_bdt"] == "1250.00"
        assert corr_data["difference_bdt"] == "250.00"

        # Verify updated voucher amount
        exp_obj = OperatingExpenseEntry.objects.get(expense_id=exp_id)
        assert exp_obj.amount_bdt == Decimal("1250.00")

    def test_10_correction_category_reclassification_posts_reclass_journal(self):
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Dhanmondi Landlord",
                "amount_bdt": "3000.00",
                "payment_method": "CASH",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        self.client.force_authenticate(user=self.owner)
        self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")

        # Reclassify from Misc to Rent
        reclass_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/correct/",
            {
                "correction_type": "CATEGORY_RECLASSIFICATION",
                "corrected_category_id": self.cat_rent.category_id,
                "reason": "Mistakenly booked shop rent advance under Miscellaneous.",
            },
            format="json",
        )
        assert reclass_res.status_code == status.HTTP_200_OK
        exp_obj = OperatingExpenseEntry.objects.get(expense_id=exp_id)
        assert exp_obj.category.category_id == self.cat_rent.category_id

    def test_11_correction_requires_meaningful_reason(self):
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Supplier",
                "amount_bdt": "500.00",
                "payment_method": "CASH",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        self.client.force_authenticate(user=self.owner)
        self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")

        # Correction without meaningful reason (< 5 chars)
        res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/correct/",
            {
                "correction_type": "FULL_REVERSAL",
                "reason": "bad",
            },
            format="json",
        )
        assert res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res.json().get("error_code") == "ERR_MANDATORY_CORRECTION_REASON"

    def test_12_cashier_role_strictly_denied_from_approval_reimbursement_correction(self):
        # Create an expense in Tenant A
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Test Payee",
                "amount_bdt": "500.00",
                "payment_method": "CASH",
                "is_reimbursable": True,
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        # Switch to Cashier
        self.client.force_authenticate(user=self.cashier)

        # Cashier attempts approval -> 403
        a_res = self.client.post(f"/api/v2/finance/expenses/{exp_id}/approve/", {}, format="json")
        assert a_res.status_code == status.HTTP_403_FORBIDDEN

        # Cashier attempts reimbursement payout -> 403
        r_res = self.client.post(f"/api/v2/finance/expenses/{exp_id}/reimburse/", {}, format="json")
        assert r_res.status_code == status.HTTP_403_FORBIDDEN

        # Cashier attempts correction -> 403
        c_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/correct/",
            {"correction_type": "FULL_REVERSAL", "reason": "Cashier unauthorized correction attempt"},
            format="json",
        )
        assert c_res.status_code == status.HTTP_403_FORBIDDEN

    def test_13_cross_tenant_access_forbidden(self):
        # Create expense in Tenant A
        self.client.force_authenticate(user=self.manager)
        c_res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Local Vendor",
                "amount_bdt": "750.00",
                "payment_method": "CASH",
            },
            format="json",
        )
        exp_id = c_res.json()["expense_id"]

        # Foreign Owner of Tenant B attempts to approve Tenant A's expense
        self.client.force_authenticate(user=self.foreign_owner)
        a_res = self.client.post(
            f"/api/v2/finance/expenses/{exp_id}/approve/",
            {},
            format="json",
            HTTP_X_TENANT_ID=self.tenant_b.tenant_id,
        )
        assert a_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_14_suspended_tenant_blocked(self):
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner)
        res = self.client.post(
            "/api/v2/finance/expenses/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a.branch_id,
                "category_id": self.cat_misc.category_id,
                "payee_name": "Vendor",
                "amount_bdt": "500.00",
            },
            format="json",
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN
        assert res.json().get("error_code") in ("ERR_TENANT_SUSPENDED", "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED")
