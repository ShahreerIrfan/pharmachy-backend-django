"""
backend/tests/test_p247_otc_rx_status_versioning.py

Architecture Decision: ADR-0247 (P247).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for OTC & Prescription Status Versioning:
1. Valid monotonic reclassification with DGDA verified source and effective date.
2. Incomplete and invalid input rejection (missing source, invalid type, short notes).
3. Point-in-time historical audit resolution (prior epoch OTC vs new epoch POM).
4. Dynamic POS safety gate enforcement (no hardcoded lists; immediate Rx requirement at checkout).
5. Multi-tenant RBAC isolation: Tenant cashiers/pharmacists blocked from mutating global catalogue.
"""

from datetime import datetime, timedelta
from decimal import Decimal
import random
import uuid

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.classification_snapshot_service import MedicineClassificationSnapshotService
from apps.core.models import (
    ClassificationType,
    MedicineClassificationSnapshot,
    MedicineProduct,
    PosPackagingUnit,
    PosRegulatoryClass,
    PosSaleOrder,
    PosSaleOrderLine,
    Tenant,
    Branch,
)
from apps.core.pos_rx_safety_service import PosRxSafetyService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def otc_rx_env(db):
    uid = uuid.uuid4().hex[:6]

    # Platform Superadmin
    superadmin = PharmacyUser.objects.create_superuser(
        email=f"superadmin_p247_{uid}@platform.internal",
        phone_number=f"+88019{random.randint(10000000, 99999999)}",
        password="SuperPassword123!",
        role=UserRole.SUPERADMIN,
    )

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p247-{uid}", name="OTC Rx Versioning Tenant", subdomain=f"p247-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p247-{uid}", name="Gulshan Branch", branch_code="GL01"
    )

    # Cashier & Pharmacist for Tenant A
    cashier = PharmacyUser.objects.create_user(
        email=f"cashier_p247_{uid}@test.com",
        phone_number=f"+88015{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )
    pharmacist = PharmacyUser.objects.create_user(
        email=f"pharmacist_p247_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    # Global Master Medicine: initially OTC (e.g. Paracetamol compound)
    product = MedicineProduct.objects.create(
        product_id=f"prod-otc-{uid}",
        brand_name=f"Ace Plus {uid}",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        is_antibiotic=False,
        is_prescription_only=False,
        is_narcotic=False,
        mrp_unit=Decimal("3.50"),
        cost_unit=Decimal("2.20"),
    )

    # Initial v1 Snapshot: OTC
    initial_eff_from = timezone.now() - timedelta(days=60)
    snap_v1 = MedicineClassificationSnapshot.objects.create(
        snapshot_id=f"mcs-v1-{uid}",
        product=product,
        version_number=1,
        classification_type=ClassificationType.OTC,
        regulatory_source="DGDA Gazette Vol 44 (Initial OTC Schedule)",
        effective_from=initial_eff_from,
        effective_to=None,
        is_current=True,
        reviewed_by=superadmin,
        reviewed_at=initial_eff_from,
        review_notes="Initial national drug registration as Over-The-Counter formulation.",
    )

    return {
        "superadmin": superadmin,
        "tenant_a": ten_a,
        "branch_a": br_a,
        "cashier": cashier,
        "pharmacist": pharmacist,
        "product": product,
        "snap_v1": snap_v1,
        "initial_eff_from": initial_eff_from,
        "uid": uid,
    }


@pytest.mark.django_db
def test_p247_valid_otc_to_rx_reclassification(otc_rx_env):
    """
    Scenario 1: Superadmin elevates an OTC product to Prescription-Only (POM)
    with verified DGDA source gazette, monotonic version increment, and SHA-256 hash.
    """
    env = otc_rx_env
    client = APIClient()
    client.force_authenticate(user=env["superadmin"])

    reclass_eff_from = timezone.now() - timedelta(days=1)
    payload = {
        "product_id": env["product"].product_id,
        "classification_type": ClassificationType.PRESCRIPTION_ONLY,
        "regulatory_source": "DGDA Gazette Notification S.R.O. 182-Act/2026",
        "effective_from": reclass_eff_from.isoformat(),
        "review_notes": "Clinical Advisory Committee reclassified compound to Prescription-Only to prevent misuse.",
    }

    res = client.post("/api/v2/catalog/classifications/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data

    assert data["product_id"] == env["product"].product_id
    assert data["version_number"] == 2
    assert data["classification_type"] == ClassificationType.PRESCRIPTION_ONLY
    assert data["regulatory_source"] == "DGDA Gazette Notification S.R.O. 182-Act/2026"
    assert data["is_current"] is True
    assert len(data["immutable_hash"]) == 64

    # Verify v1 superseded
    env["snap_v1"].refresh_from_db()
    assert env["snap_v1"].is_current is False
    assert env["snap_v1"].effective_to is not None

    # Verify master medicine product synced
    env["product"].refresh_from_db()
    assert env["product"].is_prescription_only is True


@pytest.mark.django_db
def test_p247_incomplete_and_invalid_inputs_rejected(otc_rx_env):
    """
    Scenario 2: Incomplete inputs (missing source, invalid classification type,
    empty justification notes) are rejected with HTTP 400.
    """
    env = otc_rx_env
    client = APIClient()
    client.force_authenticate(user=env["superadmin"])

    # 1. Missing regulatory source (< 3 characters)
    res_no_source = client.post(
        "/api/v2/catalog/classifications/",
        {
            "product_id": env["product"].product_id,
            "classification_type": ClassificationType.PRESCRIPTION_ONLY,
            "regulatory_source": "DG",  # Too short
            "review_notes": "Valid review notes for testing",
        },
        format="json",
    )
    assert res_no_source.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 2. Invalid classification type
    res_bad_type = client.post(
        "/api/v2/catalog/classifications/",
        {
            "product_id": env["product"].product_id,
            "classification_type": "NOT_A_VALID_TYPE",
            "regulatory_source": "DGDA Gazette 2026",
            "review_notes": "Valid review notes for testing",
        },
        format="json",
    )
    assert res_bad_type.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 3. Missing review notes (< 5 characters)
    res_short_notes = client.post(
        "/api/v2/catalog/classifications/",
        {
            "product_id": env["product"].product_id,
            "classification_type": ClassificationType.PRESCRIPTION_ONLY,
            "regulatory_source": "DGDA Gazette 2026",
            "review_notes": "N/A",  # Too short
        },
        format="json",
    )
    assert res_short_notes.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p247_point_in_time_historical_resolution(otc_rx_env):
    """
    Scenario 3: Historical point-in-time resolution correctly resolves OTC
    for past dates prior to transition, and POM after the effective date.
    """
    env = otc_rx_env
    transition_point = timezone.now() - timedelta(days=10)

    # Create v2 snapshot at transition_point
    v2_data = MedicineClassificationSnapshotService.create_snapshot(
        product_id=env["product"].product_id,
        data={
            "classification_type": ClassificationType.PRESCRIPTION_ONLY,
            "regulatory_source": "DGDA Circular Ref 2026/08",
            "effective_from": transition_point,
            "review_notes": "Transition from OTC to POM",
        },
        actor=env["superadmin"],
    )
    assert v2_data["version_number"] == 2

    client = APIClient()
    client.force_authenticate(user=env["pharmacist"])

    # Query point-in-time BEFORE transition (20 days ago) -> Must be OTC (v1)
    past_date = (transition_point - timedelta(days=10)).isoformat()
    res_past = client.get(
        f"/api/v2/catalog/classifications/point-in-time/?product_id={env['product'].product_id}&target_date={past_date}"
    )
    assert res_past.status_code == status.HTTP_200_OK
    assert res_past.data["snapshot"]["classification_type"] == ClassificationType.OTC
    assert res_past.data["snapshot"]["version_number"] == 1

    # Query point-in-time AFTER transition (yesterday) -> Must be PRESCRIPTION_ONLY (v2)
    recent_date = (timezone.now() - timedelta(days=1)).isoformat()
    res_recent = client.get(
        f"/api/v2/catalog/classifications/point-in-time/?product_id={env['product'].product_id}&target_date={recent_date}"
    )
    assert res_recent.status_code == status.HTTP_200_OK
    assert res_recent.data["snapshot"]["classification_type"] == ClassificationType.PRESCRIPTION_ONLY
    assert res_recent.data["snapshot"]["version_number"] == 2


@pytest.mark.django_db
def test_p247_pos_safety_gate_dynamic_enforcement(otc_rx_env):
    """
    Scenario 4: POS Safety Gate dynamically enforces Rx requirements without hardcoded lists.
    When product was OTC, classify_product returned requires_rx_verification=False.
    Immediately upon reclassification to PRESCRIPTION_ONLY, it demands Rx verification.
    """
    env = otc_rx_env
    product = env["product"]

    # 1. Initial State: OTC
    initial_class = PosRxSafetyService.classify_product(product)
    assert initial_class["regulatory_classification"] == PosRegulatoryClass.OTC
    assert initial_class["requires_rx_verification"] is False
    assert initial_class["snapshot_version"] == 1

    # 2. Dynamic Reclassification to PRESCRIPTION_ONLY
    MedicineClassificationSnapshotService.create_snapshot(
        product_id=product.product_id,
        data={
            "classification_type": ClassificationType.PRESCRIPTION_ONLY,
            "regulatory_source": "DGDA National Formulary 2026/09",
            "effective_from": timezone.now(),
            "review_notes": "Live escalation to POM",
        },
        actor=env["superadmin"],
    )

    # 3. Post-Reclassification: POS checkout immediately blocks unverified sale
    product.refresh_from_db()
    updated_class = PosRxSafetyService.classify_product(product)
    assert updated_class["regulatory_classification"] == PosRegulatoryClass.PRESCRIPTION_ONLY
    assert updated_class["requires_rx_verification"] is True
    assert updated_class["snapshot_version"] == 2

    # 4. Verify POS Sale Order Line captures snapshot version
    sale = PosSaleOrder.objects.create(
        sale_id=f"sale-p247-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        cashier_user=env["cashier"],
        order_number=f"SO-P247-{env['uid'].upper()}",
    )
    line = PosSaleOrderLine.objects.create(
        line_id=f"line-p247-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a"],
        sale=sale,
        line_number=1,
        product=product,
        packaging_unit=PosPackagingUnit.TABLET,
        quantity_units=10,
        conversion_factor=1,
        base_units_total=10,
        unit_price_bdt=Decimal("3.50"),
        unit_cost_bdt=Decimal("2.20"),
        line_subtotal_bdt=Decimal("35.00"),
        line_total_bdt=Decimal("35.00"),
        requires_rx_verification=True,
        rx_verified=False,
        regulatory_classification=PosRegulatoryClass.PRESCRIPTION_ONLY,
        classification_version_snapshot=updated_class["snapshot_version"],
    )

    assert line.classification_version_snapshot == 2
    assert line.requires_rx_verification is True
    assert line.rx_verified is False


@pytest.mark.django_db
def test_p247_unauthorized_tenant_mutation_blocked(otc_rx_env):
    """
    Scenario 5: Multi-tenant RBAC boundary: Tenant Cashier and Staff Pharmacist
    are strictly forbidden from creating or modifying global master classification snapshots.
    However, they can read current classification and audit history.
    """
    env = otc_rx_env

    # 1. Cashier attempts to reclassify -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier"])
    res_cashier = client_cashier.post(
        "/api/v2/catalog/classifications/",
        {
            "product_id": env["product"].product_id,
            "classification_type": ClassificationType.PRESCRIPTION_ONLY,
            "regulatory_source": "Unauthorized Attempt",
            "review_notes": "Attempting unauthorized modification",
        },
        format="json",
    )
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Staff Pharmacist attempts to reclassify -> 403 Forbidden
    client_pharmacist = APIClient()
    client_pharmacist.force_authenticate(user=env["pharmacist"])
    res_pharmacist = client_pharmacist.post(
        "/api/v2/catalog/classifications/",
        {
            "product_id": env["product"].product_id,
            "classification_type": ClassificationType.PRESCRIPTION_ONLY,
            "regulatory_source": "Pharmacist Local Override",
            "review_notes": "Attempting tenant override",
        },
        format="json",
    )
    assert res_pharmacist.status_code == status.HTTP_403_FORBIDDEN

    # 3. Staff Pharmacist reads current classification -> 200 OK
    res_current = client_pharmacist.get(
        f"/api/v2/catalog/classifications/current/?product_id={env['product'].product_id}"
    )
    assert res_current.status_code == status.HTTP_200_OK
    assert res_current.data["current_snapshot"]["version_number"] == 1
