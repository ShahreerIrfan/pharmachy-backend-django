"""
backend/tests/test_p218_barcode_inventory_workflows.py
Acceptance tests for P218: Barcode inventory workflows.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid workflows: Receive, Put-Away, Move, Count, and Verify with bilingual operator prompts.
2. Storage policy enforcement: Putting cold-chain item into ambient shelf is rejected (422/400).
3. Physical cycle counting with discrepancy audit (zero vs non-zero difference).
4. Cross-tenant isolation: Tenant B cannot execute operations on Tenant A branch (HTTP 404).
5. Unrecognized barcode handling returns clear error prompts (HTTP 404).
"""

import uuid
from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.identity.models import PharmacyUser, UserRole
from apps.core.models import (
    BarcodeInventoryAuditLog,
    BarcodeWorkflowType,
    BatchStatus,
    BatchStock,
    Branch,
    MedicineProduct,
    StorageCondition,
    StorageZonePolicy,
    Tenant,
)


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="United Hospital Pharmacy",
        subdomain=f"united-bc-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Apollo Dhaka Dispensary",
        subdomain=f"apollo-bc-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-a-{uid}",
        tenant=tenant_a,
        name="Central Main Dispensary",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-b-{uid}",
        tenant=tenant_b,
        name="Apollo Gulshan Branch",
        is_active=True,
    )


@pytest.fixture
def storage_policy_a(tenant_a, branch_a):
    return StorageZonePolicy.objects.create(
        policy_id=f"pol-{uuid.uuid4().hex[:8]}",
        tenant=tenant_a,
        branch=branch_a,
        enforce_cold_chain=True,
    )


@pytest.fixture
def pharmacist_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_bc@united.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def tenant_b_pharmacist(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_b@apollo.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        is_active=True,
    )


@pytest.fixture
def product_napa(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"prod-nap-{uid}",
        brand_name="Napa Extra 500mg",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg + 65mg",
        barcode=f"8941100{uid[:4]}",
        cost_unit=Decimal("2.50"),
        mrp_unit=Decimal("3.00"),
        requires_cold_chain=False,
    )


@pytest.fixture
def product_insulin(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"prod-ins-{uid}",
        brand_name="Mixtard 30 100IU",
        generic_name="Biphasic Isophane Insulin",
        dosage_form="Vial",
        strength="100 IU/mL",
        barcode=f"8942200{uid[:4]}",
        cost_unit=Decimal("420.00"),
        mrp_unit=Decimal("480.00"),
        requires_cold_chain=True,
    )


@pytest.fixture
def batch_napa(tenant_a, branch_a, product_napa):
    uid = uuid.uuid4().hex[:6]
    today = timezone.localdate()
    return BatchStock.objects.create(
        batch_id=f"bat-nap-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_napa,
        batch_number=f"NAP-{uid[:4]}",
        purchase_date=today - timezone.timedelta(days=10),
        expiry_date=today + timezone.timedelta(days=365),
        storage_location="SHELF-A1",
        quantity_base_units=50,
        cost_per_unit=Decimal("2.50"),
        status=BatchStatus.ACTIVE,
    )


@pytest.fixture
def batch_insulin(tenant_a, branch_a, product_insulin):
    uid = uuid.uuid4().hex[:6]
    today = timezone.localdate()
    return BatchStock.objects.create(
        batch_id=f"bat-ins-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch_number=f"INS-{uid[:4]}",
        purchase_date=today - timezone.timedelta(days=5),
        expiry_date=today + timezone.timedelta(days=180),
        storage_condition=StorageCondition.COLD_CHAIN,
        storage_location="LOC-FRIDGE-01",
        quantity_base_units=20,
        cost_per_unit=Decimal("420.00"),
        status=BatchStatus.ACTIVE,
    )


@pytest.mark.django_db
def test_valid_receive_and_verify_workflows(
    api_client, pharmacist_user, branch_a, product_napa, product_insulin, batch_napa
):
    """
    Scenario 1: Scan barcode to receive medicine and verify active batch details.
    """
    api_client.force_authenticate(user=pharmacist_user)

    # 1. Receive ambient product
    res_rx = api_client.post(
        "/api/v2/inventory/barcode/receive/",
        {"branch": branch_a.branch_id, "barcode": product_napa.barcode},
        format="json",
    )
    assert res_rx.status_code == status.HTTP_200_OK
    assert res_rx.data["product_name"] == product_napa.brand_name
    assert "গৃহীত হয়েছে" in res_rx.data["operator_prompt_bn"]

    # 2. Receive cold-chain product
    res_rx_cold = api_client.post(
        "/api/v2/inventory/barcode/receive/",
        {"branch": branch_a.branch_id, "barcode": product_insulin.barcode},
        format="json",
    )
    assert res_rx_cold.status_code == status.HTTP_200_OK
    assert res_rx_cold.data["is_cold_chain"] is True
    assert "Cold-Chain" in res_rx_cold.data["operator_prompt_en"]
    assert "রেফ্রিজারেটরে" in res_rx_cold.data["operator_prompt_bn"]

    # 3. Verify item & batch details
    res_ver = api_client.post(
        "/api/v2/inventory/barcode/verify/",
        {"branch": branch_a.branch_id, "barcode": batch_napa.batch_number},
        format="json",
    )
    assert res_ver.status_code == status.HTTP_200_OK
    assert res_ver.data["batch_number"] == batch_napa.batch_number
    assert res_ver.data["quantity_base_units"] == 50
    assert res_ver.data["is_expired"] is False


@pytest.mark.django_db
def test_put_away_and_storage_policy_enforcement(
    api_client, pharmacist_user, branch_a, storage_policy_a, batch_insulin
):
    """
    Scenario 2: Put-Away barcode workflow:
    - Attempting to put cold chain item in ambient SHELF-B2 is rejected.
    - Putting into LOC-FRIDGE-02 succeeds and updates storage_location.
    """
    api_client.force_authenticate(user=pharmacist_user)

    # Ambient attempt -> rejected
    res_fail = api_client.post(
        "/api/v2/inventory/barcode/put-away/",
        {
            "branch": branch_a.branch_id,
            "batch_number": batch_insulin.batch_number,
            "destination_location": "SHELF-B2-AMBIENT",
        },
        format="json",
    )
    assert res_fail.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "Cold-chain medicine" in str(res_fail.data)

    # Valid fridge put-away -> success
    res_ok = api_client.post(
        "/api/v2/inventory/barcode/put-away/",
        {
            "branch": branch_a.branch_id,
            "batch_number": batch_insulin.batch_number,
            "destination_location": "LOC-FRIDGE-02",
        },
        format="json",
    )
    assert res_ok.status_code == status.HTTP_200_OK
    batch_insulin.refresh_from_db()
    assert batch_insulin.storage_location == "LOC-FRIDGE-02"


@pytest.mark.django_db
def test_move_and_cycle_count_workflows(
    api_client, pharmacist_user, branch_a, batch_napa
):
    """
    Scenario 3: Internal Move and Cycle Count barcode workflows.
    """
    api_client.force_authenticate(user=pharmacist_user)

    # 1. Move from SHELF-A1 to SHELF-C3
    res_move = api_client.post(
        "/api/v2/inventory/barcode/move/",
        {
            "branch": branch_a.branch_id,
            "batch_number": batch_napa.batch_number,
            "from_location": "SHELF-A1",
            "to_location": "SHELF-C3",
        },
        format="json",
    )
    assert res_move.status_code == status.HTTP_200_OK
    batch_napa.refresh_from_db()
    assert batch_napa.storage_location == "SHELF-C3"

    # 2. Cycle Count - exact match
    res_cnt_ok = api_client.post(
        "/api/v2/inventory/barcode/count/",
        {
            "branch": branch_a.branch_id,
            "barcode": batch_napa.batch_number,
            "location": "SHELF-C3",
            "counted_quantity": 50,
        },
        format="json",
    )
    assert res_cnt_ok.status_code == status.HTTP_200_OK
    assert res_cnt_ok.data["discrepancy"] == 0
    assert "Zero discrepancy" in res_cnt_ok.data["operator_prompt_en"]

    # 3. Cycle Count - shortage (-5)
    res_cnt_short = api_client.post(
        "/api/v2/inventory/barcode/count/",
        {
            "branch": branch_a.branch_id,
            "barcode": batch_napa.batch_number,
            "location": "SHELF-C3",
            "counted_quantity": 45,
        },
        format="json",
    )
    assert res_cnt_short.status_code == status.HTTP_200_OK
    assert res_cnt_short.data["discrepancy"] == -5
    assert "Discrepancy detected" in res_cnt_short.data["operator_prompt_en"]


@pytest.mark.django_db
def test_unrecognized_barcode_returns_404(
    api_client, pharmacist_user, branch_a
):
    """
    Scenario 4: Scanning an unregistered barcode returns HTTP 404 with prompt.
    """
    api_client.force_authenticate(user=pharmacist_user)
    res = api_client.post(
        "/api/v2/inventory/barcode/verify/",
        {"branch": branch_a.branch_id, "barcode": "UNKNOWN-999-BARCODE"},
        format="json",
    )
    assert res.status_code == status.HTTP_404_NOT_FOUND
    assert "অনিবন্ধিত" in str(res.data)


@pytest.mark.django_db
def test_cross_tenant_isolation_404(
    api_client, tenant_b_pharmacist, branch_a, product_napa
):
    """
    Scenario 5: Multi-tenant boundary: User from Tenant B cannot access Tenant A branch.
    """
    api_client.force_authenticate(user=tenant_b_pharmacist)
    res = api_client.post(
        "/api/v2/inventory/barcode/receive/",
        {"branch": branch_a.branch_id, "barcode": product_napa.barcode},
        format="json",
    )
    assert res.status_code == status.HTTP_404_NOT_FOUND
