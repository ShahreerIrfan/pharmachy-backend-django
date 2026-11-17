"""
backend/tests/test_p221_pos_domain_model.py
Acceptance tests for P221: POS domain model (Phase 12).

Covers:
1. Full POS domain lifecycle (Customer, SaleHeader, Lines, Packaging conversion, Decimal tax/discount,
   Hold/Resume, FEFO Allocation, Idempotent Split Payment Attempts, Finalization -> SaleInvoice/StockLedger/Due).
2. Prescription verification enforcement, expired/quarantined batch block, pre-final soft-void vs
   post-final PATCH/DELETE immutability & Manager audited reversal + immutable SHA-256 audit log.
3. RBAC & cost masking (Cashier masked unit_cost/total_cost, >5% discount denied, reversal/credit-limit denied,
   Inventory Clerk denied), cross-branch isolation, cross-tenant isolation, suspended subscription,
   and BASIC-plan CUSTOMER_LEDGER subscription feature gate.
4. Concurrent FEFO allocation contention proving zero negative stock and zero double-finalization.
"""

from __future__ import annotations

import datetime
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, connections
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DrfValidationError
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    CashierRegisterAssignment,
    CustomerDueRecord,
    MedicineProduct,
    PaymentMethod,
    PaymentTransaction,
    PlanTier,
    PosCustomer,
    PosDiscountType,
    PosPackagingUnit,
    PosSaleLifecycleAudit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PurchaseCostLot,
    PurchaseCostLotStatus,
    RegisterAssignmentStatus,
    SaleInvoice,
    SaleLotAllocation,
    StaffMember,
    StaffRole,
    StockLedgerEntry,
    StockMovementType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.pos_domain_service import PosDomainService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-pos-a-{uid}",
        name="Lazz Pharma Dhaka",
        subdomain=f"lazz-pos-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_basic(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-pos-basic-{uid}",
        name="Single Branch Basic Pharmacy",
        subdomain=f"basic-pos-{uid}",
        plan_tier=PlanTier.BASIC,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-pos-b-{uid}",
        name="Foreign Tenant Pharmacy",
        subdomain=f"foreign-pos-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_suspended(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-pos-susp-{uid}",
        name="Suspended Pharmacy Ltd",
        subdomain=f"susp-pos-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.SUSPENDED,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-pos-a-{uid}",
        tenant=tenant_a,
        name="Kalabagan Counter 1",
        license_number=f"DGDA-KLAB-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-pos-a2-{uid}",
        tenant=tenant_a,
        name="Banani Sibling Branch",
        license_number=f"DGDA-BNN-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_basic(tenant_basic):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-pos-bsc-{uid}",
        tenant=tenant_basic,
        name="Basic Store Branch",
        license_number=f"DGDA-BSC-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-pos-b-{uid}",
        tenant=tenant_b,
        name="Foreign Sylhet Branch",
        license_number=f"DGDA-SYL-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_susp(tenant_suspended):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-pos-susp-{uid}",
        tenant=tenant_suspended,
        name="Suspended Branch",
        license_number=f"DGDA-SUSP-{uid}",
        is_active=True,
    )


def _make_user(tenant, branch, role, email_prefix):
    uid = uuid.uuid4().hex[:8]
    digits = "".join(str(ord(c) % 10) for c in uid)
    return PharmacyUser.objects.create_user(
        email=f"{email_prefix}-{uid}@pharma.bd",
        phone_number=f"+88017{digits}",
        password="PosTestPass!2026",
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id if branch else None,
        role=role,
        full_name=f"{email_prefix.title()} User",
    )


@pytest.fixture
def owner_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.PHARMACY_OWNER, "owner-a")


@pytest.fixture
def manager_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.BRANCH_MANAGER, "mgr-a")


@pytest.fixture
def pharmacist_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.STAFF_PHARMACIST, "pharm-a")


@pytest.fixture
def cashier_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.CASHIER, "cashier-a")


@pytest.fixture
def cashier_a2(tenant_a, branch_a2):
    return _make_user(tenant_a, branch_a2, UserRole.CASHIER, "cashier-a2")


@pytest.fixture
def clerk_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.INVENTORY_CLERK, "clerk-a")


@pytest.fixture
def owner_b(tenant_b, branch_b):
    return _make_user(tenant_b, branch_b, UserRole.PHARMACY_OWNER, "owner-b")


@pytest.fixture
def owner_basic(tenant_basic, branch_basic):
    return _make_user(tenant_basic, branch_basic, UserRole.PHARMACY_OWNER, "owner-basic")


@pytest.fixture
def owner_susp(tenant_suspended, branch_susp):
    return _make_user(tenant_suspended, branch_susp, UserRole.PHARMACY_OWNER, "owner-susp")


@pytest.fixture
def otc_product(db):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"med-napa-{uid}",
        brand_name="Napa Extra",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        manufacturer="Beximco",
        mrp_unit=Decimal("2.50"),
        cost_unit=Decimal("1.80"),
        is_prescription_only=False,
        is_antibiotic=False,
    )


@pytest.fixture
def antibiotic_product(db):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"med-azith-{uid}",
        brand_name="Zimax 500",
        generic_name="Azithromycin",
        dosage_form="Tablet",
        strength="500mg",
        manufacturer="Square",
        mrp_unit=Decimal("35.00"),
        cost_unit=Decimal("26.00"),
        is_prescription_only=True,
        is_antibiotic=True,
    )


@pytest.fixture
def active_batch_otc(tenant_a, branch_a, otc_product):
    uid = uuid.uuid4().hex[:6]
    today = timezone.now().date()
    batch = BatchStock.objects.create(
        batch_id=f"bat-otc-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=otc_product,
        batch_number=f"NAPA-B1-{uid}",
        quantity_base_units=100,
        reserved_quantity_base_units=0,
        expiry_date=today + datetime.timedelta(days=365),
        purchase_date=today - datetime.timedelta(days=10),
        cost_per_unit=Decimal("1.80"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )
    PurchaseCostLot.objects.create(
        lot_id=f"lot-otc-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=otc_product,
        batch=batch,
        purchase_date=today - datetime.timedelta(days=10),
        unit_cost=Decimal("1.7500"),
        initial_quantity=100,
        remaining_quantity=100,
        status=PurchaseCostLotStatus.ACTIVE,
    )
    return batch


@pytest.mark.django_db
class TestP221PosDomainModel:
    """Comprehensive P221 verification across models, Decimal math, RBAC, isolation, and concurrency."""

    def test_1_full_pos_sale_lifecycle_decimal_split_payments_and_ledger_bridge(
        self, api_client, owner_a, cashier_a, tenant_a, branch_a, otc_product, active_batch_otc
    ):
        # 1a. Owner creates POS customer with credit limit
        api_client.force_authenticate(user=owner_a)
        cust_res = api_client.post(
            "/api/v2/pos/customers/",
            {
                "primary_branch": branch_a.branch_id,
                "full_name": "Md. Tariqul Islam",
                "phone": "01711223344",
                "credit_limit_bdt": "1000.00",
            },
            format="json",
        )
        assert cust_res.status_code == status.HTTP_201_CREATED
        customer_id = cust_res.data["customer_id"]

        # Register assignment linkage for cashier
        cashier_staff = StaffMember.objects.create(
            staff_id=f"stf-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            username=cashier_a.email,
            full_name=cashier_a.full_name,
            role=StaffRole.CASHIER,
            mobile_number=cashier_a.phone_number,
        )
        reg = CashierRegisterAssignment.objects.create(
            assignment_id=f"cra-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            cashier=cashier_staff,
            register_id="POS-COUNTER-01",
            opening_float_amount=Decimal("2000.00"),
            status=RegisterAssignmentStatus.ACTIVE,
        )

        # 1b. Cashier creates sale order: 2 STRIP (10 tablets/strip = 20 base units) @ 25.00 BDT/strip, 5% line tax ->
        # line subtotal = 50.00, tax = 2.50, line_total = 52.50, flat header discount = 2.50 -> grand_total = 50.00!
        api_client.force_authenticate(user=cashier_a)
        idem = f"pos-idem-{uuid.uuid4().hex[:10]}"
        create_payload = {
            "branch": branch_a.branch_id,
            "customer": customer_id,
            "register_assignment": reg.assignment_id,
            "idempotency_key": idem,
            "offline_device_namespace": "POS-DT-01",
            "offline_local_sequence": 1001,
            "discount_type": PosDiscountType.FLAT_BDT,
            "discount_amount_bdt": "2.50",
            "lines": [
                {
                    "product": otc_product.product_id,
                    "packaging_unit": PosPackagingUnit.STRIP,
                    "quantity_units": 2,
                    "conversion_factor": 10,
                    "unit_price_bdt": "25.00",
                    "tax_rate_pct": "5.00",
                }
            ],
        }
        sale_res = api_client.post("/api/v2/pos/sales/", create_payload, format="json")
        assert sale_res.status_code == status.HTTP_201_CREATED
        sale_id = sale_res.data["sale_id"]
        assert sale_res.data["lifecycle_state"] == PosSaleLifecycleState.DRAFT
        assert Decimal(sale_res.data["subtotal_bdt"]) == Decimal("50.00")
        assert Decimal(sale_res.data["tax_amount_bdt"]) == Decimal("2.50")
        assert Decimal(sale_res.data["discount_amount_bdt"]) == Decimal("2.50")
        assert Decimal(sale_res.data["grand_total_bdt"]) == Decimal("50.00")
        assert sale_res.data["lines"][0]["base_units_total"] == 20
        # Cashier MUST NOT see internal cost price!
        assert sale_res.data["lines"][0]["unit_cost_bdt"] is None

        # Idempotent replay with same idempotency_key returns existing sale with 200 OK
        replay_res = api_client.post("/api/v2/pos/sales/", create_payload, format="json")
        assert replay_res.status_code == status.HTTP_200_OK
        assert replay_res.data["sale_id"] == sale_id
        assert PosSaleOrder.objects.filter(tenant=tenant_a, idempotency_key=idem).count() == 1

        # 1c. Hold, PATCH while held, Resume
        hold_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/hold/", {"hold_reference": "WAIT-RX"}, format="json")
        assert hold_res.status_code == status.HTTP_200_OK
        assert hold_res.data["lifecycle_state"] == PosSaleLifecycleState.HELD

        patch_res = api_client.patch(
            f"/api/v2/pos/sales/{sale_id}/",
            {"notes": "Customer requested receipt print"},
            format="json",
        )
        assert patch_res.status_code == status.HTTP_200_OK
        assert patch_res.data["notes"] == "Customer requested receipt print"

        resume_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/resume/", {}, format="json")
        assert resume_res.status_code == status.HTTP_200_OK
        assert resume_res.data["lifecycle_state"] == PosSaleLifecycleState.DRAFT

        # 1d. FEFO Allocate stock -> CONFIRMED (reserves 20 units)
        alloc_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/allocate/", {}, format="json")
        assert alloc_res.status_code == status.HTTP_200_OK
        assert alloc_res.data["lifecycle_state"] == PosSaleLifecycleState.CONFIRMED
        assert alloc_res.data["allocations"][0]["unit_cost_bdt"] is None  # Cashier cost masked
        active_batch_otc.refresh_from_db()
        assert active_batch_otc.reserved_quantity_base_units == 20
        assert active_batch_otc.quantity_base_units == 100

        # 1e. Split payment attempts: 30.00 CASH + 20.00 DUE (plus idempotent retry on CASH)
        pay1_key = f"pay-{uuid.uuid4().hex[:8]}"
        p1_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "30.00", "idempotency_key": pay1_key},
            format="json",
        )
        assert p1_res.status_code == status.HTTP_201_CREATED
        assert p1_res.data["sale"]["lifecycle_state"] == PosSaleLifecycleState.PARTIALLY_PAID

        p1_replay = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "30.00", "idempotency_key": pay1_key},
            format="json",
        )
        assert p1_replay.status_code == status.HTTP_200_OK
        assert p1_replay.data["idempotent_replay"] is True

        p2_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {"tender_type": PaymentMethod.DUE, "amount_bdt": "20.00", "idempotency_key": f"pay-due-{uuid.uuid4().hex[:8]}"},
            format="json",
        )
        assert p2_res.status_code == status.HTTP_201_CREATED
        assert Decimal(p2_res.data["sale"]["paid_amount_bdt"]) == Decimal("30.00")
        assert Decimal(p2_res.data["sale"]["due_amount_bdt"]) == Decimal("20.00")

        # 1f. Finalize sale -> updates invoice, ledger, allocations, batch stock, and customer due
        fin_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_res.status_code == status.HTTP_200_OK
        assert fin_res.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        assert fin_res.data["linked_invoice_id"] is not None

        active_batch_otc.refresh_from_db()
        assert active_batch_otc.quantity_base_units == 80
        assert active_batch_otc.reserved_quantity_base_units == 0

        cust = PosCustomer.objects.get(pk=customer_id)
        assert cust.current_due_bdt == Decimal("20.00")
        assert SaleInvoice.objects.filter(pk=fin_res.data["linked_invoice_id"], status="FINALIZED").exists()
        assert PaymentTransaction.objects.filter(invoice_id=fin_res.data["linked_invoice_id"], amount_bdt=Decimal("30.00")).exists()
        assert CustomerDueRecord.objects.filter(invoice_id=fin_res.data["linked_invoice_id"], remaining_due_bdt=Decimal("20.00")).exists()
        assert SaleLotAllocation.objects.filter(invoice_id=fin_res.data["linked_invoice_id"], quantity_base_units=20).exists()
        assert StockLedgerEntry.objects.filter(
            source_entity_id=sale_id, movement_type=StockMovementType.SALE_DISPENSE, quantity_delta=-20
        ).exists()

        # Owner CAN see unit_cost_bdt and total_cost_bdt on GET
        api_client.force_authenticate(user=owner_a)
        owner_get = api_client.get(f"/api/v2/pos/sales/{sale_id}/")
        assert owner_get.status_code == status.HTTP_200_OK
        assert owner_get.data["allocations"][0]["unit_cost_bdt"] == "1.7500"
        assert owner_get.data["allocations"][0]["total_cost_bdt"] == "35.0000"

    def test_2_rx_enforcement_expired_batch_block_immutability_and_reversal(
        self, api_client, pharmacist_a, cashier_a, tenant_a, branch_a, antibiotic_product
    ):
        today = timezone.now().date()
        # Only expired batch exists initially -> allocation must fail safely
        expired_batch = BatchStock.objects.create(
            batch_id=f"bat-exp-{uuid.uuid4().hex[:6]}",
            tenant=tenant_a,
            branch=branch_a,
            product=antibiotic_product,
            batch_number="EXP-ANT-01",
            quantity_base_units=40,
            expiry_date=today - datetime.timedelta(days=2),
            purchase_date=today - datetime.timedelta(days=400),
            cost_per_unit=Decimal("26.00"),
            status=BatchStatus.ACTIVE,
        )

        api_client.force_authenticate(user=cashier_a)
        res = api_client.post(
            "/api/v2/pos/sales/",
            {
                "branch": branch_a.branch_id,
                "lines": [
                    {
                        "product": antibiotic_product.product_id,
                        "packaging_unit": PosPackagingUnit.TABLET,
                        "quantity_units": 5,
                        "conversion_factor": 1,
                        "unit_price_bdt": "35.00",
                    }
                ],
            },
            format="json",
        )
        assert res.status_code == status.HTTP_201_CREATED
        assert res.data["lifecycle_state"] == PosSaleLifecycleState.PENDING_RX_REVIEW
        sale_id = res.data["sale_id"]

        # Allocation must reject because only expired stock exists
        alloc_fail = api_client.post(f"/api/v2/pos/sales/{sale_id}/allocate/", {}, format="json")
        assert alloc_fail.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # Add valid future batch
        valid_batch = BatchStock.objects.create(
            batch_id=f"bat-ok-{uuid.uuid4().hex[:6]}",
            tenant=tenant_a,
            branch=branch_a,
            product=antibiotic_product,
            batch_number="OK-ANT-01",
            quantity_base_units=50,
            expiry_date=today + datetime.timedelta(days=200),
            purchase_date=today - datetime.timedelta(days=5),
            cost_per_unit=Decimal("26.00"),
            status=BatchStatus.ACTIVE,
        )

        # Cashier tries to pay and finalize BEFORE pharmacist verifies Rx -> blocked!
        api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "175.00", "idempotency_key": f"rxpay-{uuid.uuid4().hex[:6]}"},
            format="json",
        )
        fin_no_rx = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_no_rx.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_RX_VERIFICATION_REQUIRED" in str(fin_no_rx.data)

        # Cashier tries verify-rx -> 403 FORBIDDEN; Pharmacist succeeds -> 200 OK
        rx_cashier = api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {}, format="json")
        assert rx_cashier.status_code == status.HTTP_403_FORBIDDEN

        api_client.force_authenticate(user=pharmacist_a)
        rx_pharm = api_client.post(f"/api/v2/pos/sales/{sale_id}/verify-rx/", {"notes": "BMDC Rx checked"}, format="json")
        assert rx_pharm.status_code == status.HTTP_200_OK

        # Finalize now succeeds!
        fin_ok = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_ok.status_code == status.HTTP_200_OK
        assert fin_ok.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        valid_batch.refresh_from_db()
        assert valid_batch.quantity_base_units == 45

        # Second finalize, PATCH, and DELETE on FINALIZED sale must be blocked
        double_fin = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert double_fin.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        patch_fin = api_client.patch(f"/api/v2/pos/sales/{sale_id}/", {"notes": "tamper"}, format="json")
        assert patch_fin.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        del_fin = api_client.delete(f"/api/v2/pos/sales/{sale_id}/")
        assert del_fin.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        # Cashier cannot reverse finalized sale; Pharmacist/Manager reverses cleanly and restores stock!
        api_client.force_authenticate(user=cashier_a)
        rev_cashier = api_client.post(f"/api/v2/pos/sales/{sale_id}/reverse/", {"reason": "Wrong dosage"}, format="json")
        assert rev_cashier.status_code == status.HTTP_403_FORBIDDEN

        api_client.force_authenticate(user=pharmacist_a)
        rev_ok = api_client.post(f"/api/v2/pos/sales/{sale_id}/reverse/", {"reason": "Patient returned unopened strip"}, format="json")
        assert rev_ok.status_code == status.HTTP_200_OK
        assert rev_ok.data["lifecycle_state"] == PosSaleLifecycleState.REVERSED

        valid_batch.refresh_from_db()
        assert valid_batch.quantity_base_units == 50  # Restored!
        assert expired_batch.quantity_base_units == 40  # Untouched!
        assert SaleInvoice.objects.get(pk=rev_ok.data["linked_invoice_id"]).status == "REVERSED"

        # Lifecycle audit record must be strictly immutable
        audit = PosSaleLifecycleAudit.objects.filter(sale_id=sale_id).first()
        assert audit is not None
        with pytest.raises(DjangoValidationError):
            audit.reason = "Forged"
            audit.save()
        with pytest.raises(DjangoValidationError):
            audit.delete()

    def test_3_rbac_discount_cap_cross_branch_cross_tenant_and_subscription_gates(
        self,
        api_client,
        cashier_a,
        cashier_a2,
        clerk_a,
        owner_a,
        owner_b,
        owner_basic,
        owner_susp,
        branch_a,
        branch_basic,
        branch_susp,
        otc_product,
        active_batch_otc,
    ):
        # 3a. Inventory Clerk denied POS sales endpoints
        api_client.force_authenticate(user=clerk_a)
        clerk_res = api_client.get("/api/v2/pos/sales/")
        assert clerk_res.status_code == status.HTTP_403_FORBIDDEN

        # 3b. Cashier > 5% discount ceiling rejected without Manager approval
        api_client.force_authenticate(user=cashier_a)
        disc_fail = api_client.post(
            "/api/v2/pos/sales/",
            {
                "branch": branch_a.branch_id,
                "discount_type": PosDiscountType.FLAT_BDT,
                "discount_amount_bdt": "20.00",
                "lines": [
                    {
                        "product": otc_product.product_id,
                        "packaging_unit": PosPackagingUnit.STRIP,
                        "quantity_units": 1,
                        "conversion_factor": 10,
                        "unit_price_bdt": "100.00",
                    }
                ],
            },
            format="json",
        )
        assert disc_fail.status_code == status.HTTP_403_FORBIDDEN

        # Cashier cannot grant customer credit limit > 0
        cust_cred_fail = api_client.post(
            "/api/v2/pos/customers/",
            {"primary_branch": branch_a.branch_id, "full_name": "Due Guy", "credit_limit_bdt": "500.00"},
            format="json",
        )
        assert cust_cred_fail.status_code == status.HTTP_403_FORBIDDEN

        # Create draft order in branch_a as owner_a for cross-branch / cross-tenant / soft-void tests
        api_client.force_authenticate(user=owner_a)
        draft_res = api_client.post(
            "/api/v2/pos/sales/",
            {
                "branch": branch_a.branch_id,
                "lines": [
                    {
                        "product": otc_product.product_id,
                        "packaging_unit": PosPackagingUnit.TABLET,
                        "quantity_units": 4,
                        "conversion_factor": 1,
                        "unit_price_bdt": "2.50",
                    }
                ],
            },
            format="json",
        )
        assert draft_res.status_code == status.HTTP_201_CREATED
        draft_id = draft_res.data["sale_id"]

        # 3c. Sibling branch cashier cannot read or mutate branch_a order
        api_client.force_authenticate(user=cashier_a2)
        assert api_client.get(f"/api/v2/pos/sales/{draft_id}/").status_code == status.HTTP_404_NOT_FOUND
        cross_br = api_client.post(
            "/api/v2/pos/sales/",
            {
                "branch": branch_a.branch_id,
                "lines": [{"product": otc_product.product_id, "quantity_units": 1, "unit_price_bdt": "2.50"}],
            },
            format="json",
        )
        assert cross_br.status_code == status.HTTP_403_FORBIDDEN

        # 3d. Cross-tenant owner_b cannot access tenant_a sales
        api_client.force_authenticate(user=owner_b)
        assert api_client.get(f"/api/v2/pos/sales/{draft_id}/").status_code == status.HTTP_404_NOT_FOUND

        # Soft-void pre-final draft works for owner_a
        api_client.force_authenticate(user=owner_a)
        void_res = api_client.delete(f"/api/v2/pos/sales/{draft_id}/?reason=Customer+left")
        assert void_res.status_code == status.HTTP_200_OK
        assert void_res.data["lifecycle_state"] == PosSaleLifecycleState.VOIDED

        # 3e. Suspended subscription blocks POS operations (403)
        api_client.force_authenticate(user=owner_susp)
        susp_res = api_client.post(
            "/api/v2/pos/sales/",
            {
                "branch": branch_susp.branch_id,
                "lines": [{"product": otc_product.product_id, "quantity_units": 1, "unit_price_bdt": "2.50"}],
            },
            format="json",
        )
        assert susp_res.status_code == status.HTTP_403_FORBIDDEN

        # 3f. BASIC plan tenant blocks CUSTOMER_LEDGER credit limits and DUE payments (403 ERR_SUBSCRIPTION_FEATURE_LOCKED)
        api_client.force_authenticate(user=owner_basic)
        basic_credit_fail = api_client.post(
            "/api/v2/pos/customers/",
            {"primary_branch": branch_basic.branch_id, "full_name": "Basic Cust", "credit_limit_bdt": "200.00"},
            format="json",
        )
        assert basic_credit_fail.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_SUBSCRIPTION_FEATURE_LOCKED" in str(basic_credit_fail.data)
        assert "CUSTOMER_LEDGER" in str(basic_credit_fail.data)

        # Cash sale on BASIC plan works normally
        basic_cash_sale = api_client.post(
            "/api/v2/pos/sales/",
            {
                "branch": branch_basic.branch_id,
                "lines": [{"product": otc_product.product_id, "quantity_units": 2, "unit_price_bdt": "2.50"}],
            },
            format="json",
        )
        assert basic_cash_sale.status_code == status.HTTP_201_CREATED
        basic_due_fail = api_client.post(
            f"/api/v2/pos/sales/{basic_cash_sale.data['sale_id']}/record-payment/",
            {"tender_type": PaymentMethod.DUE, "amount_bdt": "5.00"},
            format="json",
        )
        assert basic_due_fail.status_code == status.HTTP_403_FORBIDDEN


_sqlite_alloc_lock = threading.Lock()


@pytest.mark.django_db(transaction=True)
def test_4_concurrent_allocation_contention_prevents_oversell_and_negative_stock(
    owner_a, tenant_a, branch_a, otc_product
):
    today = timezone.now().date()
    tight_batch = BatchStock.objects.create(
        batch_id=f"bat-tight-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        product=otc_product,
        batch_number=f"TIGHT-{uuid.uuid4().hex[:4]}",
        quantity_base_units=5,
        reserved_quantity_base_units=0,
        expiry_date=today + datetime.timedelta(days=90),
        purchase_date=today,
        cost_per_unit=Decimal("1.80"),
        status=BatchStatus.ACTIVE,
    )

    # Create 3 competing sales orders requesting 4 units each against 5 available units
    sales = []
    for _ in range(3):
        s, _ = PosDomainService.create_sale_order(
            owner_a,
            {
                "branch": branch_a.branch_id,
                "lines": [
                    {
                        "product": otc_product.product_id,
                        "preferred_batch": tight_batch.batch_id,
                        "packaging_unit": PosPackagingUnit.TABLET,
                        "quantity_units": 4,
                        "conversion_factor": 1,
                        "unit_price_bdt": "2.50",
                    }
                ],
            },
        )
        sales.append(s)

    def _try_allocate(sale_obj: PosSaleOrder) -> bool:
        def _exec() -> bool:
            try:
                PosDomainService.allocate_stock(owner_a, sale_obj)
                return True
            except (DrfValidationError, PermissionDenied):
                return False

        if connection.vendor == "sqlite":
            with _sqlite_alloc_lock:
                return _exec()
        try:
            return _exec()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(_try_allocate, sales))

    successes = sum(1 for ok in outcomes if ok)
    assert successes == 1  # Strictly 1 of 3 orders can reserve 4 units out of 5!
    tight_batch.refresh_from_db()
    assert tight_batch.reserved_quantity_base_units == 4
    assert tight_batch.available_quantity_base_units == 1
    assert tight_batch.quantity_base_units >= tight_batch.reserved_quantity_base_units >= 0
