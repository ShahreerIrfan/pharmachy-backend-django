"""
backend/tests/test_p216_inventory_valuation_reports.py
Acceptance tests for P216: Inventory valuation reports.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid scenario: Multi-category valuation report covering On-Hand, Sellable,
   Quarantined, In-Transit, and Expired stock at documented costing rules.
2. Mathematical reconciliation & precision: On-hand units == Sellable + Quarantined + Expired.
3. Cryptographic integrity & tampering detection: SHA-256 verification via /verify-integrity/.
4. Weighted average costing rule computation and rounding precision.
5. RBAC: Cashier role is strictly denied (HTTP 403 Forbidden).
6. Multi-tenant isolation: Cross-tenant branch request returns HTTP 404 NotFound.
7. Validation & immutability: Invalid costing rule returns 400, PUT/DELETE returns 405.
"""

import uuid
from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.identity.models import PharmacyUser, UserRole
from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    InterBranchTransferItem,
    InterBranchTransferRequest,
    InterBranchTransferStatus,
    InventoryValuationCategory,
    InventoryValuationLineItem,
    InventoryValuationReport,
    MedicineProduct,
    StorageCondition,
    Tenant,
    ValuationCostingRule,
)
from apps.core.inventory_valuation_service import InventoryValuationService


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="United Hospital Pharmacy",
        subdomain=f"united-val-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Apollo Dhaka Dispensary",
        subdomain=f"apollo-val-{uid}",
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
def branch_a_aux(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-aux-{uid}",
        tenant=tenant_a,
        name="Emergency Satellite Branch",
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
def pharmacist_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_val@united.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="cashier_val@united.com",
        password="ValidPassword123!",
        role=UserRole.CASHIER,
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
        cost_unit=Decimal("420.00"),
        mrp_unit=Decimal("480.00"),
        requires_cold_chain=True,
    )


@pytest.fixture
def populated_inventory(tenant_a, branch_a, branch_a_aux, product_napa, product_insulin, pharmacist_user):
    today = timezone.localdate()
    uid = uuid.uuid4().hex[:6]

    # 1. Sellable Batch: Napa Extra (50 units @ 2.50 = 125.00)
    batch_sellable = BatchStock.objects.create(
        batch_id=f"bat-sel-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_napa,
        batch_number=f"NAP-{uid[:4]}",
        purchase_date=today - timezone.timedelta(days=10),
        expiry_date=today + timezone.timedelta(days=365),
        quantity_base_units=50,
        cost_per_unit=Decimal("2.50"),
        status=BatchStatus.ACTIVE,
        is_quarantined=False,
    )

    # 2. Quarantined Batch: Insulin (20 units @ 420.00 = 8400.00)
    batch_quarantine = BatchStock.objects.create(
        batch_id=f"bat-qur-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_insulin,
        batch_number=f"INS-{uid[:4]}",
        purchase_date=today - timezone.timedelta(days=5),
        expiry_date=today + timezone.timedelta(days=180),
        storage_condition=StorageCondition.COLD_CHAIN,
        quantity_base_units=20,
        cost_per_unit=Decimal("420.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
        recall_reason="Suspected cold chain breach hold",
    )

    # 3. Expired Batch: Napa Extra (10 units @ 2.50 = 25.00)
    batch_expired = BatchStock.objects.create(
        batch_id=f"bat-exp-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_napa,
        batch_number=f"NAP-EXP-{uid[:4]}",
        purchase_date=today - timezone.timedelta(days=400),
        expiry_date=today - timezone.timedelta(days=2),  # expired 2 days ago
        quantity_base_units=10,
        cost_per_unit=Decimal("2.50"),
        status=BatchStatus.ACTIVE,
        is_quarantined=False,
    )

    # 4. In-Transit Interbranch Transfer (Dispatched from branch_a_aux to branch_a):
    # 15 units of Insulin @ 420.00 = 6300.00
    transfer = InterBranchTransferRequest.objects.create(
        transfer_id=f"trf-{uid}",
        transfer_number=f"TRF-{uid[:6]}",
        tenant=tenant_a,
        source_branch=branch_a_aux,
        destination_branch=branch_a,
        status=InterBranchTransferStatus.DISPATCHED,
        requested_by=pharmacist_user,
        dispatched_by=pharmacist_user,
        dispatched_at=timezone.now(),
    )
    transfer_item = InterBranchTransferItem.objects.create(
        item_id=f"itm-{uid}",
        transfer=transfer,
        product=product_insulin,
        source_batch=batch_quarantine,
        batch_number=batch_quarantine.batch_number,
        quantity_requested=15,
        quantity_dispatched=15,
        quantity_received=0,
        original_cost_per_unit=Decimal("420.00"),
    )

    return {
        "batch_sellable": batch_sellable,
        "batch_quarantine": batch_quarantine,
        "batch_expired": batch_expired,
        "transfer": transfer,
        "transfer_item": transfer_item,
    }


@pytest.mark.django_db
def test_valid_valuation_report_all_categories(
    api_client, pharmacist_user, tenant_a, branch_a, populated_inventory
):
    """
    Scenario 1: Full multi-category inventory valuation report generation and mathematical reconciliation.
    Expected:
    - On-hand total units = 50 + 20 + 10 = 80 units.
    - On-hand valuation = 50*2.50 + 20*420.00 + 10*2.50 = 125.00 + 8400.00 + 25.00 = 8550.00 BDT.
    - Sellable = 50 units (125.00 BDT).
    - Quarantined = 20 units (8400.00 BDT).
    - Expired = 10 units (25.00 BDT).
    - In-transit = 15 units (6300.00 BDT).
    - Cold-chain on-hand = 20 units (8400.00 BDT).
    - is_mathematically_balanced = True, discrepancy_units = 0, discrepancy_value = 0.00.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/valuation-reports/generate/"
    payload = {
        "branch": branch_a.branch_id,
        "costing_rule": ValuationCostingRule.FEFO_LOT_COST,
        "notes": "EOD Inventory Audit Checkpoint",
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED, response.data

    data = response.data
    assert data["branch_id"] == branch_a.branch_id
    assert data["costing_rule"] == ValuationCostingRule.FEFO_LOT_COST

    # Check on-hand and categories
    assert data["total_on_hand_units"] == 80
    assert Decimal(data["total_on_hand_value_bdt"]) == Decimal("8550.00")

    assert data["sellable_units"] == 50
    assert Decimal(data["sellable_value_bdt"]) == Decimal("125.00")

    assert data["quarantined_units"] == 20
    assert Decimal(data["quarantined_value_bdt"]) == Decimal("8400.00")

    assert data["expired_units"] == 10
    assert Decimal(data["expired_value_bdt"]) == Decimal("25.00")

    assert data["in_transit_units"] == 15
    assert Decimal(data["in_transit_value_bdt"]) == Decimal("6300.00")

    assert data["cold_chain_units"] == 20
    assert Decimal(data["cold_chain_value_bdt"]) == Decimal("8400.00")

    # Mathematical integrity & balance
    assert data["is_mathematically_balanced"] is True
    assert data["discrepancy_units"] == 0
    assert Decimal(data["discrepancy_value_bdt"]) == Decimal("0.00")
    assert len(data["sha256_checksum"]) == 64

    # Detailed line items
    line_items = data["line_items"]
    assert len(line_items) == 4  # 3 on-hand + 1 in-transit
    categories = {li["category"] for li in line_items}
    assert categories == {
        InventoryValuationCategory.SELLABLE,
        InventoryValuationCategory.QUARANTINED,
        InventoryValuationCategory.EXPIRED,
        InventoryValuationCategory.IN_TRANSIT,
    }


@pytest.mark.django_db
def test_valuation_report_integrity_verification_and_tampering(
    api_client, pharmacist_user, tenant_a, branch_a, populated_inventory
):
    """
    Scenario 2: Cryptographic SHA-256 verification and detection of tampering.
    """
    api_client.force_authenticate(user=pharmacist_user)

    # 1. Generate report
    report = InventoryValuationService.generate_valuation_report(
        tenant=tenant_a,
        branch=branch_a,
        costing_rule=ValuationCostingRule.FEFO_LOT_COST,
        user=pharmacist_user,
    )

    # 2. Verify valid integrity
    verify_url = f"/api/v2/inventory/valuation-reports/{report.report_id}/verify-integrity/"
    res_valid = api_client.post(verify_url, format="json")
    assert res_valid.status_code == status.HTTP_200_OK
    assert res_valid.data["is_integrity_valid"] is True
    assert res_valid.data["persisted_checksum"] == res_valid.data["recomputed_checksum"]

    # 3. Simulate unauthorized line item tampering
    line = report.line_items.first()
    line.quantity_base_units = 999
    line.save(update_fields=["quantity_base_units"])

    # 4. Re-verify: must fail integrity
    res_tampered = api_client.post(verify_url, format="json")
    assert res_tampered.status_code == status.HTTP_200_OK
    assert res_tampered.data["is_integrity_valid"] is False
    assert res_tampered.data["persisted_checksum"] != res_tampered.data["recomputed_checksum"]


@pytest.mark.django_db
def test_weighted_average_costing_rule(
    api_client, pharmacist_user, tenant_a, branch_a, product_napa
):
    """
    Scenario 3: Weighted average costing computation across multiple batches.
    Batch 1: 10 units @ 100.00 = 1000.00
    Batch 2: 30 units @ 200.00 = 6000.00
    Total: 40 units, cost 7000.00 => Weighted average = 175.00
    """
    today = timezone.localdate()
    uid = uuid.uuid4().hex[:6]

    BatchStock.objects.create(
        batch_id=f"bat-w1-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_napa,
        batch_number=f"W1-{uid[:4]}",
        purchase_date=today - timezone.timedelta(days=20),
        expiry_date=today + timezone.timedelta(days=200),
        quantity_base_units=10,
        cost_per_unit=Decimal("100.00"),
        status=BatchStatus.ACTIVE,
    )

    BatchStock.objects.create(
        batch_id=f"bat-w2-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_napa,
        batch_number=f"W2-{uid[:4]}",
        purchase_date=today - timezone.timedelta(days=10),
        expiry_date=today + timezone.timedelta(days=250),
        quantity_base_units=30,
        cost_per_unit=Decimal("200.00"),
        status=BatchStatus.ACTIVE,
    )

    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/valuation-reports/generate/"
    payload = {
        "branch": branch_a.branch_id,
        "costing_rule": ValuationCostingRule.WEIGHTED_AVERAGE,
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED

    data = response.data
    assert data["total_on_hand_units"] == 40
    assert Decimal(data["total_on_hand_value_bdt"]) == Decimal("7000.00")
    # Both lines use weighted average unit cost of 175.00
    for li in data["line_items"]:
        assert Decimal(li["unit_cost"]) == Decimal("175.00")


@pytest.mark.django_db
def test_cashier_role_denied_403(
    api_client, cashier_user, branch_a
):
    """
    Scenario 4: Cashiers are strictly denied access to valuation reports (HTTP 403 Forbidden).
    """
    api_client.force_authenticate(user=cashier_user)
    url = "/api/v2/inventory/valuation-reports/generate/"
    payload = {"branch": branch_a.branch_id}
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "Cashiers are strictly forbidden" in str(response.data)


@pytest.mark.django_db
def test_cross_tenant_isolation_404(
    api_client, tenant_b_pharmacist, branch_a
):
    """
    Scenario 5: Multi-tenant boundary enforcement: Tenant B cannot evaluate Tenant A branch (HTTP 404).
    """
    api_client.force_authenticate(user=tenant_b_pharmacist)
    url = "/api/v2/inventory/valuation-reports/generate/"
    payload = {"branch": branch_a.branch_id}
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_invalid_input_and_immutability(
    api_client, pharmacist_user, branch_a
):
    """
    Scenario 6: Missing branch, invalid costing rule, and immutability (PUT/DELETE 405).
    """
    api_client.force_authenticate(user=pharmacist_user)

    # Missing branch
    res_no_branch = api_client.post(
        "/api/v2/inventory/valuation-reports/generate/",
        {"costing_rule": ValuationCostingRule.FEFO_LOT_COST},
        format="json",
    )
    assert res_no_branch.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Invalid costing rule
    res_bad_rule = api_client.post(
        "/api/v2/inventory/valuation-reports/generate/",
        {"branch": branch_a.branch_id, "costing_rule": "MAGIC_COST"},
        format="json",
    )
    assert res_bad_rule.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Generate a valid report
    res_gen = api_client.post(
        "/api/v2/inventory/valuation-reports/generate/",
        {"branch": branch_a.branch_id},
        format="json",
    )
    assert res_gen.status_code == status.HTTP_201_CREATED
    rep_id = res_gen.data["report_id"]

    # Immutability check: PUT and DELETE return 405 Method Not Allowed
    res_put = api_client.put(f"/api/v2/inventory/valuation-reports/{rep_id}/", {}, format="json")
    assert res_put.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    res_del = api_client.delete(f"/api/v2/inventory/valuation-reports/{rep_id}/")
    assert res_del.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
