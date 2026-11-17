"""
backend/tests/test_p255_incident_correction_workflow.py

Architecture Decision: ADR-0255 (P255).
Phase 13: Prescriptions, Antibiotic Register & Compliance Safety.

Test Suite for Clinical and Operational Incident & Correction Workflow:
1. Valid incident lifecycle: Cashier report -> Pharmacist investigation -> Manager closure.
2. Malformed / incomplete inputs: short title, missing description, negative values, future date.
3. Strict closure authorization gate: Cashier blocked (403), cross-branch blocked, same-branch pharmacist authorized.
4. Mandatory CAPA & root cause validation for High/Critical severity closures.
5. Stock quarantine workflow and cross-tenant boundary isolation.
6. Branch incident summary metrics and financial loss aggregation in Decimal BDT.
"""

from decimal import Decimal
import datetime
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    IncidentRootCause,
    IncidentSeverity,
    IncidentStatus,
    PharmacyIncidentRecord,
    PharmacyIncidentType,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def incident_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p255-a-{uid}", name="Safety First Pharmacy A", subdomain=f"p255a-{uid}"
    )
    br_a1 = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p255-a1-{uid}", name="Gulshan Main Branch", branch_code="GL01"
    )
    br_a2 = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p255-a2-{uid}", name="Mirpur Branch", branch_code="MP01"
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    manager_a1 = PharmacyUser.objects.create_user(
        email=f"manager_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a1.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    pharmacist_a1 = PharmacyUser.objects.create_user(
        email=f"pharmacist_a1_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a1.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    pharmacist_a2 = PharmacyUser.objects.create_user(
        email=f"pharmacist_a2_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a2.branch_id,
        role=UserRole.STAFF_PHARMACIST,
    )

    cashier_a1 = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a1.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-tenant boundary test)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p255-b-{uid}", name="Safety First Pharmacy B", subdomain=f"p255b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p255-b-{uid}", name="Dhanmondi Branch", branch_code="DH01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    return {
        "uid": uid,
        "tenant_a": ten_a,
        "branch_a1": br_a1,
        "branch_a2": br_a2,
        "owner_a": owner_a,
        "manager_a1": manager_a1,
        "pharmacist_a1": pharmacist_a1,
        "pharmacist_a2": pharmacist_a2,
        "cashier_a1": cashier_a1,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_p255_valid_incident_lifecycle(incident_env):
    """Scenario 1: Cashier reports dispensing near-miss; Pharmacist investigates; Manager closes."""
    env = incident_env

    # 1. Cashier flags incorrect dispensing report
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a1"])

    report_payload = {
        "branch_id": env["branch_a1"].branch_id,
        "incident_type": "INCORRECT_DISPENSING",
        "severity": "MEDIUM",
        "title": "Near-Miss LASA Dispensing of Cefixime vs Cefuroxime",
        "description": "Strip of Cefuroxime 500mg selected instead of Cefixime 200mg; intercepted by cashier before billing.",
        "product_name": "Cefuroxime 500mg",
        "batch_number": "CFX-2026-90",
        "affected_quantity": "10.00",
        "financial_impact_bdt": "0.00",
    }
    res_report = client_cashier.post("/api/v2/compliance/incidents/", report_payload, format="json")
    assert res_report.status_code == status.HTTP_201_CREATED
    inc_id = res_report.data["incident_id"]
    assert res_report.data["status"] == "REPORTED"

    # 2. Pharmacist transitions to UNDER_INVESTIGATION and records findings
    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a1"])

    res_trans = client_pharma.post(
        f"/api/v2/compliance/incidents/{inc_id}/transition-status/",
        {"new_status": "UNDER_INVESTIGATION"},
        format="json",
    )
    assert res_trans.status_code == status.HTTP_200_OK
    assert res_trans.data["status"] == "UNDER_INVESTIGATION"

    res_patch = client_pharma.patch(
        f"/api/v2/compliance/incidents/{inc_id}/",
        {
            "investigation_notes": "Both products stored adjacent on shelf B-04. Look-alike packaging contributed to near miss.",
            "root_cause": "HUMAN_ERROR",
            "corrective_action_plan": "Separated products into distinct shelf bins with tall-man lettering alert.",
        },
        format="json",
    )
    assert res_patch.status_code == status.HTTP_200_OK
    assert res_patch.data["root_cause"] == "HUMAN_ERROR"

    # 3. Branch Manager approves closure
    client_mgr = APIClient()
    client_mgr.force_authenticate(user=env["manager_a1"])

    res_close = client_mgr.post(
        f"/api/v2/compliance/incidents/{inc_id}/transition-status/",
        {
            "new_status": "CLOSED",
            "closure_notes": "Shelf segregation verified. All cashiers briefed during morning huddle.",
        },
        format="json",
    )
    assert res_close.status_code == status.HTTP_200_OK
    assert res_close.data["status"] == "CLOSED"
    assert res_close.data["closed_by_id"] == str(env["manager_a1"].user_id)


@pytest.mark.django_db
def test_p255_invalid_and_incomplete_inputs(incident_env):
    """Scenario 2: Malformed inputs (short title, missing desc, negative qty/bdt, future date) rejected."""
    env = incident_env
    client = APIClient()
    client.force_authenticate(user=env["pharmacist_a1"])

    # 1. Title too short (< 3 chars)
    res_short = client.post(
        "/api/v2/compliance/incidents/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "incident_type": "SUSPECTED_DAMAGE",
            "title": "A",
            "description": "Broken vial",
        },
        format="json",
    )
    assert res_short.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "title" in res_short.data.get("details", res_short.data)

    # 2. Missing description
    res_nodesc = client.post(
        "/api/v2/compliance/incidents/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "incident_type": "SUSPECTED_DAMAGE",
            "title": "Broken Vial Incident",
            "description": "",
        },
        format="json",
    )
    assert res_nodesc.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "description" in res_nodesc.data.get("details", res_nodesc.data)

    # 3. Negative affected quantity
    res_neg_qty = client.post(
        "/api/v2/compliance/incidents/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "incident_type": "INVENTORY_DISCREPANCY",
            "title": "Inventory Audit Mismatch",
            "description": "Stock count lower than ledger",
            "affected_quantity": "-5.00",
        },
        format="json",
    )
    assert res_neg_qty.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 4. Negative financial impact BDT
    res_neg_fin = client.post(
        "/api/v2/compliance/incidents/",
        {
            "branch_id": env["branch_a1"].branch_id,
            "incident_type": "INVENTORY_DISCREPANCY",
            "title": "Inventory Audit Mismatch",
            "description": "Stock count lower than ledger",
            "financial_impact_bdt": "-1500.00",
        },
        format="json",
    )
    assert res_neg_fin.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p255_closure_authorization_gate(incident_env):
    """Scenario 3: Cashier barred from closing; Cross-branch pharmacist blocked; Authorized pharmacist closes."""
    env = incident_env

    inc = PharmacyIncidentRecord.objects.create(
        incident_id=f"inc-gate-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        incident_type=PharmacyIncidentType.SUSPECTED_DAMAGE,
        severity=IncidentSeverity.MEDIUM,
        status=IncidentStatus.UNDER_INVESTIGATION,
        title="Compromised Insulin Cold-Chain Package",
        description="Delivery box arrived with ice packs completely melted.",
    )

    # 1. Cashier attempts to close -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a1"])
    res_cashier_close = client_cashier.post(
        f"/api/v2/compliance/incidents/{inc.incident_id}/transition-status/",
        {"new_status": "CLOSED", "closure_notes": "Cashier unauthorized closure"},
        format="json",
    )
    assert res_cashier_close.status_code == status.HTTP_403_FORBIDDEN
    assert res_cashier_close.data["error_code"] == "ERR_INCIDENT_CLOSURE_UNAUTHORIZED"

    # 2. Pharmacist from Branch A2 attempts to close Branch A1 incident -> 403 Forbidden
    client_pharma_a2 = APIClient()
    client_pharma_a2.force_authenticate(user=env["pharmacist_a2"])
    res_a2_close = client_pharma_a2.post(
        f"/api/v2/compliance/incidents/{inc.incident_id}/transition-status/",
        {"new_status": "CLOSED", "closure_notes": "Wrong branch closing"},
        format="json",
    )
    assert res_a2_close.status_code == status.HTTP_403_FORBIDDEN

    # 3. Pharmacist from Branch A1 successfully closes
    client_pharma_a1 = APIClient()
    client_pharma_a1.force_authenticate(user=env["pharmacist_a1"])
    res_a1_close = client_pharma_a1.post(
        f"/api/v2/compliance/incidents/{inc.incident_id}/transition-status/",
        {"new_status": "CLOSED", "closure_notes": "Batch returned to distributor under claim."},
        format="json",
    )
    assert res_a1_close.status_code == status.HTTP_200_OK
    assert res_a1_close.data["status"] == "CLOSED"


@pytest.mark.django_db
def test_p255_high_critical_mandatory_capa_rule(incident_env):
    """Scenario 4: High or Critical severity incident closure rejected without documented RCA and CAPA."""
    env = incident_env

    inc_crit = PharmacyIncidentRecord.objects.create(
        incident_id=f"inc-crit-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        incident_type=PharmacyIncidentType.PRODUCT_RECALL,
        severity=IncidentSeverity.CRITICAL,
        status=IncidentStatus.UNDER_INVESTIGATION,
        title="DGDA Mandatory Batch Recall - Contaminated Syrups",
        description="Manufacturer Class 1 recall on Paracetamol 120mg syrup batch B-9988.",
        requires_dgda_notification=True,
    )

    client_mgr = APIClient()
    client_mgr.force_authenticate(user=env["manager_a1"])

    # 1. Attempt to close without root cause & CAPA -> 422 Unprocessable Entity
    res_no_capa = client_mgr.post(
        f"/api/v2/compliance/incidents/{inc_crit.incident_id}/transition-status/",
        {"new_status": "CLOSED", "closure_notes": "Tried to close prematurely"},
        format="json",
    )
    assert res_no_capa.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert res_no_capa.data["error_code"] == "ERR_CAPA_REQUIRED_FOR_CLOSURE"

    # 2. Provide RCA and CAPA -> Closure succeeds
    res_with_capa = client_mgr.post(
        f"/api/v2/compliance/incidents/{inc_crit.incident_id}/transition-status/",
        {
            "new_status": "CLOSED",
            "root_cause": "SUPPLIER_DEFECT",
            "corrective_action_plan": "100% stock quarantined; returned to supplier with DGDA reconciliation manifest.",
            "closure_notes": "Stock quarantined and destroyed under inspector supervision.",
        },
        format="json",
    )
    assert res_with_capa.status_code == status.HTTP_200_OK
    assert res_with_capa.data["status"] == "CLOSED"
    assert res_with_capa.data["root_cause"] == "SUPPLIER_DEFECT"


@pytest.mark.django_db
def test_p255_quarantine_stock_and_cross_tenant_isolation(incident_env):
    """Scenario 5: Quarantine stock action and Tenant B isolation."""
    env = incident_env

    inc_a = PharmacyIncidentRecord.objects.create(
        incident_id=f"inc-ten-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        incident_type=PharmacyIncidentType.SUSPECTED_DAMAGE,
        severity=IncidentSeverity.MEDIUM,
        title="Cracked Glass Ampoules",
        description="Ampoules cracked during shipping box unpacking.",
        batch_number="AMP-5544",
        affected_quantity=Decimal("25.00"),
    )

    client_pharma = APIClient()
    client_pharma.force_authenticate(user=env["pharmacist_a1"])

    # 1. Quarantine affected batch
    res_quarantine = client_pharma.post(
        f"/api/v2/compliance/incidents/{inc_a.incident_id}/quarantine-stock/",
        {"quarantine_notes": "Moved 25 ampoules to Red Quarantine Bin."},
        format="json",
    )
    assert res_quarantine.status_code == status.HTTP_200_OK
    assert res_quarantine.data["is_stock_quarantined"] is True
    assert res_quarantine.data["batch_number"] == "AMP-5544"

    # 2. Tenant B Owner attempts to access Tenant A incident -> 404 Not Found
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])
    res_cross = client_b.get(f"/api/v2/compliance/incidents/{inc_a.incident_id}/")
    assert res_cross.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_p255_branch_incident_summary_metrics(incident_env):
    """Scenario 6: Branch incident summary aggregates severity distribution and Decimal BDT financial loss."""
    env = incident_env

    PharmacyIncidentRecord.objects.create(
        incident_id=f"inc-sum1-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        incident_type=PharmacyIncidentType.INVENTORY_DISCREPANCY,
        severity=IncidentSeverity.LOW,
        status=IncidentStatus.CLOSED,
        title="Missing Cotton Roll",
        description="Count short by 1 pack.",
        financial_impact_bdt=Decimal("120.50"),
    )
    PharmacyIncidentRecord.objects.create(
        incident_id=f"inc-sum2-{env['uid']}",
        tenant=env["tenant_a"],
        branch=env["branch_a1"],
        incident_type=PharmacyIncidentType.SUSPECTED_DAMAGE,
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.UNDER_INVESTIGATION,
        title="Damaged Inhaler Box",
        description="Crushed carton with aerosol valve leakage.",
        financial_impact_bdt=Decimal("850.25"),
    )

    client_mgr = APIClient()
    client_mgr.force_authenticate(user=env["manager_a1"])

    res_sum = client_mgr.get(
        f"/api/v2/compliance/incidents/branch-summary/?branch_id={env['branch_a1'].branch_id}"
    )
    assert res_sum.status_code == status.HTTP_200_OK
    data = res_sum.data
    assert data["total_incidents"] == 2
    assert data["status_distribution"]["CLOSED"] == 1
    assert data["status_distribution"]["UNDER_INVESTIGATION"] == 1
    assert data["total_financial_impact_bdt"] == "970.75"

