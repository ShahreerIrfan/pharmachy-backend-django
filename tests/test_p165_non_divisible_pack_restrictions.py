"""
backend/tests/test_p165_non_divisible_pack_restrictions.py
Architecture Decision: ADR-0165 (P165).
Phase 09: Packaging, Unit Conversion, Batches & Cost Lots.

Integration test suite for Non-Divisible Pack Restrictions,
Sterile Container Guardrails, and Clinical Exemption Protocols.
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
    MedicineProduct,
    NonDivisibleContainerType,
    NonDivisiblePackPolicy,
    PaymentMethod,
    PlanTier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db
class TestNonDivisiblePackRestrictionsIntegration:
    """Integration test suite for P165 Non-Divisible Pack Restrictions in DRF."""

    @pytest.fixture(autouse=True)
    def setup_entities(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Active Tenant A
        self.tenant_a = Tenant.objects.create(
            tenant_id=f"tn-p165-a-{self.suffix}",
            name=f"Popular Pharma {self.suffix}",
            subdomain=f"popular-p165-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_a = Branch.objects.create(
            branch_id=f"br-p165-a-{self.suffix}",
            tenant=self.tenant_a,
            name="Main Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 2. Active Tenant B
        self.tenant_b = Tenant.objects.create(
            tenant_id=f"tn-p165-b-{self.suffix}",
            name=f"Incepta Meds {self.suffix}",
            subdomain=f"incepta-p165-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch_b = Branch.objects.create(
            branch_id=f"br-p165-b-{self.suffix}",
            tenant=self.tenant_b,
            name="Branch B",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 3. Suspended Tenant
        self.tenant_suspended = Tenant.objects.create(
            tenant_id=f"tn-p165-susp-{self.suffix}",
            name=f"Suspended Pharma {self.suffix}",
            subdomain=f"susp-p165-{self.suffix}",
            plan_tier=PlanTier.STANDARD,
            status=TenantLifecycleStatus.SUSPENDED,
        )
        self.branch_suspended = Branch.objects.create(
            branch_id=f"br-p165-susp-{self.suffix}",
            tenant=self.tenant_suspended,
            name="Suspended Branch",
            status=BranchLifecycleStatus.ACTIVE,
        )

        # 4. Products
        # 4a. Lantus SoloStar: Pre-filled Insulin Pen
        self.product_insulin = MedicineProduct.objects.create(
            product_id=f"prod-lantus-{self.suffix}",
            brand_name=f"Lantus SoloStar {self.suffix}",
            generic_name="Insulin Glargine",
            dosage_form="Insulin",
            mrp_unit=Decimal("850.0000"),
            cost_unit=Decimal("720.0000"),
        )

        # 4b. Rocephin 1g: Sterile Box of 5 Vials
        self.product_rocephin = MedicineProduct.objects.create(
            product_id=f"prod-rocephin-{self.suffix}",
            brand_name=f"Rocephin 1g Sterile Box {self.suffix}",
            generic_name="Ceftriaxone",
            dosage_form="Injection",
            mrp_unit=Decimal("950.0000"),
            cost_unit=Decimal("800.0000"),
        )

        # 4c. Morphine 10mg/ml Ampoule Box (Institutional Care splittable)
        self.product_morphine = MedicineProduct.objects.create(
            product_id=f"prod-morph-{self.suffix}",
            brand_name=f"Morphine Ampoule Box {self.suffix}",
            generic_name="Morphine Sulfate",
            dosage_form="Injection",
            mrp_unit=Decimal("1200.0000"),
            cost_unit=Decimal("980.0000"),
        )

        # 5. Non-Divisible Policies
        self.policy_insulin = NonDivisiblePackPolicy.objects.create(
            policy_id=f"ndpp-lantus-{self.suffix}",
            tenant=self.tenant_a,
            branch=None,
            product=self.product_insulin,
            container_type=NonDivisibleContainerType.INSULIN_PEN,
            is_splitting_allowed=False,
            require_pharmacist_approval=True,
            approved_break_factor=1,
            sub_unit_label="PEN",
            prohibition_reason="Pre-filled sterile insulin cartridge cannot be fractured or broken.",
            exemption_protocols=[],
        )

        self.policy_rocephin = NonDivisiblePackPolicy.objects.create(
            policy_id=f"ndpp-rocephin-{self.suffix}",
            tenant=self.tenant_a,
            branch=None,
            product=self.product_rocephin,
            container_type=NonDivisibleContainerType.STERILE_VIAL_BOX,
            is_splitting_allowed=False,
            require_pharmacist_approval=True,
            approved_break_factor=5,
            sub_unit_label="VIAL",
            prohibition_reason="Sealed sterile box integrity must be maintained for retail sales.",
            exemption_protocols=[],
        )

        self.policy_morphine = NonDivisiblePackPolicy.objects.create(
            policy_id=f"ndpp-morph-{self.suffix}",
            tenant=self.tenant_a,
            branch=None,
            product=self.product_morphine,
            container_type=NonDivisibleContainerType.STERILE_VIAL_BOX,
            is_splitting_allowed=True,
            require_pharmacist_approval=True,
            approved_break_factor=10,
            sub_unit_label="AMPOULE",
            prohibition_reason="",
            exemption_protocols=["HOSPITAL_INPATIENT", "ICU_EMERGENCY_DOSE"],
        )

        # 6. Batches
        today = timezone.now().date()
        self.batch_insulin_early = BatchStock.objects.create(
            batch_id=f"b-pen-1-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_insulin,
            batch_number=f"PEN-EARLY-{self.suffix}",
            quantity_base_units=5,
            purchase_date=today - timedelta(days=20),
            expiry_date=today + timedelta(days=60),
            cost_per_unit=Decimal("720.0000"),
            status=BatchStatus.ACTIVE,
        )
        self.batch_insulin_late = BatchStock.objects.create(
            batch_id=f"b-pen-2-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_insulin,
            batch_number=f"PEN-LATE-{self.suffix}",
            quantity_base_units=20,
            purchase_date=today - timedelta(days=10),
            expiry_date=today + timedelta(days=240),
            cost_per_unit=Decimal("720.0000"),
            status=BatchStatus.ACTIVE,
        )
        self.batch_insulin_expired = BatchStock.objects.create(
            batch_id=f"b-pen-exp-{self.suffix}",
            tenant=self.tenant_a,
            branch=self.branch_a,
            product=self.product_insulin,
            batch_number=f"PEN-EXPIRED-{self.suffix}",
            quantity_base_units=10,
            purchase_date=today - timedelta(days=400),
            expiry_date=today - timedelta(days=2),
            cost_per_unit=Decimal("720.0000"),
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
        self.user_pharmacist_a = PharmacyUser.objects.create_user(
            email=f"pharma_a_{self.suffix}@popular.test",
            phone_number=f"+88017{random.randint(10000000, 99999999)}",
            full_name="Pharmacist A",
            password="Password123!",
            role=UserRole.STAFF_PHARMACIST,
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
    # 1. Validation & Pre-Flight Checks
    # --------------------------------------------------------------------------

    def test_validate_whole_unit_insulin_pen_allowed(self):
        """Dispensing 2 whole insulin pens is accepted."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/validate/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "2.0",
                "is_split_request": False,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_allowed"] is True
        assert data["container_type"] == NonDivisibleContainerType.INSULIN_PEN

    def test_validate_fractional_insulin_pen_rejected(self):
        """Dispensing 1.5 insulin pens is rejected with 422 ERR_NON_DIVISIBLE_CONTAINER_SPLIT_PROHIBITED."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/validate/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "1.5",
                "is_split_request": False,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        data = resp.data
        assert data["error_code"] == "ERR_NON_DIVISIBLE_CONTAINER_SPLIT_PROHIBITED"
        assert "Fractional dispensing" in data["detail"]

    def test_validate_sealed_sterile_box_split_rejected(self):
        """Attempting to break open sealed box of vials is rejected with 422."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/validate/",
            {
                "product_id": self.product_rocephin.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "1.0",
                "is_split_request": True,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        data = resp.data
        assert data["error_code"] == "ERR_NON_DIVISIBLE_CONTAINER_SPLIT_PROHIBITED"
        assert "Breaking sealed container" in data["detail"]

    def test_validate_split_without_exemption_protocol_rejected(self):
        """Splitting allowed container without approved exemption protocol raises 422."""
        self.client.force_authenticate(user=self.user_pharmacist_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/validate/",
            {
                "product_id": self.product_morphine.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "1.0",
                "is_split_request": True,
                "exemption_protocol": "INVALID_PROTOCOL",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        data = resp.data
        assert data["error_code"] == "ERR_INVALID_EXEMPTION_PROTOCOL"

    def test_validate_cashier_cannot_authorize_split(self):
        """Cashier role cannot authorize breaking a non-divisible pack (403 ERR_ROLE_UNAUTHORIZED)."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/validate/",
            {
                "product_id": self.product_morphine.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "1.0",
                "is_split_request": True,
                "exemption_protocol": "HOSPITAL_INPATIENT",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data
        assert data["error_code"] == "ERR_ROLE_UNAUTHORIZED"

    def test_validate_pharmacist_can_authorize_split(self):
        """Pharmacist with approved clinical protocol can authorize container split."""
        self.client.force_authenticate(user=self.user_pharmacist_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/validate/",
            {
                "product_id": self.product_morphine.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "1.0",
                "is_split_request": True,
                "exemption_protocol": "HOSPITAL_INPATIENT",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.data
        assert data["is_allowed"] is True
        assert data["approved_break_factor"] == 10

    # --------------------------------------------------------------------------
    # 2. Checkout & Stock Ledger Execution
    # --------------------------------------------------------------------------

    def test_checkout_whole_container_stock_deduction(self):
        """Checking out 2 whole pens deducts exact 2 integer base units from BatchStock."""
        self.client.force_authenticate(user=self.user_cashier_a)
        initial_stock = self.batch_insulin_early.quantity_base_units

        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/checkout/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "batch_id": self.batch_insulin_early.batch_id,
                "requested_quantity": "2.0",
                "customer_name": "Tariqul Islam",
                "payment_method": PaymentMethod.CASH,
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.data
        assert "invoice_id" in data
        assert data["status"] == InvoiceStatus.FINALIZED
        assert Decimal(data["total_amount"]) == Decimal("1700.00") # 2 * 850.00

        self.batch_insulin_early.refresh_from_db()
        assert self.batch_insulin_early.quantity_base_units == initial_stock - 2

    def test_checkout_fefo_allocation(self):
        """FEFO checkout takes earliest unexpired batch first."""
        self.client.force_authenticate(user=self.user_cashier_a)
        # batch_insulin_early has 5 units, batch_insulin_late has 20 units
        # Requesting 7 pens: 5 from early, 2 from late
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/checkout/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "7.0",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED
        allocations = resp.data["allocations"]
        assert len(allocations) == 2
        assert allocations[0]["batch_id"] == self.batch_insulin_early.batch_id
        assert allocations[0]["quantity"] == 5
        assert allocations[1]["batch_id"] == self.batch_insulin_late.batch_id
        assert allocations[1]["quantity"] == 2

        self.batch_insulin_early.refresh_from_db()
        self.batch_insulin_late.refresh_from_db()
        assert self.batch_insulin_early.quantity_base_units == 0
        assert self.batch_insulin_late.quantity_base_units == 18

    def test_checkout_expired_batch_rejected(self):
        """Attempting to checkout from an expired batch raises 422 ERR_EXPIRED_BATCH."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/checkout/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "batch_id": self.batch_insulin_expired.batch_id,
                "requested_quantity": "1.0",
            },
            format="json",
        )
        assert resp.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
        data = resp.data
        assert data["error_code"] == "ERR_EXPIRED_BATCH"

    def test_checkout_insufficient_stock_rejected(self):
        """Requesting more units than unexpired stock raises 422 ERR_INSUFFICIENT_STOCK."""
        self.client.force_authenticate(user=self.user_cashier_a)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/checkout/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "50.0", # 50 > 25 available
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
        """Reversing a sale restores exact integer units to BatchStock."""
        self.client.force_authenticate(user=self.user_cashier_a)

        # Checkout 3 pens from late batch
        checkout_resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/checkout/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "batch_id": self.batch_insulin_late.batch_id,
                "requested_quantity": "3.0",
            },
            format="json",
        )
        assert checkout_resp.status_code == status.HTTP_201_CREATED
        invoice_id = checkout_resp.data["invoice_id"]

        self.batch_insulin_late.refresh_from_db()
        assert self.batch_insulin_late.quantity_base_units == 17

        # Reverse sale
        self.client.force_authenticate(user=self.user_manager_a)
        rev_resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/reverse/",
            {
                "invoice_id": invoice_id,
                "reason": "Cold chain excursion return",
            },
            format="json",
        )
        assert rev_resp.status_code == status.HTTP_200_OK
        assert rev_resp.data["restored_base_units"] == 3

        self.batch_insulin_late.refresh_from_db()
        assert self.batch_insulin_late.quantity_base_units == 20

    # --------------------------------------------------------------------------
    # 4. Multi-Tenant & Subscription Isolation
    # --------------------------------------------------------------------------

    def test_cross_tenant_isolation(self):
        """Tenant B cannot checkout Tenant A's products/batches."""
        self.client.force_authenticate(user=self.user_b)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/checkout/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_a.branch_id,
                "requested_quantity": "1.0",
            },
            format="json",
        )
        assert resp.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN, status.HTTP_422_UNPROCESSABLE_ENTITY]

    def test_suspended_tenant_rejected(self):
        """Suspended tenant is blocked from checkout operations."""
        self.client.force_authenticate(user=self.user_suspended)
        resp = self.client.post(
            "/api/v2/inventory/non-divisible-packs/checkout/",
            {
                "product_id": self.product_insulin.product_id,
                "branch_id": self.branch_suspended.branch_id,
                "requested_quantity": "1.0",
            },
            format="json",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        data = resp.data if hasattr(resp, "data") else resp.json()
        assert data.get("error_code") == "ERR_TENANT_SUSPENDED" or "suspended" in str(data).lower()
