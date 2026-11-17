"""
backend/tests/test_p224_cart_prices_and_line_totals.py
Acceptance & integration tests for P224: Cart prices and line totals (Phase 12).

Covers:
1. Non-compounding line + header discounts (FLAT_BDT, PERCENTAGE, PROMO_POLICY) and
   deterministic rounding modes (NONE, HALF_UP_WHOLE_BDT, FLOOR_WHOLE_BDT, NEAREST_FIVE_BDT, MANUAL_ADJ).
2. EXCLUSIVE vs INCLUSIVE VAT math (net_before_tax + tax == total to the exact paisa)
   and BranchOperatingSettings.tax_defaults integration.
3. Reproducible server-side SHA-256 confirmation (pricing-breakdown + confirm-pricing),
   automatic confirmation invalidation on cart edits, ERR_PRICE_CONFIRMATION_MISMATCH on
   confirm & finalize, end-to-end checkout -> SaleInvoice totals & proportional reversal.
4. RBAC & anti-bypass guards: Cashier <=5% concession ceiling (blocking negative rounding
   bypass), Cashier manual rounding ceiling, Cashier unit_price_bdt override block
   (update-pricing, scan-add, cart-line), Manager approval & override attribution.
5. Invalid inputs, payment-floor reduction guard (ERR_PAYMENT_EXCEEDS_UPDATED_TOTAL),
   optimistic expected_version conflict, immutable post-finalization state, and
   append-only PosCartPricingAudit (including rejected audits saved outside rollback).
6. Tenant/branch isolation, Inventory Clerk denial, suspended tenant lock, and cost redaction.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    BranchOperatingSettings,
    LooseTabletSalesConfig,
    MedicineProduct,
    PackagingHierarchyNode,
    PlanTier,
    PosCartPricingAudit,
    PosDiscountType,
    PosPackagingUnit,
    PosRoundingMode,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosTaxMode,
    SaleInvoice,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.pos_cart_pricing_service import PosCartPricingService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p224-a-{uid}",
        name="Dhaka Care Pharmacy",
        subdomain=f"dhaka-p224-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p224-b-{uid}",
        name="Chattogram Rx",
        subdomain=f"ctg-p224-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_susp(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p224-susp-{uid}",
        name="Suspended P224 Pharmacy",
        subdomain=f"susp-p224-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.SUSPENDED,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p224-a-{uid}",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        license_number=f"DGDA-P224A-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p224-a2-{uid}",
        tenant=tenant_a,
        name="Mirpur Branch",
        license_number=f"DGDA-P224A2-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p224-b-{uid}",
        tenant=tenant_b,
        name="Agrabad Branch",
        license_number=f"DGDA-P224B-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_susp(tenant_susp):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p224-susp-{uid}",
        tenant=tenant_susp,
        name="Suspended Branch",
        license_number=f"DGDA-P224S-{uid}",
        is_active=True,
    )


def _mk_user(tenant, branch, role: str, tag: str) -> PharmacyUser:
    uid = uuid.uuid4().hex[:8]
    digits = "".join(str(ord(c) % 10) for c in uid)
    return PharmacyUser.objects.create_user(
        email=f"p224-{tag}-{uid}@pharmacy.bd",
        phone_number=f"+88018{digits}",
        password="Password123!",
        full_name=f"{tag.title()} User",
        role=role,
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id if branch else None,
    )


@pytest.fixture
def cashier_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.CASHIER, "cashier")


@pytest.fixture
def cashier_a2(tenant_a, branch_a2):
    return _mk_user(tenant_a, branch_a2, UserRole.CASHIER, "cashier2")


@pytest.fixture
def manager_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.BRANCH_MANAGER, "manager")


@pytest.fixture
def clerk_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.INVENTORY_CLERK, "clerk")


@pytest.fixture
def cashier_b(tenant_b, branch_b):
    return _mk_user(tenant_b, branch_b, UserRole.CASHIER, "cashierb")


@pytest.fixture
def cashier_susp(tenant_susp, branch_susp):
    return _mk_user(tenant_susp, branch_susp, UserRole.CASHIER, "cashiers")


@pytest.fixture
def catalog_fixture(tenant_a, branch_a):
    today = timezone.now().date()
    med_napa = MedicineProduct.objects.create(
        product_id=f"med-p224-napa-{uuid.uuid4().hex[:5]}",
        brand_name="Napa Extend 665mg",
        generic_name="Paracetamol",
        dosage_form="Tablet",
        strength="665mg",
        barcode=f"894224{uuid.uuid4().hex[:6]}",
        mrp_unit=Decimal("2.00"),
        cost_unit=Decimal("1.20"),
    )
    med_seclo = MedicineProduct.objects.create(
        product_id=f"med-p224-seclo-{uuid.uuid4().hex[:5]}",
        brand_name="Seclo 20mg",
        generic_name="Omeprazole",
        dosage_form="Capsule",
        strength="20mg",
        barcode=f"894225{uuid.uuid4().hex[:6]}",
        mrp_unit=Decimal("10.00"),
        cost_unit=Decimal("6.50"),
    )

    for prod, strip_mrp in [(med_napa, Decimal("20.00")), (med_seclo, Decimal("100.00"))]:
        uid = uuid.uuid4().hex[:5]
        PackagingHierarchyNode.objects.create(
            node_id=f"node-p224-strip-{uid}",
            tenant=tenant_a,
            branch=branch_a,
            product=prod,
            parent_unit="STRIP",
            child_unit="TABLET",
            conversion_factor=10,
            tier_level=1,
            is_base_level=True,
            effective_from=today - datetime.timedelta(days=5),
            is_active=True,
        )
        LooseTabletSalesConfig.objects.create(
            config_id=f"ltsc-p224-{uid}",
            tenant=tenant_a,
            branch=branch_a,
            product=prod,
            allow_loose_sales=True,
            min_loose_dispense_units=1,
            max_loose_dispense_units=100,
            strip_size_pieces=10,
            strip_mrp=strip_mrp,
            loose_unit_mrp=prod.mrp_unit,
            apply_loose_unit_surcharge=False,
            loose_surcharge_percentage=Decimal("0.00"),
        )
        BatchStock.objects.create(
            batch_id=f"bat-p224-{uid}",
            tenant=tenant_a,
            branch=branch_a,
            product=prod,
            batch_number=f"LOT-{uid.upper()}",
            expiry_date=today + datetime.timedelta(days=365),
            purchase_date=today,
            quantity_base_units=1000,
            reserved_quantity_base_units=0,
            cost_per_unit=prod.cost_unit,
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
    return {"napa": med_napa, "seclo": med_seclo}


def _open_cart_with_items(api_client, user, branch, catalog):
    api_client.force_authenticate(user=user)
    open_resp = api_client.post(
        "/api/v2/pos/sales/quick-cart/",
        {"branch": branch.branch_id, "force_new": True},
        format="json",
    )
    assert open_resp.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)
    sale = open_resp.data["sale"]

    # Add 2 STRIP of Napa (2 x 20.00 = 40.00)
    r1 = api_client.post(
        f"/api/v2/pos/sales/{sale['sale_id']}/scan-add/",
        {
            "product_id": catalog["napa"].product_id,
            "packaging_unit": PosPackagingUnit.STRIP,
            "quantity_delta": 2,
            "expected_version": sale["version"],
        },
        format="json",
    )
    assert r1.status_code == status.HTTP_200_OK
    sale = r1.data["sale"]

    # Add 1 STRIP of Seclo (1 x 100.00 = 100.00) -> Subtotal = 140.00
    r2 = api_client.post(
        f"/api/v2/pos/sales/{sale['sale_id']}/scan-add/",
        {
            "product_id": catalog["seclo"].product_id,
            "packaging_unit": PosPackagingUnit.STRIP,
            "quantity_delta": 1,
            "expected_version": sale["version"],
        },
        format="json",
    )
    assert r2.status_code == status.HTTP_200_OK
    return r2.data["sale"]


@pytest.mark.django_db
class TestP224CartPricesAndLineTotals:
    def test_1_line_and_header_discounts_idempotent_no_compounding_and_rounding_modes(
        self, api_client, branch_a, manager_a, catalog_fixture
    ):
        sale = _open_cart_with_items(api_client, manager_a, branch_a, catalog_fixture)
        line_napa = sale["lines"][0]   # gross 40.00
        line_seclo = sale["lines"][1]  # gross 100.00

        api_client.force_authenticate(user=manager_a)
        # Apply 10% line discount on Napa (4.00) + 5.00 flat line discount on Seclo + 10.00 header promo discount
        upd = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "discount_type": PosDiscountType.PROMO_POLICY,
                "header_discount_amount_bdt": "10.00",
                "discount_reason": "Eid health campaign",
                "expected_version": sale["version"],
                "line_adjustments": [
                    {
                        "line_id": line_napa["line_id"],
                        "discount_type": PosDiscountType.PERCENTAGE,
                        "discount_rate_pct": "10.00",
                    },
                    {
                        "line_id": line_seclo["line_id"],
                        "discount_type": PosDiscountType.FLAT_BDT,
                        "discount_amount_bdt": "5.00",
                    },
                ],
            },
            format="json",
        )
        assert upd.status_code == status.HTTP_200_OK, upd.data
        bd = upd.data["pricing_breakdown"]
        # Subtotal: 140.00; Line discounts: 4.00 + 5.00 = 9.00; Header discount: 10.00; Total discount: 19.00; Net: 121.00
        assert bd["subtotal_bdt"] == "140.00"
        assert bd["line_discount_total_bdt"] == "9.00"
        assert bd["header_discount_amount_bdt"] == "10.00"
        assert bd["discount_amount_bdt"] == "19.00"
        assert bd["grand_total_bdt"] == "121.00"
        first_hash = bd["calculated_pricing_hash"]

        # Recompute repeatedly in service: MUST NOT compound/double-count header discount!
        sale_obj = PosSaleOrder.objects.get(sale_id=sale["sale_id"])
        for _ in range(3):
            sale_obj, recomputed_bd = PosCartPricingService.recompute_sale_totals(sale_obj, manager_a)
            assert recomputed_bd["line_discount_total_bdt"] == "9.00"
            assert recomputed_bd["header_discount_amount_bdt"] == "10.00"
            assert recomputed_bd["discount_amount_bdt"] == "19.00"
            assert recomputed_bd["grand_total_bdt"] == "121.00"
            assert recomputed_bd["calculated_pricing_hash"] == first_hash

        # Test HALF_UP_WHOLE_BDT, FLOOR_WHOLE_BDT, NEAREST_FIVE_BDT on fractional total (apply 3.33% header disc on 121 -> wait reset to clean rate)
        upd_frac = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "discount_type": PosDiscountType.PERCENTAGE,
                "discount_rate_pct": "2.75",
                "rounding_mode": PosRoundingMode.NONE,
                "expected_version": upd.data["sale"]["version"],
            },
            format="json",
        )
        assert upd_frac.status_code == status.HTTP_200_OK
        # Net after line discounts = 140 - 9 = 131.00; 2.75% = 3.6025 -> 3.60; 131.00 - 3.60 = 127.40
        assert upd_frac.data["pricing_breakdown"]["grand_total_bdt"] == "127.40"

        # HALF_UP_WHOLE_BDT on 127.40 -> 127.00 (adj -0.40)
        r_half = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"rounding_mode": PosRoundingMode.HALF_UP_WHOLE_BDT, "expected_version": upd_frac.data["sale"]["version"]},
            format="json",
        )
        assert r_half.status_code == status.HTTP_200_OK
        assert r_half.data["pricing_breakdown"]["rounding_adjustment_bdt"] == "-0.40"
        assert r_half.data["pricing_breakdown"]["grand_total_bdt"] == "127.00"

        # NEAREST_FIVE_BDT on 127.40 -> 125.00 (adj -2.40)
        r_five = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"rounding_mode": PosRoundingMode.NEAREST_FIVE_BDT, "expected_version": r_half.data["sale"]["version"]},
            format="json",
        )
        assert r_five.status_code == status.HTTP_200_OK
        assert r_five.data["pricing_breakdown"]["rounding_adjustment_bdt"] == "-2.40"
        assert r_five.data["pricing_breakdown"]["grand_total_bdt"] == "125.00"

        # FLOOR_WHOLE_BDT on 127.40 -> 127.00
        r_floor = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"rounding_mode": PosRoundingMode.FLOOR_WHOLE_BDT, "expected_version": r_five.data["sale"]["version"]},
            format="json",
        )
        assert r_floor.status_code == status.HTTP_200_OK
        assert r_floor.data["pricing_breakdown"]["grand_total_bdt"] == "127.00"

    def test_2_inclusive_and_exclusive_tax_and_branch_operating_settings_defaults(
        self, api_client, tenant_a, branch_a, cashier_a, catalog_fixture
    ):
        BranchOperatingSettings.objects.update_or_create(
            tenant=tenant_a,
            branch=branch_a,
            defaults={
                "tax_defaults": {
                    "default_vat_rate": "5.00",
                    "is_vat_inclusive": True,
                    "tax_id_number": "BIN-123456789",
                }
            },
        )
        sale = _open_cart_with_items(api_client, cashier_a, branch_a, catalog_fixture)
        line_seclo = sale["lines"][1]  # 100.00 BDT

        # First test EXCLUSIVE 5% VAT at header level on 140.00 BDT -> net=140.00, tax=7.00, total=147.00
        api_client.force_authenticate(user=cashier_a)
        r_excl = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "tax_mode": PosTaxMode.EXCLUSIVE,
                "tax_rate_pct": "5.00",
                "expected_version": sale["version"],
            },
            format="json",
        )
        assert r_excl.status_code == status.HTTP_200_OK, r_excl.data
        bd_excl = r_excl.data["pricing_breakdown"]
        assert bd_excl["net_before_tax_bdt"] == "140.00"
        assert bd_excl["tax_amount_bdt"] == "7.00"
        assert bd_excl["grand_total_bdt"] == "147.00"
        assert Decimal(bd_excl["net_before_tax_bdt"]) + Decimal(bd_excl["tax_amount_bdt"]) == Decimal(bd_excl["grand_total_bdt"])

        # Now apply BranchOperatingSettings tax_defaults (5% INCLUSIVE):
        # Total remains 140.00 BDT; 140.00 / 1.05 = 133.33 net_before_tax, tax = 140.00 - 133.33 = 6.67!
        r_incl = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "apply_branch_tax_defaults": True,
                "expected_version": r_excl.data["sale"]["version"],
            },
            format="json",
        )
        assert r_incl.status_code == status.HTTP_200_OK, r_incl.data
        bd_incl = r_incl.data["pricing_breakdown"]
        assert bd_incl["tax_mode"] == PosTaxMode.INCLUSIVE
        assert bd_incl["tax_rate_pct"] == "5.00"
        assert bd_incl["grand_total_bdt"] == "140.00"
        assert bd_incl["net_before_tax_bdt"] == "133.33"
        assert bd_incl["tax_amount_bdt"] == "6.67"
        assert Decimal(bd_incl["net_before_tax_bdt"]) + Decimal(bd_incl["tax_amount_bdt"]) == Decimal(bd_incl["grand_total_bdt"])

        # Verify line-level inclusive tax precision too (clear header tax, set 5% INCLUSIVE on Seclo 100.00)
        r_line_tax = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "tax_rate_pct": "0.00",
                "expected_version": r_incl.data["sale"]["version"],
                "line_adjustments": [
                    {"line_id": line_seclo["line_id"], "tax_mode": PosTaxMode.INCLUSIVE, "tax_rate_pct": "5.00"}
                ],
            },
            format="json",
        )
        assert r_line_tax.status_code == status.HTTP_200_OK
        seclo_bd = r_line_tax.data["pricing_breakdown"]["lines"][1]
        assert seclo_bd["line_total_bdt"] == "100.00"
        assert seclo_bd["net_before_tax_bdt"] == "95.24"
        assert seclo_bd["line_tax_amount_bdt"] == "4.76"
        assert Decimal(seclo_bd["net_before_tax_bdt"]) + Decimal(seclo_bd["line_tax_amount_bdt"]) == Decimal(seclo_bd["line_total_bdt"])

        # GET pricing-breakdown exposes branch_tax_defaults metadata
        bd_get = api_client.get(f"/api/v2/pos/sales/{sale['sale_id']}/pricing-breakdown/")
        assert bd_get.status_code == status.HTTP_200_OK
        assert bd_get.data["branch_tax_defaults"]["default_vat_rate_pct"] == "5.00"
        assert bd_get.data["branch_tax_defaults"]["tax_id_number"] == "BIN-123456789"

    def test_3_reproducible_confirm_pricing_hash_invalidates_on_edit_and_checkout_reversal(
        self, api_client, branch_a, manager_a, cashier_a, catalog_fixture
    ):
        sale = _open_cart_with_items(api_client, cashier_a, branch_a, catalog_fixture)
        api_client.force_authenticate(user=cashier_a)

        # Confirm pricing mismatch rejection (stale expected_grand_total_bdt)
        bad_confirm = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/confirm-pricing/",
            {"expected_grand_total_bdt": "135.00", "expected_version": sale["version"]},
            format="json",
        )
        assert bad_confirm.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_confirm.data["error_code"] == "ERR_PRICE_CONFIRMATION_MISMATCH"
        # Rejected audit persisted despite rollback
        assert PosCartPricingAudit.objects.filter(
            sale_id=sale["sale_id"], was_rejected=True, rejection_code="ERR_PRICE_CONFIRMATION_MISMATCH"
        ).exists()

        # Breakdown hash is deterministic across repeated GET calls
        bd1 = api_client.get(f"/api/v2/pos/sales/{sale['sale_id']}/pricing-breakdown/")
        bd2 = api_client.get(f"/api/v2/pos/sales/{sale['sale_id']}/pricing-breakdown/")
        assert bd1.data["calculated_pricing_hash"] == bd2.data["calculated_pricing_hash"]
        calc_hash = bd1.data["calculated_pricing_hash"]

        # Confirm pricing with exact total & hash
        good_confirm = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/confirm-pricing/",
            {
                "expected_grand_total_bdt": "140.00",
                "expected_pricing_hash": calc_hash,
                "expected_version": sale["version"],
            },
            format="json",
        )
        assert good_confirm.status_code == status.HTTP_200_OK
        confirmed_sale = good_confirm.data["sale"]
        assert confirmed_sale["is_pricing_confirmed"] is True
        assert confirmed_sale["pricing_confirmation_hash"] == calc_hash

        # Cart edit invalidates prior confirmation hash automatically
        edit_qty = api_client.patch(
            f"/api/v2/pos/sales/{sale['sale_id']}/cart-line/",
            {"line_id": confirmed_sale["lines"][0]["line_id"], "delta": 1, "expected_version": confirmed_sale["version"]},
            format="json",
        )
        assert edit_qty.status_code == status.HTTP_200_OK
        edited_sale = edit_qty.data["sale"]
        # Napa becomes 3 strips (60.00) + Seclo 1 strip (100.00) = 160.00
        assert edited_sale["grand_total_bdt"] == "160.00"
        assert edited_sale["is_pricing_confirmed"] is False
        assert edited_sale["pricing_confirmation_hash"] == ""
        assert edited_sale["current_pricing_hash"] != calc_hash

        # Record payment for 160.00 and attempt finalize with OLD stale hash -> blocked!
        pay_resp = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/record-payment/",
            {"tender_type": "CASH", "amount_bdt": "160.00", "idempotency_key": f"pay-p224-{uuid.uuid4().hex[:6]}"},
            format="json",
        )
        assert pay_resp.status_code == status.HTTP_201_CREATED

        stale_fin = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/finalize/",
            {"expected_pricing_hash": calc_hash},
            format="json",
        )
        assert stale_fin.status_code == status.HTTP_400_BAD_REQUEST
        assert stale_fin.data["error_code"] == "ERR_PRICE_CONFIRMATION_MISMATCH"

        # Finalize with current hash succeeds & writes matching SaleInvoice
        ok_fin = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/finalize/",
            {
                "expected_grand_total_bdt": "160.00",
                "expected_pricing_hash": edited_sale["current_pricing_hash"],
            },
            format="json",
        )
        assert ok_fin.status_code == status.HTTP_200_OK, ok_fin.data
        assert ok_fin.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        assert ok_fin.data["is_pricing_confirmed"] is True

        inv = SaleInvoice.objects.get(invoice_id=ok_fin.data["linked_invoice_id"])
        assert inv.net_amount == Decimal("160.00")

        # Manager reverses finalized sale -> refunded_amount_bdt reconciles cleanly & stock restores
        api_client.force_authenticate(user=manager_a)
        rev = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/reverse/",
            {"reason": "Customer requested full cancellation after billing"},
            format="json",
        )
        assert rev.status_code == status.HTTP_200_OK
        assert rev.data["lifecycle_state"] == PosSaleLifecycleState.REVERSED
        assert rev.data["refunded_amount_bdt"] == "160.00"

    def test_4_cashier_discount_rounding_bypass_and_unit_price_override_permissions(
        self, api_client, branch_a, cashier_a, manager_a, catalog_fixture
    ):
        sale = _open_cart_with_items(api_client, cashier_a, branch_a, catalog_fixture)
        line_napa = sale["lines"][0]
        api_client.force_authenticate(user=cashier_a)

        # 1. Cashier <= 5% discount succeeds (5% of 140 = 7.00 -> 133.00)
        ok_disc = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"discount_type": PosDiscountType.PERCENTAGE, "discount_rate_pct": "5.00", "expected_version": sale["version"]},
            format="json",
        )
        assert ok_disc.status_code == status.HTTP_200_OK
        assert ok_disc.data["pricing_breakdown"]["grand_total_bdt"] == "133.00"

        # 2. Anti-bypass guard: Cashier 5% discount + -0.50 manual rounding (combined 5.36% > 5%) -> 403!
        bypass = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "discount_type": PosDiscountType.PERCENTAGE,
                "discount_rate_pct": "5.00",
                "rounding_mode": PosRoundingMode.MANUAL_ADJ,
                "rounding_adjustment_bdt": "-0.50",
                "expected_version": ok_disc.data["sale"]["version"],
            },
            format="json",
        )
        assert bypass.status_code == status.HTTP_403_FORBIDDEN
        assert bypass.data["error_code"] == "ERR_CASHIER_DISCOUNT_LIMIT_EXCEEDED"

        # 3. Cashier > 0.99 manual rounding (e.g. +2.00) -> 403 ERR_ROUNDING_APPROVAL_REQUIRED
        bad_round_cashier = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "rounding_mode": PosRoundingMode.MANUAL_ADJ,
                "rounding_adjustment_bdt": "2.00",
                "expected_version": ok_disc.data["sale"]["version"],
            },
            format="json",
        )
        assert bad_round_cashier.status_code == status.HTTP_403_FORBIDDEN
        assert bad_round_cashier.data["error_code"] == "ERR_ROUNDING_APPROVAL_REQUIRED"

        # 4. Cashier overriding unit_price_bdt via update-pricing -> 403 ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL
        override_upd = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "expected_version": ok_disc.data["sale"]["version"],
                "line_adjustments": [{"line_id": line_napa["line_id"], "unit_price_bdt": "5.00"}],
            },
            format="json",
        )
        assert override_upd.status_code == status.HTTP_403_FORBIDDEN
        assert override_upd.data["error_code"] == "ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL"

        # 5. Cashier overriding unit_price_bdt via cart-line & scan-add -> 403 ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL
        override_patch = api_client.patch(
            f"/api/v2/pos/sales/{sale['sale_id']}/cart-line/",
            {"line_id": line_napa["line_id"], "unit_price_bdt": "5.00"},
            format="json",
        )
        assert override_patch.status_code == status.HTTP_403_FORBIDDEN
        assert override_patch.data["error_code"] == "ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL"

        override_scan = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/scan-add/",
            {
                "product_id": catalog_fixture["napa"].product_id,
                "packaging_unit": PosPackagingUnit.TABLET,
                "unit_price_bdt": "0.50",
            },
            format="json",
        )
        assert override_scan.status_code == status.HTTP_403_FORBIDDEN
        assert override_scan.data["error_code"] == "ERR_PRICE_OVERRIDE_REQUIRES_APPROVAL"

        # 6. Branch Manager overrides price & grants 15% discount -> 200 OK + attribution saved
        api_client.force_authenticate(user=manager_a)
        mgr_ok = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {
                "discount_type": PosDiscountType.PERCENTAGE,
                "discount_rate_pct": "15.00",
                "discount_reason": "Corporate clinic contract",
                "expected_version": ok_disc.data["sale"]["version"],
                "line_adjustments": [
                    {
                        "line_id": line_napa["line_id"],
                        "unit_price_bdt": "25.00",
                        "override_reason": "Batch special MRP",
                    }
                ],
            },
            format="json",
        )
        assert mgr_ok.status_code == status.HTTP_200_OK, mgr_ok.data
        # Napa becomes 2 * 25.00 = 50.00 + Seclo 100.00 = 150.00 subtotal; 15% discount = 22.50; net = 127.50
        assert mgr_ok.data["pricing_breakdown"]["subtotal_bdt"] == "150.00"
        assert mgr_ok.data["pricing_breakdown"]["discount_amount_bdt"] == "22.50"
        assert mgr_ok.data["pricing_breakdown"]["grand_total_bdt"] == "127.50"
        assert mgr_ok.data["sale"]["lines"][0]["price_source"] == "MANAGER_OVERRIDE"
        assert mgr_ok.data["sale"]["lines"][0]["price_override_approved_by_email"] == manager_a.email

    def test_5_invalid_inputs_payment_floor_guard_version_conflict_and_immutable_audits(
        self, api_client, branch_a, manager_a, catalog_fixture
    ):
        sale = _open_cart_with_items(api_client, manager_a, branch_a, catalog_fixture)
        api_client.force_authenticate(user=manager_a)

        # Negative flat discount rejected
        neg_disc = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"discount_type": PosDiscountType.FLAT_BDT, "header_discount_amount_bdt": "-10.00"},
            format="json",
        )
        assert neg_disc.status_code == status.HTTP_400_BAD_REQUEST

        # Float discount rejected (strict Decimal only)
        flt_disc = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"discount_type": PosDiscountType.PERCENTAGE, "discount_rate_pct": 5.5},
            format="json",
        )
        assert flt_disc.status_code == status.HTTP_400_BAD_REQUEST

        # Manual rounding out of +/-5.00 bounds rejected
        out_round = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"rounding_mode": PosRoundingMode.MANUAL_ADJ, "rounding_adjustment_bdt": "-15.00"},
            format="json",
        )
        assert out_round.status_code == status.HTTP_400_BAD_REQUEST
        assert out_round.data["error_code"] == "ERR_ROUNDING_BOUNDS_EXCEEDED"

        # Stale expected_version rejected
        stale_ver = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"discount_type": PosDiscountType.FLAT_BDT, "header_discount_amount_bdt": "5.00", "expected_version": 999},
            format="json",
        )
        assert stale_ver.status_code == status.HTTP_400_BAD_REQUEST
        assert stale_ver.data["error_code"] == "ERR_CART_VERSION_CONFLICT"

        # Record 130.00 non-cash (bKash) payment, then try dropping grand total to 100.00 -> blocked!
        pay_bkash = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/record-payment/",
            {"tender_type": "BKASH", "amount_bdt": "130.00", "idempotency_key": f"bk-{uuid.uuid4().hex[:6]}"},
            format="json",
        )
        assert pay_bkash.status_code == status.HTTP_201_CREATED

        drop_below = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"discount_type": PosDiscountType.FLAT_BDT, "header_discount_amount_bdt": "40.00"},
            format="json",
        )
        assert drop_below.status_code == status.HTTP_400_BAD_REQUEST
        assert drop_below.data["error_code"] == "ERR_PAYMENT_EXCEEDS_UPDATED_TOTAL"

        # Check PosCartPricingAudit immutability & pricing-audits endpoint
        audits_resp = api_client.get(f"/api/v2/pos/sales/{sale['sale_id']}/pricing-audits/")
        assert audits_resp.status_code == status.HTTP_200_OK
        assert len(audits_resp.data) >= 3  # rejected bounds, version conflict, payment floor

        audit_obj = PosCartPricingAudit.objects.filter(sale_id=sale["sale_id"]).first()
        with pytest.raises(DjangoValidationError):
            audit_obj.grand_total_bdt = Decimal("999.00")
            audit_obj.save()
        with pytest.raises(DjangoValidationError):
            audit_obj.delete()

        # Finalize sale then verify pricing mutation blocked (ERR_IMMUTABLE_SALE_STATE)
        pay_cash = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/record-payment/",
            {"tender_type": "CASH", "amount_bdt": "10.00", "idempotency_key": f"csh-{uuid.uuid4().hex[:6]}"},
            format="json",
        )
        assert pay_cash.status_code == status.HTTP_201_CREATED
        fin = api_client.post(f"/api/v2/pos/sales/{sale['sale_id']}/finalize/", {}, format="json")
        assert fin.status_code == status.HTTP_200_OK

        post_fin_edit = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"discount_type": PosDiscountType.FLAT_BDT, "header_discount_amount_bdt": "5.00"},
            format="json",
        )
        assert post_fin_edit.status_code == status.HTTP_400_BAD_REQUEST
        assert post_fin_edit.data["error_code"] == "ERR_IMMUTABLE_SALE_STATE"

    def test_6_tenant_branch_rbac_subscription_and_cost_redaction_isolation(
        self, api_client, branch_a, branch_susp, cashier_a, cashier_a2, clerk_a, cashier_b, cashier_susp, catalog_fixture
    ):
        sale = _open_cart_with_items(api_client, cashier_a, branch_a, catalog_fixture)

        # Cashier response redacts unit_cost_bdt
        api_client.force_authenticate(user=cashier_a)
        r_cashier_get = api_client.get(f"/api/v2/pos/sales/{sale['sale_id']}/")
        assert r_cashier_get.status_code == status.HTTP_200_OK
        assert r_cashier_get.data["lines"][0]["unit_cost_bdt"] is None

        # Inventory Clerk denied pricing mutation and confirmation
        api_client.force_authenticate(user=clerk_a)
        assert api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/update-pricing/",
            {"discount_type": PosDiscountType.FLAT_BDT, "header_discount_amount_bdt": "2.00"},
            format="json",
        ).status_code == status.HTTP_403_FORBIDDEN
        assert api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/confirm-pricing/", {}, format="json"
        ).status_code == status.HTTP_403_FORBIDDEN

        # Sibling branch Cashier denied (branch scoped -> 403 or 404 depending on queryset filter)
        api_client.force_authenticate(user=cashier_a2)
        sib = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/confirm-pricing/", {}, format="json"
        )
        assert sib.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        # Cross-tenant Cashier blocked
        api_client.force_authenticate(user=cashier_b)
        xt = api_client.get(f"/api/v2/pos/sales/{sale['sale_id']}/pricing-breakdown/")
        assert xt.status_code == status.HTTP_404_NOT_FOUND

        # Suspended tenant blocked (403 middleware or domain guard)
        api_client.force_authenticate(user=cashier_susp)
        susp_resp = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {"branch": branch_susp.branch_id},
            format="json",
        )
        assert susp_resp.status_code == status.HTTP_403_FORBIDDEN
        body = susp_resp.json() if hasattr(susp_resp, "json") else susp_resp.data
        assert body["error_code"] in (
            "ERR_SUBSCRIPTION_SUSPENDED",
            "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
        )
