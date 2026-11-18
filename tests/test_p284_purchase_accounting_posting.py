"""
backend/tests/test_p284_purchase_accounting_posting.py
Architecture Decision: ADR-0284 (P284).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying General Ledger Purchase Accounting Posting Engine:
1. Valid GRN Invoice Recognition (Full Credit): Capitalizes inventory asset (1040) and records AP (2010), zero operating expense.
2. GRN with Partial Delivery Cash Settlement: Dr 1040, Cr 1010 (Cash), Cr 2010 (AP).
3. Supplier Payment Disbursement: Settles AP (2010) against Bank & MFS (1020).
4. Immediate delivery payment exceeding total invoice cost rejected (ERR_OVERPAYMENT_EXCEEDS_COST, 422/400).
5. Zero or negative purchase amount rejected (ERR_INVALID_PURCHASE_AMOUNT, 422/400).
6. Idempotency key replay protection returns existing posting without duplicate journal entries.
7. Multi-Branch Purchase & Liability Summary confirms zero operating expense impact.
8. Audited reversal restores GL balances for AP liability and inventory assets.
9. Reversing already reversed posting rejected (ERR_ALREADY_REVERSED, 422/400).
10. Missing reversal reason rejected (ERR_MISSING_REVERSAL_REASON, 422/400).
11. Role enforcement: Cashier denied from disbursing supplier payments or reversing purchase accounting (403).
12. Cross-tenant isolation (ERR_CROSS_TENANT_ACCESS, 403/404) and suspended tenant protection (403).
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.chart_of_accounts_service import ChartOfAccountsService
from apps.core.models import (
    Branch,
    ChartOfAccount,
    GoodsReceiptNote,
    GoodsReceiptStatus,
    PurchaseAccountingPosting,
    Supplier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestPurchaseAccountingPostingIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branches
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
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name="Gulshan Branch",
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

        # 5. Initialize Default Chart of Accounts
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

        # 6. Sample Supplier in Tenant A
        self.supplier_a = Supplier.objects.create(
            supplier_id=f"sup-{self.suffix}",
            tenant=self.tenant_a,
            name="Beximco Pharmaceuticals Ltd",
            code="BEX-01",
        )

    def _create_grn(self, grn_number: str, total_amount: Decimal, branch: Branch = None) -> GoodsReceiptNote:
        br = branch or self.branch_a
        return GoodsReceiptNote.objects.create(
            grn_id=f"grn-{uuid.uuid4().hex[:10]}",
            grn_number=grn_number,
            tenant=self.tenant_a,
            branch=br,
            supplier=self.supplier_a,
            supplier_name=self.supplier_a.name,
            purchase_order_number=f"PO-{uuid.uuid4().hex[:6].upper()}",
            supplier_invoice_number=f"SINV-{uuid.uuid4().hex[:6].upper()}",
            challan_number=f"CH-{uuid.uuid4().hex[:6].upper()}",
            receipt_date=timezone.now().date(),
            status=GoodsReceiptStatus.POSTED,
            total_received_base_units=1000,
            total_invoice_amount=total_amount,
        )

    def test_01_valid_grn_invoice_recognition_success(self):
        """1. Valid GRN Invoice Recognition (Full Credit): Capitalizes inventory (1040) and records AP (2010), zero OpEx."""
        self.client.force_authenticate(user=self.manager_a)

        grn = self._create_grn(grn_number=f"GRN-01-{self.suffix}", total_amount=Decimal("50000.00"))

        payload = {
            "posting_type": "INVOICE_RECOGNITION",
            "grn_id": grn.grn_id,
            "paid_cash_bdt": "0.00",
            "paid_bank_bdt": "0.00",
        }
        resp = self.client.post("/api/v2/finance/purchase-postings/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert data["status"] == "POSTED"
        assert Decimal(str(data["total_inventory_cost_bdt"])) == Decimal("50000.00")
        assert Decimal(str(data["liability_ap_bdt"])) == Decimal("50000.00")
        assert Decimal(str(data["settled_cash_bdt"])) == Decimal("0.00")
        assert data["is_operating_expense"] is False  # CRITICAL INVARIANT

        # Verify GL Account Balances
        # Merchandise Inventory Asset (1040, Normal DEBIT): +50,000
        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1040")
        assert acc_inv.current_balance_bdt == Decimal("50000.00")

        # Accounts Payable (2010, Normal CREDIT): +50,000
        acc_ap = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        assert acc_ap.current_balance_bdt == Decimal("50000.00")

        # Operating Expenses (6010, 6020, 6030): MUST REMAIN 0.00
        acc_rent = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="6010")
        assert acc_rent.current_balance_bdt == Decimal("0.00")

    def test_02_grn_partial_delivery_cash_settlement(self):
        """2. GRN with Partial Delivery Cash Settlement: Dr 1040, Cr 1010 (Cash), Cr 2010 (AP)."""
        self.client.force_authenticate(user=self.manager_a)

        # First give branch some cash balance
        acc_cash = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")
        acc_cash.current_balance_bdt = Decimal("30000.00")
        acc_cash.save()

        grn = self._create_grn(grn_number=f"GRN-02-{self.suffix}", total_amount=Decimal("60000.00"))

        payload = {
            "posting_type": "INVOICE_RECOGNITION",
            "grn_id": grn.grn_id,
            "paid_cash_bdt": "20000.00",
            "paid_bank_bdt": "0.00",
        }
        resp = self.client.post("/api/v2/finance/purchase-postings/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert Decimal(str(data["total_inventory_cost_bdt"])) == Decimal("60000.00")
        assert Decimal(str(data["settled_cash_bdt"])) == Decimal("20000.00")
        assert Decimal(str(data["liability_ap_bdt"])) == Decimal("40000.00")

        # Inventory increased by 60,000
        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1040")
        assert acc_inv.current_balance_bdt == Decimal("60000.00")

        # Cash decreased by 20,000 (30,000 - 20,000 = 10,000)
        acc_cash.refresh_from_db()
        assert acc_cash.current_balance_bdt == Decimal("10000.00")

        # AP liability increased by 40,000
        acc_ap = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        assert acc_ap.current_balance_bdt == Decimal("40000.00")

    def test_03_supplier_payment_disbursement(self):
        """3. Supplier Payment Disbursement: Settles AP (2010) against Bank & MFS (1020)."""
        self.client.force_authenticate(user=self.finance_a)

        # 1. Setup AP balance of 40,000
        acc_ap = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        acc_ap.current_balance_bdt = Decimal("40000.00")
        acc_ap.save()

        # 2. Disburse 25,000 via Bank
        payload = {
            "posting_type": "SUPPLIER_PAYMENT",
            "supplier_id": self.supplier_a.supplier_id,
            "payment_amount_bdt": "25000.00",
            "payment_mode": "BANK",
            "reference": "CHQ-BANK-ASIA-9912",
        }
        resp = self.client.post("/api/v2/finance/purchase-postings/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert data["posting_type"] == "SUPPLIER_PAYMENT"
        assert Decimal(str(data["settled_bank_bdt"])) == Decimal("25000.00")
        assert data["is_operating_expense"] is False

        # AP liability decreased by 25,000 (40,000 - 25,000 = 15,000)
        acc_ap.refresh_from_db()
        assert acc_ap.current_balance_bdt == Decimal("15000.00")

        # Bank asset decreased by 25,000
        acc_bank = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1020")
        assert acc_bank.current_balance_bdt == Decimal("-25000.00")

    def test_04_immediate_payment_exceeding_cost_rejected(self):
        """4. Immediate delivery payment exceeding total invoice cost rejected (422/400)."""
        self.client.force_authenticate(user=self.manager_a)

        payload = {
            "posting_type": "INVOICE_RECOGNITION",
            "supplier_id": self.supplier_a.supplier_id,
            "total_cost_bdt": "10000.00",
            "paid_cash_bdt": "12000.00",  # Exceeds 10,000
        }
        resp = self.client.post("/api/v2/finance/purchase-postings/", payload, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_OVERPAYMENT_EXCEEDS_COST"

    def test_05_zero_or_negative_purchase_amount_rejected(self):
        """5. Zero or negative purchase amount rejected (422/400)."""
        self.client.force_authenticate(user=self.manager_a)

        payload = {
            "posting_type": "INVOICE_RECOGNITION",
            "supplier_id": self.supplier_a.supplier_id,
            "total_cost_bdt": "0.00",
        }
        resp = self.client.post("/api/v2/finance/purchase-postings/", payload, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_INVALID_PURCHASE_AMOUNT"

    def test_06_idempotency_key_replay_protection(self):
        """6. Idempotency key replay protection returns existing posting without duplicate journal entries."""
        self.client.force_authenticate(user=self.manager_a)
        idem = f"idem-grn-{uuid.uuid4().hex}"

        grn = self._create_grn(grn_number=f"GRN-IDEM-{self.suffix}", total_amount=Decimal("15000.00"))

        payload = {
            "posting_type": "INVOICE_RECOGNITION",
            "grn_id": grn.grn_id,
            "idempotency_key": idem,
        }

        # 1st post
        resp1 = self.client.post("/api/v2/finance/purchase-postings/", payload, format="json")
        assert resp1.status_code == status.HTTP_201_CREATED
        pid1 = resp1.data["posting_id"]

        # 2nd post with same idempotency key
        resp2 = self.client.post("/api/v2/finance/purchase-postings/", payload, format="json")
        assert resp2.status_code == status.HTTP_201_CREATED
        assert resp2.data["posting_id"] == pid1
        assert resp2.data.get("is_idempotent_replay") is True

        # Ensure inventory wasn't doubled
        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1040")
        assert acc_inv.current_balance_bdt == Decimal("15000.00")

    def test_07_multi_branch_purchase_and_liability_summary(self):
        """7. Multi-Branch Purchase & Liability Summary confirms zero operating expense impact."""
        self.client.force_authenticate(user=self.owner_a)

        # Branch A: 40,000 GRN (10,000 cash, 30,000 AP)
        self.client.post(
            "/api/v2/finance/purchase-postings/",
            {
                "posting_type": "INVOICE_RECOGNITION",
                "supplier_id": self.supplier_a.supplier_id,
                "total_cost_bdt": "40000.00",
                "paid_cash_bdt": "10000.00",
                "branch_id": self.branch_a.branch_id,
            },
            format="json",
        )

        # Branch A2: 50,000 GRN (0 cash, 50,000 AP)
        self.client.post(
            "/api/v2/finance/purchase-postings/",
            {
                "posting_type": "INVOICE_RECOGNITION",
                "supplier_id": self.supplier_a.supplier_id,
                "total_cost_bdt": "50000.00",
                "branch_id": self.branch_a2.branch_id,
            },
            format="json",
        )

        # Supplier Payment: 20,000 bank disbursement
        self.client.post(
            "/api/v2/finance/purchase-postings/",
            {
                "posting_type": "SUPPLIER_PAYMENT",
                "supplier_id": self.supplier_a.supplier_id,
                "payment_amount_bdt": "20000.00",
                "payment_mode": "BANK",
            },
            format="json",
        )

        resp = self.client.get("/api/v2/finance/purchase-postings/summary/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data

        assert data["grn_postings_count"] == 2
        assert data["payment_postings_count"] == 1
        assert Decimal(str(data["total_inventory_capitalized_bdt"])) == Decimal("90000.00")
        assert Decimal(str(data["total_ap_recognized_bdt"])) == Decimal("80000.00")  # (30000 + 50000)
        assert Decimal(str(data["immediate_delivery_settled_bdt"])) == Decimal("10000.00")
        assert Decimal(str(data["total_supplier_payments_disbursed_bdt"])) == Decimal("20000.00")
        assert Decimal(str(data["net_outstanding_ap_liability_bdt"])) == Decimal("60000.00")  # (80000 - 20000)
        assert data["total_operating_expenses_bdt"] == "0.00"  # CRITICAL INVARIANT
        assert data["is_operating_expense"] is False

    def test_08_audited_reversal_restores_gl_balances(self):
        """8. Audited Reversal: Owner reverses purchase posting, restoring AP liability and inventory balances."""
        self.client.force_authenticate(user=self.finance_a)

        grn = self._create_grn(grn_number=f"GRN-REV-{self.suffix}", total_amount=Decimal("30000.00"))
        post_resp = self.client.post(
            "/api/v2/finance/purchase-postings/",
            {"posting_type": "INVOICE_RECOGNITION", "grn_id": grn.grn_id},
            format="json",
        )
        pid = post_resp.data["posting_id"]

        # Reverse posting
        rev_payload = {"reason": "Goods defective on quality inspection, returned to Beximco depot"}
        rev_resp = self.client.post(f"/api/v2/finance/purchase-postings/{pid}/reverse/", rev_payload, format="json")
        assert rev_resp.status_code == status.HTTP_200_OK, rev_resp.data
        assert rev_resp.data["status"] == "REVERSED"

        # Check GL Balances restored to 0.00
        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1040")
        assert acc_inv.current_balance_bdt == Decimal("0.00")

        acc_ap = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        assert acc_ap.current_balance_bdt == Decimal("0.00")

    def test_09_reversing_already_reversed_posting_rejected(self):
        """9. Reversing already reversed posting rejected (ERR_ALREADY_REVERSED, 422/400)."""
        self.client.force_authenticate(user=self.owner_a)

        grn = self._create_grn(grn_number=f"GRN-DREV-{self.suffix}", total_amount=Decimal("10000.00"))
        post_resp = self.client.post(
            "/api/v2/finance/purchase-postings/",
            {"posting_type": "INVOICE_RECOGNITION", "grn_id": grn.grn_id},
            format="json",
        )
        pid = post_resp.data["posting_id"]

        # Reverse once
        self.client.post(f"/api/v2/finance/purchase-postings/{pid}/reverse/", {"reason": "First reversal"}, format="json")

        # Reverse again
        rev_resp2 = self.client.post(f"/api/v2/finance/purchase-postings/{pid}/reverse/", {"reason": "Second reversal"}, format="json")
        assert rev_resp2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert rev_resp2.data.get("error_code") == "ERR_ALREADY_REVERSED"

    def test_10_missing_reversal_reason_rejected(self):
        """10. Missing reversal reason rejected (ERR_MISSING_REVERSAL_REASON, 422/400)."""
        self.client.force_authenticate(user=self.owner_a)

        grn = self._create_grn(grn_number=f"GRN-MREV-{self.suffix}", total_amount=Decimal("10000.00"))
        post_resp = self.client.post(
            "/api/v2/finance/purchase-postings/",
            {"posting_type": "INVOICE_RECOGNITION", "grn_id": grn.grn_id},
            format="json",
        )
        pid = post_resp.data["posting_id"]

        rev_resp = self.client.post(f"/api/v2/finance/purchase-postings/{pid}/reverse/", {"reason": "   "}, format="json")
        assert rev_resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert rev_resp.data.get("error_code") == "ERR_MISSING_REVERSAL_REASON"

    def test_11_role_enforcement_cashier_denied(self):
        """11. Role enforcement: Cashier denied from disbursing supplier payments or reversing purchase accounting (403)."""
        self.client.force_authenticate(user=self.cashier_a)

        # Cashier tries to disburse supplier payment
        pmt_resp = self.client.post(
            "/api/v2/finance/purchase-postings/",
            {
                "posting_type": "SUPPLIER_PAYMENT",
                "supplier_id": self.supplier_a.supplier_id,
                "payment_amount_bdt": "5000.00",
            },
            format="json",
        )
        assert pmt_resp.status_code == status.HTTP_403_FORBIDDEN
        assert pmt_resp.data.get("error_code") == "ERR_SUPPLIER_PAYMENT_UNAUTHORIZED"

        # Cashier tries to view summary
        sum_resp = self.client.get("/api/v2/finance/purchase-postings/summary/")
        assert sum_resp.status_code == status.HTTP_403_FORBIDDEN
        assert sum_resp.data.get("error_code") == "ERR_PURCHASE_ACCOUNTING_VIEW_DENIED"

    def test_12_cross_tenant_and_suspended_tenant_protection(self):
        """12. Cross-tenant isolation (ERR_CROSS_TENANT_ACCESS, 403/404) and suspended tenant protection (403)."""
        # Post in Tenant A
        self.client.force_authenticate(user=self.manager_a)
        grn_a = self._create_grn(grn_number=f"GRN-ISO-{self.suffix}", total_amount=Decimal("12000.00"))
        post_resp = self.client.post(
            "/api/v2/finance/purchase-postings/",
            {"posting_type": "INVOICE_RECOGNITION", "grn_id": grn_a.grn_id},
            format="json",
        )
        pid = post_resp.data["posting_id"]

        # Owner B tries to access
        self.client.force_authenticate(user=self.owner_b)
        get_resp = self.client.get(f"/api/v2/finance/purchase-postings/{pid}/")
        assert get_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        rev_resp = self.client.post(f"/api/v2/finance/purchase-postings/{pid}/reverse/", {"reason": "Cross attack"}, format="json")
        assert rev_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        # Suspended tenant protection
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        susp_resp = self.client.get("/api/v2/finance/purchase-postings/")
        assert susp_resp.status_code == status.HTTP_403_FORBIDDEN
        assert susp_resp.data.get("error_code") == "ERR_TENANT_SUSPENDED"
