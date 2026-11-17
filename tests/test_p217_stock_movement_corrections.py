"""
backend/tests/test_p217_stock_movement_corrections.py
Acceptance tests for P217: Stock movement corrections.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid scenario: Reversal of incorrect posted movement via auditable compensating entry,
   restoring physical batch balance without in-place row overwrite.
2. Double-reversal prevention: Attempting to reverse an already reversed movement is rejected (422/400).
3. Negative stock guard: Reversal that would cause negative batch stock is blocked (422/400).
4. RBAC: Cashier role is strictly forbidden from executing corrections (HTTP 403 Forbidden).
5. Cross-tenant isolation: Tenant B cannot reverse Tenant A ledger entries (HTTP 404 NotFound).
6. Immutability & validation: PUT/DELETE returns 405, short explanation rejected (422/400).
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
    MedicineProduct,
    MovementCorrectionReason,
    MovementCorrectionStatus,
    StockLedgerEntry,
    StockMovementCorrection,
    StockMovementType,
    Tenant,
)
from apps.core.stock_movement_correction_service import StockMovementCorrectionService


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="United Hospital Pharmacy",
        subdomain=f"united-cor-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Apollo Dhaka Dispensary",
        subdomain=f"apollo-cor-{uid}",
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
def pharmacist_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_cor@united.com",
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
        email="cashier_cor@united.com",
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
        quantity_base_units=40,
        cost_per_unit=Decimal("2.50"),
        status=BatchStatus.ACTIVE,
    )


@pytest.fixture
def posted_erroneous_entry(tenant_a, branch_a, product_napa, batch_napa, pharmacist_user):
    """
    Erroneous outward dispense entry (-10 units, balance 50 -> 40).
    """
    uid = uuid.uuid4().hex[:8]
    entry = StockLedgerEntry(
        entry_id=f"stkld-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_napa,
        batch=batch_napa,
        batch_number=batch_napa.batch_number,
        movement_type=StockMovementType.SALE_DISPENSE,
        quantity_delta=-10,
        balance_before=50,
        balance_after=40,
        unit_cost=Decimal("2.50"),
        total_cost=Decimal("25.00"),
        source_entity_type="SALE_INVOICE",
        source_entity_id="INV-ERR-001",
        source_reference="INV-ERR-001",
        posted_by=pharmacist_user,
    )
    entry.immutable_verification_hash = entry.compute_hash()
    entry.save()
    return entry


@pytest.mark.django_db
def test_valid_compensating_reversal(
    api_client, pharmacist_user, tenant_a, branch_a, batch_napa, posted_erroneous_entry
):
    """
    Scenario 1: Reverse incorrect posted inventory move via auditable compensating entry.
    Initial state: batch qty = 40, original entry delta = -10.
    Reversal expectation:
    - Compensating entry delta = +10, balance_before = 40, balance_after = 50.
    - Batch stock quantity restored to 50.
    - Original entry remains unmodified.
    - StockMovementCorrection status is COMPLETED.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/movement-corrections/"
    payload = {
        "branch": branch_a.branch_id,
        "original_entry": posted_erroneous_entry.entry_id,
        "reason": MovementCorrectionReason.MISTYPED_QUANTITY,
        "explanation": "Cashier mistakenly punched 10 units instead of 1 unit. Reversing erroneous dispense.",
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_201_CREATED, response.data

    data = response.data
    assert data["original_entry_id"] == posted_erroneous_entry.entry_id
    assert data["reversed_quantity_delta"] == 10
    assert data["status"] == MovementCorrectionStatus.COMPLETED
    assert Decimal(data["unit_cost_bdt"]) == Decimal("2.50")
    assert Decimal(data["total_cost_impact_bdt"]) == Decimal("25.00")

    # 1. Verify batch stock restored to 50
    batch_napa.refresh_from_db()
    assert batch_napa.quantity_base_units == 50

    # 2. Verify original entry remains immutable in DB
    posted_erroneous_entry.refresh_from_db()
    assert posted_erroneous_entry.quantity_delta == -10
    assert posted_erroneous_entry.balance_after == 40

    # 3. Verify compensating StockLedgerEntry exists
    comp_entry = StockLedgerEntry.objects.get(entry_id=data["compensating_entry_id"])
    assert comp_entry.quantity_delta == 10
    assert comp_entry.balance_before == 40
    assert comp_entry.balance_after == 50
    assert comp_entry.movement_type == StockMovementType.STOCK_ADJUSTMENT_IN
    assert comp_entry.source_entity_type == "STOCK_MOVEMENT_CORRECTION"


@pytest.mark.django_db
def test_double_reversal_prevention(
    api_client, pharmacist_user, branch_a, posted_erroneous_entry
):
    """
    Scenario 2: Attempting to reverse an entry that has already been corrected is rejected.
    """
    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/movement-corrections/"
    payload = {
        "branch": branch_a.branch_id,
        "original_entry": posted_erroneous_entry.entry_id,
        "reason": MovementCorrectionReason.MISTYPED_QUANTITY,
        "explanation": "First legitimate reversal of error.",
    }
    res1 = api_client.post(url, payload, format="json")
    assert res1.status_code == status.HTTP_201_CREATED

    # Attempt second reversal of same entry
    res2 = api_client.post(url, payload, format="json")
    assert res2.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "already been reversed" in str(res2.data)


@pytest.mark.django_db
def test_negative_stock_guard(
    api_client, pharmacist_user, tenant_a, branch_a, product_napa, batch_napa
):
    """
    Scenario 3: An inward movement (+100) is attempted to be reversed (-100), but batch stock is only 40.
    Reversal is blocked because 40 - 100 = -60 (negative stock prohibited).
    """
    uid = uuid.uuid4().hex[:8]
    inward_entry = StockLedgerEntry(
        entry_id=f"stkld-in-{uid}",
        tenant=tenant_a,
        branch=branch_a,
        product=product_napa,
        batch=batch_napa,
        batch_number=batch_napa.batch_number,
        movement_type=StockMovementType.PURCHASE_RECEIPT,
        quantity_delta=100,
        balance_before=0,
        balance_after=100,
        unit_cost=Decimal("2.50"),
        total_cost=Decimal("250.00"),
        source_entity_type="PURCHASE_RECEIPT",
        source_entity_id="GRN-001",
        source_reference="GRN-001",
        posted_by=pharmacist_user,
    )
    inward_entry.immutable_verification_hash = inward_entry.compute_hash()
    inward_entry.save()

    # Current batch stock is only 40
    batch_napa.quantity_base_units = 40
    batch_napa.save(update_fields=["quantity_base_units"])

    api_client.force_authenticate(user=pharmacist_user)
    url = "/api/v2/inventory/movement-corrections/"
    payload = {
        "branch": branch_a.branch_id,
        "original_entry": inward_entry.entry_id,
        "reason": MovementCorrectionReason.DUPLICATE_POSTING,
        "explanation": "Attempting to reverse mistaken GRN of 100 units.",
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "negative batch stock" in str(response.data)


@pytest.mark.django_db
def test_cashier_role_denied_403(
    api_client, cashier_user, branch_a, posted_erroneous_entry
):
    """
    Scenario 4: Cashiers are strictly forbidden from performing movement corrections (HTTP 403 Forbidden).
    """
    api_client.force_authenticate(user=cashier_user)
    url = "/api/v2/inventory/movement-corrections/"
    payload = {
        "branch": branch_a.branch_id,
        "original_entry": posted_erroneous_entry.entry_id,
        "reason": MovementCorrectionReason.MISTYPED_QUANTITY,
        "explanation": "Cashier unauthorized attempt.",
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "Cashiers are strictly forbidden" in str(response.data)


@pytest.mark.django_db
def test_cross_tenant_isolation_404(
    api_client, tenant_b_pharmacist, branch_a, posted_erroneous_entry
):
    """
    Scenario 5: Multi-tenant boundary: Tenant B user cannot reverse Tenant A ledger entry (HTTP 404).
    """
    api_client.force_authenticate(user=tenant_b_pharmacist)
    url = "/api/v2/inventory/movement-corrections/"
    payload = {
        "branch": branch_a.branch_id,
        "original_entry": posted_erroneous_entry.entry_id,
        "reason": MovementCorrectionReason.MISTYPED_QUANTITY,
        "explanation": "Cross-tenant unauthorized correction.",
    }
    response = api_client.post(url, payload, format="json")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_validation_and_immutability(
    api_client, pharmacist_user, branch_a, posted_erroneous_entry
):
    """
    Scenario 6: Validation of mandatory explanation and HTTP 405 Method Not Allowed on PUT/DELETE.
    """
    api_client.force_authenticate(user=pharmacist_user)

    # Missing / short explanation
    res_short = api_client.post(
        "/api/v2/inventory/movement-corrections/",
        {
            "branch": branch_a.branch_id,
            "original_entry": posted_erroneous_entry.entry_id,
            "reason": MovementCorrectionReason.MISTYPED_QUANTITY,
            "explanation": "bad",  # < 5 characters
        },
        format="json",
    )
    assert res_short.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Valid execution
    res_valid = api_client.post(
        "/api/v2/inventory/movement-corrections/",
        {
            "branch": branch_a.branch_id,
            "original_entry": posted_erroneous_entry.entry_id,
            "reason": MovementCorrectionReason.MISTYPED_QUANTITY,
            "explanation": "Valid correction explanation.",
        },
        format="json",
    )
    assert res_valid.status_code == status.HTTP_201_CREATED
    correc_id = res_valid.data["correction_id"]

    # Immutability
    res_put = api_client.put(f"/api/v2/inventory/movement-corrections/{correc_id}/", {}, format="json")
    assert res_put.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    res_del = api_client.delete(f"/api/v2/inventory/movement-corrections/{correc_id}/")
    assert res_del.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
