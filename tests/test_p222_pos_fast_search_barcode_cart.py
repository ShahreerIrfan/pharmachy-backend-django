"""
backend/tests/test_p222_pos_fast_search_barcode_cart.py
Acceptance tests for P222: Fast search and barcode cart (Phase 12).

Covers:
1. Prioritized barcode/GTIN/variant SKU/EN-BN & phonetic lookup, validated PosSkuInventoryReadModel
   provenance, keyboard quick-cart initialization, scan-add line merge, +/-/U unit toggle & checkout.
2. Safety blocking: empty query, unmatched barcode, expired/quarantined/recalled stock excluded from
   sellable FEFO stock & blocked on scan-add, insufficient stock block, NonDivisiblePackPolicy block,
   empty cart allocation block, and immutable PosCartScanEvent tamper rejection.
3. RBAC & isolation boundaries: Cashier unit_cost_bdt redaction, Cashier denied manual snapshot
   refresh (403), Cashier >5% line discount denied (403), Clerk denied POS search/cart, cross-branch &
   cross-tenant denial, and suspended subscription block (403 ERR_SUBSCRIPTION_SUSPENDED).
4. Optimistic cart version conflict detection (ERR_CART_VERSION_CONFLICT) & multi-threaded
   scan/allocation contention proving zero negative stock and exact Decimal financial totals.
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
    BarcodeAlternateIdentifier,
    BarcodeIdentifierType,
    BarcodePackagingLevel,
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    MedicineVariant,
    NonDivisibleContainerType,
    NonDivisiblePackPolicy,
    PackagingHierarchyNode,
    PlanTier,
    PosCartScanEvent,
    PosPackagingUnit,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSkuInventoryReadModel,
    Tenant,
    TenantLifecycleStatus,
    TenantMedicineConfig,
)
from apps.core.pos_domain_service import PosDomainService
from apps.core.pos_search_cart_service import PosSearchCartService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p222-a-{uid}",
        name="Dhaka Model Pharmacy",
        subdomain=f"dhaka-p222-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p222-b-{uid}",
        name="Chittagong Pharmacy",
        subdomain=f"ctg-p222-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_suspended(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p222-susp-{uid}",
        name="Suspended P222 Pharmacy",
        subdomain=f"susp-p222-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.SUSPENDED,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p222-a-{uid}",
        tenant=tenant_a,
        name="Dhanmondi Flagship",
        license_number=f"DGDA-P222A-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p222-a2-{uid}",
        tenant=tenant_a,
        name="Uttara Counter 2",
        license_number=f"DGDA-P222U-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p222-b-{uid}",
        tenant=tenant_b,
        name="Agrabad Branch",
        license_number=f"DGDA-P222B-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_susp(tenant_suspended):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p222-susp-{uid}",
        tenant=tenant_suspended,
        name="Suspended Branch",
        license_number=f"DGDA-P222S-{uid}",
        is_active=True,
    )


def _make_user(tenant, branch, role, prefix):
    uid = uuid.uuid4().hex[:8]
    digits = "".join(str(ord(c) % 10) for c in uid)
    return PharmacyUser.objects.create_user(
        email=f"{prefix}-{uid}@pharma.bd",
        phone_number=f"+88018{digits}",
        password="P222TestPass!2026",
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id if branch else None,
        role=role,
        full_name=f"{prefix.title()} User",
    )


@pytest.fixture
def manager_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.BRANCH_MANAGER, "mgr-p222")


@pytest.fixture
def cashier_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.CASHIER, "cashier-p222")


@pytest.fixture
def cashier_a2(tenant_a, branch_a2):
    return _make_user(tenant_a, branch_a2, UserRole.CASHIER, "cashier-a2")


@pytest.fixture
def clerk_a(tenant_a, branch_a):
    return _make_user(tenant_a, branch_a, UserRole.INVENTORY_CLERK, "clerk-p222")


@pytest.fixture
def owner_b(tenant_b, branch_b):
    return _make_user(tenant_b, branch_b, UserRole.PHARMACY_OWNER, "owner-p222b")


@pytest.fixture
def owner_susp(tenant_suspended, branch_susp):
    return _make_user(tenant_suspended, branch_susp, UserRole.PHARMACY_OWNER, "owner-susp")


@pytest.fixture
def napa_product(db):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"med-napa-{uid}",
        brand_name="Napa",
        brand_name_bn="নাপা",
        generic_name="Paracetamol",
        generic_name_bn="প্যারাসিটামল",
        dosage_form="Tablet",
        strength="500mg",
        manufacturer="Beximco Pharmaceuticals",
        barcode=f"8801000{uid}",
        mrp_unit=Decimal("1.20"),
        cost_unit=Decimal("0.85"),
        is_prescription_only=False,
    )


@pytest.fixture
def insulin_sealed_product(db):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"med-ins-{uid}",
        brand_name="Lantus SoloStar",
        brand_name_bn="ল্যান্টাস পেন",
        generic_name="Insulin Glargine",
        generic_name_bn="ইনসুলিন গ্লারজিন",
        dosage_form="Pre-filled Pen",
        strength="100 IU/mL",
        manufacturer="Sanofi",
        barcode=f"8809000{uid}",
        mrp_unit=Decimal("950.00"),
        cost_unit=Decimal("780.00"),
        is_prescription_only=True,
        requires_cold_chain=True,
    )


@pytest.mark.django_db
class TestP222FastSearchBarcodeCart:
    """Comprehensive P222 verification across search, read models, safety rules, RBAC & cart workflows."""

    def test_1_prioritized_lookup_bilingual_phonetic_snapshot_and_keyboard_cart_flow(
        self, api_client, manager_a, cashier_a, tenant_a, branch_a, napa_product
    ):
        today = timezone.now().date()
        # Packaging hierarchy: 1 STRIP = 10 TABLET, 1 BOX = 10 STRIP (100 tablets) with box tier GTIN
        box_gtin = f"GTINBOX-{uuid.uuid4().hex[:6].upper()}"
        PackagingHierarchyNode.objects.create(
            node_id=f"pkg-s-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=napa_product,
            parent_unit="STRIP",
            child_unit="TABLET",
            conversion_factor=10,
            tier_level=1,
            is_base_level=True,
            effective_from=today - datetime.timedelta(days=5),
            is_active=True,
        )
        PackagingHierarchyNode.objects.create(
            node_id=f"pkg-b-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=napa_product,
            parent_unit="BOX",
            child_unit="STRIP",
            conversion_factor=10,
            tier_level=2,
            is_base_level=False,
            tier_gtin_barcode=box_gtin,
            effective_from=today - datetime.timedelta(days=5),
            is_active=True,
        )

        variant = MedicineVariant.objects.create(
            variant_id=f"var-napa-{uuid.uuid4().hex[:6]}",
            product=napa_product,
            sku=f"SKU-NAPA-{uuid.uuid4().hex[:5].upper()}",
            variant_name="Napa 500mg 10s Blister",
            strength="500mg",
            pack_size="10 Tablets",
            pack_size_numeric=10,
            barcode=f"VARBC-{uuid.uuid4().hex[:6]}",
            mrp_unit=Decimal("1.20"),
            cost_unit=Decimal("0.85"),
            mrp_pack=Decimal("12.00"),
            cost_pack=Decimal("8.50"),
        )
        alt_barcode = f"ALT-NAPA-{uuid.uuid4().hex[:6].upper()}"
        BarcodeAlternateIdentifier.objects.create(
            identifier_id=f"bc-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            tenant_id_str=tenant_a.tenant_id,
            product=napa_product,
            variant=variant,
            barcode_value=alt_barcode,
            identifier_type=BarcodeIdentifierType.INTERNAL_STORE_BARCODE,
            packaging_level=BarcodePackagingLevel.STRIP,
            priority_order=1,
            pack_quantity_multiplier=10,
            is_active=True,
        )
        TenantMedicineConfig.objects.create(
            config_id=f"cfg-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            product=napa_product,
            variant=variant,
            local_brand_name="Napa Original",
            local_sku=f"LOC-{uuid.uuid4().hex[:5].upper()}",
            shelf_location="Rack B-4",
            is_visible=True,
        )

        # Active FEFO batches (25 tablets) + an expired batch (40 tablets) that MUST NOT become sellable!
        BatchStock.objects.create(
            batch_id=f"bat-fefo-{uuid.uuid4().hex[:6]}",
            tenant=tenant_a,
            branch=branch_a,
            product=napa_product,
            batch_number="NAPA-ACT-01",
            quantity_base_units=25,
            reserved_quantity_base_units=0,
            expiry_date=today + datetime.timedelta(days=180),
            purchase_date=today - datetime.timedelta(days=20),
            cost_per_unit=Decimal("0.85"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )
        BatchStock.objects.create(
            batch_id=f"bat-exp-{uuid.uuid4().hex[:6]}",
            tenant=tenant_a,
            branch=branch_a,
            product=napa_product,
            batch_number="NAPA-EXP-99",
            quantity_base_units=40,
            reserved_quantity_base_units=0,
            expiry_date=today - datetime.timedelta(days=5),
            purchase_date=today - datetime.timedelta(days=400),
            cost_per_unit=Decimal("0.70"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )

        # Manager builds canonical branch SKU snapshot read model
        api_client.force_authenticate(user=manager_a)
        rebuild_res = api_client.post(
            "/api/v2/pos/search/refresh-snapshot/",
            {"branch": branch_a.branch_id},
            format="json",
        )
        assert rebuild_res.status_code == status.HTTP_200_OK
        assert rebuild_res.data["synced_skus"] >= 1
        rm = PosSkuInventoryReadModel.objects.filter(
            tenant=tenant_a, branch=branch_a, product=napa_product
        ).first()
        assert rm is not None
        assert rm.sellable_base_units == 25
        assert rm.available_base_units == 25
        assert rm.expired_base_units == 40
        assert len(rm.sha256_checksum) == 64

        # Cashier runs prioritized lookups: Alternate Barcode -> Packaging Tier GTIN -> Variant SKU -> Bengali -> Phonetic
        api_client.force_authenticate(user=cashier_a)
        alt_res = api_client.get(
            f"/api/v2/pos/search/barcode-lookup/?branch={branch_a.branch_id}&q={alt_barcode}"
        )
        assert alt_res.status_code == status.HTTP_200_OK
        assert alt_res.data["matched"] is True
        assert alt_res.data["item"]["resolution_source"] == "BARCODE_ALTERNATE"
        assert alt_res.data["item"]["resolved_packaging_unit"] == "STRIP"
        assert alt_res.data["item"]["resolved_conversion_factor"] == 10
        assert alt_res.data["item"]["available_base_units"] == 25
        assert alt_res.data["item"]["expired_base_units"] == 40
        assert alt_res.data["item"]["shelf_location"] == "Rack B-4"
        assert alt_res.data["item"]["unit_cost_bdt"] is None  # Strictly redacted for Cashier!

        gtin_res = api_client.get(
            f"/api/v2/pos/search/?branch={branch_a.branch_id}&q={box_gtin}"
        )
        assert gtin_res.status_code == status.HTTP_200_OK
        assert gtin_res.data["results"][0]["resolution_source"] == "PACKAGING_TIER_GTIN"
        assert gtin_res.data["results"][0]["resolved_packaging_unit"] == "BOX"
        assert gtin_res.data["results"][0]["resolved_conversion_factor"] == 100

        sku_res = api_client.get(
            f"/api/v2/pos/search/?branch={branch_a.branch_id}&q={variant.sku}"
        )
        assert sku_res.status_code == status.HTTP_200_OK
        assert sku_res.data["results"][0]["resolution_source"] == "VARIANT_SKU_EXACT"

        bn_res = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=নাপা")
        assert bn_res.status_code == status.HTTP_200_OK
        assert any(r["product_id"] == napa_product.product_id for r in bn_res.data["results"])

        phonetic_res = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=paracetamol")
        assert phonetic_res.status_code == status.HTTP_200_OK
        assert any(r["product_id"] == napa_product.product_id for r in phonetic_res.data["results"])

        # Cashier opens offline snapshot-cache read model cleanly with offline estimate disclaimer
        snap_cache_res = api_client.get(f"/api/v2/pos/search/snapshot-cache/?branch={branch_a.branch_id}")
        assert snap_cache_res.status_code == status.HTTP_200_OK
        assert snap_cache_res.data["count"] >= 1
        assert "offline_disclaimer_en" in snap_cache_res.data

        # Keyboard-first counter flow: quick-cart -> scan-add x2 (merges 1 -> 2 STRIPs) -> +/- / unit cycle U -> finalize!
        cart_res = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {"branch": branch_a.branch_id, "offline_device_namespace": "pos-counter-1"},
            format="json",
        )
        assert cart_res.status_code == status.HTTP_201_CREATED
        sale_id = cart_res.data["sale"]["sale_id"]
        assert cart_res.data["sale"]["lifecycle_state"] == PosSaleLifecycleState.DRAFT
        assert cart_res.data["sale"]["version"] == 1

        # Scan 1: 1 STRIP (10 tablets) @ 12.00 BDT
        scan1 = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"barcode": alt_barcode, "quantity_delta": 1, "expected_version": 1},
            format="json",
        )
        assert scan1.status_code == status.HTTP_200_OK
        assert len(scan1.data["sale"]["lines"]) == 1
        assert scan1.data["sale"]["grand_total_bdt"] == "12.00"
        assert scan1.data["sale"]["version"] == 2
        line_id = scan1.data["sale"]["lines"][0]["line_id"]

        # Scan 2 same barcode: merges same line from qty=1 -> qty=2 STRIPs (20 base units <= 25 FEFO), 24.00 BDT
        scan2 = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"barcode": alt_barcode, "quantity_delta": 1, "expected_version": 2},
            format="json",
        )
        assert scan2.status_code == status.HTTP_200_OK
        assert len(scan2.data["sale"]["lines"]) == 1
        assert scan2.data["sale"]["lines"][0]["quantity_units"] == 2
        assert scan2.data["sale"]["lines"][0]["base_units_total"] == 20
        assert scan2.data["sale"]["grand_total_bdt"] == "24.00"
        assert scan2.data["sale"]["version"] == 3

        # Keyboard '-' decrement delta=-1 -> 1 STRIP (12.00 BDT)
        dec_res = api_client.patch(
            f"/api/v2/pos/sales/{sale_id}/cart-line/",
            {"line_id": line_id, "delta": -1, "expected_version": 3},
            format="json",
        )
        assert dec_res.status_code == status.HTTP_200_OK
        assert dec_res.data["sale"]["lines"][0]["quantity_units"] == 1
        assert dec_res.data["sale"]["grand_total_bdt"] == "12.00"

        # Keyboard 'U' unit toggle: convert 1 STRIP -> 2 TABLET (2 * 1.20 = 2.40 BDT)
        unit_res = api_client.patch(
            f"/api/v2/pos/sales/{sale_id}/cart-line/",
            {"line_id": line_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_units": 2},
            format="json",
        )
        assert unit_res.status_code == status.HTTP_200_OK
        assert unit_res.data["sale"]["lines"][0]["packaging_unit"] == PosPackagingUnit.TABLET
        assert unit_res.data["sale"]["lines"][0]["base_units_total"] == 2
        assert unit_res.data["sale"]["grand_total_bdt"] == "2.40"

        # Complete downstream P221 allocate -> pay -> finalize seamlessly!
        assert api_client.post(f"/api/v2/pos/sales/{sale_id}/allocate/", {}, format="json").status_code == status.HTTP_200_OK
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{sale_id}/record-payment/",
                {"tender_type": "CASH", "amount_bdt": "2.40", "idempotency_key": f"pay-{uuid.uuid4().hex[:8]}"},
                format="json",
            ).status_code
            == status.HTTP_201_CREATED
        )
        fin_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_res.status_code == status.HTTP_200_OK
        assert fin_res.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED

        # Scan events audit trail captured every scan & line adjustment
        events_res = api_client.get(f"/api/v2/pos/search/scan-events/?sale={sale_id}")
        assert events_res.status_code == status.HTTP_200_OK
        assert len(events_res.data) >= 4

    def test_2_invalid_inputs_expired_quarantine_non_divisible_and_immutable_scan_events(
        self, api_client, cashier_a, tenant_a, branch_a, napa_product, insulin_sealed_product
    ):
        today = timezone.now().date()
        # Napa has ONLY quarantined & expired stock in branch_a -> must report is_sellable=False and block scan-add!
        BatchStock.objects.create(
            batch_id=f"bat-quar-{uuid.uuid4().hex[:6]}",
            tenant=tenant_a,
            branch=branch_a,
            product=napa_product,
            batch_number="QUAR-01",
            quantity_base_units=30,
            reserved_quantity_base_units=0,
            expiry_date=today + datetime.timedelta(days=200),
            purchase_date=today,
            cost_per_unit=Decimal("0.80"),
            status=BatchStatus.QUARANTINED,
            is_quarantined=True,
        )

        # Insulin sealed pen has active stock (5 packs) + NonDivisiblePackPolicy prohibiting loose tablet split
        BatchStock.objects.create(
            batch_id=f"bat-ins-{uuid.uuid4().hex[:6]}",
            tenant=tenant_a,
            branch=branch_a,
            product=insulin_sealed_product,
            batch_number="INS-ACT-01",
            quantity_base_units=5,
            reserved_quantity_base_units=0,
            expiry_date=today + datetime.timedelta(days=240),
            purchase_date=today,
            cost_per_unit=Decimal("780.00"),
            status=BatchStatus.ACTIVE,
        )
        NonDivisiblePackPolicy.objects.create(
            policy_id=f"ndpp-{uuid.uuid4().hex[:8]}",
            tenant=tenant_a,
            branch=branch_a,
            product=insulin_sealed_product,
            container_type=NonDivisibleContainerType.INSULIN_PEN,
            is_splitting_allowed=False,
            is_active=True,
        )

        api_client.force_authenticate(user=cashier_a)

        # Empty query rejected cleanly (422)
        empty_q = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=   ")
        assert empty_q.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # Quarantined product appears in search as BLOCKED (is_sellable=False, available=0)
        q_search = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=Napa")
        assert q_search.status_code == status.HTTP_200_OK
        assert q_search.data["results"][0]["is_sellable"] is False
        assert q_search.data["results"][0]["block_reason"] == "QUARANTINED_STOCK_BLOCKED"

        # Open quick-cart
        cart = api_client.post(
            "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id}, format="json"
        ).data["sale"]
        sale_id = cart["sale_id"]

        # Guard: cannot allocate an empty cart!
        empty_alloc = api_client.post(f"/api/v2/pos/sales/{sale_id}/allocate/", {}, format="json")
        assert empty_alloc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_EMPTY_CART" in str(empty_alloc.data)

        # Unmatched barcode rejected & logged
        unmatched = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"barcode": "NON-EXISTENT-99999"},
            format="json",
        )
        assert unmatched.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_BARCODE_NOT_FOUND" in str(unmatched.data)
        assert PosCartScanEvent.objects.filter(sale_id=sale_id, was_rejected=True, rejection_code="ERR_BARCODE_NOT_FOUND").exists()

        # Quarantined stock scan-add blocked cleanly
        quar_add = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": napa_product.product_id, "quantity_delta": 1},
            format="json",
        )
        assert quar_add.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_EXPIRED_OR_UNSELLABLE_STOCK" in str(quar_add.data)

        # NonDivisiblePackPolicy blocks loose TABLET split on sealed Insulin pen
        split_add = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {"product_id": insulin_sealed_product.product_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": 1},
            format="json",
        )
        assert split_add.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_NON_DIVISIBLE_PACK" in str(split_add.data)

        # Sealed BOX/STRIP (factor >= 5) succeeds up to available 5 base units, exceeding stock is blocked
        sealed_ok = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {
                "product_id": insulin_sealed_product.product_id,
                "packaging_unit": PosPackagingUnit.BOX,
                "conversion_factor": 5,
                "quantity_delta": 1,
            },
            format="json",
        )
        assert sealed_ok.status_code == status.HTTP_200_OK

        over_stock = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/scan-add/",
            {
                "product_id": insulin_sealed_product.product_id,
                "packaging_unit": PosPackagingUnit.BOX,
                "conversion_factor": 5,
                "quantity_delta": 1,
            },
            format="json",
        )
        assert over_stock.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_INSUFFICIENT_SELLABLE_STOCK" in str(over_stock.data)

        # Append-only PosCartScanEvent strictly blocks update and delete!
        scan_ev = PosCartScanEvent.objects.filter(sale_id=sale_id).first()
        assert scan_ev is not None
        scan_ev.raw_query = "tampered"
        with pytest.raises(DjangoValidationError):
            scan_ev.save()
        with pytest.raises(DjangoValidationError):
            scan_ev.delete()

    def test_3_rbac_cost_masking_cross_branch_cross_tenant_and_suspended_plan(
        self,
        api_client,
        manager_a,
        cashier_a,
        cashier_a2,
        clerk_a,
        owner_b,
        owner_susp,
        tenant_a,
        branch_a,
        branch_a2,
        branch_b,
        branch_susp,
        napa_product,
    ):
        today = timezone.now().date()
        BatchStock.objects.create(
            batch_id=f"bat-rbac-{uuid.uuid4().hex[:6]}",
            tenant=tenant_a,
            branch=branch_a,
            product=napa_product,
            batch_number="RBAC-B1",
            quantity_base_units=20,
            reserved_quantity_base_units=0,
            expiry_date=today + datetime.timedelta(days=180),
            purchase_date=today,
            cost_per_unit=Decimal("0.85"),
            status=BatchStatus.ACTIVE,
        )

        # Manager sees cost price; Cashier does not
        api_client.force_authenticate(user=manager_a)
        mgr_search = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=Napa")
        assert mgr_search.status_code == status.HTTP_200_OK
        assert mgr_search.data["results"][0]["unit_cost_bdt"] == "0.8500"

        api_client.force_authenticate(user=cashier_a)
        cash_search = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=Napa")
        assert cash_search.status_code == status.HTTP_200_OK
        assert cash_search.data["results"][0]["unit_cost_bdt"] is None

        # Cashier denied manual server snapshot rebuild (403)
        cash_rebuild = api_client.post(
            "/api/v2/pos/search/refresh-snapshot/",
            {"branch": branch_a.branch_id},
            format="json",
        )
        assert cash_rebuild.status_code == status.HTTP_403_FORBIDDEN

        # Cashier opens quick cart & scans 10 tablets (12.00 BDT); >5% line discount is denied (403)!
        sale = api_client.post(
            "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id}, format="json"
        ).data["sale"]
        added = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/scan-add/",
            {"product_id": napa_product.product_id, "packaging_unit": "STRIP", "conversion_factor": 10, "quantity_delta": 1},
            format="json",
        ).data["sale"]
        line_id = added["lines"][0]["line_id"]

        disc_denied = api_client.patch(
            f"/api/v2/pos/sales/{sale['sale_id']}/cart-line/",
            {"line_id": line_id, "discount_amount_bdt": "5.00"},
            format="json",
        )
        assert disc_denied.status_code == status.HTTP_403_FORBIDDEN

        # Inventory Clerk denied POS counter quick-cart (403)
        api_client.force_authenticate(user=clerk_a)
        clerk_cart = api_client.post(
            "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id}, format="json"
        )
        assert clerk_cart.status_code == status.HTTP_403_FORBIDDEN

        # Sibling branch Cashier denied reading/writing branch_a search or cart
        api_client.force_authenticate(user=cashier_a2)
        cross_branch_search = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=Napa")
        assert cross_branch_search.status_code == status.HTTP_403_FORBIDDEN
        cross_branch_scan = api_client.post(
            f"/api/v2/pos/sales/{sale['sale_id']}/scan-add/",
            {"product_id": napa_product.product_id, "quantity_delta": 1},
            format="json",
        )
        assert cross_branch_scan.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        # Foreign tenant denied accessing tenant_a branch search or cart
        api_client.force_authenticate(user=owner_b)
        cross_tenant_search = api_client.get(f"/api/v2/pos/search/?branch={branch_a.branch_id}&q=Napa")
        assert cross_tenant_search.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        # Suspended tenant subscription blocked (403 ERR_SUBSCRIPTION_SUSPENDED)
        api_client.force_authenticate(user=owner_susp)
        susp_search = api_client.get(f"/api/v2/pos/search/?branch={branch_susp.branch_id}&q=Napa")
        assert susp_search.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_SUBSCRIPTION_SUSPENDED" in str(susp_search.data)


_sqlite_scan_lock = threading.Lock()


@pytest.mark.django_db(transaction=True)
def test_4_optimistic_version_conflict_and_concurrent_scan_allocation_contention(
    manager_a, tenant_a, branch_a, napa_product
):
    today = timezone.now().date()
    tight_batch = BatchStock.objects.create(
        batch_id=f"bat-p222-tight-{uuid.uuid4().hex[:6]}",
        tenant=tenant_a,
        branch=branch_a,
        product=napa_product,
        batch_number=f"T222-{uuid.uuid4().hex[:4]}",
        quantity_base_units=3,
        reserved_quantity_base_units=0,
        expiry_date=today + datetime.timedelta(days=120),
        purchase_date=today,
        cost_per_unit=Decimal("0.85"),
        status=BatchStatus.ACTIVE,
    )

    # 4a. Optimistic cart version conflict check (concurrent tabs submitting same expected_version=1)
    shared_cart, _ = PosSearchCartService.get_or_create_quick_cart(
        manager_a, {"branch": branch_a.branch_id, "force_new": True}
    )

    def _compete_same_cart(_idx: int) -> bool:
        def _exec() -> bool:
            try:
                PosSearchCartService.scan_add_to_cart(
                    manager_a,
                    shared_cart,
                    {
                        "product_id": napa_product.product_id,
                        "packaging_unit": PosPackagingUnit.TABLET,
                        "quantity_delta": 1,
                        "expected_version": 1,
                    },
                )
                return True
            except (DrfValidationError, PermissionDenied):
                return False

        if connection.vendor == "sqlite":
            with _sqlite_scan_lock:
                return _exec()
        try:
            return _exec()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=3) as pool:
        same_cart_results = list(pool.map(_compete_same_cart, range(3)))

    assert sum(1 for ok in same_cart_results if ok) == 1
    assert PosCartScanEvent.objects.filter(
        sale=shared_cart, was_rejected=True, rejection_code="ERR_CART_VERSION_CONFLICT"
    ).count() == 2

    # Clean up shared cart line before running tight FEFO allocation contention
    shared_cart.refresh_from_db()
    PosDomainService.void_pre_final_sale(manager_a, shared_cart, "Reset before multi-counter FEFO test")

    # 4b. Multi-counter concurrent scan-add + FEFO allocate against 3 available base units (each counter requests 2)
    counter_carts = [
        PosSearchCartService.get_or_create_quick_cart(
            manager_a, {"branch": branch_a.branch_id, "force_new": True}
        )[0]
        for _ in range(3)
    ]

    def _scan_and_allocate(cart_obj: PosSaleOrder) -> bool:
        def _exec() -> bool:
            try:
                updated_cart, _ = PosSearchCartService.scan_add_to_cart(
                    manager_a,
                    cart_obj,
                    {
                        "product_id": napa_product.product_id,
                        "packaging_unit": PosPackagingUnit.TABLET,
                        "quantity_delta": 2,
                    },
                )
                PosDomainService.allocate_stock(manager_a, updated_cart)
                return True
            except (DrfValidationError, PermissionDenied):
                return False

        if connection.vendor == "sqlite":
            with _sqlite_scan_lock:
                return _exec()
        try:
            return _exec()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=3) as pool:
        counter_outcomes = list(pool.map(_scan_and_allocate, counter_carts))

    assert sum(1 for ok in counter_outcomes if ok) == 1
    tight_batch.refresh_from_db()
    assert tight_batch.reserved_quantity_base_units == 2
    assert tight_batch.available_quantity_base_units == 1
    assert tight_batch.quantity_base_units >= tight_batch.reserved_quantity_base_units >= 0

