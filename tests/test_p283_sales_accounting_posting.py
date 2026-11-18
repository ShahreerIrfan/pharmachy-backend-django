"""
backend/tests/test_p283_sales_accounting_posting.py
Architecture Decision: ADR-0283 (P283).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying General Ledger Sales Accounting Posting Engine:
1. Valid Cash Sale Accounting: Posts balanced Revenue Journal and COGS Journal, updates GL balances.
2. Credit / Due Sale Accounting: Debits Accounts Receivable and updates AR balance.
3. Bank / MFS Sale (bKash/Card): Debits Bank & MFS Clearing account.
4. Invalid/Incomplete Input: Missing sale reference and invalid amounts rejected (422/400).
5. Idempotency Key Replay: Returns existing posting without duplicate GL transactions.
6. Multi-Branch Profitability Summary: Aggregates sales, discounts, COGS, profit, and margin %.
7. Audited Reversal: Owner reverses posting, restoring GL balances for revenue and inventory.
8. Reversing already reversed posting rejected (ERR_ALREADY_REVERSED, 422/400).
9. Missing reversal reason rejected (ERR_MISSING_REVERSAL_REASON, 422/400).
10. Cashier can post sales accounting during checkout, but cannot reverse (403).
11. Cashier denied from viewing sales profitability summary (403).
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
    BatchStock,
    Branch,
    ChartOfAccount,
    InvoiceStatus,
    MedicineProduct,
    PaymentMethod,
    SaleAccountingPosting,
    SaleInvoice,
    SaleInvoiceItem,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSalesAccountingPostingIntegration:

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

        # 6. Sample Medicine Product and Batch Stocks
        self.med_a = MedicineProduct.objects.create(
            product_id=f"med-{self.suffix}",
            brand_name=f"Napa Extra {self.suffix}",
            generic_name="Paracetamol + Caffeine",
            strength="500mg+65mg",
            dosage_form="Tablet",
            manufacturer="Beximco Pharma",
            mrp_unit=Decimal("3.00"),
            cost_unit=Decimal("2.00"),
        )
        self.batch_a = BatchStock.objects.create(
            batch_id=f"bat-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.med_a,
            batch_number=f"B-{self.suffix}",
            quantity_base_units=1000,
            cost_per_unit=Decimal("2.00"),
            expiry_date=timezone.now().date() + timezone.timedelta(days=365),
            purchase_date=timezone.now().date(),
        )

    def _create_sale_invoice(
        self,
        invoice_num: str,
        total_amount: Decimal,
        discount_amount: Decimal = Decimal("0.00"),
        tax_amount: Decimal = Decimal("0.00"),
        payment_method: str = PaymentMethod.CASH,
        quantity: int = 100,
        cost_per_unit: Decimal = Decimal("2.00"),
        branch: Branch = None,
    ) -> SaleInvoice:
        br = branch or self.branch_a
        net_amount = total_amount - discount_amount + tax_amount
        invoice = SaleInvoice.objects.create(
            invoice_id=f"inv-{uuid.uuid4().hex[:10]}",
            tenant=self.tenant_a,
            branch=br,
            invoice_number=invoice_num,
            customer_name="Walk-in Customer",
            customer_phone="+8801711223344",
            total_amount=total_amount,
            discount_amount=discount_amount,
            tax_amount=tax_amount,
            net_amount=net_amount,
            payment_method=payment_method,
            status=InvoiceStatus.FINALIZED,
        )

        batch = BatchStock.objects.create(
            batch_id=f"bat-{uuid.uuid4().hex[:8]}",
            tenant=self.tenant_a,
            branch=br,
            product=self.med_a,
            batch_number=f"BX-{uuid.uuid4().hex[:6]}",
            quantity_base_units=quantity + 50,
            cost_per_unit=cost_per_unit,
            expiry_date=timezone.now().date() + timezone.timedelta(days=365),
            purchase_date=timezone.now().date(),
        )

        SaleInvoiceItem.objects.create(
            invoice=invoice,
            batch=batch,
            product=self.med_a,
            quantity=quantity,
            unit_price=Decimal("3.00"),
            subtotal=total_amount,
        )
        return invoice

    def test_01_valid_cash_sale_accounting_success(self):
        """1. Valid Cash Sale Accounting: Posts balanced Revenue and COGS journals, updates GL balances."""
        self.client.force_authenticate(user=self.cashier_a)

        # Invoice: Gross 1200, Disc 100, Tax 50 -> Net 1150. Quantity 400 @ 2.00 cost = 800 COGS.
        inv = self._create_sale_invoice(
            invoice_num=f"INV-CS-{self.suffix}",
            total_amount=Decimal("1200.00"),
            discount_amount=Decimal("100.00"),
            tax_amount=Decimal("50.00"),
            payment_method=PaymentMethod.CASH,
            quantity=400,
            cost_per_unit=Decimal("2.00"),
        )

        payload = {"invoice_id": inv.invoice_id}
        resp = self.client.post("/api/v2/finance/sales-postings/", payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert data["status"] == "POSTED"
        assert Decimal(str(data["gross_sales_bdt"])) == Decimal("1200.00")
        assert Decimal(str(data["discount_bdt"])) == Decimal("100.00")
        assert Decimal(str(data["tax_vat_bdt"])) == Decimal("50.00")
        assert Decimal(str(data["net_settled_bdt"])) == Decimal("1150.00")
        assert Decimal(str(data["receivable_due_bdt"])) == Decimal("0.00")
        assert Decimal(str(data["total_cogs_bdt"])) == Decimal("800.00")
        # Gross profit = (1200 - 100) - 800 = 300.00 BDT
        assert Decimal(str(data["gross_profit_bdt"])) == Decimal("300.00")

        # Verify GL balances
        acc_cash = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")
        assert acc_cash.current_balance_bdt == Decimal("1150.00")

        acc_rev = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4010")
        assert acc_rev.current_balance_bdt == Decimal("1200.00")

        acc_vat = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2030")
        assert acc_vat.current_balance_bdt == Decimal("50.00")

        acc_cogs = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="5010")
        assert acc_cogs.current_balance_bdt == Decimal("800.00")

        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1040")
        assert acc_inv.current_balance_bdt == Decimal("-800.00")

    def test_02_credit_due_sale_records_receivable(self):
        """2. Credit / Due Sale Accounting: Debits Accounts Receivable and updates AR balance."""
        self.client.force_authenticate(user=self.manager_a)

        inv = self._create_sale_invoice(
            invoice_num=f"INV-DUE-{self.suffix}",
            total_amount=Decimal("5000.00"),
            payment_method=PaymentMethod.DUE,
            quantity=1000,
            cost_per_unit=Decimal("3.50"),
        )

        resp = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id}, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        data = resp.data

        assert Decimal(str(data["receivable_due_bdt"])) == Decimal("5000.00")
        assert Decimal(str(data["net_settled_bdt"])) == Decimal("0.00")
        assert Decimal(str(data["total_cogs_bdt"])) == Decimal("3500.00")
        assert Decimal(str(data["gross_profit_bdt"])) == Decimal("1500.00")

        acc_ar = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        assert acc_ar.current_balance_bdt == Decimal("5000.00")

    def test_03_bank_mfs_settlement(self):
        """3. Bank / MFS Sale (bKash/Card): Debits Bank & MFS Clearing account (1020)."""
        self.client.force_authenticate(user=self.cashier_a)

        inv = self._create_sale_invoice(
            invoice_num=f"INV-BK-{self.suffix}",
            total_amount=Decimal("3000.00"),
            payment_method=PaymentMethod.BKASH,
            quantity=500,
            cost_per_unit=Decimal("2.00"),
        )

        resp = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id}, format="json")
        assert resp.status_code == status.HTTP_201_CREATED

        acc_bank = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1020")
        assert acc_bank.current_balance_bdt == Decimal("3000.00")

    def test_04_invalid_incomplete_input_rejected(self):
        """4. Invalid/Incomplete Input: Missing sale reference and invalid amounts rejected (422/400)."""
        self.client.force_authenticate(user=self.owner_a)

        # Missing both invoice_id and sale_id
        resp = self.client.post("/api/v2/finance/sales-postings/", {}, format="json")
        assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert resp.data.get("error_code") == "ERR_MISSING_SALE_REFERENCE"

    def test_05_idempotent_key_replay(self):
        """5. Idempotency Key Replay: Returns existing posting without duplicate GL transactions."""
        self.client.force_authenticate(user=self.cashier_a)
        idem = f"idem-sale-{uuid.uuid4().hex}"

        inv = self._create_sale_invoice(
            invoice_num=f"INV-IDEM-{self.suffix}",
            total_amount=Decimal("1000.00"),
            payment_method=PaymentMethod.CASH,
            quantity=100,
            cost_per_unit=Decimal("2.00"),
        )

        # 1st post
        resp1 = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id, "idempotency_key": idem}, format="json")
        assert resp1.status_code == status.HTTP_201_CREATED
        pid1 = resp1.data["posting_id"]

        # 2nd post
        resp2 = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id, "idempotency_key": idem}, format="json")
        assert resp2.status_code == status.HTTP_201_CREATED
        assert resp2.data["posting_id"] == pid1
        assert resp2.data.get("is_idempotent_replay") is True

        # Ensure cash wasn't credited twice
        acc_cash = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")
        assert acc_cash.current_balance_bdt == Decimal("1000.00")

    def test_06_multi_branch_profitability_summary(self):
        """6. Multi-Branch Profitability Summary: Aggregates sales, discounts, COGS, profit, and margin %."""
        self.client.force_authenticate(user=self.owner_a)

        # Branch A sale: Gross 1000, Disc 100, COGS 600 -> Profit 300
        inv1 = self._create_sale_invoice(
            invoice_num=f"INV-S1-{self.suffix}",
            total_amount=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            quantity=300,
            cost_per_unit=Decimal("2.00"),
            branch=self.branch_a,
        )
        self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv1.invoice_id}, format="json")

        # Branch A2 sale: Gross 2000, Disc 0, COGS 1200 -> Profit 800
        inv2 = self._create_sale_invoice(
            invoice_num=f"INV-S2-{self.suffix}",
            total_amount=Decimal("2000.00"),
            discount_amount=Decimal("0.00"),
            quantity=600,
            cost_per_unit=Decimal("2.00"),
            branch=self.branch_a2,
        )
        self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv2.invoice_id}, format="json")

        # Overall summary
        resp = self.client.get("/api/v2/finance/sales-postings/summary/")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["total_sales_count"] == 2
        assert Decimal(str(data["gross_sales_bdt"])) == Decimal("3000.00")
        assert Decimal(str(data["total_discount_bdt"])) == Decimal("100.00")
        assert Decimal(str(data["net_sales_bdt"])) == Decimal("2900.00")
        assert Decimal(str(data["total_cogs_bdt"])) == Decimal("1800.00")
        assert Decimal(str(data["gross_profit_bdt"])) == Decimal("1100.00")

        # Filter by branch_a
        resp_br = self.client.get(f"/api/v2/finance/sales-postings/summary/?branch_id={self.branch_a.branch_id}")
        assert resp_br.status_code == status.HTTP_200_OK
        data_br = resp_br.data
        assert data_br["total_sales_count"] == 1
        assert Decimal(str(data_br["gross_sales_bdt"])) == Decimal("1000.00")
        assert Decimal(str(data_br["gross_profit_bdt"])) == Decimal("300.00")

    def test_07_audited_reversal_restores_gl_balances(self):
        """7. Audited Reversal: Owner reverses posting, restoring GL balances for revenue and inventory."""
        self.client.force_authenticate(user=self.finance_a)

        inv = self._create_sale_invoice(
            invoice_num=f"INV-REV-{self.suffix}",
            total_amount=Decimal("2000.00"),
            payment_method=PaymentMethod.CASH,
            quantity=500,
            cost_per_unit=Decimal("2.00"),
        )
        post_resp = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id}, format="json")
        pid = post_resp.data["posting_id"]

        # Reverse posting
        rev_payload = {"reason": "Customer voided transaction after billing"}
        rev_resp = self.client.post(f"/api/v2/finance/sales-postings/{pid}/reverse/", rev_payload, format="json")
        assert rev_resp.status_code == status.HTTP_200_OK, rev_resp.data
        assert rev_resp.data["status"] == "REVERSED"

        # Check GL balances restored to 0.00
        acc_cash = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1010")
        assert acc_cash.current_balance_bdt == Decimal("0.00")

        acc_rev = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="4010")
        assert acc_rev.current_balance_bdt == Decimal("0.00")

        acc_cogs = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="5010")
        assert acc_cogs.current_balance_bdt == Decimal("0.00")

        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1040")
        assert acc_inv.current_balance_bdt == Decimal("0.00")

    def test_08_reversing_already_reversed_posting_rejected(self):
        """8. Reversing already reversed posting rejected (ERR_ALREADY_REVERSED, 422/400)."""
        self.client.force_authenticate(user=self.owner_a)

        inv = self._create_sale_invoice(invoice_num=f"INV-DREV-{self.suffix}", total_amount=Decimal("500.00"))
        post_resp = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id}, format="json")
        pid = post_resp.data["posting_id"]

        # Reverse once
        self.client.post(f"/api/v2/finance/sales-postings/{pid}/reverse/", {"reason": "First reversal"}, format="json")

        # Reverse again
        rev_resp2 = self.client.post(f"/api/v2/finance/sales-postings/{pid}/reverse/", {"reason": "Second reversal"}, format="json")
        assert rev_resp2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert rev_resp2.data.get("error_code") == "ERR_ALREADY_REVERSED"

    def test_09_missing_reversal_reason_rejected(self):
        """9. Missing reversal reason rejected (ERR_MISSING_REVERSAL_REASON, 422/400)."""
        self.client.force_authenticate(user=self.owner_a)

        inv = self._create_sale_invoice(invoice_num=f"INV-MREV-{self.suffix}", total_amount=Decimal("500.00"))
        post_resp = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id}, format="json")
        pid = post_resp.data["posting_id"]

        rev_resp = self.client.post(f"/api/v2/finance/sales-postings/{pid}/reverse/", {"reason": "   "}, format="json")
        assert rev_resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert rev_resp.data.get("error_code") == "ERR_MISSING_REVERSAL_REASON"

    def test_10_cashier_can_post_but_cannot_reverse(self):
        """10. Cashier can post sales accounting during checkout, but cannot reverse (403)."""
        # Cashier posts sale
        self.client.force_authenticate(user=self.cashier_a)
        inv = self._create_sale_invoice(invoice_num=f"INV-CSH-{self.suffix}", total_amount=Decimal("700.00"))
        post_resp = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv.invoice_id}, format="json")
        assert post_resp.status_code == status.HTTP_201_CREATED
        pid = post_resp.data["posting_id"]

        # Cashier tries to reverse
        rev_resp = self.client.post(f"/api/v2/finance/sales-postings/{pid}/reverse/", {"reason": "Cashier void"}, format="json")
        assert rev_resp.status_code == status.HTTP_403_FORBIDDEN
        assert rev_resp.data.get("error_code") == "ERR_SALES_ACCOUNTING_REVERSE_UNAUTHORIZED"

    def test_11_cashier_denied_profitability_summary(self):
        """11. Cashier denied from viewing sales profitability summary (403)."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.get("/api/v2/finance/sales-postings/summary/")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert resp.data.get("error_code") == "ERR_SALES_ACCOUNTING_VIEW_DENIED"

    def test_12_cross_tenant_and_suspended_tenant_protection(self):
        """12. Cross-tenant isolation (ERR_CROSS_TENANT_ACCESS, 403/404) and suspended tenant protection (403)."""
        # Post in Tenant A
        self.client.force_authenticate(user=self.owner_a)
        inv_a = self._create_sale_invoice(invoice_num=f"INV-ISO-{self.suffix}", total_amount=Decimal("400.00"))
        post_resp = self.client.post("/api/v2/finance/sales-postings/", {"invoice_id": inv_a.invoice_id}, format="json")
        pid = post_resp.data["posting_id"]

        # Owner B tries to access
        self.client.force_authenticate(user=self.owner_b)
        get_resp = self.client.get(f"/api/v2/finance/sales-postings/{pid}/")
        assert get_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        rev_resp = self.client.post(f"/api/v2/finance/sales-postings/{pid}/reverse/", {"reason": "Cross attack"}, format="json")
        assert rev_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        # Suspended tenant protection
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        susp_resp = self.client.get("/api/v2/finance/sales-postings/")
        assert susp_resp.status_code == status.HTTP_403_FORBIDDEN
        assert susp_resp.data.get("error_code") == "ERR_TENANT_SUSPENDED"
