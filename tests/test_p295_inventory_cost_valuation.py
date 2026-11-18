"""
backend/tests/test_p295_inventory_cost_valuation.py
Architecture Decision: ADR-0295 (P295).
Phase 15: General Ledger, Operating Expenses & Profitability.

Comprehensive targeted integration tests for Inventory Cost Valuation & GL Reconciliation:
1. Owner generates balanced inventory valuation report when physical lots match GL 1030.
2. Discrepancy between GL and physical lots detects VARIANCE_DETECTED.
3. Owner posts audited valuation write-down adjustment (shortage/shrinkage).
4. Owner posts audited valuation write-up adjustment (overage/surplus).
5. Branch Manager is blocked from posting valuation adjustments (403 Forbidden).
6. Cashier is strictly denied access to inventory cost valuation (403 Forbidden).
7. Staff Pharmacist is strictly denied access (403 Forbidden).
8. Branch Manager is restricted to assigned branch (403 on other branches).
9. Invalid adjustment input payload is rejected (missing reason, invalid amount, bad type).
10. CSV export endpoint delivers proper headers and tabular rows.
11. Cross-tenant branch access is forbidden (403/404).
12. Suspended tenant subscription blocks inventory valuation (403 Forbidden).
13. Unauthenticated requests are rejected.
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
    BatchStatus,
    BatchStock,
    Branch,
    ChartOfAccount,
    GLAccountCategory,
    InventoryValuationAdjustmentLog,
    InventoryValuationAdjustmentType,
    JournalEntry,
    JournalEntryLine,
    JournalEntryStatus,
    JournalSourceModule,
    MedicineProduct,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestInventoryCostValuationIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Tenants & Branches
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Square Pharma {self.suffix}",
            subdomain=f"sq-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name="Dhanmondi Branch",
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}",
            tenant=self.tenant_a,
            name="Gulshan Branch",
        )

        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"pop-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-b1-{self.suffix}",
            tenant=self.tenant_b,
            name="Uttara Branch",
        )

        # 2. Users for Tenant A
        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Pharmacy Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.finance_officer_a = PharmacyUser.objects.create_user(
            email=f"fin_{self.suffix}@pharma.com",
            phone_number=f"+880172{random.randint(1000000, 9999999)}",
            full_name="Finance Officer A",
            password="StrongPassword123!",
            role=UserRole.FINANCE_OFFICER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a1 = PharmacyUser.objects.create_user(
            email=f"bm1_{self.suffix}@pharma.com",
            phone_number=f"+880173{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A1",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a2 = PharmacyUser.objects.create_user(
            email=f"bm2_{self.suffix}@pharma.com",
            phone_number=f"+880174{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A2",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
        )
        self.cashier_a = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@pharma.com",
            phone_number=f"+880175{random.randint(1000000, 9999999)}",
            full_name="POS Cashier A",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.staff_pharm_a = PharmacyUser.objects.create_user(
            email=f"pharm_{self.suffix}@pharma.com",
            phone_number=f"+880176{random.randint(1000000, 9999999)}",
            full_name="Staff Pharmacist A",
            password="StrongPassword123!",
            role=UserRole.STAFF_PHARMACIST,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )

        # 3. Foreign User for Tenant B
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"owner_b_{self.suffix}@pharma.com",
            phone_number=f"+880177{random.randint(1000000, 9999999)}",
            full_name="Foreign Owner B",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b1.branch_id,
        )

        # 4. Initialize Chart of Accounts for Tenant A & B
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_a.tenant_id, actor_user=self.owner_a
        )
        ChartOfAccountsService.initialize_default_chart_of_accounts(
            tenant_id=self.tenant_b.tenant_id, actor_user=self.owner_b
        )

        # 5. Create Sample Products
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"med-napa-{self.suffix}",
            brand_name="Napa Extra 500mg",
            generic_name="Paracetamol + Caffeine",
            dosage_form="TABLET",
            cost_unit=Decimal("2.50"),
            mrp_unit=Decimal("3.50"),
        )
        self.product_seclo = MedicineProduct.objects.create(
            product_id=f"med-seclo-{self.suffix}",
            brand_name="Seclo 20mg Capsule",
            generic_name="Omeprazole",
            dosage_form="CAPSULE",
            cost_unit=Decimal("6.00"),
            mrp_unit=Decimal("8.00"),
        )

    def _setup_balanced_inventory(self):
        """Sets up physical batches and matching GL 1030 inventory postings."""
        today = timezone.now().date()
        next_year = today + timezone.timedelta(days=365)
        past_date = today - timezone.timedelta(days=30)

        # Physical Batches:
        # Batch 1 (A1): 1,000 units Napa @ 2.50 = 2,500.00 (Active)
        self.batch_a1_1 = BatchStock.objects.create(
            batch_id=f"bat-a1-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.product_napa,
            batch_number="BCH-NAPA-001",
            quantity_base_units=1000,
            cost_per_unit=Decimal("2.50"),
            purchase_date=today,
            expiry_date=next_year,
            status=BatchStatus.ACTIVE,
        )

        # Batch 2 (A1): 500 units Seclo @ 6.00 = 3,000.00 (Active)
        self.batch_a1_2 = BatchStock.objects.create(
            batch_id=f"bat-a1-2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.product_seclo,
            batch_number="BCH-SECLO-001",
            quantity_base_units=500,
            cost_per_unit=Decimal("6.00"),
            purchase_date=today,
            expiry_date=next_year,
            status=BatchStatus.ACTIVE,
        )

        # Batch 3 (A1): 100 units Napa @ 2.50 = 250.00 (Quarantined)
        self.batch_a1_3 = BatchStock.objects.create(
            batch_id=f"bat-a1-3-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.product_napa,
            batch_number="BCH-NAPA-002",
            quantity_base_units=100,
            cost_per_unit=Decimal("2.50"),
            purchase_date=today,
            expiry_date=next_year,
            status=BatchStatus.QUARANTINED,
            is_quarantined=True,
        )

        # Batch 4 (A1 Expired): 50 units Napa @ 2.50 = 125.00 (Expired / Loss disposition)
        self.batch_a1_4 = BatchStock.objects.create(
            batch_id=f"bat-a1-4-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a1,
            product=self.product_napa,
            batch_number="BCH-NAPA-EXP",
            quantity_base_units=50,
            cost_per_unit=Decimal("2.50"),
            purchase_date=past_date,
            expiry_date=past_date,
            status=BatchStatus.EXPIRED,
        )

        # Batch 5 (A2): 400 units Napa @ 2.50 = 1,000.00 (Active)
        self.batch_a2_1 = BatchStock.objects.create(
            batch_id=f"bat-a2-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a2,
            product=self.product_napa,
            batch_number="BCH-NAPA-GUL",
            quantity_base_units=400,
            cost_per_unit=Decimal("2.50"),
            purchase_date=today,
            expiry_date=next_year,
            status=BatchStatus.ACTIVE,
        )

        # Total on-hand physical valuation (Active + Quarantined):
        # Branch A1 = 2,500 + 3,000 + 250 = 5,750.00
        # Branch A2 = 1,000.00
        # Total Consolidated = 6,750.00

        # Post matching GL entries:
        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        acc_ap = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="2010")

        # Post GL entry for Branch A1: Dr 1030 (5,750.00), Cr 2010 (5,750.00)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=today,
            source_module=JournalSourceModule.PROCUREMENT,
            source_reference=f"grn-a1-{self.suffix}",
            narration="Procurement GRN Inventory Receipt A1",
            lines=[
                {"account_id": str(acc_inv.account_id), "debit_amount_bdt": Decimal("5750.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_ap.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("5750.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        # Post GL entry for Branch A2: Dr 1030 (1,000.00), Cr 2010 (1,000.00)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a2.branch_id,
            posting_date=today,
            source_module=JournalSourceModule.PROCUREMENT,
            source_reference=f"grn-a2-{self.suffix}",
            narration="Procurement GRN Inventory Receipt A2",
            lines=[
                {"account_id": str(acc_inv.account_id), "debit_amount_bdt": Decimal("1000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_ap.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("1000.00")},
            ],
            actor_user=self.finance_officer_a,
        )

    # =========================================================================
    # TESTS
    # =========================================================================

    def test_01_owner_generates_balanced_inventory_valuation_report(self):
        """Owner generates consolidated valuation report and proves BALANCED status."""
        self._setup_balanced_inventory()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        data = response.data

        # 1. Physical inventory valuation
        phys = data["physical_inventory"]
        assert phys["total_on_hand_units"] == 2000 # 1000 + 500 + 100 (A1) + 400 (A2)
        assert Decimal(phys["sellable_value_bdt"]) == Decimal("6500.00")
        assert Decimal(phys["quarantined_value_bdt"]) == Decimal("250.00")
        assert Decimal(phys["total_physical_valuation_bdt"]) == Decimal("6750.00")

        # 2. Loss disposition
        loss = data["loss_disposition"]
        assert loss["expired_units"] == 50
        assert Decimal(loss["expired_loss_value_bdt"]) == Decimal("125.00")

        # 3. GL 1030 Merchandise Inventory
        gl = data["accounting_gl"]
        assert Decimal(gl["gl_book_value_bdt"]) == Decimal("6750.00")

        # 4. Reconciliation
        rec = data["reconciliation"]
        assert Decimal(rec["valuation_variance_bdt"]) == Decimal("0.00")
        assert rec["is_balanced"] is True
        assert rec["status"] == "BALANCED"

    def test_02_variance_detected_when_gl_differs_from_physical_lots(self):
        """Variance is detected when GL book balance differs from physical lot valuation."""
        self._setup_balanced_inventory()

        acc_inv = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="1030")
        acc_loss = ChartOfAccount.objects.get(tenant=self.tenant_a, account_code="5020")

        # Post an unadjusted stock adjustment in GL of BDT 500 without updating physical batches
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=timezone.now().date(),
            source_module=JournalSourceModule.STOCK_ADJUSTMENT,
            source_reference=f"unreconciled-{self.suffix}",
            narration="Manual Ledger Drift Entry",
            lines=[
                {"account_id": str(acc_loss.account_id), "debit_amount_bdt": Decimal("500.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_id": str(acc_inv.account_id), "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("500.00")},
            ],
            actor_user=self.finance_officer_a,
        )

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_200_OK
        rec = response.data["reconciliation"]

        # GL is now 6,250.00 vs Physical 6,750.00 -> Variance = -500.00
        assert Decimal(rec["valuation_variance_bdt"]) == Decimal("-500.00")
        assert rec["is_balanced"] is False
        assert rec["status"] == "VARIANCE_DETECTED"

    def test_03_owner_posts_audited_valuation_write_down_adjustment(self):
        """Owner posts audited WRITE_DOWN adjustment, updating GL 1030 with audit log."""
        self._setup_balanced_inventory()

        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a1.branch_id,
            "adjustment_type": "WRITE_DOWN",
            "amount_bdt": "250.00",
            "reason": "Damaged bottle physical write-down",
            "audit_notes": "Cycle count audit physically verified by inventory supervisor",
        }

        response = self.client.post("/api/v2/finance/inventory-valuation/adjust/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        data = response.data

        assert data["adjustment_type"] == "WRITE_DOWN"
        assert Decimal(data["amount_bdt"]) == Decimal("250.00")
        assert data["status"] == "ADJUSTMENT_POSTED"
        assert data["adjustment_id"].startswith("ivadj-")

        # Verify audit log in database
        log_entry = InventoryValuationAdjustmentLog.objects.get(adjustment_id=data["adjustment_id"])
        assert log_entry.amount_bdt == Decimal("250.00")
        assert log_entry.performed_by == self.owner_a
        assert log_entry.journal_entry is not None

        # Verify journal entry was posted with Dr 5020 / Cr 1030
        j_lines = log_entry.journal_entry.lines.all()
        assert len(j_lines) == 2
        line_inv = next(l for l in j_lines if l.account.account_code == "1030")
        line_loss = next(l for l in j_lines if l.account.account_code == "5020")
        assert line_inv.credit_amount_bdt == Decimal("250.00")
        assert line_loss.debit_amount_bdt == Decimal("250.00")

    def test_04_owner_posts_audited_valuation_write_up_adjustment(self):
        """Owner posts audited WRITE_UP adjustment for found surplus stock."""
        self._setup_balanced_inventory()

        self.client.force_authenticate(user=self.owner_a)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a1.branch_id,
            "adjustment_type": "WRITE_UP",
            "amount_bdt": "150.00",
            "reason": "Surplus carton found during DGDA inspection",
            "audit_notes": "Added to register",
        }

        response = self.client.post("/api/v2/finance/inventory-valuation/adjust/", payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        data = response.data

        assert data["adjustment_type"] == "WRITE_UP"
        log_entry = InventoryValuationAdjustmentLog.objects.get(adjustment_id=data["adjustment_id"])
        line_inv = next(l for l in log_entry.journal_entry.lines.all() if l.account.account_code == "1030")
        assert line_inv.debit_amount_bdt == Decimal("150.00")

    def test_05_branch_manager_blocked_from_adjustments(self):
        """Branch Manager alone is blocked from posting valuation adjustments (403 Forbidden)."""
        self.client.force_authenticate(user=self.bm_a1)
        payload = {
            "tenant_id": self.tenant_a.tenant_id,
            "branch_id": self.branch_a1.branch_id,
            "adjustment_type": "WRITE_DOWN",
            "amount_bdt": "100.00",
            "reason": "Unauthorized adjustment",
        }

        response = self.client.post("/api/v2/finance/inventory-valuation/adjust/", payload, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_VALUATION_ADJUST_FORBIDDEN"

    def test_06_cashier_denied_access_to_valuation(self):
        """POS cashier is strictly blocked from accessing inventory valuation."""
        self.client.force_authenticate(user=self.cashier_a)
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_INVENTORY_VALUATION_UNAUTHORIZED"

    def test_07_staff_pharmacist_denied_access(self):
        """Staff pharmacist is strictly blocked from accessing inventory valuation."""
        self.client.force_authenticate(user=self.staff_pharm_a)
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_INVENTORY_VALUATION_UNAUTHORIZED"

    def test_08_branch_manager_restricted_to_assigned_branch(self):
        """Branch Manager can access own branch valuation, but is forbidden from other branches."""
        self._setup_balanced_inventory()

        self.client.force_authenticate(user=self.bm_a1)

        # 1. Own branch -> 200 OK
        resp_own = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a1.branch_id}"
        )
        assert resp_own.status_code == status.HTTP_200_OK
        assert resp_own.data["branch_id"] == str(self.branch_a1.branch_id)

        # 2. Other branch -> 403 Forbidden
        resp_other = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a2.branch_id}"
        )
        assert resp_other.status_code == status.HTTP_403_FORBIDDEN
        assert resp_other.data.get("error_code") == "ERR_BRANCH_ACCESS_DENIED"

    def test_09_invalid_adjustment_payload_validation(self):
        """Reject invalid adjustment input payload."""
        self.client.force_authenticate(user=self.owner_a)

        # 1. Missing reason
        res_reason = self.client.post(
            "/api/v2/finance/inventory-valuation/adjust/",
            {"tenant_id": self.tenant_a.tenant_id, "adjustment_type": "WRITE_DOWN", "amount_bdt": "100.00", "reason": ""},
            format="json",
        )
        assert res_reason.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res_reason.data.get("error_code") == "ERR_MISSING_REASON"

        # 2. Non-positive amount
        res_amt = self.client.post(
            "/api/v2/finance/inventory-valuation/adjust/",
            {"tenant_id": self.tenant_a.tenant_id, "adjustment_type": "WRITE_DOWN", "amount_bdt": "-50.00", "reason": "Bad amount"},
            format="json",
        )
        assert res_amt.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res_amt.data.get("error_code") == "ERR_INVALID_AMOUNT"

        # 3. Invalid adjustment_type
        res_type = self.client.post(
            "/api/v2/finance/inventory-valuation/adjust/",
            {"tenant_id": self.tenant_a.tenant_id, "adjustment_type": "INVALID_TYPE", "amount_bdt": "100.00", "reason": "Test"},
            format="json",
        )
        assert res_type.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert res_type.data.get("error_code") == "ERR_INVALID_ADJUSTMENT_TYPE"

    def test_10_csv_export_endpoint(self):
        """Test CSV export endpoint delivers proper headers and CSV formatting."""
        self._setup_balanced_inventory()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/export/?tenant_id={self.tenant_a.tenant_id}&format=csv"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response["Content-Type"].startswith("text/csv")
        assert "attachment; filename=" in response["Content-Disposition"]

        content = response.content.decode("utf-8")
        assert "INVENTORY COST VALUATION & GL RECONCILIATION" in content
        assert "PHYSICAL_INVENTORY,Total On-Hand Units,2000" in content
        assert "PHYSICAL_INVENTORY,Total Physical Valuation BDT,6750.00" in content
        assert "ACCOUNTING_GL,GL 1030 Merchandise Inventory Book Value BDT,6750.00" in content
        assert "RECONCILIATION,Valuation Variance BDT,0.00" in content
        assert "RECONCILIATION,Reconciliation Status,BALANCED" in content

    def test_11_cross_tenant_branch_isolation(self):
        """User from Tenant A cannot query Branch belonging to Tenant B."""
        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_b1.branch_id}"
        )
        assert response.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    def test_12_suspended_tenant_subscription_blocks_valuation(self):
        """Suspended tenant subscription blocks inventory valuation."""
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()

        self.client.force_authenticate(user=self.owner_a)
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data.get("error_code") == "ERR_TENANT_SUSPENDED"

    def test_13_unauthenticated_request_rejected(self):
        """Unauthenticated requests are rejected."""
        response = self.client.get(
            f"/api/v2/finance/inventory-valuation/?tenant_id={self.tenant_a.tenant_id}"
        )
        assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
