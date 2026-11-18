"""
backend/tests/test_p285_control_account_reconciliation.py
Architecture Decision: ADR-0285 (P285).
Phase 15: General Ledger, Operating Expenses & Profitability.

Integration tests verifying General Ledger Supplier & Customer Control Accounts:
1. Customer AR reconciliation matched (GL 1030 matches customer subledger total, status=MATCHED, variance=0.00).
2. Supplier AP reconciliation matched (GL 2010 matches supplier subledger total, status=MATCHED, variance=0.00).
3. Customer AR discrepancy flagged when GL differs from subledger (status=DISCREPANCY_FLAGGED, is_balanced=False).
4. Supplier AP discrepancy flagged when GL differs from subledger (status=DISCREPANCY_FLAGGED, is_balanced=False).
5. Apply audited balancing adjustment creates balanced journal and updates run status to RECONCILED.
6. Live overview endpoint returns side-by-side AR and AP control accounts with party counts and variance.
7. Invalid control account type rejected (ERR_INVALID_CONTROL_ACCOUNT_TYPE, 400).
8. Applying adjustment to already balanced run rejected (ERR_NO_ADJUSTMENT_NEEDED, 400).
9. Missing adjustment explanation reason rejected (ERR_MISSING_ADJUSTMENT_REASON, 400).
10. Cashier role strictly denied from viewing or reconciling control accounts (ERR_STAFF_ROLE_DENIED, 403).
11. Cross-tenant isolation prevents accessing or adjusting another tenant's reconciliation run (403/404).
12. Suspended tenant protection blocks control account operations (ERR_TENANT_SUSPENDED, 403).
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
    ControlAccountReconciliationRun,
    ControlAccountReconciliationStatus,
    ControlAccountType,
    CustomerReceivableLedgerEntry,
    PosCustomer,
    Supplier,
    SupplierLedgerEntry,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestControlAccountReconciliationIntegration:

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

        # 5. Initialize Default Charts of Accounts
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a)
        ChartOfAccountsService.initialize_default_chart_of_accounts(tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b)

        # 6. Sample Customer & Supplier in Tenant A
        self.customer_a = PosCustomer.objects.create(
            customer_id=f"cust-{self.suffix}",
            tenant=self.tenant_a,
            full_name="Rahim Uddin",
            phone=f"+880181{random.randint(1000000, 9999999)}",
            current_due_bdt=Decimal("5000.00"),
        )
        self.supplier_a = Supplier.objects.create(
            supplier_id=f"sup-{self.suffix}",
            tenant=self.tenant_a,
            name="Incepta Pharmaceuticals Ltd",
            code=f"INC-{self.suffix[:4].upper()}",
            current_balance=Decimal("12500.00"),
        )

    def test_01_customer_ar_reconciliation_matched_success(self):
        # Set GL 1030 to exactly match customer due balance (5000.00 BDT)
        gl_1030 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        gl_1030.current_balance_bdt = Decimal("5000.00")
        gl_1030.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert data["control_account_type"] == "CUSTOMER_AR"
        assert data["gl_account_code"] == "1030"
        assert Decimal(data["gl_control_balance_bdt"]) == Decimal("5000.00")
        assert Decimal(data["subledger_total_balance_bdt"]) == Decimal("5000.00")
        assert Decimal(data["variance_bdt"]) == Decimal("0.00")
        assert data["is_balanced"] is True
        assert data["status"] == ControlAccountReconciliationStatus.MATCHED
        assert data["party_count"] >= 1

    def test_02_supplier_ap_reconciliation_matched_success(self):
        # Set GL 2010 to exactly match supplier payable balance (12500.00 BDT)
        gl_2010 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        gl_2010.current_balance_bdt = Decimal("12500.00")
        gl_2010.save()

        self.client.force_authenticate(user=self.finance_a)
        response = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "SUPPLIER_AP"},
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert data["control_account_type"] == "SUPPLIER_AP"
        assert data["gl_account_code"] == "2010"
        assert Decimal(data["gl_control_balance_bdt"]) == Decimal("12500.00")
        assert Decimal(data["subledger_total_balance_bdt"]) == Decimal("12500.00")
        assert Decimal(data["variance_bdt"]) == Decimal("0.00")
        assert data["is_balanced"] is True
        assert data["status"] == ControlAccountReconciliationStatus.MATCHED

    def test_03_customer_ar_discrepancy_flagged(self):
        # GL 1030 is 8000.00 while customer due is 5000.00 -> variance 3000.00
        gl_1030 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        gl_1030.current_balance_bdt = Decimal("8000.00")
        gl_1030.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert Decimal(data["variance_bdt"]) == Decimal("3000.00")
        assert data["is_balanced"] is False
        assert data["status"] == ControlAccountReconciliationStatus.DISCREPANCY_FLAGGED

    def test_04_supplier_ap_discrepancy_flagged(self):
        # GL 2010 is 16000.00 while supplier payable is 12500.00 -> variance 3500.00
        gl_2010 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        gl_2010.current_balance_bdt = Decimal("16000.00")
        gl_2010.save()

        self.client.force_authenticate(user=self.finance_a)
        response = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "SUPPLIER_AP"},
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data
        assert Decimal(data["variance_bdt"]) == Decimal("3500.00")
        assert data["is_balanced"] is False
        assert data["status"] == ControlAccountReconciliationStatus.DISCREPANCY_FLAGGED

    def test_05_apply_balancing_adjustment_success(self):
        # 1. Create a flagged reconciliation run with 3000.00 variance
        gl_1030 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        gl_1030.current_balance_bdt = Decimal("8000.00")
        gl_1030.save()

        self.client.force_authenticate(user=self.owner_a)
        recon_res = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        run_id = recon_res.data["run_id"]
        assert recon_res.data["status"] == ControlAccountReconciliationStatus.DISCREPANCY_FLAGGED

        # 2. Apply audited balancing adjustment
        adjust_res = self.client.post(
            f"/api/v2/finance/control-accounts/{run_id}/adjust/",
            {"reason": "Audited year-end AR discrepancy write-down", "contra_account_code": "3020"},
            format="json",
        )
        assert adjust_res.status_code == status.HTTP_200_OK, adjust_res.data
        adj_data = adjust_res.data
        assert adj_data["status"] == ControlAccountReconciliationStatus.RECONCILED
        assert adj_data["is_balanced"] is True
        assert Decimal(adj_data["variance_bdt"]) == Decimal("0.00")
        assert adj_data["adjustment_journal_number"] is not None
        assert "year-end AR discrepancy" in adj_data["reconciliation_notes"]

        # 3. Check GL account was updated
        gl_1030.refresh_from_db()
        assert gl_1030.current_balance_bdt == Decimal("5000.00")

    def test_06_live_overview_endpoint(self):
        gl_1030 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        gl_1030.current_balance_bdt = Decimal("5000.00")
        gl_1030.save()

        gl_2010 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")
        gl_2010.current_balance_bdt = Decimal("12500.00")
        gl_2010.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get("/api/v2/finance/control-accounts/overview/")
        assert response.status_code == status.HTTP_200_OK, response.data
        data = response.data
        assert data["overall_status"] == "BALANCED"
        assert data["is_fully_reconciled"] is True
        assert data["accounts_receivable"]["account_code"] == "1030"
        assert data["accounts_payable"]["account_code"] == "2010"

    def test_07_invalid_control_account_type_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "FIXED_ASSET"},
            format="json",
        )
        assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY), response.data
        assert response.data["error_code"] == "ERR_INVALID_CONTROL_ACCOUNT_TYPE"

    def test_08_apply_adjustment_to_balanced_run_rejected(self):
        gl_1030 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        gl_1030.current_balance_bdt = Decimal("5000.00")
        gl_1030.save()

        self.client.force_authenticate(user=self.owner_a)
        recon_res = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        run_id = recon_res.data["run_id"]
        assert recon_res.data["is_balanced"] is True

        adjust_res = self.client.post(
            f"/api/v2/finance/control-accounts/{run_id}/adjust/",
            {"reason": "Attempting unneeded adjustment"},
            format="json",
        )
        assert adjust_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY), adjust_res.data
        assert adjust_res.data["error_code"] == "ERR_NO_ADJUSTMENT_NEEDED"

    def test_09_missing_adjustment_reason_rejected(self):
        gl_1030 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        gl_1030.current_balance_bdt = Decimal("9000.00")
        gl_1030.save()

        self.client.force_authenticate(user=self.owner_a)
        recon_res = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        run_id = recon_res.data["run_id"]

        adjust_res = self.client.post(
            f"/api/v2/finance/control-accounts/{run_id}/adjust/",
            {"reason": "   "},
            format="json",
        )
        assert adjust_res.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY), adjust_res.data
        assert adjust_res.data["error_code"] == "ERR_MISSING_ADJUSTMENT_REASON"

    def test_10_cashier_role_strictly_denied(self):
        self.client.force_authenticate(user=self.cashier_a)
        res_overview = self.client.get("/api/v2/finance/control-accounts/overview/")
        assert res_overview.status_code == status.HTTP_403_FORBIDDEN
        assert res_overview.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

        res_recon = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        assert res_recon.status_code == status.HTTP_403_FORBIDDEN
        assert res_recon.data["error_code"] == "ERR_STAFF_ROLE_DENIED"

    def test_11_cross_tenant_isolation_enforced(self):
        gl_1030 = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        gl_1030.current_balance_bdt = Decimal("5000.00")
        gl_1030.save()

        self.client.force_authenticate(user=self.owner_a)
        recon_res = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        run_id = recon_res.data["run_id"]

        # Tenant B owner attempts to access Tenant A run
        self.client.force_authenticate(user=self.owner_b)
        get_res = self.client.get(f"/api/v2/finance/control-accounts/{run_id}/")
        assert get_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        adjust_res = self.client.post(
            f"/api/v2/finance/control-accounts/{run_id}/adjust/",
            {"reason": "Malicious foreign adjustment"},
            format="json",
        )
        assert adjust_res.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_blocked(self):
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        recon_res = self.client.post(
            "/api/v2/finance/control-accounts/reconcile/",
            {"control_account_type": "CUSTOMER_AR"},
            format="json",
        )
        assert recon_res.status_code == status.HTTP_403_FORBIDDEN
        data = recon_res.json() if hasattr(recon_res, "json") else getattr(recon_res, "data", {})
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(recon_res.content).lower()
