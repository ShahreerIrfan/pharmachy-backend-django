"""
backend/tests/test_p223_cart_item_unit_selection.py
Acceptance & integration tests for P223: Cart item unit selection (Phase 12).

Covers:
1. Effective packaging hierarchy resolution (TABLET/STRIP/BOX), exact integer base-unit
   conversion (KEEP_QUANTITY & CONVERT_PRESERVE_BASE), strict Decimal MRP & loose surcharge,
   fractional/float/boolean rejection, exact remainder block (ERR_INEXACT_BASE_UNIT_CONVERSION),
   and anti-factor-spoofing validation (ERR_INVALID_UNIT_CONVERSION).
2. Container & safety guardrails: NonDivisiblePackPolicy hard block (ERR_NON_DIVISIBLE_PACK),
   conditional Pharmacist/Manager split sign-off (403 ERR_PHARMACIST_SPLIT_APPROVAL_REQUIRED for Cashier
   vs 200 approved override for Pharmacist with rejected/approved PosCartUnitSelectionAudit persistence),
   LooseTabletSalesConfig (prohibited + min/max dispense limits + surcharge), dosage-form incompatibility
   (ERR_INCOMPATIBLE_DOSING_UNIT), Cashier cost redaction, and audit log immutability.
3. End-to-end checkout integration, FEFO allocation release/re-reserve on unit switch, reversal
   restoring exact base units, immutable state guard (ERR_IMMUTABLE_SALE_STATE), optimistic version
   conflict (ERR_CART_VERSION_CONFLICT), branch/tenant isolation, and suspended tenant block.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    LooseTabletSalesConfig,
    MedicineProduct,
    MedicineVariant,
    NonDivisibleContainerType,
    NonDivisiblePackPolicy,
    PackagingHierarchyNode,
    PlanTier,
    PosCartUnitSelectionAudit,
    PosPackagingUnit,
    PosSaleLifecycleState,
    PosSaleOrderLine,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p223-a-{uid}",
        name="Dhaka Care Pharmacy",
        subdomain=f"dhaka-p223-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p223-b-{uid}",
        name="Sylhet Medico",
        subdomain=f"sylhet-p223-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_susp(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p223-susp-{uid}",
        name="Suspended P223 Pharmacy",
        subdomain=f"susp-p223-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.SUSPENDED,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p223-a-{uid}",
        tenant=tenant_a,
        name="Banani Main Branch",
        license_number=f"DGDA-P223A-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p223-a2-{uid}",
        tenant=tenant_a,
        name="Uttara Branch",
        license_number=f"DGDA-P223A2-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p223-b-{uid}",
        tenant=tenant_b,
        name="Sylhet Central",
        license_number=f"DGDA-P223B-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_susp(tenant_susp):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p223-s-{uid}",
        tenant=tenant_susp,
        name="Suspended Branch",
        license_number=f"DGDA-P223S-{uid}",
        is_active=True,
    )


def _mk_user(tenant, branch, role: str, tag: str) -> PharmacyUser:
    uid = uuid.uuid4().hex[:8]
    digits = "".join(str(ord(c) % 10) for c in uid)
    return PharmacyUser.objects.create_user(
        email=f"p223-{tag}-{uid}@pharmacy.bd",
        phone_number=f"+88017{digits}",
        password="P223TestPass!2026",
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id if branch else None,
        role=role,
        full_name=f"{tag.title()} User",
    )


@pytest.fixture
def owner_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.PHARMACY_OWNER, "owner")


@pytest.fixture
def manager_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.BRANCH_MANAGER, "manager")


@pytest.fixture
def pharmacist_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.STAFF_PHARMACIST, "pharm")


@pytest.fixture
def cashier_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.CASHIER, "cashier")


@pytest.fixture
def clerk_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.INVENTORY_CLERK, "clerk")


@pytest.fixture
def owner_b(tenant_b, branch_b):
    return _mk_user(tenant_b, branch_b, UserRole.PHARMACY_OWNER, "ownerb")


@pytest.fixture
def owner_susp(tenant_susp, branch_susp):
    return _mk_user(tenant_susp, branch_susp, UserRole.PHARMACY_OWNER, "ownersusp")


@pytest.fixture
def napa_extend_product(tenant_a, branch_a):
    uid = uuid.uuid4().hex[:6]
    prod = MedicineProduct.objects.create(
        product_id=f"prod-p223-napa-{uid}",
        brand_name="Napa Extend",
        brand_name_bn="নাপা এক্সটেন্ড",
        generic_name="Paracetamol 665mg",
        generic_name_bn="প্যারাসিটামল ৬৬৫মিগ্রা",
        dosage_form="Tablet",
        strength="665mg",
        manufacturer="Beximco",
        barcode=f"8941100{uid}",
        mrp_unit=Decimal("2.00"),
        cost_unit=Decimal("1.10"),
    )
    var = MedicineVariant.objects.create(
        variant_id=f"var-p223-napa-{uid}",
        product=prod,
        sku=f"SKU-NAPA-{uid}",
        variant_name="10x10 Box",
        strength="665mg",
        pack_size="10x10",
        pack_size_numeric=100,
        mrp_unit=Decimal("2.00"),
        cost_unit=Decimal("1.10"),
        mrp_pack=Decimal("185.00"),
        cost_pack=Decimal("110.00"),
        barcode=f"8941101{uid}",
        is_active=True,
    )
    today = timezone.now().date()
    PackagingHierarchyNode.objects.create(
        node_id=f"node-p223-strip-{uid}",
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
    PackagingHierarchyNode.objects.create(
        node_id=f"node-p223-box-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=prod,
        parent_unit="BOX",
        child_unit="STRIP",
        conversion_factor=10,
        tier_level=2,
        is_base_level=False,
        effective_from=today - datetime.timedelta(days=5),
        is_active=True,
    )
    LooseTabletSalesConfig.objects.create(
        config_id=f"ltsc-p223-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=prod,
        allow_loose_sales=True,
        min_loose_dispense_units=1,
        max_loose_dispense_units=100,
        strip_size_pieces=10,
        strip_mrp=Decimal("19.00"),
        loose_unit_mrp=Decimal("2.00"),
        apply_loose_unit_surcharge=False,
        loose_surcharge_percentage=Decimal("0.00"),
    )
    BatchStock.objects.create(
        batch_id=f"bat-p223-napa-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=prod,
        batch_number=f"NP-{uid}",
        quantity_base_units=250,
        reserved_quantity_base_units=0,
        expiry_date=today + datetime.timedelta(days=365),
        purchase_date=today,
        cost_per_unit=Decimal("1.10"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )
    return prod, var


@pytest.mark.django_db
def test_1_packaging_hierarchy_unit_selection_conversion_surcharge_and_validation(
    api_client, cashier_a, manager_a, tenant_a, branch_a, napa_extend_product
):
    prod, var = napa_extend_product
    api_client.force_authenticate(user=cashier_a)

    # 1a. Lookup search unit options before creating cart
    opt_resp = api_client.get(
        f"/api/v2/pos/search/unit-options/?branch={branch_a.branch_id}&product_id={prod.product_id}&variant_id={var.variant_id}"
    )
    assert opt_resp.status_code == status.HTTP_200_OK
    assert opt_resp.data["available_base_units"] == 250
    assert opt_resp.data["normalized_dosage_form"] == "TABLET"
    opts_by_code = {o["packaging_unit"]: o for o in opt_resp.data["packaging_options"]}
    assert opts_by_code[PosPackagingUnit.TABLET]["conversion_factor"] == 1
    assert opts_by_code[PosPackagingUnit.TABLET]["unit_price_bdt"] == "2.00"
    assert opts_by_code[PosPackagingUnit.STRIP]["conversion_factor"] == 10
    assert opts_by_code[PosPackagingUnit.STRIP]["unit_price_bdt"] == "19.00"
    assert opts_by_code[PosPackagingUnit.BOX]["conversion_factor"] == 100
    assert opts_by_code[PosPackagingUnit.BOX]["unit_price_bdt"] == "185.00"
    assert all(o["is_allowed"] for o in opt_resp.data["packaging_options"])

    # 1b. Open quick-cart & add 20 TABLET
    cart_resp = api_client.post(
        "/api/v2/pos/sales/quick-cart/",
        {"branch": branch_a.branch_id, "force_new": True},
        format="json",
    )
    assert cart_resp.status_code == status.HTTP_201_CREATED
    sale_id = cart_resp.data["sale"]["sale_id"]

    add_resp = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/scan-add/",
        {"product_id": prod.product_id, "packaging_unit": "TABLET", "quantity_delta": 20},
        format="json",
    )
    assert add_resp.status_code == status.HTTP_200_OK
    line = add_resp.data["sale"]["lines"][0]
    line_id = line["line_id"]
    assert line["base_units_total"] == 20
    assert line["unit_cost_bdt"] is None  # Cashier cost masking preserved
    assert add_resp.data["sale"]["subtotal_bdt"] == "40.00"

    # Sale-level unit-options check
    sale_opts = api_client.get(f"/api/v2/pos/sales/{sale_id}/unit-options/?line_id={line_id}")
    assert sale_opts.status_code == status.HTTP_200_OK
    assert sale_opts.data["current_packaging_unit"] == "TABLET"
    assert sale_opts.data["current_base_units_total"] == 20

    # 1c. Switch 20 TABLET -> STRIP preserving exact base units (20 / 10 = 2 STRIP)
    ver_before = add_resp.data["sale"]["version"]
    sel_strip = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {
            "line_id": line_id,
            "packaging_unit": "STRIP",
            "conversion_mode": "CONVERT_PRESERVE_BASE",
            "expected_version": ver_before,
        },
        format="json",
    )
    assert sel_strip.status_code == status.HTTP_200_OK
    upd_line = sel_strip.data["sale"]["lines"][0]
    assert upd_line["packaging_unit"] == "STRIP"
    assert upd_line["quantity_units"] == 2
    assert upd_line["conversion_factor"] == 10
    assert upd_line["base_units_total"] == 20
    assert upd_line["unit_price_bdt"] == "19.00"
    assert upd_line["dosage_form_snapshot"] == "Tablet"
    assert sel_strip.data["sale"]["subtotal_bdt"] == "38.00"
    assert sel_strip.data["sale"]["version"] == ver_before + 1
    assert sel_strip.data["unit_audit"]["from_unit"] == "TABLET"
    assert sel_strip.data["unit_audit"]["to_unit"] == "STRIP"
    assert len(sel_strip.data["unit_audit"]["sha256_hash"]) == 64

    # 1d. Upgrade 2 STRIP -> 2 BOX (KEEP_QUANTITY -> 200 base units, MRP 185.00 * 2 = 370.00)
    sel_box = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "BOX", "conversion_mode": "KEEP_QUANTITY"},
        format="json",
    )
    assert sel_box.status_code == status.HTTP_200_OK
    box_line = sel_box.data["sale"]["lines"][0]
    assert box_line["packaging_unit"] == "BOX"
    assert box_line["quantity_units"] == 2
    assert box_line["conversion_factor"] == 100
    assert box_line["base_units_total"] == 200
    assert sel_box.data["sale"]["subtotal_bdt"] == "370.00"

    # 1e. Inexact base-unit remainder on CONVERT_PRESERVE_BASE (set qty=15 TABLET, try converting -> STRIP)
    p223_switch_tab = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "TABLET", "quantity_units": 15},
        format="json",
    )
    assert p223_switch_tab.status_code == status.HTTP_200_OK

    inexact = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "STRIP", "conversion_mode": "CONVERT_PRESERVE_BASE"},
        format="json",
    )
    assert inexact.status_code == status.HTTP_400_BAD_REQUEST
    assert inexact.data["error_code"] == "ERR_INEXACT_BASE_UNIT_CONVERSION"

    # 1f. Stock excess rejection (3 BOX = 300 > 250 sellable)
    excess = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "BOX", "quantity_units": 3},
        format="json",
    )
    assert excess.status_code == status.HTTP_400_BAD_REQUEST
    assert excess.data["error_code"] == "ERR_INSUFFICIENT_SELLABLE_STOCK"

    # 1g. Strict integer rejection (float 1.5, fractional string "1.5", bool True, 0) & factor spoofing
    for bad_qty in (1.5, "1.5", "1.0", True, 0, -2):
        r_bad = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/select-unit/",
            {"line_id": line_id, "packaging_unit": "TABLET", "quantity_units": bad_qty},
            format="json",
        )
        assert r_bad.status_code == status.HTTP_400_BAD_REQUEST
        assert r_bad.data["error_code"] in ("ERR_FRACTIONAL_UNIT_PROHIBITED", "ERR_INVALID_UNIT_QUANTITY")

    spoof_factor = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "STRIP", "conversion_factor": 12, "quantity_units": 1},
        format="json",
    )
    assert spoof_factor.status_code == status.HTTP_400_BAD_REQUEST
    assert spoof_factor.data["error_code"] == "ERR_INVALID_UNIT_CONVERSION"

    spoof_box = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "BOX", "conversion_factor": 1, "quantity_units": 1},
        format="json",
    )
    assert spoof_box.status_code == status.HTTP_400_BAD_REQUEST
    assert spoof_box.data["error_code"] == "ERR_INVALID_UNIT_CONVERSION"

    # 1h. LooseTabletSalesConfig bounds & 10% surcharge check
    ltsc = LooseTabletSalesConfig.objects.get(tenant=tenant_a, branch=branch_a, product=prod)
    ltsc.allow_loose_sales = True
    ltsc.min_loose_dispense_units = 2
    ltsc.max_loose_dispense_units = 8
    ltsc.apply_loose_unit_surcharge = True
    ltsc.loose_surcharge_percentage = Decimal("10.00")
    ltsc.save()
    surcharge_ok = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "TABLET", "quantity_units": 4},
        format="json",
    )
    assert surcharge_ok.status_code == status.HTTP_200_OK
    assert surcharge_ok.data["sale"]["lines"][0]["unit_price_bdt"] == "2.20"
    assert surcharge_ok.data["sale"]["lines"][0]["container_policy_applied"] == "LOOSE_SURCHARGE_APPLIED"

    below_min = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "TABLET", "quantity_units": 1},
        format="json",
    )
    assert below_min.status_code == status.HTTP_400_BAD_REQUEST
    assert below_min.data["error_code"] == "ERR_BELOW_MIN_LOOSE_DISPENSE"

    above_max = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "TABLET", "quantity_units": 9},
        format="json",
    )
    assert above_max.status_code == status.HTTP_400_BAD_REQUEST
    assert above_max.data["error_code"] == "ERR_EXCEEDS_MAX_LOOSE_DISPENSE"

    ltsc.allow_loose_sales = False
    ltsc.prohibition_reason = "Moisture-sensitive blister strip only"
    ltsc.save()
    loose_blocked = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "TABLET", "quantity_units": 4},
        format="json",
    )
    assert loose_blocked.status_code == status.HTTP_400_BAD_REQUEST
    assert loose_blocked.data["error_code"] == "ERR_LOOSE_TABLET_SALES_PROHIBITED"

    # Manager inspects unit audits
    api_client.force_authenticate(user=manager_a)
    audit_resp = api_client.get(f"/api/v2/pos/sales/{sale_id}/unit-audits/")
    assert audit_resp.status_code == status.HTTP_200_OK
    codes = {a["rejection_code"] for a in audit_resp.data if a["was_rejected"]}
    assert "ERR_INEXACT_BASE_UNIT_CONVERSION" in codes
    assert "ERR_LOOSE_TABLET_SALES_PROHIBITED" in codes
    assert "ERR_INSUFFICIENT_SELLABLE_STOCK" in codes


@pytest.mark.django_db
def test_2_container_restrictions_pharmacist_override_dosage_form_and_audit_immutability(
    api_client, cashier_a, pharmacist_a, clerk_a, tenant_a, branch_a
):
    today = timezone.now().date()
    uid = uuid.uuid4().hex[:6]

    # Product A: Sterile Injection Vial Box with conditional split (Pharmacist sign-off required)
    vial_prod = MedicineProduct.objects.create(
        product_id=f"prod-p223-vial-{uid}",
        brand_name="Ceftron IV 1g",
        generic_name="Ceftriaxone",
        dosage_form="Injection",
        strength="1g",
        manufacturer="Square",
        mrp_unit=Decimal("180.00"),
        cost_unit=Decimal("120.00"),
    )
    NonDivisiblePackPolicy.objects.create(
        policy_id=f"nd-p223-vial-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=vial_prod,
        container_type=NonDivisibleContainerType.STERILE_VIAL_BOX,
        is_splitting_allowed=True,
        require_pharmacist_approval=True,
        approved_break_factor=10,
        prohibition_reason="Sterile vial box break requires pharmacist verification",
    )
    BatchStock.objects.create(
        batch_id=f"bat-p223-vial-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=vial_prod,
        batch_number=f"CF-{uid}",
        quantity_base_units=40,
        expiry_date=today + datetime.timedelta(days=200),
        purchase_date=today,
        cost_per_unit=Decimal("120.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )

    # Product B: Syrup Bottle (incompatible with TABLET/STRIP dosing unit)
    syrup_prod = MedicineProduct.objects.create(
        product_id=f"prod-p223-syr-{uid}",
        brand_name="Alatrol Syrup",
        generic_name="Cetirizine",
        dosage_form="Syrup",
        strength="5mg/5ml",
        manufacturer="Square",
        mrp_unit=Decimal("45.00"),
        cost_unit=Decimal("28.00"),
    )
    BatchStock.objects.create(
        batch_id=f"bat-p223-syr-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=syrup_prod,
        batch_number=f"AL-{uid}",
        quantity_base_units=36,
        expiry_date=today + datetime.timedelta(days=200),
        purchase_date=today,
        cost_per_unit=Decimal("28.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )

    # Product C: Hard NonDivisible Insulin Pen Box (is_splitting_allowed=False)
    insulin_prod = MedicineProduct.objects.create(
        product_id=f"prod-p223-ins-{uid}",
        brand_name="Lantus SoloStar",
        generic_name="Insulin Glargine",
        dosage_form="Injection",
        strength="100IU/ml",
        manufacturer="Sanofi",
        mrp_unit=Decimal("900.00"),
        cost_unit=Decimal("650.00"),
    )
    NonDivisiblePackPolicy.objects.create(
        policy_id=f"nd-p223-ins-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=insulin_prod,
        container_type=NonDivisibleContainerType.SEALED_BOX,
        is_splitting_allowed=False,
        prohibition_reason="Manufacturer sealed cold-chain box only",
    )
    BatchStock.objects.create(
        batch_id=f"bat-p223-ins-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=insulin_prod,
        batch_number=f"LN-{uid}",
        quantity_base_units=20,
        expiry_date=today + datetime.timedelta(days=200),
        purchase_date=today,
        cost_per_unit=Decimal("650.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )

    # Open cashier cart with BOX of vial, BOTTLE of syrup, BOX of insulin
    api_client.force_authenticate(user=cashier_a)
    cart_resp = api_client.post(
        "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id, "force_new": True}, format="json"
    )
    sale_id = cart_resp.data["sale"]["sale_id"]

    r1 = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/scan-add/",
        {"product_id": vial_prod.product_id, "packaging_unit": "BOX", "quantity_delta": 1},
        format="json",
    )
    r2 = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/scan-add/",
        {"product_id": syrup_prod.product_id, "packaging_unit": "BOTTLE", "quantity_delta": 1},
        format="json",
    )
    r3 = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/scan-add/",
        {"product_id": insulin_prod.product_id, "packaging_unit": "BOX", "quantity_delta": 1},
        format="json",
    )
    assert r1.status_code == r2.status_code == r3.status_code == status.HTTP_200_OK
    lines = r3.data["sale"]["lines"]
    vial_line = next(l for l in lines if l["product_id"] == vial_prod.product_id)
    syrup_line = next(l for l in lines if l["product_id"] == syrup_prod.product_id)
    insulin_line = next(l for l in lines if l["product_id"] == insulin_prod.product_id)

    # Cashier unit-options shows VIAL blocked pending pharmacist approval; BOX allowed
    cashier_opts = api_client.get(
        f"/api/v2/pos/sales/{sale_id}/unit-options/?line_id={vial_line['line_id']}"
    )
    assert cashier_opts.status_code == status.HTTP_200_OK
    vial_opt_map = {o["packaging_unit"]: o for o in cashier_opts.data["packaging_options"]}
    assert vial_opt_map["VIAL"]["is_allowed"] is False
    assert vial_opt_map["VIAL"]["requires_pharmacist_approval"] is True
    assert vial_opt_map["VIAL"]["block_error_code"] == "ERR_PHARMACIST_SPLIT_APPROVAL_REQUIRED"

    # Cashier blocked 403 trying to split vial box -> must STILL persist rejected audit outside rollback
    cashier_split = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": vial_line["line_id"], "packaging_unit": "VIAL", "quantity_units": 2},
        format="json",
    )
    assert cashier_split.status_code == status.HTTP_403_FORBIDDEN
    assert cashier_split.data["error_code"] == "ERR_PHARMACIST_SPLIT_APPROVAL_REQUIRED"
    assert PosCartUnitSelectionAudit.objects.filter(
        sale_id=sale_id,
        was_rejected=True,
        rejection_code="ERR_PHARMACIST_SPLIT_APPROVAL_REQUIRED",
    ).exists()

    # Syrup rejects TABLET with ERR_INCOMPATIBLE_DOSING_UNIT
    syrup_bad = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": syrup_line["line_id"], "packaging_unit": "TABLET", "quantity_units": 1},
        format="json",
    )
    assert syrup_bad.status_code == status.HTTP_400_BAD_REQUEST
    assert syrup_bad.data["error_code"] == "ERR_INCOMPATIBLE_DOSING_UNIT"

    # Insulin hard NonDivisible blocks splitting regardless of role
    api_client.force_authenticate(user=pharmacist_a)
    ins_bad = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": insulin_line["line_id"], "packaging_unit": "VIAL", "quantity_units": 1},
        format="json",
    )
    assert ins_bad.status_code == status.HTTP_400_BAD_REQUEST
    assert ins_bad.data["error_code"] == "ERR_NON_DIVISIBLE_PACK"

    # Pharmacist approves splitting sterile vial box cleanly
    pharm_opts = api_client.get(f"/api/v2/pos/sales/{sale_id}/unit-options/?line_id={vial_line['line_id']}")
    assert {o["packaging_unit"]: o for o in pharm_opts.data["packaging_options"]}["VIAL"]["is_allowed"] is True

    pharm_split = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {
            "line_id": vial_line["line_id"],
            "packaging_unit": "VIAL",
            "quantity_units": 2,
            "override_reason": "Single IV emergency dose",
        },
        format="json",
    )
    assert pharm_split.status_code == status.HTTP_200_OK
    vial_upd = next(l for l in pharm_split.data["sale"]["lines"] if l["line_id"] == vial_line["line_id"])
    assert vial_upd["packaging_unit"] == "VIAL"
    assert vial_upd["container_policy_applied"] == "PHARMACIST_OVERRIDE_APPROVED"
    assert vial_upd["container_override_approved_by_email"] == pharmacist_a.email
    assert vial_upd["container_override_reason"] == "Single IV emergency dose"

    # InventoryClerk denied access to select-unit
    api_client.force_authenticate(user=clerk_a)
    clerk_deny = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": syrup_line["line_id"], "packaging_unit": "BOTTLE", "quantity_units": 2},
        format="json",
    )
    assert clerk_deny.status_code == status.HTTP_403_FORBIDDEN

    # Audit immutability & PosSaleOrderLine DB CheckConstraint check
    audit_obj = PosCartUnitSelectionAudit.objects.filter(sale_id=sale_id, was_rejected=False).first()
    assert audit_obj is not None
    with pytest.raises(DjangoValidationError):
        audit_obj.to_unit = "BOX"
        audit_obj.save()
    with pytest.raises(DjangoValidationError):
        audit_obj.delete()

    # Verify DB check constraint prevents corrupt base_units_total mismatch
    db_line = PosSaleOrderLine.objects.get(line_id=vial_line["line_id"])
    with pytest.raises((DjangoValidationError, IntegrityError)):
        with transaction.atomic():
            PosSaleOrderLine.objects.filter(pk=db_line.pk).update(base_units_total=999)


@pytest.mark.django_db
def test_3_allocation_release_checkout_reversal_immutable_state_and_isolation(
    api_client,
    cashier_a,
    manager_a,
    owner_b,
    owner_susp,
    tenant_a,
    branch_a,
    branch_a2,
    branch_b,
    branch_susp,
    napa_extend_product,
):
    prod, _ = napa_extend_product
    api_client.force_authenticate(user=manager_a)

    # 3a. Open cart, add 1 BOX (100u), allocate -> verify BatchStock reserves 100
    cart_resp = api_client.post(
        "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id, "force_new": True}, format="json"
    )
    sale_id = cart_resp.data["sale"]["sale_id"]

    add_resp = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/scan-add/",
        {"product_id": prod.product_id, "packaging_unit": "BOX", "quantity_delta": 1},
        format="json",
    )
    line_id = add_resp.data["sale"]["lines"][0]["line_id"]
    alloc_1 = api_client.post(f"/api/v2/pos/sales/{sale_id}/allocate/", {}, format="json")
    assert alloc_1.status_code == status.HTTP_200_OK
    batch = BatchStock.objects.get(tenant=tenant_a, branch=branch_a, product=prod)
    assert batch.reserved_quantity_base_units == 100

    # 3b. Switch unit on allocated draft cart (1 BOX -> 3 STRIP = 30u): must release 100 reserved cleanly!
    sel_resp = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "STRIP", "quantity_units": 3},
        format="json",
    )
    assert sel_resp.status_code == status.HTTP_200_OK
    batch.refresh_from_db()
    assert batch.reserved_quantity_base_units == 0
    assert sel_resp.data["sale"]["subtotal_bdt"] == "57.00"  # 3 * 19.00

    # Stale expected_version conflict check
    stale_resp = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {
            "line_id": line_id,
            "packaging_unit": "TABLET",
            "quantity_units": 5,
            "expected_version": sel_resp.data["sale"]["version"] - 1,
        },
        format="json",
    )
    assert stale_resp.status_code == status.HTTP_400_BAD_REQUEST
    assert stale_resp.data["error_code"] == "ERR_CART_VERSION_CONFLICT"

    # 3c. Allocate -> Pay -> Finalize -> Reverse verifies exact base-unit stock conservation (250 - 30 = 220 -> +30 = 250)
    assert api_client.post(f"/api/v2/pos/sales/{sale_id}/allocate/", {}, format="json").status_code == status.HTTP_200_OK
    pay_resp = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/record-payment/",
        {"tender_type": "CASH", "amount_bdt": "57.00", "idempotency_key": f"pay-p223-{uuid.uuid4().hex[:6]}"},
        format="json",
    )
    assert pay_resp.status_code == status.HTTP_201_CREATED
    fin_resp = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
    assert fin_resp.status_code == status.HTTP_200_OK
    assert fin_resp.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
    batch.refresh_from_db()
    assert batch.quantity_base_units == 220
    assert batch.reserved_quantity_base_units == 0

    # Cannot change unit on FINALIZED sale
    locked_unit = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/select-unit/",
        {"line_id": line_id, "packaging_unit": "TABLET", "quantity_units": 1},
        format="json",
    )
    assert locked_unit.status_code == status.HTTP_400_BAD_REQUEST
    assert locked_unit.data["error_code"] == "ERR_IMMUTABLE_SALE_STATE"

    # Reverse restores +30 base units cleanly
    rev_resp = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/reverse/", {"reason": "Customer returned sealed strips"}, format="json"
    )
    assert rev_resp.status_code == status.HTTP_200_OK
    batch.refresh_from_db()
    assert batch.quantity_base_units == 250

    # 3d. Cross-branch, cross-tenant & suspended subscription isolation
    api_client.force_authenticate(user=cashier_a)
    xb_resp = api_client.get(
        f"/api/v2/pos/search/unit-options/?branch={branch_a2.branch_id}&product_id={prod.product_id}"
    )
    assert xb_resp.status_code == status.HTTP_403_FORBIDDEN

    api_client.force_authenticate(user=owner_b)
    xt_resp = api_client.get(f"/api/v2/pos/sales/{sale_id}/unit-options/")
    assert xt_resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

    api_client.force_authenticate(user=owner_susp)
    susp_resp = api_client.get(
        f"/api/v2/pos/search/unit-options/?branch={branch_susp.branch_id}&product_id={prod.product_id}"
    )
    assert susp_resp.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_SUBSCRIPTION_SUSPENDED" in str(susp_resp.data)
