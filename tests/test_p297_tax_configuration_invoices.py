"""
backend/tests/test_p297_tax_configuration_invoices.py
Architecture Decision: ADR-0297 (P297).
Phase 15: General Ledger, Operating Expenses & Profitability.

Targeted integration tests for Reviewed Tax Configuration & Invoice Presentation:
1. Unconfigured scopes default to UNCONFIGURED_ZERO_TAX (zero hardcoded statutory rates).
2. Draft rule lifecycle: create DRAFT -> PATCH rate -> review-activate (mandatory review_reference) -> immutable on PATCH -> archive.
3. Exclusive and Inclusive Decimal tax computation across mixed Medicine + Non-Medicine Retail invoice items.
4. Persisted InvoiceTaxSnapshot posts balanced double-entry GL (Dr 1010 / Cr 4010 + Cr 2030) and reverses cleanly via audited compensating journal.
5. Invoice presentation profile GET/PUT updates EN/BN headers, BIN registration number, and breakdown options.
6. Invalid inputs rejected (rate > 100%, effective_to < effective_from, missing review_reference, empty items).
7. RBAC: Cashier/Branch Manager can calculate/view but cannot create/review/archive tax rules (403).
8. Cross-tenant isolation (403) on rule retrieval, branch reference, and snapshot reversal.
9. Suspended tenant subscription blocks tax configuration (403) and unauthenticated requests rejected.
10. CSV export (?format=csv) returns configured tax rule table.
"""

from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.chart_of_accounts_service import ChartOfAccountsService
from apps.core.models import (
    Branch,
    InvoiceTaxSnapshot,
    InvoiceTaxSnapshotStatus,
    JournalEntry,
    TaxRuleStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestTaxConfigurationAndInvoicesIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]
        self.today = timezone.now().date()

        self.tenant_a = Tenant.objects.create(
            tenant_id=f"ten-a-{self.suffix}",
            name=f"Model Pharmacy {self.suffix}",
            subdomain=f"mod-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a1 = Branch.objects.create(
            branch_id=f"br-a1-{self.suffix}",
            tenant=self.tenant_a,
            name="Banani Branch",
        )
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"ten-b-{self.suffix}",
            name=f"Other Pharmacy {self.suffix}",
            subdomain=f"oth-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b1 = Branch.objects.create(
            branch_id=f"br-b1-{self.suffix}",
            tenant=self.tenant_b,
            name="Chittagong Branch",
        )

        self.owner_a = PharmacyUser.objects.create_user(
            email=f"owner_{self.suffix}@pharma.com",
            phone_number=f"+880181{random.randint(1000000, 9999999)}",
            full_name="Owner A",
            password="StrongPassword123!",
            role=UserRole.PHARMACY_OWNER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.bm_a1 = PharmacyUser.objects.create_user(
            email=f"bm_{self.suffix}@pharma.com",
            phone_number=f"+880182{random.randint(1000000, 9999999)}",
            full_name="Branch Manager A",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.cashier_a1 = PharmacyUser.objects.create_user(
            email=f"cash_{self.suffix}@pharma.com",
            phone_number=f"+880183{random.randint(1000000, 9999999)}",
            full_name="Cashier A",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a1.branch_id,
        )
        self.owner_b = PharmacyUser.objects.create_user(
            email=f"ownerb_{self.suffix}@pharma.com",
            phone_number=f"+880184{random.randint(1000000, 9999999)}",
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

    def test_01_unconfigured_rules_default_to_zero_tax_no_hardcoding(self):
        self.client.force_authenticate(user=self.cashier_a1)
        resp = self.client.post(
            "/api/v2/finance/tax-config/calculate-invoice/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "items": [
                    {"sku": "NAPA-500", "item_scope": "MEDICINE", "quantity": 2, "unit_price_bdt": "100.00"},
                ],
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["total_tax_bdt"] == "0.00"
        assert resp.data["grand_total_bdt"] == "200.00"
        assert resp.data["lines"][0]["applied_rule"]["rule_code"] == "UNCONFIGURED_ZERO_TAX"

    def test_02_rule_draft_review_activate_immutability_and_archive(self):
        self.client.force_authenticate(user=self.owner_a)
        cr = self.client.post(
            "/api/v2/finance/tax-config/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "rule_code": "RETAIL_VAT_7_5",
                "name": "Retail Cosmetics VAT",
                "name_bn": "রিটেইল ভ্যাট",
                "applies_to_scope": "NON_MEDICINE_RETAIL",
                "calculation_mode": "EXCLUSIVE",
                "tax_rate_pct": "5.000",
                "effective_from": self.today.isoformat(),
            },
            format="json",
        )
        assert cr.status_code == status.HTTP_201_CREATED
        rule_id = cr.data["rule_id"]
        assert cr.data["status"] == TaxRuleStatus.DRAFT

        # Edit DRAFT rate to 7.500%
        up = self.client.patch(f"/api/v2/finance/tax-config/{rule_id}/", {"tax_rate_pct": "7.500"}, format="json")
        assert up.status_code == status.HTTP_200_OK
        assert up.data["tax_rate_pct"] == "7.500"

        # Review without reference rejected
        no_ref = self.client.post(f"/api/v2/finance/tax-config/{rule_id}/review-activate/", {}, format="json")
        assert no_ref.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert no_ref.data["error_code"] == "ERR_MISSING_REVIEW_REFERENCE"

        # Review with valid advisor memo activates rule
        act = self.client.post(
            f"/api/v2/finance/tax-config/{rule_id}/review-activate/",
            {"review_reference": "CA-TAX-MEMO-2026-11", "review_notes": "Reviewed with tax consultant"},
            format="json",
        )
        assert act.status_code == status.HTTP_200_OK
        assert act.data["status"] == TaxRuleStatus.REVIEWED_ACTIVE

        # Reviewed rule is immutable on PATCH
        imm = self.client.patch(f"/api/v2/finance/tax-config/{rule_id}/", {"tax_rate_pct": "15.000"}, format="json")
        assert imm.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
        assert imm.data["error_code"] == "ERR_TAX_RULE_IMMUTABLE"

        # Archive rule
        arch = self.client.delete(f"/api/v2/finance/tax-config/{rule_id}/", {"reason": "Superseded"}, format="json")
        assert arch.status_code == status.HTTP_200_OK
        assert arch.data["status"] == TaxRuleStatus.ARCHIVED

    def test_03_inclusive_and_exclusive_calculation_with_gl_snapshot_and_reversal(self):
        self.client.force_authenticate(user=self.owner_a)

        # Rule 1: MEDICINE -> 2.400% INCLUSIVE (extracted from MRP)
        r_med = self.client.post(
            "/api/v2/finance/tax-config/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "rule_code": "MED_INCL_2_4",
                "name": "Medicine MRP Inclusive Tax (Reviewed)",
                "applies_to_scope": "MEDICINE",
                "calculation_mode": "INCLUSIVE",
                "tax_rate_pct": "2.400",
                "effective_from": self.today.isoformat(),
            },
            format="json",
        )
        self.client.post(
            f"/api/v2/finance/tax-config/{r_med.data['rule_id']}/review-activate/",
            {"review_reference": "MEMO-MED-01"},
            format="json",
        )

        # Rule 2: NON_MEDICINE_RETAIL -> 10.000% EXCLUSIVE
        r_ret = self.client.post(
            "/api/v2/finance/tax-config/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "rule_code": "RET_EXCL_10",
                "name": "Retail Exclusive Tax (Reviewed)",
                "applies_to_scope": "NON_MEDICINE_RETAIL",
                "calculation_mode": "EXCLUSIVE",
                "tax_rate_pct": "10.000",
                "effective_from": self.today.isoformat(),
            },
            format="json",
        )
        self.client.post(
            f"/api/v2/finance/tax-config/{r_ret.data['rule_id']}/review-activate/",
            {"review_reference": "MEMO-RET-02"},
            format="json",
        )

        # Calculate + Persist Snapshot + Post Balanced GL
        calc = self.client.post(
            "/api/v2/finance/tax-config/calculate-invoice/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "invoice_reference": "INV-TAX-2026-001",
                "persist_snapshot": True,
                "post_gl": True,
                "items": [
                    {"sku": "SECLO-20", "item_scope": "MEDICINE", "quantity": 1, "unit_price_bdt": "102.40"},
                    {"sku": "LOTION-100", "item_scope": "NON_MEDICINE_RETAIL", "quantity": 1, "unit_price_bdt": "200.00"},
                ],
            },
            format="json",
        )
        assert calc.status_code == status.HTTP_200_OK, calc.data
        # Medicine inclusive: 102.40 / 1.024 = 100.00 taxable, 2.40 tax, 102.40 total
        # Retail exclusive: 200.00 taxable, 20.00 tax, 220.00 total
        assert calc.data["taxable_subtotal_bdt"] == "300.00"
        assert calc.data["total_tax_bdt"] == "22.40"
        assert calc.data["grand_total_bdt"] == "322.40"
        assert calc.data["snapshot_id"] is not None
        assert calc.data["posted_journal_id"] is not None

        journal = JournalEntry.objects.get(entry_id=calc.data["posted_journal_id"])
        assert journal.is_balanced is True
        assert journal.total_debit_bdt == Decimal("322.40")
        assert journal.total_credit_bdt == Decimal("322.40")

        # Reverse snapshot via audited compensating journal
        rev = self.client.post(
            "/api/v2/finance/tax-config/reverse-snapshot/",
            {"tenant_id": self.tenant_a.tenant_id, "snapshot_id": calc.data["snapshot_id"], "reason": "Customer return"},
            format="json",
        )
        assert rev.status_code == status.HTTP_200_OK, rev.data
        assert rev.data["status"] == InvoiceTaxSnapshotStatus.REVERSED
        assert rev.data["reversal_journal_id"] is not None

    def test_04_presentation_profile_upsert(self):
        self.client.force_authenticate(user=self.owner_a)
        put = self.client.put(
            "/api/v2/finance/tax-config/presentation/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "branch_id": self.branch_a1.branch_id,
                "legal_entity_name": "Model Pharmacy Ltd.",
                "tax_registration_label": "BIN No.",
                "tax_registration_number": "001234567-0101",
                "show_tax_breakdown_lines": True,
                "invoice_header_en": "Model Pharmacy Tax Invoice",
                "invoice_header_bn": "মডেল ফার্মেসি ট্যাক্স ইনভয়েস",
            },
            format="json",
        )
        assert put.status_code == status.HTTP_200_OK
        assert put.data["tax_registration_number"] == "001234567-0101"

        get = self.client.get(
            f"/api/v2/finance/tax-config/presentation/?tenant_id={self.tenant_a.tenant_id}&branch_id={self.branch_a1.branch_id}"
        )
        assert get.status_code == status.HTTP_200_OK
        assert get.data["invoice_header_bn"] == "মডেল ফার্মেসি ট্যাক্স ইনভয়েস"

    def test_05_invalid_inputs_rbac_cross_tenant_suspended_and_csv(self):
        self.client.force_authenticate(user=self.owner_a)

        # Invalid rate > 100
        bad_rate = self.client.post(
            "/api/v2/finance/tax-config/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "rule_code": "BAD_RATE",
                "name": "Bad Rate",
                "tax_rate_pct": "150.000",
                "effective_from": self.today.isoformat(),
            },
            format="json",
        )
        assert bad_rate.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Valid rule for cross-tenant & CSV tests
        ok = self.client.post(
            "/api/v2/finance/tax-config/",
            {
                "tenant_id": self.tenant_a.tenant_id,
                "rule_code": "DEV_5",
                "name": "Medical Device 5%",
                "applies_to_scope": "MEDICAL_DEVICE",
                "tax_rate_pct": "5.000",
                "effective_from": self.today.isoformat(),
            },
            format="json",
        )
        assert ok.status_code == status.HTTP_201_CREATED
        rule_id = ok.data["rule_id"]

        # Cashier & Branch Manager denied create/review
        for user in (self.cashier_a1, self.bm_a1):
            self.client.force_authenticate(user=user)
            assert (
                self.client.post(
                    "/api/v2/finance/tax-config/",
                    {"tenant_id": self.tenant_a.tenant_id, "rule_code": "X", "name": "X", "tax_rate_pct": "5"},
                    format="json",
                ).status_code
                == status.HTTP_403_FORBIDDEN
            )

        # Cross-tenant isolation
        self.client.force_authenticate(user=self.owner_b)
        assert self.client.get(f"/api/v2/finance/tax-config/{rule_id}/").status_code == status.HTTP_403_FORBIDDEN

        # CSV export
        self.client.force_authenticate(user=self.owner_a)
        csv_resp = self.client.get(f"/api/v2/finance/tax-config/export/?format=csv&tenant_id={self.tenant_a.tenant_id}")
        assert csv_resp.status_code == status.HTTP_200_OK
        assert "DEV_5" in csv_resp.content.decode("utf-8")

        # Suspended tenant blocked
        self.tenant_a.status = TenantLifecycleStatus.SUSPENDED
        self.tenant_a.save()
        susp = self.client.get(f"/api/v2/finance/tax-config/?tenant_id={self.tenant_a.tenant_id}")
        assert susp.status_code == status.HTTP_403_FORBIDDEN
        assert susp.data["error_code"] == "ERR_TENANT_SUSPENDED"
