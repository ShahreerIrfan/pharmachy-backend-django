"""
backend/tests/test_p256_recall_alert_dispensing.py

Architecture Decision: ADR-0256 (P256).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Batch Recall Alert to Dispensing:
1. Valid recall publication: Automated stock quarantine & open reservation eviction.
2. POS dispensing gate blocks recalled batches with clinical alert.
3. Incomplete and invalid inputs: short batch, missing product/reason, invalid class.
4. RBAC clearance & cross-tenant isolation: Cashier blocked, Tenant B blocked.
5. Historical sales impact lookup WITHOUT rewriting immutable past sales history.
6. Audited recall lifting & stock restoration lifecycle.
"""

from decimal import Decimal
import datetime
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchRecallNotification,
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    RecallClass,
    RecallNoticeStatus,
    RecallSource,
    SaleInvoice,
    SaleLotAllocation,
    SaleLotAllocationStatus,
    StockReservation,
    StockReservationStatus,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def recall_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p256-a-{uid}", name="Recall Test Pharmacy A", subdomain=f"p256a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p256-a-{uid}", name="Gulshan Main Branch", branch_code="GL01"
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    manager_a = PharmacyUser.objects.create_user(
        email=f"manager_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    pharmacist_a = PharmacyUser.objects.create_user(
        email=f"pharmacist_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-tenant boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p256-b-{uid}", name="Recall Test Pharmacy B", subdomain=f"p256b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p256-b-{uid}", name="Dhanmondi Branch", branch_code="DH01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    product = MedicineProduct.objects.create(
        product_id=f"prod-{uid}",
        brand_name="Azithro 500",
        generic_name="Azithromycin",
        dosage_form="Tablet",
        strength="500mg",
        manufacturer="Beximco Pharmaceuticals Ltd",
        mrp_unit=Decimal("35.00"),
        cost_unit=Decimal("28.00"),
    )

    return {
        "uid": uid,
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "pharmacist_a": pharmacist_a,
        "cashier_a": cashier_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
        "product": product,
    }


@pytest.mark.django_db
def test_p256_valid_recall_publication_and_stock_propagation(recall_env):
    """Scenario 1: Pharmacist publishes recall; on-hand stock is quarantined and active reservations evicted."""
    env = recall_env
    batch_no = f"RCL-BT-{env['uid']}"

    # Setup BatchStock
    batch_stock = BatchStock.objects.create(
        batch_id=f"btk-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=batch_no,
        quantity_base_units=Decimal("150.00"),
        reserved_quantity_base_units=Decimal("25.00"),
        expiry_date=datetime.date.today() + datetime.timedelta(days=365),
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("45.00"),
        status=BatchStatus.ACTIVE,
        is_quarantined=False,
    )

    # Setup open reservation
    reservation = StockReservation.objects.create(
        reservation_id=f"res-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch=batch_stock,
        quantity_base_units=Decimal("25.00"),
        status=StockReservationStatus.ACTIVE,
    )

    # Publish Recall via Pharmacist
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    payload = {
        "branch_id": env["branch_a"].branch_id,
        "product_name": "Azithromycin 500mg USP",
        "batch_number": batch_no,
        "recall_class": "CLASS_I",
        "recall_source": "DGDA_MANDATORY",
        "title": "DGDA Mandatory Class I Recall - Substandard Dissolution",
        "recall_reason": "Statutory DGDA notification: batch failed bioequivalence and dissolution criteria.",
        "dgda_order_reference": "DGDA/PMS/2026/8812",
    }
    res = client.post("/api/v2/compliance/recalls/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data

    assert data["batch_number"] == batch_no.upper()
    assert data["status"] == "QUARANTINED_AND_ACTIONED"
    assert data["quarantined_units_count"] == "150.00"
    assert data["affected_reservations_evicted"] == 1

    # Verify BatchStock was updated to RECALLED and quarantined
    batch_stock.refresh_from_db()
    assert batch_stock.status == BatchStatus.RECALLED
    assert batch_stock.is_quarantined is True
    assert batch_stock.reserved_quantity_base_units == Decimal("0.00")
    assert "dissolution" in batch_stock.recall_reason.lower()

    # Verify StockReservation was evicted (RELEASED)
    reservation.refresh_from_db()
    assert reservation.status == StockReservationStatus.RELEASED


@pytest.mark.django_db
def test_p256_pos_dispensing_gate_blocks_recalled_batch(recall_env):
    """Scenario 2: Real-time POS scanning gate blocks recalled batches with clinical alert."""
    env = recall_env
    recalled_batch = f"BT-LOCKED-{env['uid']}"
    unrecalled_batch = f"BT-SAFE-{env['uid']}"

    BatchRecallNotification.objects.create(
        recall_id=f"rcl-gate-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product_name="Ciprofloxacin 500mg",
        batch_number=recalled_batch,
        recall_class=RecallClass.CLASS_I,
        status=RecallNoticeStatus.QUARANTINED_AND_ACTIONED,
        title="Class I Urgent Recall",
        recall_reason="Microbial contamination detected.",
    )

    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    # 1. Gate check for recalled batch -> Prohibited
    res_locked = client.post(
        "/api/v2/compliance/recalls/check-dispensing-gate/",
        {"batch_number": recalled_batch, "branch_id": env["branch_a"].branch_id},
        format="json",
    )
    assert res_locked.status_code == status.HTTP_200_OK
    assert res_locked.data["is_dispensing_allowed"] is False
    assert res_locked.data["error_code"] == "ERR_BATCH_RECALLED_DISPENSING_BLOCKED"
    assert "strictly prohibited" in res_locked.data["message_en"]

    # 2. Gate check for un-recalled safe batch -> Allowed
    res_safe = client.post(
        "/api/v2/compliance/recalls/check-dispensing-gate/",
        {"batch_number": unrecalled_batch, "branch_id": env["branch_a"].branch_id},
        format="json",
    )
    assert res_safe.status_code == status.HTTP_200_OK
    assert res_safe.data["is_dispensing_allowed"] is True


@pytest.mark.django_db
def test_p256_invalid_and_incomplete_inputs(recall_env):
    """Scenario 3: Malformed inputs (short batch, missing product/reason, invalid class) rejected."""
    env = recall_env
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    # 1. Short batch number (< 2 chars)
    res_short = client.post(
        "/api/v2/compliance/recalls/",
        {
            "branch_id": env["branch_a"].branch_id,
            "product_name": "Paracetamol 500mg",
            "batch_number": "B",
            "recall_reason": "Defective coating",
        },
        format="json",
    )
    assert res_short.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "batch_number" in res_short.data.get("details", res_short.data)

    # 2. Missing product name
    res_noprod = client.post(
        "/api/v2/compliance/recalls/",
        {
            "branch_id": env["branch_a"].branch_id,
            "product_name": "",
            "batch_number": "BATCH-100",
            "recall_reason": "Defective coating",
        },
        format="json",
    )
    assert res_noprod.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "product_name" in res_noprod.data.get("details", res_noprod.data)

    # 3. Invalid recall class
    res_invclass = client.post(
        "/api/v2/compliance/recalls/",
        {
            "branch_id": env["branch_a"].branch_id,
            "product_name": "Paracetamol 500mg",
            "batch_number": "BATCH-100",
            "recall_class": "CLASS_UNKNOWN_99",
            "recall_reason": "Defective coating",
        },
        format="json",
    )
    assert res_invclass.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "recall_class" in res_invclass.data.get("details", res_invclass.data)


@pytest.mark.django_db
def test_p256_unauthorized_and_cross_tenant_isolation(recall_env):
    """Scenario 4: Cashier barred from publishing recalls; Tenant B cannot access Tenant A recalls."""
    env = recall_env

    # 1. Cashier attempts to publish recall -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])
    res_cashier = client_cashier.post(
        "/api/v2/compliance/recalls/",
        {
            "branch_id": env["branch_a"].branch_id,
            "product_name": "Omeprazole 20mg",
            "batch_number": "OMP-8800",
            "recall_reason": "Cashier unauthorized recall attempt",
        },
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # Create recall in Tenant A
    rcl_a = BatchRecallNotification.objects.create(
        recall_id=f"rcl-ten-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product_name="Omeprazole 20mg",
        batch_number="OMP-7711",
        recall_class=RecallClass.CLASS_II,
        title="Class II Voluntary Recall",
        recall_reason="Packaging blister puncture.",
    )

    # 2. Tenant B Owner attempts to access Tenant A recall -> 404 Not Found
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])
    res_cross = client_b.get(f"/api/v2/compliance/recalls/{rcl_a.recall_id}/")
    assert res_cross.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_p256_historical_sales_impact_without_rewriting_history(recall_env):
    """Scenario 5: Audit historical sales for customer outreach WITHOUT mutating past sales/invoices."""
    env = recall_env
    batch_no = f"BT-HIST-{env['uid']}"

    # Setup BatchStock
    batch_stock = BatchStock.objects.create(
        batch_id=f"btk-hist-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=batch_no,
        quantity_base_units=Decimal("50.00"),
        expiry_date=datetime.date.today() + datetime.timedelta(days=365),
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("12.00"),
        status=BatchStatus.ACTIVE,
    )

    # Setup finalized historical sale invoice
    inv = SaleInvoice.objects.create(
        invoice_id=f"inv-hist-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        invoice_number=f"INV-2026-{env['uid']}",
        customer_name="Rahim Uddin",
        customer_phone="01711000000",
        total_amount=Decimal("300.00"),
        tax_amount=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        net_amount=Decimal("300.00"),
        payment_method="CASH",
        status="PAID",
    )

    # Link historical lot allocation
    alloc = SaleLotAllocation.objects.create(
        allocation_id=f"alloc-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        invoice=inv,
        product=env["product"],
        batch=batch_stock,
        quantity_base_units=20,
        unit_selling_price=Decimal("15.00"),
        total_selling_price=Decimal("300.00"),
        unit_cost=Decimal("12.00"),
        total_cost=Decimal("240.00"),
        gross_profit=Decimal("60.00"),
        status=SaleLotAllocationStatus.COMMITTED,
    )



    # Publish recall
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    res_pub = client_pharma.post(
        "/api/v2/compliance/recalls/",
        {
            "branch_id": env["branch_a"].branch_id,
            "product_name": "Paracetamol 500mg Rapid",
            "batch_number": batch_no,
            "recall_reason": "Potency degradation over time.",
        },
        format="json",
    )
    assert res_pub.status_code == status.HTTP_201_CREATED
    rcl_id = res_pub.data["recall_id"]

    # Query historical sales impact
    res_impact = client_pharma.get(f"/api/v2/compliance/recalls/{rcl_id}/historical-impact/")
    assert res_impact.status_code == status.HTTP_200_OK
    impact_data = res_impact.data

    assert impact_data["total_impacted_invoices"] == 1
    assert impact_data["total_units_sold_historically"] == "20.00"
    assert impact_data["total_value_sold_bdt"] == "300.00"
    assert len(impact_data["impacted_sales"]) == 1
    assert impact_data["impacted_sales"][0]["invoice_number"] == inv.invoice_number

    # Verify that past financial records and allocations were NOT rewritten or mutated
    inv.refresh_from_db()
    alloc.refresh_from_db()
    assert inv.net_amount == Decimal("300.00")
    assert alloc.quantity_base_units == 20
    assert alloc.total_selling_price == Decimal("300.00")


@pytest.mark.django_db
def test_p256_recall_lifting_reversal_lifecycle(recall_env):
    """Scenario 6: Audited recall lifting reverses quarantine and restores unexpired batch stock."""
    env = recall_env
    batch_no = f"BT-LIFT-{env['uid']}"

    # Setup BatchStock with future expiry date
    future_date = datetime.date.today() + datetime.timedelta(days=365)
    batch_stock = BatchStock.objects.create(
        batch_id=f"btk-lift-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=batch_no,
        quantity_base_units=Decimal("80.00"),
        expiry_date=future_date,
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("50.00"),
        status=BatchStatus.RECALLED,
        is_quarantined=True,
    )

    recall = BatchRecallNotification.objects.create(
        recall_id=f"rcl-lift-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product_name="Salbutamol Inhaler 100mcg",
        batch_number=batch_no,
        status=RecallNoticeStatus.QUARANTINED_AND_ACTIONED,
        title="False Alarm Suspected Recall",
        recall_reason="Initial lab test variance; confirmed false positive upon re-testing.",
    )

    client_mgr = APIClient()
    client_mgr.force_authenticate(user=env["manager_a"])

    # Lift recall
    res_lift = client_mgr.post(
        f"/api/v2/compliance/recalls/{recall.recall_id}/lift/",
        {"reason": "DGDA clearance memo issued after certified laboratory re-assay."},
        format="json",
    )
    assert res_lift.status_code == status.HTTP_200_OK
    assert res_lift.data["status"] == "LIFTED"
    assert res_lift.data["lifted_by_id"] == str(env["manager_a"].user_id)

    # Verify stock restored to ACTIVE and quarantine lifted
    batch_stock.refresh_from_db()
    assert batch_stock.status == BatchStatus.ACTIVE
    assert batch_stock.is_quarantined is False

