"""
backend/tests/test_p257_temperature_safety_review.py
Architecture Decision: ADR-0257 (P257).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Targeted Verification Test Suite for Temperature Excursion Safety Reviews:
- Scenario 1: Valid pharmacist review releasing unexpired batch to active sellable stock.
- Scenario 2: Invalid/incomplete inputs, short justification, and expired batch release prevention.
- Scenario 3: Unauthorized role (Cashier) and cross-tenant boundary isolation.
- Scenario 4: Immutability enforcement (PUT/PATCH/DELETE strictly rejected).
- Scenario 5: Condemn & Disposal outcome emitting immutable StockLedgerEntry.
- Scenario 6: POS Dispensing Gate & cold-chain antibiotic pre-check.
"""

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    ExcursionExposureSeverity,
    ExcursionReviewOutcome,
    MedicineProduct,
    StockLedgerEntry,
    StockMovementType,
    TemperatureExcursionSafetyReview,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def temp_safety_env():
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p257-a-{uid}", name="Green Life Pharma A", subdomain=f"p257a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p257-a-{uid}", name="Gulshan Main Branch", branch_code="GL01"
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
        email=f"pharma_{uid}@test.com",
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
        tenant_id=f"ten-p257-b-{uid}", name="Blue Sky Pharma B", subdomain=f"p257b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p257-b-{uid}", name="Banani Branch", branch_code="BB01"
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
        brand_name="Insulin Glargine 100IU",
        generic_name="Insulin Glargine",
        dosage_form="Injection",
        strength="100 IU/ml",
        manufacturer="Sanofi Bangladesh Ltd",
        mrp_unit=Decimal("850.00"),
        cost_unit=Decimal("700.00"),
        requires_cold_chain=True,
    )

    antibiotic_prod = MedicineProduct.objects.create(
        product_id=f"prod-ab-{uid}",
        brand_name="Augmentin 156.25mg Suspension",
        generic_name="Amoxicillin + Clavulanic Acid",
        dosage_form="Dry Syrup",
        strength="156.25mg/5ml",
        manufacturer="GlaxoSmithKline",
        mrp_unit=Decimal("220.00"),
        cost_unit=Decimal("180.00"),
        is_antibiotic=True,
        requires_cold_chain=True,
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
        "antibiotic_prod": antibiotic_prod,
    }


@pytest.mark.django_db
def test_p257_valid_safety_review_release_to_active(temp_safety_env):
    """Scenario 1: Pharmacist reviews temperature excursion with stability justification and unquarantines batch."""
    env = temp_safety_env
    batch_no = f"BT-TEMP-{env['uid']}"

    # Quarantined batch with future expiry
    future_exp = datetime.date.today() + datetime.timedelta(days=180)
    batch = BatchStock.objects.create(
        batch_id=f"btk-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=batch_no,
        quantity_base_units=Decimal("50.00"),
        reserved_quantity_base_units=Decimal("50.00"),
        expiry_date=future_exp,
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("700.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
    )

    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    payload = {
        "branch_id": env["branch_a"].branch_id,
        "batch_id": batch.batch_id,
        "max_exposed_temperature": "11.50",
        "min_exposed_temperature": "3.00",
        "exposure_duration_hours": "3.50",
        "review_decision": "RELEASE_TO_ACTIVE",
        "clinical_justification": "Sanofi manufacturer stability documentation authorizes cumulative excursion up to 12C for 8 hours.",
        "manufacturer_consulted": True,
        "manufacturer_reference_no": "SAN-QA-2026-901",
        "action_taken_notes": "Continuous data logger audit completed; integrity verified.",
    }

    res = client.post("/api/v2/compliance/temperature-reviews/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data

    assert data["batch_number"] == batch_no
    assert data["review_decision"] == "RELEASE_TO_ACTIVE"
    assert data["severity"] == "MODERATE"
    assert data["reviewed_quantity_base_units"] == "50.00"
    assert data["total_exposed_value_bdt"] == "35000.00"
    assert data["is_tamper_free"] is True
    assert len(data["sha256_audit_hash"]) == 64

    # Verify batch stock state restoration
    batch.refresh_from_db()
    assert batch.status == BatchStatus.ACTIVE
    assert batch.is_quarantined is False
    assert batch.reserved_quantity_base_units == Decimal("0.00")


@pytest.mark.django_db
def test_p257_invalid_inputs_and_expired_batch_release_prevention(temp_safety_env):
    """Scenario 2: Preflight validation rejects short justification and forbids releasing expired stock."""
    env = temp_safety_env
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    future_exp = datetime.date.today() + datetime.timedelta(days=180)
    batch = BatchStock.objects.create(
        batch_id=f"btk-inv-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=f"BT-INV-{env['uid']}",
        quantity_base_units=Decimal("20.00"),
        expiry_date=future_exp,
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("700.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
    )

    # 1. Short clinical justification (< 15 characters)
    res_short = client.post(
        "/api/v2/compliance/temperature-reviews/",
        {
            "branch_id": env["branch_a"].branch_id,
            "batch_id": batch.batch_id,
            "max_exposed_temperature": "12.00",
            "review_decision": "RELEASE_TO_ACTIVE",
            "clinical_justification": "Looks ok.",
        },
        format="json",
    )
    assert res_short.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "clinical_justification" in str(res_short.data)

    # 2. Invalid review decision enum
    res_enum = client.post(
        "/api/v2/compliance/temperature-reviews/",
        {
            "branch_id": env["branch_a"].branch_id,
            "batch_id": batch.batch_id,
            "max_exposed_temperature": "12.00",
            "review_decision": "INVALID_DECISION",
            "clinical_justification": "Adequate length clinical justification provided.",
        },
        format="json",
    )
    assert res_enum.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "review_decision" in str(res_enum.data)

    # 3. Expired batch CANNOT be released to active sellable stock
    past_exp = datetime.date.today() - datetime.timedelta(days=5)
    expired_batch = BatchStock.objects.create(
        batch_id=f"btk-exp-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=f"BT-EXPIRED-{env['uid']}",
        quantity_base_units=Decimal("10.00"),
        expiry_date=past_exp,
        purchase_date=datetime.date.today() - datetime.timedelta(days=365),
        cost_per_unit=Decimal("700.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
    )

    res_exp = client.post(
        "/api/v2/compliance/temperature-reviews/",
        {
            "branch_id": env["branch_a"].branch_id,
            "batch_id": expired_batch.batch_id,
            "max_exposed_temperature": "10.00",
            "review_decision": "RELEASE_TO_ACTIVE",
            "clinical_justification": "Stability documentation indicates acceptable potency despite breach.",
        },
        format="json",
    )
    assert res_exp.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "expired" in str(res_exp.data).lower()


@pytest.mark.django_db
def test_p257_unauthorized_role_and_cross_tenant_isolation(temp_safety_env):
    """Scenario 3: Cashiers are denied review authority; cross-tenant access is blocked."""
    env = temp_safety_env

    batch = BatchStock.objects.create(
        batch_id=f"btk-auth-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=f"BT-AUTH-{env['uid']}",
        quantity_base_units=Decimal("15.00"),
        expiry_date=datetime.date.today() + datetime.timedelta(days=100),
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("700.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
    )

    # 1. Cashier attempt -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])

    res_cashier = client_cashier.post(
        "/api/v2/compliance/temperature-reviews/",
        {
            "branch_id": env["branch_a"].branch_id,
            "batch_id": batch.batch_id,
            "max_exposed_temperature": "12.00",
            "review_decision": "RELEASE_TO_ACTIVE",
            "clinical_justification": "Cashier attempting unauthorized safety approval override.",
        },
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Cross-tenant attempt -> Tenant B owner cannot review Tenant A's batch
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])

    res_b = client_b.post(
        "/api/v2/compliance/temperature-reviews/",
        {
            "branch_id": env["branch_a"].branch_id,
            "batch_id": batch.batch_id,
            "max_exposed_temperature": "12.00",
            "review_decision": "RELEASE_TO_ACTIVE",
            "clinical_justification": "Valid length justification from cross-tenant user.",
        },
        format="json",
    )
    assert res_b.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_ENTITY]


@pytest.mark.django_db
def test_p257_immutability_enforcement(temp_safety_env):
    """Scenario 4: Safety review decisions are immutable once signed; PUT/PATCH/DELETE are rejected."""
    env = temp_safety_env
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    batch = BatchStock.objects.create(
        batch_id=f"btk-imm-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=f"BT-IMM-{env['uid']}",
        quantity_base_units=Decimal("10.00"),
        expiry_date=datetime.date.today() + datetime.timedelta(days=90),
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("700.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
    )

    # Create initial review
    res_create = client.post(
        "/api/v2/compliance/temperature-reviews/",
        {
            "branch_id": env["branch_a"].branch_id,
            "batch_id": batch.batch_id,
            "max_exposed_temperature": "11.00",
            "review_decision": "RELEASE_TO_ACTIVE",
            "clinical_justification": "Manufacturer cold chain stability curve confirms valid potency.",
        },
        format="json",
    )
    assert res_create.status_code == status.HTTP_201_CREATED
    rev_id = res_create.data["review_id"]

    # Attempt PATCH -> 400 Bad Request
    res_patch = client.patch(
        f"/api/v2/compliance/temperature-reviews/{rev_id}/",
        {"review_decision": "CONDEMN_DISPOSAL"},
        format="json",
    )
    assert res_patch.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert "ERR_REVIEW_DECISION_IMMUTABLE" in str(res_patch.data)

    # Attempt DELETE -> 403 Forbidden
    res_del = client.delete(f"/api/v2/compliance/temperature-reviews/{rev_id}/")
    assert res_del.status_code in [status.HTTP_403_FORBIDDEN, status.HTTP_405_METHOD_NOT_ALLOWED]


@pytest.mark.django_db
def test_p257_condemn_disposal_stock_ledger_integration(temp_safety_env):
    """Scenario 5: Severe temperature excursion triggers condemnation and immutable EXPIRY_DISPOSAL ledger entry."""
    env = temp_safety_env
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a"])

    batch = BatchStock.objects.create(
        batch_id=f"btk-cnd-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["product"],
        batch_number=f"BT-CND-{env['uid']}",
        quantity_base_units=Decimal("40.00"),
        expiry_date=datetime.date.today() + datetime.timedelta(days=200),
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("700.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
    )

    payload = {
        "branch_id": env["branch_a"].branch_id,
        "batch_id": batch.batch_id,
        "max_exposed_temperature": "24.50",
        "exposure_duration_hours": "14.00",
        "review_decision": "CONDEMN_DISPOSAL",
        "clinical_justification": "Extreme room temperature excursion exceeded manufacturer denaturation limit. Protein precipitated.",
        "action_taken_notes": "Segregated in hazardous pharmaceutical waste quarantine bin.",
    }

    res = client.post("/api/v2/compliance/temperature-reviews/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data

    assert data["review_decision"] == "CONDEMN_DISPOSAL"
    assert data["severity"] == "CRITICAL"
    assert data["total_exposed_value_bdt"] == "28000.00"

    # Verify batch is archived
    batch.refresh_from_db()
    assert batch.status == BatchStatus.ARCHIVED
    assert batch.is_active is False
    assert batch.is_quarantined is False

    # Verify StockLedgerEntry was recorded
    ledger = StockLedgerEntry.objects.filter(
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        batch_number=batch.batch_number,
        movement_type=StockMovementType.EXPIRY_DISPOSAL,
    ).first()
    assert ledger is not None
    assert ledger.quantity_delta == -40
    assert ledger.unit_cost == Decimal("700.00")


@pytest.mark.django_db
def test_p257_pos_dispensing_gate_and_cold_chain_antibiotic_safety(temp_safety_env):
    """Scenario 6: Real-time POS gate blocks quarantined antibiotic batch until verified safety clearance."""
    env = temp_safety_env
    client = APIClient()
    client.force_authenticate(user=env["cashier_a"])

    ab_batch_no = f"BT-AUG-{env['uid']}"
    batch = BatchStock.objects.create(
        batch_id=f"btk-aug-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        product=env["antibiotic_prod"],
        batch_number=ab_batch_no,
        quantity_base_units=Decimal("30.00"),
        expiry_date=datetime.date.today() + datetime.timedelta(days=120),
        purchase_date=datetime.date.today(),
        cost_per_unit=Decimal("180.00"),
        status=BatchStatus.QUARANTINED,
        is_quarantined=True,
    )

    # 1. Cashier checks batch safety before carting -> Dispensing is BLOCKED
    res_gate_block = client.get(
        f"/api/v2/compliance/temperature-reviews/check-batch-safety/?batch_number={ab_batch_no}&branch_id={env['branch_a'].branch_id}"
    )
    assert res_gate_block.status_code == status.HTTP_200_OK
    assert res_gate_block.data["is_dispensing_allowed"] is False
    assert res_gate_block.data["error_code"] == "ERR_TEMPERATURE_EXCURSION_UNREVIEWED"
    assert res_gate_block.data["is_antibiotic"] is True

    # 2. Pharmacist reviews and safely clears the batch
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a"])

    res_review = client_pharma.post(
        "/api/v2/compliance/temperature-reviews/",
        {
            "branch_id": env["branch_a"].branch_id,
            "batch_id": batch.batch_id,
            "max_exposed_temperature": "10.00",
            "exposure_duration_hours": "1.50",
            "review_decision": "RELEASE_TO_ACTIVE",
            "clinical_justification": "Amoxicillin-clavulanate dry suspension stable up to 25C prior to reconstitution per GSK monograph.",
        },
        format="json",
    )
    assert res_review.status_code == status.HTTP_201_CREATED

    # 3. Cashier re-checks batch safety -> Dispensing is now ALLOWED
    res_gate_allow = client.get(
        f"/api/v2/compliance/temperature-reviews/check-batch-safety/?batch_number={ab_batch_no}&branch_id={env['branch_a'].branch_id}"
    )
    assert res_gate_allow.status_code == status.HTTP_200_OK
    assert res_gate_allow.data["is_dispensing_allowed"] is True
