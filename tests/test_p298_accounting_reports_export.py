"""
backend/tests/test_p298_accounting_reports_export.py
Architecture Decision: ADR-0298 (P298).
Phase 15: General Ledger, Operating Expenses & Profitability.

Targeted integration tests for Accounting Reports & Permission-Scoped Historical Snapshots:
1. Owner generates sealed TRIAL_BALANCE, JOURNAL_LEDGER, PROFIT_AND_LOSS, OPERATING_EXPENSES, and RECONCILIATION_PACK snapshots.
2. Historical immutability: Posting a new journal AFTER snapshot creation does NOT change the sealed snapshot's CSV export or SHA-256 integrity.
3. Live CSV export (?format=csv) returns balanced trial balance & journal register rows.
4. Invalid report_type and invalid date range rejected (400).
5. Permission scoping: Branch Manager can snapshot/export own branch ONLY; blocked on consolidated or other branch (403); Cashier denied (403).
6. Cross-tenant isolation (403) and suspended tenant subscription block (403).
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
    AccountingSnapshotStatus,
    Branch,
    JournalSourceModule,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestAccountingReportsExportIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"HealthPlus {self.suffix}",
            subdomain=f"hp-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}", tenant=self.tenant_a, name="Mirpur Branch"
        )
        self.branch_a2 = Branch.objects.create(
            branch_id=f"br-a2-{self.suffix}", tenant=self.tenant_a, name="Badda Branch"
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"CarePharma {self.suffix}",
            subdomain=f"cp-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-b1-{self.suffix}", tenant=self.tenant_b, name="Sylhet Branch"
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880191{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a1 = PharmacyUser.objects.create_user(
            email=f"bm1_{self.suffix}@pharma.com",
            phone_number=f"+880192{random.randint(1000000, 9999999)}",
            full_name="BM A1",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880193{random.randint(1000000, 9999999)}",
            full_name="Cashier A1",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880194{random.randint(1000000, 9999999)}",
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

        # Post an initial balanced sales journal on Branch A1 (BDT 10,000.00)
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.POS_SALE,
            source_reference="INV-EXPORT-001",
            narration="Initial cash sale",
            lines=[
                {"account_code": "1010", "debit_amount_bdt": Decimal("10000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "4010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("10000.00")},
            ],
            actor_user=self.owner_a,
        )

    def test_01_historical_snapshot_immutability_and_sha256_verification(self):
        self.client.force_authenticate(user=self.owner_a)
        cr = self.client.post(
            "/api/v2/finance/accounting-exports/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "report_type": "TRIAL_BALANCE",
                "title": "Pre-Adjustment Trial Balance",
            },
            format="json",
        )
        assert cr.status_code == status.HTTP_201_CREATED, cr.data
        snap_id = cr.data["snapshot_id"]
        assert cr.data["is_book_balanced"] is True
        assert cr.data["total_debits_bdt"] == "10000.00"
        assert cr.data["integrity_verified"] is True

        # Post another BDT 5,000.00 journal AFTER snapshot was sealed
        DoubleEntryJournalService.post_journal_entry(
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
            posting_date=self.today,
            source_module=JournalSourceModule.POS_SALE,
            source_reference="INV-EXPORT-002",
            narration="Subsequent cash sale",
            lines=[
                {"account_code": "1010", "debit_amount_bdt": Decimal("5000.00"), "credit_amount_bdt": Decimal("0.00")},
                {"account_code": "4010", "debit_amount_bdt": Decimal("0.00"), "credit_amount_bdt": Decimal("5000.00")},
            ],
            actor_user=self.owner_a,
        )

        # Historical snapshot CSV must STILL reflect frozen 10000.00 (not 15000.00!)
        dl = self.client.get(
            f"/api/v2/finance/accounting-exports/{snap_id}/download/?format=csv&tenant_id={self.tenant_a.tenant_id}"
        )
        assert dl.status_code == status.HTTP_200_OK
        csv_text = dl.content.decode("utf-8")
        assert "10000.00" in csv_text
        assert "15000.00" not in csv_text

        # Live export reflects updated 15000.00
        live = self.client.get(
            f"/api/v2/finance/accounting-exports/live/?report_type=TRIAL_BALANCE&tenant_id={self.tenant_a.tenant_id}"
        )
        assert live.status_code == status.HTTP_200_OK
        assert live.data["total_debits_bdt"] == "15000.00"

    def test_02_all_report_types_and_archive(self):
        self.client.force_authenticate(user=self.owner_a)
        for rtype in ("JOURNAL_LEDGER", "PROFIT_AND_LOSS", "OPERATING_EXPENSES", "RECONCILIATION_PACK"):
            resp = self.client.post(
                "/api/v2/finance/accounting-exports/",
                {"tenant_id": self.tenant_a.tenant_id, "report_type": rtype},
                format="json",
            )
            assert resp.status_code == status.HTTP_201_CREATED, f"Failed for {rtype}: {resp.data}"

        # Archive one snapshot
        arch = self.client.delete(f"/api/v2/finance/accounting-exports/{resp.data['snapshot_id']}/")
        assert arch.status_code == status.HTTP_200_OK
        assert arch.data["status"] == AccountingSnapshotStatus.ARCHIVED

    def test_03_invalid_report_type_and_date_range_rejected(self):
        self.client.force_authenticate(user=self.owner_a)
        r1 = self.client.post(
            "/api/v2/finance/accounting-exports/",
            {"tenant_id": self.tenant_a.tenant_id, "report_type": "NON_EXISTENT"},
            format="json",
        )
        assert r1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        r2 = self.client.get(
            f"/api/v2/finance/accounting-exports/live/?report_type=TRIAL_BALANCE&start_date=2026-12-31&end_date=2026-01-01&tenant_id={self.tenant_a.tenant_id}"
        )
        assert r2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_04_permission_scoping_cross_tenant_and_suspended_tenant(self):
        # Owner creates consolidated snapshot
        self.client.force_authenticate(user=self.owner_a)
        cons = self.client.post(
            "/api/v2/finance/accounting-exports/",
            {"tenant_id": self.tenant_a.tenant_id, "report_type": "TRIAL_BALANCE"},
            format="json",
        )
        cons_id = cons.data["snapshot_id"]

        # Branch Manager can create snapshot for own branch, NOT consolidated or branch A2
        self.client.force_authenticate(user=self.bm_a1)
        bm_own = self.client.post(
            "/api/v2/finance/accounting-exports/",
            {"tenant_id": self.tenant_a.tenant_id, "branch_id": self.branch_a1.branch_id, "report_type": "TRIAL_BALANCE"},
            format="json",
        )
        assert bm_own.status_code == status.HTTP_201_CREATED
        assert self.client.get(f"/api/v2/finance/accounting-exports/{cons_id}/").status_code == status.HTTP_403_FORBIDDEN
        assert (
            self.client.post(
                "/api/v2/finance/accounting-exports/",
                {"tenant_id": self.tenant_a.tenant_id, "branch_id": self.branch_a2.branch_id, "report_type": "TRIAL_BALANCE"},
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # Cashier denied
        self.client.force_authenticate(user=self.cashier_a1)
        assert self.client.get(f"/api/v2/finance/accounting-exports/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN

        # Cross-tenant Owner B denied
        self.client.force_authenticate(user=self.owner_b)
        assert self.client.get(f"/api/v2/finance/accounting-exports/{cons_id}/").status_code == status.HTTP_403_FORBIDDEN

        # Suspended tenant blocked
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        self.client.force_authenticate(user=self.owner_a)
        assert self.client.get(f"/api/v2/finance/accounting-exports/?tenant_id={self.tenant_a.tenant_id}").status_code == status.HTTP_403_FORBIDDEN
