"""
backend/tests/test_p263_customer_purchase_history.py
Architecture Decision: ADR-0263 (P263).
Phase 14: Customers, Suppliers, Dues & Payments.

Targeted Verification Test Suite for Customer Purchase History:
- Scenario 1: Valid unified timeline of sales, returns, and due collections with accurate financial aggregation.
- Scenario 2: Parameter validation rejection (page <= 0, page_size > 100, invalid event_type, invalid date formats).
- Scenario 3: Role-based PHI / PII privacy masking (cashier receives masked phone/doctor vs unmasked for owner/pharmacist).
- Scenario 4: Strict cross-tenant isolation and 404 for foreign or nonexistent customers.
- Scenario 5: Event type filtering (SALE, RETURN, COLLECTION) and pagination slicing.
"""

from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    CustomerDueRecord,
    DueStatus,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    PosCustomer,
    PosCustomerCreditStatus,
    PosPackagingUnit,
    PosRefundMethod,
    PosReturnSafetyDisposition,
    PosReturnStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    PosSaleOrderLine,
    PosSaleReturn,
    PosSaleReturnLine,
    PrescriptionRecord,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def history_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p263-a-{uid}",
        name="Lazz Pharma Dhanmondi",
        subdomain=f"p263a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p263-a-{uid}",
        name="Dhanmondi Main",
        branch_code=f"D{uid[:3].upper()}",
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
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

    # Customer in Tenant A
    customer = PosCustomer.objects.create(
        tenant=ten_a,
        primary_branch=br_a,
        customer_id=f"cust-p263-{uid}",
        full_name="Mahmudur Rahman",
        phone="+8801712345678",
        credit_status=PosCustomerCreditStatus.APPROVED,
        credit_limit_bdt=Decimal("5000.00"),
        current_due_bdt=Decimal("200.00"),
        created_by=owner_a,
    )

    # Product
    product = MedicineProduct.objects.create(
        product_id=f"prod-p263-{uid}",
        brand_name="Napa Extra",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        mrp_unit=Decimal("3.00"),
        cost_unit=Decimal("2.20"),
    )

    # Prescription
    rx = PrescriptionRecord.objects.create(
        prescription_id=f"rx-p263-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=customer,
        doctor_name="Dr. Shamsul Huq, MBBS, FCPS",
        doctor_bmdc_no="A-45892",
        patient_token=f"pat-tok-{uid}",
        patient_age=45,
        prescription_date=timezone.now().date(),
    )

    # 1. Sale Order
    now = timezone.now()
    sale = PosSaleOrder.objects.create(
        tenant=ten_a,
        branch=br_a,
        sale_id=f"sale-p263-{uid}",
        order_number=f"SO-P263-{uid}",
        customer=customer,
        prescription=rx,
        cashier_user=cashier_a,
        subtotal_bdt=Decimal("600.00"),
        discount_amount_bdt=Decimal("0.00"),
        tax_amount_bdt=Decimal("0.00"),
        grand_total_bdt=Decimal("600.00"),
        paid_amount_bdt=Decimal("400.00"),
        due_amount_bdt=Decimal("200.00"),
        lifecycle_state=PosSaleLifecycleState.FINALIZED,
        finalized_at=now - timezone.timedelta(days=2),
        created_at=now - timezone.timedelta(days=2),
    )
    PosSaleOrderLine.objects.create(
        tenant=ten_a,
        branch=br_a,
        sale=sale,
        line_id=f"line-p263-{uid}",
        line_number=1,
        product=product,
        quantity_units=200,
        conversion_factor=1,
        base_units_total=200,
        unit_price_bdt=Decimal("3.00"),
        line_subtotal_bdt=Decimal("600.00"),
        line_total_bdt=Decimal("600.00"),
    )

    # 2. Sales Return
    sale_return = PosSaleReturn.objects.create(
        tenant=ten_a,
        branch=br_a,
        return_id=f"ret-p263-{uid}",
        return_number=f"RET-P263-{uid}",
        original_sale=sale,
        processed_by=cashier_a,
        status=PosReturnStatus.COMPLETED,
        refund_method=PosRefundMethod.CASH,
        total_refund_amount_bdt=Decimal("60.00"),
        reason="Customer purchased excess tablets",
        created_at=now - timezone.timedelta(days=1),
    )
    PosSaleReturnLine.objects.create(
        tenant=ten_a,
        branch=br_a,
        sale_return=sale_return,
        sale_line=sale.lines.first(),
        return_line_id=f"rline-p263-{uid}",
        product=product,
        quantity_units=20,
        base_units_returned=20,
        unit_price_bdt=Decimal("3.00"),
        refund_amount_bdt=Decimal("60.00"),
        safety_disposition=PosReturnSafetyDisposition.RESTOCKABLE,
    )

    # 3. Customer Due Record & Collection
    due_record = CustomerDueRecord.objects.create(
        due_id=f"due-p263-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=customer,
        sale=sale,
        customer_name=customer.full_name,
        customer_phone=customer.phone,
        total_due_bdt=Decimal("200.00"),
        paid_amount_bdt=Decimal("100.00"),
        remaining_due_bdt=Decimal("100.00"),
        status=DueStatus.PARTIAL,
    )
    collection = CustomerDueCollection.objects.create(
        collection_id=f"col-p263-{uid}",
        tenant=ten_a,
        branch=br_a,
        due_record=due_record,
        customer=customer,
        sale=sale,
        tender_type=PaymentMethod.BKASH,
        amount_bdt=Decimal("100.00"),
        status=CustomerDueCollectionStatus.CAPTURED,
        collected_at=now,
    )

    # Tenant B (Cross-Tenant Boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p263-b-{uid}",
        name="Apollo Health B",
        subdomain=f"p263b-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "pharmacist_a": pharmacist_a,
        "cashier_a": cashier_a,
        "customer": customer,
        "sale": sale,
        "sale_return": sale_return,
        "collection": collection,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_p263_valid_unified_timeline(history_env):
    """Scenario 1: Valid unified timeline of sales, returns, and collections with financial summary."""
    client = APIClient()
    client.force_authenticate(user=history_env["owner_a"])

    customer_id = history_env["customer"].customer_id
    url = f"/api/v2/pos/customers/{customer_id}/purchase-history/"
    resp = client.get(url)

    assert resp.status_code == status.HTTP_200_OK, resp.data
    data = resp.data

    # Check Customer Summary
    cust = data["customer"]
    assert cust["customer_id"] == customer_id
    assert cust["full_name"] == "Mahmudur Rahman"
    assert cust["total_purchases_bdt"] == "600.00"
    assert cust["total_returns_bdt"] == "60.00"
    assert cust["total_collections_bdt"] == "100.00"
    assert cust["net_spend_bdt"] == "540.00"  # 600.00 - 60.00
    assert cust["current_due_bdt"] == "200.00"

    # Check Pagination
    pag = data["pagination"]
    assert pag["count"] == 3
    assert pag["page"] == 1
    assert pag["total_pages"] == 1

    # Check Results (Chronological Descending: Collection -> Return -> Sale)
    results = data["results"]
    assert len(results) == 3
    event_types = [item["event_type"] for item in results]
    assert event_types == ["COLLECTION", "RETURN", "SALE"]

    # Verify SALE item details
    sale_item = next(item for item in results if item["event_type"] == "SALE")
    assert sale_item["gross_amount_bdt"] == "600.00"
    assert sale_item["net_amount_bdt"] == "600.00"
    assert sale_item["paid_amount_bdt"] == "400.00"
    assert sale_item["is_prescription_sale"] is True
    assert sale_item["prescriber_info"]["doctor_name"] == "Dr. Shamsul Huq, MBBS, FCPS"
    assert len(sale_item["items_summary"]) == 1
    assert sale_item["items_summary"][0]["product_name"] == "Napa Extra"

    # Verify RETURN item details
    ret_item = next(item for item in results if item["event_type"] == "RETURN")
    assert ret_item["gross_amount_bdt"] == "60.00"
    assert ret_item["reason"] == "Customer purchased excess tablets"
    assert len(ret_item["items_summary"]) == 1
    assert ret_item["items_summary"][0]["quantity"] == 20

    # Verify COLLECTION item details
    col_item = next(item for item in results if item["event_type"] == "COLLECTION")
    assert col_item["gross_amount_bdt"] == "100.00"
    assert col_item["tender_type"] == PaymentMethod.BKASH


@pytest.mark.django_db
def test_p263_parameter_validation_rejections(history_env):
    """Scenario 2: Parameter validation rejection for invalid page, page_size, event_type, and dates."""
    client = APIClient()
    client.force_authenticate(user=history_env["owner_a"])
    customer_id = history_env["customer"].customer_id
    url = f"/api/v2/pos/customers/{customer_id}/purchase-history/"

    # Invalid page number (< 1)
    r1 = client.get(url, {"page": 0})
    assert r1.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "page" in r1.data or "page" in str(r1.data)

    # Invalid page_size (> 100)
    r2 = client.get(url, {"page_size": 101})
    assert r2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "page_size" in r2.data or "page_size" in str(r2.data)

    # Invalid event_type
    r3 = client.get(url, {"event_type": "UNKNOWN_EVENT"})
    assert r3.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "event_type" in r3.data or "event_type" in str(r3.data)

    # Invalid date format
    r4 = client.get(url, {"start_date": "2026-15-40"})
    assert r4.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "start_date" in r4.data or "start_date" in str(r4.data)

    # Inverted date range (start > end)
    r5 = client.get(url, {"start_date": "2026-12-01", "end_date": "2026-01-01"})
    assert r5.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "start_date" in r5.data or "start_date" in str(r5.data)


@pytest.mark.django_db
def test_p263_role_based_privacy_masking(history_env):
    """Scenario 3: Role-based PHI / PII privacy masking for cashier vs unmasked for owner/pharmacist."""
    customer_id = history_env["customer"].customer_id
    url = f"/api/v2/pos/customers/{customer_id}/purchase-history/"

    # 1. Cashier Access -> Masked Phone and Masked Prescriber
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=history_env["cashier_a"])
    resp_cashier = client_cashier.get(url)
    assert resp_cashier.status_code == status.HTTP_200_OK

    cashier_cust = resp_cashier.data["customer"]
    assert cashier_cust["is_phone_masked"] is True
    assert "*" in cashier_cust["phone"]
    assert cashier_cust["phone"].startswith("+88017")
    assert cashier_cust["phone"].endswith("5678")

    sale_item_cashier = next(i for i in resp_cashier.data["results"] if i["event_type"] == "SALE")
    assert sale_item_cashier["prescriber_info"]["is_masked"] is True
    assert "Masked" in sale_item_cashier["prescriber_info"]["doctor_name"]
    assert sale_item_cashier["prescriber_info"]["bmdc_reg_number"] == "****"

    # 2. Staff Pharmacist Access -> Unmasked
    client_pharm = APIClient()
    client_pharm.force_authenticate(user=history_env["pharmacist_a"])
    resp_pharm = client_pharm.get(url)
    assert resp_pharm.status_code == status.HTTP_200_OK

    pharm_cust = resp_pharm.data["customer"]
    assert pharm_cust["is_phone_masked"] is False
    assert pharm_cust["phone"] == "+8801712345678"

    sale_item_pharm = next(i for i in resp_pharm.data["results"] if i["event_type"] == "SALE")
    assert sale_item_pharm["prescriber_info"]["is_masked"] is False
    assert sale_item_pharm["prescriber_info"]["doctor_name"] == "Dr. Shamsul Huq, MBBS, FCPS"
    assert sale_item_pharm["prescriber_info"]["bmdc_reg_number"] == "A-45892"


@pytest.mark.django_db
def test_p263_cross_tenant_isolation(history_env):
    """Scenario 4: Strict cross-tenant isolation and 404 for foreign or nonexistent customers."""
    client_b = APIClient()
    client_b.force_authenticate(user=history_env["owner_b"])

    # Tenant B tries to query Tenant A customer
    customer_id = history_env["customer"].customer_id
    url = f"/api/v2/pos/customers/{customer_id}/purchase-history/"
    resp_cross = client_b.get(url)
    assert resp_cross.status_code == status.HTTP_404_NOT_FOUND

    # Nonexistent customer in Tenant A
    client_a = APIClient()
    client_a.force_authenticate(user=history_env["owner_a"])
    resp_nonexistent = client_a.get("/api/v2/pos/customers/nonexistent-id/purchase-history/")
    assert resp_nonexistent.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_p263_filtering_and_pagination(history_env):
    """Scenario 5: Event type filtering (SALE, RETURN, COLLECTION) and pagination slicing."""
    client = APIClient()
    client.force_authenticate(user=history_env["owner_a"])
    customer_id = history_env["customer"].customer_id
    url = f"/api/v2/pos/customers/{customer_id}/purchase-history/"

    # Filter by SALE only
    resp_sale = client.get(url, {"event_type": "SALE"})
    assert resp_sale.status_code == status.HTTP_200_OK
    assert resp_sale.data["pagination"]["count"] == 1
    assert all(item["event_type"] == "SALE" for item in resp_sale.data["results"])

    # Filter by RETURN only
    resp_return = client.get(url, {"event_type": "RETURN"})
    assert resp_return.status_code == status.HTTP_200_OK
    assert resp_return.data["pagination"]["count"] == 1
    assert all(item["event_type"] == "RETURN" for item in resp_return.data["results"])

    # Filter by COLLECTION only
    resp_col = client.get(url, {"event_type": "COLLECTION"})
    assert resp_col.status_code == status.HTTP_200_OK
    assert resp_col.data["pagination"]["count"] == 1
    assert all(item["event_type"] == "COLLECTION" for item in resp_col.data["results"])

    # Pagination slicing (page_size=1)
    resp_p1 = client.get(url, {"page": 1, "page_size": 1})
    assert resp_p1.status_code == status.HTTP_200_OK
    assert resp_p1.data["pagination"]["total_pages"] == 3
    assert resp_p1.data["pagination"]["has_next"] is True
    assert resp_p1.data["pagination"]["has_previous"] is False
    assert len(resp_p1.data["results"]) == 1

    resp_p2 = client.get(url, {"page": 2, "page_size": 1})
    assert resp_p2.status_code == status.HTTP_200_OK
    assert resp_p2.data["pagination"]["has_next"] is True
    assert resp_p2.data["pagination"]["has_previous"] is True
    assert len(resp_p2.data["results"]) == 1
    assert resp_p2.data["results"][0]["event_id"] != resp_p1.data["results"][0]["event_id"]
