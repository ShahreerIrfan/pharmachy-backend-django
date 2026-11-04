"""
backend/tests/test_p164_loose_tablet_sales.py
Architecture Decision: ADR-0164 (P164).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Loose Tablet Sales, Partial Strip Dispensing,
Exact Base-Piece Stock Deduction, Blister Guardrails, and FEFO Concurrency.
"""

import random
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchLifecycleStatus,
    InvoiceStatus,
    LooseTabletSalesConfig,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestLooseTabletSalesIntegration:
    """Integration test suite for P164 Loose Tablet Sales in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p164-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"popular-p164-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p164-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Active Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p164-b-{self.suffix}",
            name=f"Incepta Meds {self.suffix}",
            subdomain=f"incepta-p164-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p164-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Branch B",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p164-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p164-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p164-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Products
        # 4a. Napa 500mg: Regular tablet, loose sales permitted
        self.product_napa = MedicineProduct.objects.create(
            product_id=f"prod-napa-{self.suffix}",
            brand_name=f"Napa 500mg {self.suffix}",
            generic_name="Paracetamol",
            dosage_form="Tablet",
            mrp_unit=Decimal("3.0000"),
            cost_unit=Decimal("2.2000"),
        )

        # 4b. Ceevit 1000mg Effervescent: Hygroscopic, loose tablets strictly prohibited
        self.product_ceevit = MedicineProduct.objects.create(
            product_id=f"prod-ceevit-{self.suffix}",
            brand_name=f"Ceevit Effervescent {self.suffix}",
            generic_name="Ascorbic Acid",
            dosage_form="Tablet",
            mrp_unit=Decimal("15.0000"),
            cost_unit=Decimal("11.0000"),
        )

        # 4c. Zimax 500mg: Surcharge tablet
        self.product_zimax = MedicineProduct.objects.create(
            product_id=f"prod-zimax-{self.suffix}",
            brand_name=f"Zimax 500mg {self.suffix}",
            generic_name="Azithromycin",
            dosage_form="Tablet",
            mrp_unit=Decimal("35.0000"),
            cost_unit=Decimal("28.0000"),
        )

        # 5. Configurations
        self.config_napa = LooseTabletSalesConfig.objects.create(
            config_id=f"ltsc-napa-{self.suffix}",
            tenant=self.tenant_a,
            branch=None,
            product=self.product_napa,
            allow_loose_sales=True,
            min_loose_dispense_units=1,
            max_loose_dispense_units=100,
            strip_size_pieces=10,
            strip_mrp=Decimal("30.0000"),
            loose_unit_mrp=Decimal("3.0000"),
            apply_loose_unit_surcharge=False,
            loose_surcharge_percentage=Decimal("0.00"),
            prohibition_reason="",
        )

        self.config_ceevit = LooseTabletSalesConfig.objects.create(
            config_id=f"ltsc-ceevit-{self.suffix}",
            tenant=self.tenant_a,
            branch=None,
            product=self.product_ceevit,
            allow_loose_sales=False,
            min_loose_dispense_units=1,
            max_loose_dispense_units=50,
            strip_size_pieces=10,
            strip_mrp=Decimal("150.0000"),
            loose_unit_mrp=Decimal("15.0000"),
            apply_loose_unit_surcharge=False,
            loose_surcharge_percentage=Decimal("0.00"),
            prohibition_reason="Hygroscopic formulation degrades upon breaking blister seal.",
        )

        self.config_zimax = LooseTabletSalesConfig.objects.create(
            config_id=f"ltsc-zimax-{self.suffix}",
            tenant=self.tenant_a,
            branch=None,
            product=self.product_zimax,
            allow_loose_sales=True,
            min_loose_dispense_units=1,
            max_loose_dispense_units=100,
            strip_size_pieces=10,
            strip_mrp=Decimal("350.0000"),
            loose_unit_mrp=Decimal("35.0000"),
            apply_loose_unit_surcharge=True,
            loose_surcharge_percentage=Decimal("10.00"),
            prohibition_reason="",
        )

        # 6. Batches for Tenant A
        today = timezone.now().date()
        self.batch_napa_early = BatchStock.objects.create(
            batch_id=f"b-napa-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"NAPA-EARLY-{self.suffix}",
            quantity_base_units=10,
            purchase_date=today - timedelta(days=10),
            expiry_date=today + timedelta(days=30),
            cost_per_unit=Decimal("2.2000"),
            status=BatchStatus.ACTIVE,
        )
        self.batch_napa_late = BatchStock.objects.create(
            batch_id=f"b-napa-2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"NAPA-LATE-{self.suffix}",
            quantity_base_units=50,
            purchase_date=today - timedelta(days=10),
            expiry_date=today + timedelta(days=180),
            cost_per_unit=Decimal("2.2000"),
            status=BatchStatus.ACTIVE,
        )
        self.batch_napa_expired = BatchStock.objects.create(
            batch_id=f"b-napa-exp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_napa,
            batch_number=f"NAPA-EXPIRED-{self.suffix}",
            quantity_base_units=100,
            purchase_date=today - timedelta(days=400),
            expiry_date=today - timedelta(days=5),
            cost_per_unit=Decimal("2.2000"),
            status=BatchStatus.EXPIRED,
        )

        # 7. Users
        self.user_manager_a = PharmacyUser.objects.create_user(
            email=f"mgr_a_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Manager A",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_cashier_a = PharmacyUser.objects.create_user(
            email=f"csh_a_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Cashier A",
            password="Password123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant_a.tenant_id,
            branch_id=self.branch_a.branch_id,
        )
        self.user_b = PharmacyUser.objects.create_user(
            email=f"usr_b_{self.suffix}@incepta.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="User B",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_b.tenant_id,
            branch_id=self.branch_b.branch_id,
        )
        self.user_suspended = PharmacyUser.objects.create_user(
            email=f"usr_susp_{self.suffix}@susp.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="User Suspended",
            password="Password123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant_suspended.tenant_id,
            branch_id=self.branch_suspended.branch_id,
        )

    # --------------------------------------------------------------------------
    # 1. Quote Calculations
    # --------------------------------------------------------------------------

    def test_quote_valid_strip_and_loose(self):
        """1 strip (10 tablets) + 4 loose pieces = 14 base units, exact Decimal calculation."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/quote/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_a.branch_id,
                "strips": 1,
                "loose_pieces": 4,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["allow_loose_sales"] is True
        assert data["normalized_strips"] == 1
        assert data["normalized_loose_pieces"] == 4
        assert data["total_base_units"] == 14
        assert Decimal(data["strip_total"]) == Decimal("30.00")
        assert Decimal(data["loose_total"]) == Decimal("12.00")
        assert Decimal(data["final_quote_amount"]) == Decimal("42.00")
        assert data["is_stock_sufficient"] is True

    def test_quote_prohibited_loose_sales_rejected(self):
        """Selling loose pieces of moisture-sensitive tablets raises 400/422 ERR_LOOSE_TABLET_SALES_PROHIBITED."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/quote/",
            {
                "product_id": self.product_ceevit.product_id,
                "branch_id": self.branch_a.branch_id,
                "strips": 0,
                "loose_pieces": 3,
            },
            format="json",
        )
        assert resp.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        data = resp.data
        assert data["error_code"] == "ERR_LOOSE_TABLET_SALES_PROHIBITED"
        assert "Hygroscopic" in data["detail"]

    def test_quote_prohibited_allows_full_strips(self):
        """Even if loose tablet sales are prohibited, full strips (loose_pieces=0) can be sold."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/quote/",
            {
                "product_id": self.product_ceevit.product_id,
                "branch_id": self.branch_a.branch_id,
                "strips": 2,
                "loose_pieces": 0,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["normalized_strips"] == 2
        assert data["normalized_loose_pieces"] == 0
        assert data["total_base_units"] == 20
        assert Decimal(data["final_quote_amount"]) == Decimal("300.00")

    def test_quote_surcharge_calculation(self):
        """Loose unit surcharge is applied only to loose pieces."""
        self.client.force_authenticate(user=self.user_cashier_a)
        # Zimax: 350.00 strip (10 tablets), loose = 35 * 1.10 = 38.50
        # 1 strip + 2 loose = 350.00 + 77.00 = 427.00
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/quote/",
            {
                "product_id": self.product_zimax.product_id,
                "branch_id": self.branch_a.branch_id,
                "strips": 1,
                "loose_pieces": 2,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["normalized_strips"] == 1
        assert data["normalized_loose_pieces"] == 2
        assert data["total_base_units"] == 12
        assert Decimal(data["strip_total"]) == Decimal("350.00")
        assert Decimal(data["loose_total"]) == Decimal("77.00")
        assert Decimal(data["final_quote_amount"]) == Decimal("427.00")

    # --------------------------------------------------------------------------
    # 2. Checkout & Stock Ledger Execution
    # --------------------------------------------------------------------------

    def test_checkout_specific_batch_deduction(self):
        """Checking out from a specific batch deducts exact integer units."""
        self.client.force_authenticate(user=self.user_cashier_a)
        initial_stock = self.batch_napa_late.quantity_base_units

        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/checkout/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_a.branch_id,
                "batch_id": self.batch_napa_late.batch_id,
                "strips": 0,
                "loose_pieces": 7,
                "customer_name": "Karim Hossain",
                "payment_method": PaymentMethod.CASH,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.data
        assert "invoice_id" in data
        assert data["status"] == InvoiceStatus.FINALIZED

        self.batch_napa_late.refresh_from_db()
        assert self.batch_napa_late.quantity_base_units == initial_stock - 7

    def test_checkout_fefo_allocation(self):
        """Automatic FEFO allocation deducts from earliest unexpired batch first."""
        self.client.force_authenticate(user=self.user_cashier_a)
        # batch_napa_early has 10 units, batch_napa_late has 50 units
        # Requesting 14 units: 10 from early, 4 from late
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/checkout/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_a.branch_id,
                "strips": 1,
                "loose_pieces": 4,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.data
        allocations = data["allocations"]
        assert len(allocations) == 2
        assert allocations[0]["batch_id"] == self.batch_napa_early.batch_id
        assert allocations[0]["quantity"] == 10
        assert allocations[1]["batch_id"] == self.batch_napa_late.batch_id
        assert allocations[1]["quantity"] == 4

        self.batch_napa_early.refresh_from_db()
        self.batch_napa_late.refresh_from_db()
        assert self.batch_napa_early.quantity_base_units == 0
        assert self.batch_napa_late.quantity_base_units == 46

    def test_checkout_expired_batch_rejected(self):
        """Attempting to dispense from an expired batch raises 400/422 ERR_EXPIRED_BATCH."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/checkout/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_a.branch_id,
                "batch_id": self.batch_napa_expired.batch_id,
                "strips": 0,
                "loose_pieces": 5,
            },
            format="json",
        )
        assert resp.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        data = resp.data
        assert data["error_code"] == "ERR_EXPIRED_BATCH"
        assert "expired" in data["detail"].lower()

    def test_checkout_insufficient_stock_rejected(self):
        """Requesting more units than available raises 400/422 ERR_INSUFFICIENT_STOCK."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/checkout/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_a.branch_id,
                "strips": 20, # 200 units > 60 available unexpired
                "loose_pieces": 0,
            },
            format="json",
        )
        assert resp.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        data = resp.data
        assert data["error_code"] == "ERR_INSUFFICIENT_STOCK"

    # --------------------------------------------------------------------------
    # 3. Reversal & Restock
    # --------------------------------------------------------------------------

    def test_reverse_sale_recredits_stock(self):
        """Reversing a sale re-credits the exact integer base units back to BatchStock."""
        self.client.force_authenticate(user=self.user_cashier_a)

        # Checkout 6 loose tablets from late batch
        checkout_resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/checkout/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_a.branch_id,
                "batch_id": self.batch_napa_late.batch_id,
                "strips": 0,
                "loose_pieces": 6,
            },
            format="json",
        )
        assert checkout_resp.status_code == status.HTTP_201_CREATED
        invoice_id = checkout_resp.data["invoice_id"]

        self.batch_napa_late.refresh_from_db()
        assert self.batch_napa_late.quantity_base_units == 44

        # Reverse sale
        self.client.force_authenticate(user=self.user_manager_a)
        rev_resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/reverse/",
            {
                "invoice_id": invoice_id,
                "reason": "Patient returned medication",
            },
            format="json",
        )
        assert rev_resp.status_code == status.HTTP_200_OK
        assert rev_resp.data["restored_base_units"] == 6

        self.batch_napa_late.refresh_from_db()
        assert self.batch_napa_late.quantity_base_units == 50

        # Cannot reverse twice
        rev_twice = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/reverse/",
            {"invoice_id": invoice_id},
            format="json",
        )
        assert rev_twice.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        assert rev_twice.data["error_code"] == "ERR_ALREADY_CANCELLED"

    # --------------------------------------------------------------------------
    # 4. RBAC & Security Boundaries
    # --------------------------------------------------------------------------

    def test_cashier_cannot_configure_policy(self):
        """Cashier role cannot modify loose sales configuration policies."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/",
            {
                "product_id": self.product_napa.product_id,
                "allow_loose_sales": False,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data
        assert data.get("error_code") in ["ERR_ROLE_UNAUTHORIZED", "ERR_PERMISSION_DENIED"]

    def test_cross_tenant_isolation(self):
        """Tenant B cannot checkout Tenant A's products/batches."""
        self.client.force_authenticate(user=self.user_b)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/checkout/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_a.branch_id, # Tenant A's branch
                "strips": 1,
                "loose_pieces": 0,
            },
            format="json",
        )
        # Should be rejected because branch_a does not belong to tenant_b
        assert resp.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN, status.HTTP_422_UNPROCESSABLE_ENTITY]

    def test_suspended_tenant_rejected(self):
        """Suspended tenant is blocked from checkout operations."""
        self.client.force_authenticate(user=self.user_suspended)
        resp = self.client.post(
            "/api/v2/inventory/loose-tablet-sales/checkout/",
            {
                "product_id": self.product_napa.product_id,
                "branch_id": self.branch_suspended.branch_id,
                "strips": 1,
                "loose_pieces": 0,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(data).lower()
