"""
backend/tests/test_p225_held_and_resumed_carts.py
Acceptance & integration tests for P225: Held and resumed carts (Phase 12).

Covers:
1. Valid held basket workflow: park non-empty cart with reference/note/TTL & optional FEFO
   stock reservation, quick-cart counter isolation for next customer, branch held list
   inspection, cashier cost redaction, and clean resume.
2. Stale stock reservation expiry & conservation: expire-stale-holds / expire-hold / resume
   auto-release decrementing BatchStock.reserved_quantity_base_units cleanly with 0 drift.
3. Stale pricing drift (PRICE_CHANGED): catalog/tenant MRP or cart pricing drift flagged
   with bilingual EN/BN prompt, blocked unless acknowledge_stale=True, repriced cleanly to
   live Decimal totals, and prior P224 confirmation invalidated.
4. Stock shortage & unsafe batch detection (UNSAFE_BATCH / STOCK_SHORTAGE), cancel-hold
   reservation release & void, empty cart guard (ERR_EMPTY_CART), invalid TTLs
   (ERR_INVALID_HOLD_TTL), and optimistic version conflicts (ERR_CART_VERSION_CONFLICT).
5. Role RBAC, cross-branch denial, cross-tenant isolation, suspended subscription lock,
   and strictly immutable append-only PosHeldCartAudit records.
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
    MedicineProduct,
    PackagingHierarchyNode,
    PlanTier,
    PosAllocationStatus,
    PosHeldCartAudit,
    PosPackagingUnit,
    PosSaleAllocation,
    PosSaleLifecycleState,
    PosSaleOrder,
    Tenant,
    TenantLifecycleStatus,
    TenantMedicineConfig,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p225-a-{uid}",
        name="Dhaka Care Pharmacy",
        subdomain=f"dhaka-p225-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p225-b-{uid}",
        name="Chattogram Rx",
        subdomain=f"ctg-p225-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_susp(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p225-susp-{uid}",
        name="Suspended P225 Pharmacy",
        subdomain=f"susp-p225-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.SUSPENDED,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p225-a-{uid}",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        license_number=f"DGDA-P225A-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p225-a2-{uid}",
        tenant=tenant_a,
        name="Mirpur Branch",
        license_number=f"DGDA-P225A2-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p225-b-{uid}",
        tenant=tenant_b,
        name="Agrabad Branch",
        license_number=f"DGDA-P225B-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_susp(tenant_susp):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p225-susp-{uid}",
        tenant=tenant_susp,
        name="Suspended Branch",
        license_number=f"DGDA-P225S-{uid}",
        is_active=True,
    )


def _mk_user(tenant, branch, role: str, tag: str) -> PharmacyUser:
    uid = uuid.uuid4().hex[:8]
    digits = "".join(str(ord(c) % 10) for c in uid)
    return PharmacyUser.objects.create_user(
        email=f"p225-{tag}-{uid}@pharmacy.bd",
        phone_number=f"+88017{digits}",
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
def finance_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.FINANCE_OFFICER, "finance")


@pytest.fixture
def auditor_a(tenant_a, branch_a):
    return _mk_user(tenant_a, branch_a, UserRole.AUDITOR, "auditor")


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
        product_id=f"med-p225-napa-{uuid.uuid4().hex[:5]}",
        brand_name="Napa Extra 500mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg",
        barcode=f"894225a{uuid.uuid4().hex[:5]}",
        mrp_unit=Decimal("2.50"),
        cost_unit=Decimal("1.50"),
    )
    med_monas = MedicineProduct.objects.create(
        product_id=f"med-p225-monas-{uuid.uuid4().hex[:5]}",
        brand_name="Monas 10mg",
        generic_name="Montelukast",
        dosage_form="Tablet",
        strength="10mg",
        barcode=f"894225b{uuid.uuid4().hex[:5]}",
        mrp_unit=Decimal("15.00"),
        cost_unit=Decimal("10.00"),
    )

    batches = {}
    for key, prod in [("napa", med_napa), ("monas", med_monas)]:
        uid = uuid.uuid4().hex[:5]
        PackagingHierarchyNode.objects.create(
            node_id=f"node-p225-strip-{uid}",
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
        batches[key] = BatchStock.objects.create(
            batch_id=f"bat-p225-{key}-{uid}",
            tenant=tenant_a,
            branch=branch_a,
            product=prod,
            batch_number=f"LOT-{key.upper()}-{uid.upper()}",
            expiry_date=today + datetime.timedelta(days=365),
            purchase_date=today,
            quantity_base_units=100,
            reserved_quantity_base_units=0,
            cost_per_unit=prod.cost_unit,
            status=BatchStatus.ACTIVE,
            is_active=True,
        )

    return {"napa": med_napa, "monas": med_monas, "batches": batches}


def _open_cart_with_item(api_client, user, branch, product, qty=1, unit=PosPackagingUnit.TABLET):
    api_client.force_authenticate(user=user)
    cart_res = api_client.post(
        "/api/v2/pos/sales/quick-cart/",
        {"branch": branch.branch_id, "force_new": True},
        format="json",
    )
    assert cart_res.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), cart_res.data
    sale = cart_res.data["sale"]
    scan_res = api_client.post(
        f"/api/v2/pos/sales/{sale['sale_id']}/scan-add/",
        {
            "barcode": product.barcode,
            "quantity_units": qty,
            "packaging_unit": unit,
            "expected_version": sale["version"],
        },
        format="json",
    )
    assert scan_res.status_code == status.HTTP_200_OK, scan_res.data
    return scan_res.data["sale"]


@pytest.mark.django_db
class TestP225HeldAndResumedCarts:
    def test_valid_hold_with_fefo_reservation_quick_cart_isolation_and_clean_resume(
        self, api_client, branch_a, cashier_a, catalog_fixture
    ):
        BranchOperatingSettings.objects.create(
            tenant=branch_a.tenant,
            branch=branch_a,
            device_policies={"hold_cart_ttl_minutes": 45, "hold_reservation_ttl_minutes": 20},
        )
        napa = catalog_fixture["napa"]
        batch_napa = catalog_fixture["batches"]["napa"]
        sale = _open_cart_with_item(api_client, cashier_a, branch_a, napa, qty=2, unit=PosPackagingUnit.STRIP)
        sale_id = sale["sale_id"]

        # Confirm pricing prior to hold so hash baseline exists
        conf_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/confirm-pricing/",
            {"expected_grand_total_bdt": "50.00"},
            format="json",
        )
        assert conf_res.status_code == status.HTTP_200_OK
        confirmed_sale = conf_res.data["sale"]

        # Hold basket with FEFO stock reservation (2 strips = 20 base units)
        hold_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {
                "hold_reference": "ATM-CASH-WAIT",
                "hold_note": "Patient stepped out to withdraw cash",
                "reserve_stock": True,
                "expected_version": confirmed_sale["version"],
            },
            format="json",
        )
        assert hold_res.status_code == status.HTTP_200_OK, hold_res.data
        assert hold_res.data["lifecycle_state"] == PosSaleLifecycleState.HELD
        assert hold_res.data["hold_reference"] == "ATM-CASH-WAIT"
        assert hold_res.data["hold_stock_reserved"] is True
        assert Decimal(hold_res.data["hold_snapshot_grand_total_bdt"]) == Decimal("50.00")
        assert hold_res.data["staleness"]["is_stale"] is False
        # Cashier cost masking check
        assert hold_res.data["lines"][0]["unit_cost_bdt"] is None

        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 20
        assert batch_napa.available_quantity_base_units == 80

        # Counter isolation: quick-cart returns a NEW DRAFT basket so next walk-in starts cleanly
        next_cart_res = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {"branch": branch_a.branch_id},
            format="json",
        )
        assert next_cart_res.status_code == status.HTTP_201_CREATED
        assert next_cart_res.data["sale"]["sale_id"] != sale_id
        assert next_cart_res.data["sale"]["lifecycle_state"] == PosSaleLifecycleState.DRAFT

        # List held carts -> fresh, zero cost leakage
        held_list = api_client.get(f"/api/v2/pos/sales/held/?branch={branch_a.branch_id}")
        assert held_list.status_code == status.HTTP_200_OK
        assert held_list.data["count"] == 1
        assert held_list.data["stale_count"] == 0
        row = held_list.data["results"][0]
        assert row["sale_id"] == sale_id
        assert row["hold_reference"] == "ATM-CASH-WAIT"
        assert row["staleness"]["is_stale"] is False
        assert row["sale"]["lines"][0]["unit_cost_bdt"] is None

        # Resume cleanly without needing acknowledge_stale since nothing expired/changed
        resume_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/resume/",
            {"expected_version": hold_res.data["version"]},
            format="json",
        )
        assert resume_res.status_code == status.HTTP_200_OK, resume_res.data
        assert resume_res.data["lifecycle_state"] == PosSaleLifecycleState.DRAFT
        assert resume_res.data["resumed_at"] is not None
        assert resume_res.data["staleness"]["is_stale"] is False

        # Immutable audit trail contains HELD & RESUMED
        audits_res = api_client.get(f"/api/v2/pos/sales/{sale_id}/hold-audits/")
        assert audits_res.status_code == status.HTTP_200_OK
        actions = [a["action"] for a in audits_res.data]
        assert "HELD" in actions
        assert "RESUMED" in actions

    def test_stale_reservation_expiry_sweep_and_stock_conservation(
        self, api_client, branch_a, cashier_a, catalog_fixture
    ):
        monas = catalog_fixture["monas"]
        batch_monas = catalog_fixture["batches"]["monas"]
        sale = _open_cart_with_item(api_client, cashier_a, branch_a, monas, qty=30, unit=PosPackagingUnit.TABLET)
        sale_id = sale["sale_id"]

        hold_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {
                "hold_reference": "RX-FORGOT",
                "reserve_stock": True,
                "hold_ttl_minutes": 30,
                "reservation_ttl_minutes": 5,
            },
            format="json",
        )
        assert hold_res.status_code == status.HTTP_200_OK
        batch_monas.refresh_from_db()
        assert batch_monas.reserved_quantity_base_units == 30
        assert batch_monas.available_quantity_base_units == 70

        # Simulate elapsed reservation timeout (still within 30m hold TTL, past 5m reservation TTL)
        PosSaleOrder.objects.filter(pk=sale_id).update(
            hold_reservation_expires_at=timezone.now() - datetime.timedelta(minutes=2)
        )

        # Held list reports stale reservation expired
        held_list = api_client.get(f"/api/v2/pos/sales/held/?branch={branch_a.branch_id}")
        assert held_list.status_code == status.HTTP_200_OK
        assert held_list.data["stale_count"] == 1
        assert held_list.data["results"][0]["staleness"]["is_reservation_expired"] is True
        assert "RESERVATION_EXPIRED" in held_list.data["results"][0]["staleness"]["stale_reasons"]

        # Sweep releases timed-out reservations cleanly back to BatchStock
        sweep_res = api_client.post(
            "/api/v2/pos/sales/expire-stale-holds/",
            {"branch": branch_a.branch_id, "void_expired_holds": False},
            format="json",
        )
        assert sweep_res.status_code == status.HTTP_200_OK, sweep_res.data
        assert sweep_res.data["expired_count"] == 1
        assert sweep_res.data["released_base_units"] == 30

        batch_monas.refresh_from_db()
        assert batch_monas.reserved_quantity_base_units == 0
        assert batch_monas.available_quantity_base_units == 100
        assert PosSaleAllocation.objects.filter(sale_id=sale_id, status=PosAllocationStatus.EXPIRED).count() == 1

        # Resuming now without acknowledge_stale blocks with 400 prompt because reservation expired earlier
        blocked = api_client.post(f"/api/v2/pos/sales/{sale_id}/resume/", {}, format="json")
        assert blocked.status_code == status.HTTP_400_BAD_REQUEST
        assert blocked.data["error_code"] == "ERR_HELD_CART_REQUIRES_CONFIRMATION"
        assert "RESERVATION_EXPIRED" in blocked.data["details"]["stale_reasons"]
        assert blocked.data["message_bn"]

        # Acknowledged resume re-reserves FEFO stock safely
        ok_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/resume/",
            {"acknowledge_stale": True, "re_reserve_stock": True},
            format="json",
        )
        assert ok_res.status_code == status.HTTP_200_OK, ok_res.data
        assert ok_res.data["lifecycle_state"] == PosSaleLifecycleState.DRAFT
        batch_monas.refresh_from_db()
        assert batch_monas.reserved_quantity_base_units == 30

    def test_stale_price_change_prompt_repricing_and_confirmation_invalidation(
        self, api_client, branch_a, cashier_a, catalog_fixture
    ):
        monas = catalog_fixture["monas"]
        sale = _open_cart_with_item(api_client, cashier_a, branch_a, monas, qty=1, unit=PosPackagingUnit.STRIP)
        sale_id = sale["sale_id"]  # 10 tablets @ 15.00 = 150.00 BDT

        conf_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/confirm-pricing/",
            {"expected_grand_total_bdt": "150.00"},
            format="json",
        )
        assert conf_res.status_code == status.HTTP_200_OK
        assert conf_res.data["sale"]["is_pricing_confirmed"] is True

        hold_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"hold_reference": "PRICE-CHECK", "reserve_stock": True},
            format="json",
        )
        assert hold_res.status_code == status.HTTP_200_OK
        assert Decimal(hold_res.data["hold_snapshot_grand_total_bdt"]) == Decimal("150.00")

        # Simulate price revision via approved TenantMedicineConfig while cart was held (15.00 -> 18.00 / tab => 180 / strip)
        TenantMedicineConfig.objects.create(
            tenant=branch_a.tenant,
            product=monas,
            custom_mrp_unit=Decimal("18.00"),
            moderation_status="APPROVED",
        )

        # Resume without acknowledgement MUST fail with bilingual prompt & outside-rollback audit log
        stale_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/resume/", {}, format="json")
        assert stale_res.status_code == status.HTTP_400_BAD_REQUEST, stale_res.data
        assert stale_res.data["error_code"] == "ERR_HELD_CART_REQUIRES_CONFIRMATION"
        assert "PRICE_CHANGED" in stale_res.data["details"]["stale_reasons"]
        assert Decimal(stale_res.data["details"]["held_grand_total_bdt"]) == Decimal("150.00")
        assert Decimal(stale_res.data["details"]["live_grand_total_bdt"]) == Decimal("180.00")
        assert stale_res.data["message_bn"]

        # Prompt rejection audit survived rollback
        assert PosHeldCartAudit.objects.filter(
            sale_id=sale_id,
            action="RESUME_STALE_PROMPTED",
            was_rejected=True,
            rejection_code="ERR_HELD_CART_REQUIRES_CONFIRMATION",
        ).exists()

        # Resume with acknowledge_stale=True reprices cleanly to 180.00 and invalidates stale P224 confirmation
        ack_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/resume/",
            {"acknowledge_stale": True, "reprice_to_live": True},
            format="json",
        )
        assert ack_res.status_code == status.HTTP_200_OK, ack_res.data
        assert ack_res.data["lifecycle_state"] == PosSaleLifecycleState.DRAFT
        assert Decimal(ack_res.data["grand_total_bdt"]) == Decimal("180.00")
        assert ack_res.data["is_pricing_confirmed"] is False

    def test_stock_shortage_unsafe_batch_and_cancel_hold(
        self, api_client, branch_a, cashier_a, catalog_fixture
    ):
        napa = catalog_fixture["napa"]
        batch_napa = catalog_fixture["batches"]["napa"]

        # Cart needs 20 units (unreserved hold)
        sale = _open_cart_with_item(api_client, cashier_a, branch_a, napa, qty=20, unit=PosPackagingUnit.TABLET)
        sale_id = sale["sale_id"]
        hold_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"hold_reference": "SHORTAGE-TEST", "reserve_stock": False},
            format="json",
        )
        assert hold_res.status_code == status.HTTP_200_OK

        # Reduce live batch stock below required 20 units
        batch_napa.quantity_base_units = 8
        batch_napa.save(update_fields=["quantity_base_units", "updated_at"])

        prompt_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/resume/", {}, format="json")
        assert prompt_res.status_code == status.HTTP_400_BAD_REQUEST
        assert "STOCK_SHORTAGE" in prompt_res.data["details"]["stale_reasons"]
        assert prompt_res.data["details"]["shortfall_items"][0]["shortfall_base_units"] == 12

        # Requesting forced re-reserve on shortage blocks cleanly with ERR_INSUFFICIENT_DISPENSABLE_STOCK
        force_reserve_fail = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/resume/",
            {"acknowledge_stale": True, "re_reserve_stock": True},
            format="json",
        )
        assert force_reserve_fail.status_code == status.HTTP_400_BAD_REQUEST
        assert force_reserve_fail.data["error_code"] == "ERR_INSUFFICIENT_DISPENSABLE_STOCK"

        # Resuming with acknowledge_stale=True without forced reserve opens DRAFT so cashier can adjust qty
        open_draft = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/resume/",
            {"acknowledge_stale": True, "re_reserve_stock": False},
            format="json",
        )
        assert open_draft.status_code == status.HTTP_200_OK
        assert open_draft.data["lifecycle_state"] == PosSaleLifecycleState.DRAFT

        # Now test unsafe quarantined batch during reserved hold & cancel-hold
        batch_napa.quantity_base_units = 50
        batch_napa.save(update_fields=["quantity_base_units", "updated_at"])
        rehold = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"hold_reference": "QUARANTINE-TEST", "reserve_stock": True},
            format="json",
        )
        assert rehold.status_code == status.HTTP_200_OK
        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 20

        # Quarantine batch while basket is on hold -> resume auto-releases unsafe reservation and prompts
        batch_napa.is_quarantined = True
        batch_napa.status = BatchStatus.QUARANTINED
        batch_napa.save(update_fields=["is_quarantined", "status", "updated_at"])

        unsafe_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/resume/", {}, format="json")
        assert unsafe_res.status_code == status.HTTP_400_BAD_REQUEST
        assert "UNSAFE_BATCH" in unsafe_res.data["details"]["stale_reasons"]
        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 0

        # cancel-hold cleanly voids the held basket
        cancel_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/cancel-hold/",
            {"reason": "Customer cancelled after quarantine alert"},
            format="json",
        )
        assert cancel_res.status_code == status.HTTP_200_OK
        assert cancel_res.data["lifecycle_state"] == PosSaleLifecycleState.VOIDED

    def test_empty_cart_invalid_ttl_version_conflict_and_expire_hold_action(
        self, api_client, branch_a, cashier_a, catalog_fixture
    ):
        api_client.force_authenticate(user=cashier_a)
        empty_cart_res = api_client.post(
            "/api/v2/pos/sales/quick-cart/",
            {"branch": branch_a.branch_id, "force_new": True},
            format="json",
        )
        empty_id = empty_cart_res.data["sale"]["sale_id"]

        # Holding an empty cart MUST fail cleanly
        empty_hold = api_client.post(
            f"/api/v2/pos/sales/{empty_id}/hold/",
            {"hold_reference": "EMPTY-NO"},
            format="json",
        )
        assert empty_hold.status_code == status.HTTP_400_BAD_REQUEST
        assert empty_hold.data["error_code"] == "ERR_EMPTY_CART"
        assert PosHeldCartAudit.objects.filter(
            sale_id=empty_id, action="HOLD_REJECTED", rejection_code="ERR_EMPTY_CART"
        ).exists()

        # Add item & check invalid TTL + version conflicts
        napa = catalog_fixture["napa"]
        batch_napa = catalog_fixture["batches"]["napa"]
        sale = _open_cart_with_item(api_client, cashier_a, branch_a, napa, qty=5, unit=PosPackagingUnit.TABLET)
        sale_id = sale["sale_id"]

        bad_ttl_zero = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"hold_ttl_minutes": 0},
            format="json",
        )
        assert bad_ttl_zero.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_ttl_zero.data["error_code"] == "ERR_INVALID_HOLD_TTL"

        bad_ttl_float = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"hold_ttl_minutes": 15.5},
            format="json",
        )
        assert bad_ttl_float.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_ttl_float.data["error_code"] == "ERR_INVALID_HOLD_TTL"

        bad_res_gt_hold = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"hold_ttl_minutes": 10, "reservation_ttl_minutes": 20},
            format="json",
        )
        assert bad_res_gt_hold.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_res_gt_hold.data["error_code"] == "ERR_INVALID_HOLD_TTL"

        stale_ver = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"expected_version": 999},
            format="json",
        )
        assert stale_ver.status_code == status.HTTP_400_BAD_REQUEST
        assert stale_ver.data["error_code"] == "ERR_CART_VERSION_CONFLICT"

        # Valid hold then explicit expire-hold releases reservation cleanly
        hold_ok = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/hold/",
            {"hold_reference": "EXP-ONE", "reserve_stock": True},
            format="json",
        )
        assert hold_ok.status_code == status.HTTP_200_OK
        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 5

        exp_one = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/expire-hold/",
            {"void_hold": False, "reason": "Cashier manual release"},
            format="json",
        )
        assert exp_one.status_code == status.HTTP_200_OK
        assert exp_one.data["released_base_units"] == 5
        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 0

    def test_rbac_cross_branch_cross_tenant_suspended_tenant_and_audit_immutability(
        self,
        api_client,
        branch_a,
        branch_a2,
        branch_b,
        branch_susp,
        cashier_a,
        cashier_a2,
        clerk_a,
        finance_a,
        auditor_a,
        cashier_b,
        cashier_susp,
        catalog_fixture,
    ):
        napa = catalog_fixture["napa"]
        sale = _open_cart_with_item(api_client, cashier_a, branch_a, napa, qty=2, unit=PosPackagingUnit.TABLET)
        sale_id = sale["sale_id"]

        # Read-only / clerk roles CANNOT hold/resume/expire/cancel
        for denied_user in (clerk_a, finance_a, auditor_a):
            api_client.force_authenticate(user=denied_user)
            r_hold = api_client.post(f"/api/v2/pos/sales/{sale_id}/hold/", {"hold_reference": "NOPE"}, format="json")
            assert r_hold.status_code == status.HTTP_403_FORBIDDEN
            r_sweep = api_client.post("/api/v2/pos/sales/expire-stale-holds/", {"branch": branch_a.branch_id}, format="json")
            assert r_sweep.status_code == status.HTTP_403_FORBIDDEN

        # Put sale on hold cleanly by cashier_a
        api_client.force_authenticate(user=cashier_a)
        held = api_client.post(f"/api/v2/pos/sales/{sale_id}/hold/", {"hold_reference": "RBAC-HELD"}, format="json")
        assert held.status_code == status.HTTP_200_OK

        # Finance / Auditor CAN inspect read-only held list & audits
        for reader in (finance_a, auditor_a):
            api_client.force_authenticate(user=reader)
            assert api_client.get(f"/api/v2/pos/sales/held/?branch={branch_a.branch_id}").status_code == status.HTTP_200_OK
            assert api_client.get(f"/api/v2/pos/sales/{sale_id}/hold-audits/").status_code == status.HTTP_200_OK

        # Cross-branch cashier in same tenant denied cleanly
        api_client.force_authenticate(user=cashier_a2)
        assert api_client.get(f"/api/v2/pos/sales/held/?branch={branch_a.branch_id}").status_code == status.HTTP_403_FORBIDDEN
        assert api_client.post("/api/v2/pos/sales/expire-stale-holds/", {"branch": branch_a.branch_id}, format="json").status_code == status.HTTP_403_FORBIDDEN

        # Cross-tenant user gets 404 on foreign sale
        api_client.force_authenticate(user=cashier_b)
        assert api_client.post(f"/api/v2/pos/sales/{sale_id}/resume/", {}, format="json").status_code == status.HTTP_404_NOT_FOUND
        assert api_client.get(f"/api/v2/pos/sales/held/?branch={branch_b.branch_id}").data["count"] == 0

        # Suspended tenant blocked (403)
        api_client.force_authenticate(user=cashier_susp)
        susp_res = api_client.get(f"/api/v2/pos/sales/held/?branch={branch_susp.branch_id}")
        assert susp_res.status_code == status.HTTP_403_FORBIDDEN
        assert susp_res.data["error_code"] == "ERR_SUBSCRIPTION_SUSPENDED"

        # Append-only PosHeldCartAudit immutability check
        audit = PosHeldCartAudit.objects.filter(sale_id=sale_id).first()
        assert audit is not None
        assert len(audit.sha256_hash) == 64
        with pytest.raises(DjangoValidationError):
            audit.hold_reference = "TAMPERED"
            audit.save()
        with pytest.raises(DjangoValidationError):
            audit.delete()

