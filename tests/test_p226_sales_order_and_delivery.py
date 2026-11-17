"""
backend/tests/test_p226_sales_order_and_delivery.py
Acceptance & integration tests for P226: Sales order and delivery (Phase 12).

Covers:
1. Preorder pickup reserve -> prepaid payment -> pack -> dispatch auto-finalize -> deliver (POD)
   with exact BatchStock.reserved_quantity_base_units & StockLedgerService conservation.
2. Home delivery with delivery_fee_bdt Decimal math + SHA-256 pricing binding, PATCH mutation,
   failed delivery attempt, out-for-delivery retry, COD bKash collection & finalization,
   and post-delivery soft-archive (while blocking active in-transit archive).
3. Address privacy masking per FieldVisibilityService (Cashier/Finance Officer masked,
   Owner/Manager/Pharmacist unmasked), Finance reveal denial, Cashier operational
   reveal-address logged without raw PII in audits, redact-address & terminal reveal lock.
4. Rx verification gate before packing/dispatching, cancel releasing FEFO reservations,
   return-to-branch reversing finalized invoice & restoring stock, immutable post-dispatch guard.
5. RBAC, BASIC-plan DUE feature gate, suspended subscription lock, cross-branch/cross-tenant
   isolation, duplicate idempotency replay, optimistic version conflicts, and immutable
   PosDeliveryAudit records.
"""

from __future__ import annotations

import datetime
import json
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
    MedicineProduct,
    PackagingHierarchyNode,
    PlanTier,
    PosDeliveryAudit,
    PosDeliveryFulfillmentState,
    PosDeliveryOrder,
    PosPackagingUnit,
    PosSaleLifecycleState,
    StockLedgerEntry,
    StockMovementType,
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
        tenant_id=f"ten-p226-a-{uid}",
        name="Dhaka Health Pharmacy",
        subdomain=f"dhaka-p226-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p226-b-{uid}",
        name="Sylhet Med Mart",
        subdomain=f"sylhet-p226-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_basic(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-p226-bas-{uid}",
        name="Basic Plan Pharmacy",
        subdomain=f"basic-p226-{uid}",
        plan_tier=PlanTier.BASIC,
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p226-a1-{uid}",
        tenant=tenant_a,
        name="Dhanmondi Branch",
        license_number=f"DGDA-P226A1-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p226-a2-{uid}",
        tenant=tenant_a,
        name="Banani Branch",
        license_number=f"DGDA-P226A2-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p226-b1-{uid}",
        tenant=tenant_b,
        name="Sylhet Main Branch",
        license_number=f"DGDA-P226B1-{uid}",
        is_active=True,
    )


@pytest.fixture
def branch_basic(tenant_basic):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-p226-bas-{uid}",
        tenant=tenant_basic,
        name="Basic Main Branch",
        license_number=f"DGDA-P226BAS-{uid}",
        is_active=True,
    )


def _mk_user(tenant, branch, role: str, prefix: str):
    uid = uuid.uuid4().hex[:8]
    return PharmacyUser.objects.create_user(
        email=f"{prefix}-{uid}@pharmacy.bd",
        phone_number=f"+88017{uuid.uuid4().int % 100000000:08d}",
        password="TestPass123!@#",
        full_name=f"{prefix.title()} User",
        role=role,
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id,
        is_active=True,
    )


@pytest.fixture
def users_a(tenant_a, branch_a, branch_a2):
    return {
        "owner": _mk_user(tenant_a, branch_a, UserRole.PHARMACY_OWNER, "owner"),
        "manager": _mk_user(tenant_a, branch_a, UserRole.BRANCH_MANAGER, "mgr"),
        "pharmacist": _mk_user(tenant_a, branch_a, UserRole.STAFF_PHARMACIST, "pharm"),
        "cashier": _mk_user(tenant_a, branch_a, UserRole.CASHIER, "cashier"),
        "finance": _mk_user(tenant_a, branch_a, UserRole.FINANCE_OFFICER, "fin"),
        "clerk": _mk_user(tenant_a, branch_a, UserRole.INVENTORY_CLERK, "clk"),
        "cashier_a2": _mk_user(tenant_a, branch_a2, UserRole.CASHIER, "cashiera2"),
    }


@pytest.fixture
def cashier_b(tenant_b, branch_b):
    return _mk_user(tenant_b, branch_b, UserRole.CASHIER, "cashierb")


@pytest.fixture
def cashier_basic(tenant_basic, branch_basic):
    return _mk_user(tenant_basic, branch_basic, UserRole.CASHIER, "cashierbas")


@pytest.fixture
def catalog_fixture(db, tenant_a, branch_a, branch_basic, tenant_basic):
    uid = uuid.uuid4().hex[:6]
    today = timezone.now().date()
    napa = MedicineProduct.objects.create(
        product_id=f"med-p226-napa-{uid}",
        brand_name="Napa Extend",
        generic_name="Paracetamol",
        strength="665mg",
        dosage_form="Tablet",
        barcode=f"8942261{uid}",
        mrp_unit=Decimal("2.00"),
        cost_unit=Decimal("1.40"),
    )
    PackagingHierarchyNode.objects.create(
        node_id=f"node-p226-napa-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=napa,
        parent_unit="STRIP",
        child_unit="TABLET",
        conversion_factor=10,
        tier_level=1,
        is_base_level=True,
        effective_from=today - datetime.timedelta(days=5),
        is_active=True,
    )
    PackagingHierarchyNode.objects.create(
        node_id=f"node-p226-bas-{uid}",
        tenant=tenant_basic,
        branch=branch_basic,
        product=napa,
        parent_unit="STRIP",
        child_unit="TABLET",
        conversion_factor=10,
        tier_level=1,
        is_base_level=True,
        effective_from=today - datetime.timedelta(days=5),
        is_active=True,
    )

    antibiotic = MedicineProduct.objects.create(
        product_id=f"med-p226-azith-{uid}",
        brand_name="Azithrocin 500",
        generic_name="Azithromycin",
        strength="500mg",
        dosage_form="Tablet",
        barcode=f"8942262{uid}",
        mrp_unit=Decimal("35.00"),
        cost_unit=Decimal("26.00"),
        is_prescription_only=True,
        is_antibiotic=True,
    )

    b_napa = BatchStock.objects.create(
        batch_id=f"bat-226-napa-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=napa,
        batch_number=f"LOT-NAPA-{uid[:4].upper()}",
        expiry_date=today + datetime.timedelta(days=365),
        purchase_date=today,
        quantity_base_units=100,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("1.4000"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )
    b_abx = BatchStock.objects.create(
        batch_id=f"bat-226-abx-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=antibiotic,
        batch_number=f"LOT-ABX-{uid[:4].upper()}",
        expiry_date=today + datetime.timedelta(days=300),
        purchase_date=today,
        quantity_base_units=40,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("26.0000"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )
    BatchStock.objects.create(
        batch_id=f"bat-226-bas-{uid}",
        tenant=tenant_basic,
        branch=branch_basic,
        product=napa,
        batch_number=f"LOT-BAS-{uid[:4].upper()}",
        expiry_date=today + datetime.timedelta(days=300),
        purchase_date=today,
        quantity_base_units=50,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("1.5000"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )
    return {
        "napa": napa,
        "antibiotic": antibiotic,
        "batch_napa": b_napa,
        "batch_abx": b_abx,
    }


class TestP226SalesOrderAndDelivery:
    def test_preorder_pickup_reserve_pack_deliver_prepaid_reconciliation(
        self, api_client, branch_a, users_a, catalog_fixture
    ):
        cashier = users_a["cashier"]
        napa = catalog_fixture["napa"]
        batch_napa = catalog_fixture["batch_napa"]

        api_client.force_authenticate(user=cashier)
        create_res = api_client.post(
            "/api/v2/pos/deliveries/",
            {
                "branch": branch_a.branch_id,
                "fulfillment_mode": "PREORDER_PICKUP",
                "delivery_method": "BRANCH_PICKUP",
                "reserve_stock": True,
                "reservation_ttl_minutes": 180,
                "recipient_name": "Karim Hasan",
                "recipient_phone": "01711223344",
                "delivery_area": "Dhanmondi",
                "delivery_city": "Dhaka",
                "lines": [
                    {
                        "product_id": napa.product_id,
                        "packaging_unit": PosPackagingUnit.STRIP,
                        "quantity_units": 3,
                    }
                ],
            },
            format="json",
        )
        assert create_res.status_code == status.HTTP_201_CREATED
        delv = create_res.data
        delv_id = delv["delivery_id"]
        sale_id = delv["sale_id"]

        assert delv["fulfillment_mode"] == "PREORDER_PICKUP"
        assert delv["fulfillment_state"] == PosDeliveryFulfillmentState.STOCK_RESERVED
        assert delv["stock_reserved"] is True
        assert delv["sale_grand_total_bdt"] == "60.00"
        assert delv["cod_expected_bdt"] == "60.00"

        # Verify FEFO stock reservation (3 strips = 30 base units reserved, qty still 100)
        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 30
        assert batch_napa.quantity_base_units == 100

        # Prepaid cash capture on sale before pack & dispatch
        pay_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {
                "tender_type": "CASH",
                "amount_bdt": "60.00",
                "idempotency_key": f"pay-pre-{delv_id}",
            },
            format="json",
        )
        assert pay_res.status_code == status.HTTP_201_CREATED

        pack_res = api_client.post(f"/api/v2/pos/deliveries/{delv_id}/pack/", {}, format="json")
        assert pack_res.status_code == status.HTTP_200_OK
        assert pack_res.data["fulfillment_state"] == PosDeliveryFulfillmentState.PACKED

        disp_res = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/dispatch/",
            {"note": "Ready at counter 2"},
            format="json",
        )
        assert disp_res.status_code == status.HTTP_200_OK
        assert disp_res.data["fulfillment_state"] == PosDeliveryFulfillmentState.DISPATCHED
        # Prepaid dispatch auto-finalizes invoice and posts StockLedger SALE_DISPENSE
        assert disp_res.data["sale_lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        assert disp_res.data["linked_invoice_id"] is not None
        assert disp_res.data["cod_expected_bdt"] == "0.00"

        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 0
        assert batch_napa.quantity_base_units == 70
        assert StockLedgerEntry.objects.filter(
            source_entity_type="POS_SALE_ORDER",
            source_entity_id=sale_id,
            movement_type=StockMovementType.SALE_DISPENSE,
            quantity_delta=-30,
        ).exists()

        deliver_res = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/deliver/",
            {"proof_of_delivery_ref": "OTP-4491", "proof_recipient_relation": "SELF"},
            format="json",
        )
        assert deliver_res.status_code == status.HTTP_200_OK
        assert deliver_res.data["fulfillment_state"] == PosDeliveryFulfillmentState.DELIVERED
        assert deliver_res.data["proof_of_delivery_ref"] == "OTP-4491"

    def test_home_delivery_cod_fee_math_patch_failed_attempt_and_soft_archive(
        self, api_client, branch_a, users_a, catalog_fixture
    ):
        cashier = users_a["cashier"]
        napa = catalog_fixture["napa"]
        batch_napa = catalog_fixture["batch_napa"]

        api_client.force_authenticate(user=cashier)
        # Missing address for HOME_DELIVERY fails cleanly
        bad_addr = api_client.post(
            "/api/v2/pos/deliveries/",
            {
                "branch": branch_a.branch_id,
                "fulfillment_mode": "HOME_DELIVERY",
                "recipient_name": "Nadia Islam",
                "recipient_phone": "01812345678",
                "lines": [{"product_id": napa.product_id, "packaging_unit": "STRIP", "quantity_units": 2}],
            },
            format="json",
        )
        assert bad_addr.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_addr.data["error_code"] == "ERR_DELIVERY_ADDRESS_REQUIRED"

        # Float delivery fee rejected strictly
        bad_fee = api_client.post(
            "/api/v2/pos/deliveries/",
            {
                "branch": branch_a.branch_id,
                "fulfillment_mode": "HOME_DELIVERY",
                "recipient_name": "Nadia Islam",
                "recipient_phone": "01812345678",
                "delivery_address_line": "Flat 4B, House 19, Road 8A, Dhanmondi",
                "delivery_fee_bdt": 50.5,
                "lines": [{"product_id": napa.product_id, "packaging_unit": "STRIP", "quantity_units": 2}],
            },
            format="json",
        )
        assert bad_fee.status_code == status.HTTP_400_BAD_REQUEST
        assert bad_fee.data["error_code"] == "ERR_INVALID_DELIVERY_FEE"

        # Open quick cart first (2 strips = 40.00 BDT) -> attach HOME_DELIVERY + 80.00 fee = 120.00 BDT
        cart = api_client.post(
            "/api/v2/pos/sales/quick-cart/", {"branch": branch_a.branch_id, "force_new": True}, format="json"
        ).data["sale"]
        api_client.post(
            f"/api/v2/pos/sales/{cart['sale_id']}/scan-add/",
            {"product_id": napa.product_id, "packaging_unit": "STRIP", "quantity_delta": 2},
            format="json",
        )
        now = timezone.now()
        create_res = api_client.post(
            "/api/v2/pos/deliveries/",
            {
                "branch": branch_a.branch_id,
                "sale_id": cart["sale_id"],
                "fulfillment_mode": "HOME_DELIVERY",
                "delivery_method": "OWN_RIDER",
                "reserve_stock": True,
                "recipient_name": "Nadia Islam",
                "recipient_phone": "01812345678",
                "delivery_address_line": "Flat 4B, House 19, Road 8A",
                "delivery_area": "Dhanmondi",
                "delivery_city": "Dhaka",
                "delivery_fee_bdt": "80.00",
                "scheduled_window_start": (now + datetime.timedelta(hours=1)).isoformat(),
                "scheduled_window_end": (now + datetime.timedelta(hours=3)).isoformat(),
            },
            format="json",
        )
        assert create_res.status_code == status.HTTP_201_CREATED
        delv = create_res.data
        delv_id = delv["delivery_id"]
        assert delv["sale_grand_total_bdt"] == "120.00"
        assert delv["delivery_fee_bdt"] == "80.00"
        assert delv["cod_expected_bdt"] == "120.00"

        # Pricing breakdown & hash include delivery_fee_bdt accurately
        brk = api_client.get(f"/api/v2/pos/sales/{cart['sale_id']}/pricing-breakdown/").data
        assert brk["delivery_fee_bdt"] == "80.00"
        assert brk["grand_total_bdt"] == "120.00"

        # Mutable PATCH updates fee to 100.00 -> total becomes 140.00 BDT
        patch_res = api_client.patch(
            f"/api/v2/pos/deliveries/{delv_id}/",
            {"delivery_fee_bdt": "100.00", "expected_version": delv["version"]},
            format="json",
        )
        assert patch_res.status_code == status.HTTP_200_OK
        assert patch_res.data["sale_grand_total_bdt"] == "140.00"
        assert patch_res.data["cod_expected_bdt"] == "140.00"

        # Pack cleanly
        pack_res = api_client.post(f"/api/v2/pos/deliveries/{delv_id}/pack/", {}, format="json")
        assert pack_res.status_code == status.HTTP_200_OK

        # Dispatch without rider/courier fails cleanly
        no_rider = api_client.post(f"/api/v2/pos/deliveries/{delv_id}/dispatch/", {}, format="json")
        assert no_rider.status_code == status.HTTP_400_BAD_REQUEST
        assert no_rider.data["error_code"] == "ERR_DISPATCH_ASSIGNEE_REQUIRED"

        # Valid dispatch keeps COD reservation locked while in transit
        disp_res = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/dispatch/",
            {"rider_name": "Sumon Mia", "rider_phone": "01900112233"},
            format="json",
        )
        assert disp_res.status_code == status.HTTP_200_OK
        assert disp_res.data["fulfillment_state"] == PosDeliveryFulfillmentState.DISPATCHED
        assert disp_res.data["stock_reserved"] is True
        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 20

        # Active dispatched order cannot be directly PATCHed or DELETE/archived
        locked_patch = api_client.patch(
            f"/api/v2/pos/deliveries/{delv_id}/", {"delivery_area": "Mirpur"}, format="json"
        )
        assert locked_patch.status_code == status.HTTP_400_BAD_REQUEST
        assert locked_patch.data["error_code"] == "ERR_IMMUTABLE_DELIVERY_STATE"

        locked_del = api_client.delete(f"/api/v2/pos/deliveries/{delv_id}/")
        assert locked_del.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

        # Out-for-delivery -> Failed Attempt -> Redeliver OUT_FOR_DELIVERY
        assert (
            api_client.post(f"/api/v2/pos/deliveries/{delv_id}/out-for-delivery/", {}, format="json").status_code
            == status.HTTP_200_OK
        )
        fail_res = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/fail-attempt/",
            {"reason": "Recipient phone unreachable at gate"},
            format="json",
        )
        assert fail_res.status_code == status.HTTP_200_OK
        assert fail_res.data["failed_attempt_count"] == 1
        assert fail_res.data["fulfillment_state"] == PosDeliveryFulfillmentState.FAILED_ATTEMPT

        assert (
            api_client.post(f"/api/v2/pos/deliveries/{delv_id}/out-for-delivery/", {}, format="json").status_code
            == status.HTTP_200_OK
        )

        # Deliver with short COD rejected
        short_cod = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/deliver/",
            {"proof_of_delivery_ref": "BKASH-TX-1", "cod_collected_bdt": "100.00", "cod_tender_type": "BKASH"},
            format="json",
        )
        assert short_cod.status_code == status.HTTP_400_BAD_REQUEST
        assert short_cod.data["error_code"] == "ERR_INSUFFICIENT_COD_AMOUNT"

        # Exact COD settles payment, finalizes SaleInvoice, and releases reserved -> SALE_DISPENSE
        ok_deliv = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/deliver/",
            {
                "proof_of_delivery_ref": "BKASH-TX-991",
                "cod_collected_bdt": "140.00",
                "cod_tender_type": "BKASH",
                "redact_address_after_delivery": True,
            },
            format="json",
        )
        assert ok_deliv.status_code == status.HTTP_200_OK
        assert ok_deliv.data["fulfillment_state"] == PosDeliveryFulfillmentState.DELIVERED
        assert ok_deliv.data["sale_lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        assert ok_deliv.data["cod_collected_bdt"] == "140.00"
        assert ok_deliv.data["delivery_address_line"] == "[REDACTED_AFTER_COMPLETION]"

        batch_napa.refresh_from_db()
        assert batch_napa.reserved_quantity_base_units == 0
        assert batch_napa.quantity_base_units == 80

        # Soft-archive terminal order cleanly
        arch_res = api_client.delete(f"/api/v2/pos/deliveries/{delv_id}/?reason=Completed+cleanup")
        assert arch_res.status_code == status.HTTP_200_OK
        assert arch_res.data["is_archived"] is True

    def test_address_privacy_masking_audited_reveal_and_terminal_lock(
        self, api_client, branch_a, users_a, catalog_fixture
    ):
        cashier = users_a["cashier"]
        finance = users_a["finance"]
        manager = users_a["manager"]
        napa = catalog_fixture["napa"]

        api_client.force_authenticate(user=cashier)
        delv = api_client.post(
            "/api/v2/pos/deliveries/",
            {
                "branch": branch_a.branch_id,
                "fulfillment_mode": "HOME_DELIVERY",
                "recipient_name": "Farhana Rahman",
                "recipient_phone": "01799887766",
                "delivery_address_line": "Apartment 7C, House 42, Lake Circus",
                "delivery_area": "Kalabagan",
                "delivery_city": "Dhaka",
                "delivery_landmark_notes": "Opposite green gate, call on arrival",
                "lines": [{"product_id": napa.product_id, "packaging_unit": "STRIP", "quantity_units": 1}],
            },
            format="json",
        ).data
        delv_id = delv["delivery_id"]

        # Create/GET response masks PII for CASHIER while preserving zone area/city
        assert delv["_address_privacy_masked"] is True
        assert "Apartment 7C" not in delv["delivery_address_line"]
        assert delv["recipient_phone"] == "017****7766"
        assert delv["delivery_area"] == "Kalabagan"

        # FINANCE_OFFICER sees masked PII and is DENIED operational reveal-address
        api_client.force_authenticate(user=finance)
        fin_get = api_client.get(f"/api/v2/pos/deliveries/{delv_id}/")
        assert fin_get.status_code == status.HTTP_200_OK
        assert fin_get.data["_address_privacy_masked"] is True
        assert "Lake Circus" not in fin_get.data["delivery_address_line"]

        fin_reveal = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/reveal-address/",
            {"reason": "Checking finance record"},
            format="json",
        )
        assert fin_reveal.status_code == status.HTTP_403_FORBIDDEN

        # BRANCH_MANAGER sees unmasked PII directly via patient_pii_visibility=True
        api_client.force_authenticate(user=manager)
        mgr_get = api_client.get(f"/api/v2/pos/deliveries/{delv_id}/")
        assert mgr_get.status_code == status.HTTP_200_OK
        assert mgr_get.data["_address_privacy_masked"] is False
        assert mgr_get.data["delivery_address_line"] == "Apartment 7C, House 42, Lake Circus"

        # CASHIER operational reveal requires reason & logs PII-free PosDeliveryAudit
        api_client.force_authenticate(user=cashier)
        no_reason = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/reveal-address/", {"reason": ""}, format="json"
        )
        assert no_reason.status_code == status.HTTP_400_BAD_REQUEST
        assert no_reason.data["error_code"] == "ERR_REASON_REQUIRED"

        ok_reveal = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/reveal-address/",
            {"reason": "Printing rider handover slip"},
            format="json",
        )
        assert ok_reveal.status_code == status.HTTP_200_OK
        assert ok_reveal.data["delivery_address_line"] == "Apartment 7C, House 42, Lake Circus"
        assert ok_reveal.data["_address_privacy_masked"] is False

        # Ensure audit log NEVER stored raw street address or full phone
        audit_row = PosDeliveryAudit.objects.get(delivery_id=delv_id, action="ADDRESS_REVEALED")
        serialized_meta = json.dumps(audit_row.metadata_json)
        assert "Apartment 7C" not in serialized_meta
        assert "01799887766" not in serialized_meta

        # Cancel + redact locks future reveal attempts with outside-rollback rejection audit
        api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/cancel/",
            {"reason": "Customer postponed", "redact_address": True},
            format="json",
        )
        locked_reveal = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/reveal-address/",
            {"reason": "Late check"},
            format="json",
        )
        assert locked_reveal.status_code == status.HTTP_400_BAD_REQUEST
        assert locked_reveal.data["error_code"] == "ERR_ADDRESS_PRIVACY_LOCKED"
        assert PosDeliveryAudit.objects.filter(
            delivery_id=delv_id, action="ADDRESS_REVEAL_DENIED", was_rejected=True
        ).exists()

    def test_rx_gate_cancel_release_and_finalized_return_to_branch_conservation(
        self, api_client, branch_a, users_a, catalog_fixture
    ):
        cashier = users_a["cashier"]
        pharmacist = users_a["pharmacist"]
        manager = users_a["manager"]
        abx = catalog_fixture["antibiotic"]
        batch_abx = catalog_fixture["batch_abx"]

        api_client.force_authenticate(user=cashier)
        create_res = api_client.post(
            "/api/v2/pos/deliveries/",
            {
                "branch": branch_a.branch_id,
                "fulfillment_mode": "HOME_DELIVERY",
                "reserve_stock": True,
                "recipient_name": "Tariqul Islam",
                "recipient_phone": "01611002233",
                "delivery_address_line": "Road 14, Block C, Banani",
                "lines": [{"product_id": abx.product_id, "packaging_unit": "TABLET", "quantity_units": 6}],
            },
            format="json",
        )
        assert create_res.status_code == status.HTTP_201_CREATED
        delv_id = create_res.data["delivery_id"]
        sale_id = create_res.data["sale_id"]

        batch_abx.refresh_from_db()
        assert batch_abx.reserved_quantity_base_units == 6

        # Unverified Rx/antibiotic order CANNOT be packed
        rx_blocked = api_client.post(f"/api/v2/pos/deliveries/{delv_id}/pack/", {}, format="json")
        assert rx_blocked.status_code == status.HTTP_400_BAD_REQUEST
        assert rx_blocked.data["error_code"] == "ERR_RX_VERIFICATION_REQUIRED"

        # Pharmacist verifies Rx -> Cashier can pack cleanly
        api_client.force_authenticate(user=pharmacist)
        vrx = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/verify-rx/",
            {"notes": "Verified antibiotic Rx #901"},
            format="json",
        )
        assert vrx.status_code == status.HTTP_200_OK

        api_client.force_authenticate(user=cashier)
        assert api_client.post(f"/api/v2/pos/deliveries/{delv_id}/pack/", {}, format="json").status_code == 200

        # Prepaid payment -> Dispatch finalizes sale & dispenses 6 units (qty=34, reserved=0)
        api_client.post(
            f"/api/v2/pos/sales/{sale_id}/record-payment/",
            {"tender_type": "CASH", "amount_bdt": "210.00", "idempotency_key": f"pay-rx-{delv_id}"},
            format="json",
        )
        disp = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/dispatch/",
            {"courier_partner_name": "Steadfast Rx Express"},
            format="json",
        )
        assert disp.status_code == status.HTTP_200_OK
        assert disp.data["sale_lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        batch_abx.refresh_from_db()
        assert batch_abx.quantity_base_units == 34
        assert batch_abx.reserved_quantity_base_units == 0

        # Cashier cannot reverse finalized delivery on return-to-branch; Manager can
        cashier_ret = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/return-to-branch/",
            {"reason": "Customer refused parcel"},
            format="json",
        )
        assert cashier_ret.status_code == status.HTTP_403_FORBIDDEN

        api_client.force_authenticate(user=manager)
        mgr_ret = api_client.post(
            f"/api/v2/pos/deliveries/{delv_id}/return-to-branch/",
            {"reason": "Customer refused parcel"},
            format="json",
        )
        assert mgr_ret.status_code == status.HTTP_200_OK
        assert mgr_ret.data["fulfillment_state"] == PosDeliveryFulfillmentState.RETURNED_TO_BRANCH
        assert mgr_ret.data["sale_lifecycle_state"] == PosSaleLifecycleState.REVERSED

        # Stock fully restored to 40 units via CUSTOMER_RETURN
        batch_abx.refresh_from_db()
        assert batch_abx.quantity_base_units == 40
        assert StockLedgerEntry.objects.filter(
            source_entity_type="POS_SALE_REVERSAL",
            source_entity_id=sale_id,
            movement_type=StockMovementType.CUSTOMER_RETURN,
            quantity_delta=6,
        ).exists()

    def test_rbac_plan_gate_isolation_idempotency_version_and_audit_immutability(
        self, api_client, tenant_a, branch_a, branch_a2, branch_b, branch_basic, users_a, cashier_b, cashier_basic, catalog_fixture
    ):
        napa = catalog_fixture["napa"]

        # INVENTORY_CLERK denied entirely on POS deliveries
        api_client.force_authenticate(user=users_a["clerk"])
        assert api_client.get("/api/v2/pos/deliveries/").status_code == status.HTTP_403_FORBIDDEN

        # FINANCE_OFFICER read-only: allowed GET, denied POST write
        api_client.force_authenticate(user=users_a["finance"])
        assert api_client.get("/api/v2/pos/deliveries/").status_code == status.HTTP_200_OK
        assert (
            api_client.post(
                "/api/v2/pos/deliveries/",
                {
                    "branch": branch_a.branch_id,
                    "fulfillment_mode": "PREORDER_PICKUP",
                    "recipient_name": "Test",
                    "recipient_phone": "01700000000",
                    "lines": [{"product_id": napa.product_id, "packaging_unit": "TABLET", "quantity_units": 1}],
                },
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # BASIC plan blocks DUE tender on preorder/delivery
        api_client.force_authenticate(user=cashier_basic)
        bas_delv = api_client.post(
            "/api/v2/pos/deliveries/",
            {
                "branch": branch_basic.branch_id,
                "fulfillment_mode": "PREORDER_PICKUP",
                "recipient_name": "Basic Customer",
                "recipient_phone": "01711009988",
                "lines": [{"product_id": napa.product_id, "packaging_unit": "TABLET", "quantity_units": 2}],
            },
            format="json",
        )
        assert bas_delv.status_code == status.HTTP_201_CREATED
        due_locked = api_client.post(
            f"/api/v2/pos/sales/{bas_delv.data['sale_id']}/record-payment/",
            {"tender_type": "DUE", "amount_bdt": "4.00"},
            format="json",
        )
        assert due_locked.status_code == status.HTTP_403_FORBIDDEN
        assert due_locked.data["error_code"] == "ERR_SUBSCRIPTION_FEATURE_LOCKED"

        # Idempotent replay creates single PosDeliveryOrder
        api_client.force_authenticate(user=users_a["cashier"])
        idem = f"idem-delv-{uuid.uuid4().hex[:8]}"
        payload = {
            "branch": branch_a.branch_id,
            "idempotency_key": idem,
            "fulfillment_mode": "PREORDER_PICKUP",
            "recipient_name": "Sadia Afrin",
            "recipient_phone": "01722334455",
            "lines": [{"product_id": napa.product_id, "packaging_unit": "TABLET", "quantity_units": 5}],
        }
        first = api_client.post("/api/v2/pos/deliveries/", payload, format="json")
        replay = api_client.post("/api/v2/pos/deliveries/", payload, format="json")
        assert first.status_code == status.HTTP_201_CREATED
        assert replay.status_code == status.HTTP_200_OK
        assert first.data["delivery_id"] == replay.data["delivery_id"]
        assert PosDeliveryOrder.objects.filter(idempotency_key=idem).count() == 1

        delv_id = first.data["delivery_id"]

        # Stale version conflict rejected cleanly
        ver_conflict = api_client.patch(
            f"/api/v2/pos/deliveries/{delv_id}/",
            {"delivery_area": "Gulshan", "expected_version": 999},
            format="json",
        )
        assert ver_conflict.status_code == status.HTTP_400_BAD_REQUEST
        assert ver_conflict.data["error_code"] == "ERR_CART_VERSION_CONFLICT"

        # Cross-branch isolation
        api_client.force_authenticate(user=users_a["cashier_a2"])
        assert (
            api_client.get(f"/api/v2/pos/deliveries/?branch={branch_a.branch_id}").status_code
            in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        )
        list_b = api_client.get("/api/v2/pos/deliveries/")
        assert list_b.status_code == status.HTTP_200_OK
        assert all(item["delivery_id"] != delv_id for item in list_b.data.get("results", []))
        assert api_client.get(f"/api/v2/pos/deliveries/{delv_id}/").status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        )

        # Cross-tenant isolation
        api_client.force_authenticate(user=cashier_b)
        assert api_client.get(f"/api/v2/pos/deliveries/{delv_id}/").status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        )
        assert (
            api_client.get(f"/api/v2/pos/deliveries/?branch={branch_a.branch_id}").status_code
            in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        )
        list_b = api_client.get("/api/v2/pos/deliveries/")
        assert list_b.status_code == status.HTTP_200_OK
        assert all(item["delivery_id"] != delv_id for item in list_b.data.get("results", []))

        # Audit immutability enforced at ORM level
        audit = PosDeliveryAudit.objects.filter(delivery_id=delv_id).first()
        assert audit is not None
        audit.reason = "Tampered"
        with pytest.raises(DjangoValidationError):
            audit.save()
        with pytest.raises(DjangoValidationError):
            audit.delete()

        # Suspended tenant locks write operations cleanly
        tenant_a.status = TenantLifecycleStatus.SUSPENDED
        tenant_a.save(update_fields=["status", "updated_at"])
        api_client.force_authenticate(user=users_a["cashier"])
        susp_res = api_client.post(f"/api/v2/pos/deliveries/{delv_id}/pack/", {}, format="json")
        assert susp_res.status_code == status.HTTP_403_FORBIDDEN
        susp_body = susp_res.json() if hasattr(susp_res, "json") else susp_res.data
        assert susp_body.get("error_code") == "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED"
        assert susp_body.get("details", {}).get("tenant_status") == "SUSPENDED"
