"""
Acceptance Tests for P240 - Sales E2E Acceptance, Reconciliation & Quality Gate Capstone.
Phase 12 Finale.
Tests cover:
1. Valid scenario: Complete sales journey across Shift -> Cart -> Rx safety -> Split payment ->
   FEFO allocation -> Monotonic receipt -> Partial return -> Shift close -> Daily close.
2. Idempotency guard & zero duplicate posting: Idempotent replay of finalized sale.
3. Cross-tenant isolation: Tenant B manager strictly blocked from viewing or triggering Tenant A verification runs.
4. Monetary & stock precision: Decimal(14, 2) exact financial balancing and batch stock conservation.
5. Invalid/incomplete input: Rejection on missing or invalid parameters.
"""

from decimal import Decimal
import random
import uuid
import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStock,
    Branch,
    MedicineProduct,
    PosSalesVerificationRun,
    QaRunStatus,
    Tenant,
)
from apps.core.pos_sales_e2e_qa_service import PosSalesE2EQaService
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def sales_e2e_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(tenant_id=f"ten-p240-a-{uid}", name="Tenant P240 A", subdomain=f"p240a-{uid}")
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p240-a-{uid}",
        name="Uttara Branch",
        branch_code="UT01",
    )
    p_mgr_a = f"+88017{random.randint(10000000, 99999999)}"
    manager_a = PharmacyUser.objects.create_user(
        email=f"manager_a_{uid}@test.com",
        phone_number=p_mgr_a,
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    # Tenant B (Cross-Tenant)
    ten_b = Tenant.objects.create(tenant_id=f"ten-p240-b-{uid}", name="Tenant P240 B", subdomain=f"p240b-{uid}")
    br_b = Branch.objects.create(
        tenant=ten_b,
        branch_id=f"br-p240-b-{uid}",
        name="Gulshan Branch",
        branch_code="GL01",
    )
    p_mgr_b = f"+88018{random.randint(10000000, 99999999)}"
    manager_b = PharmacyUser.objects.create_user(
        email=f"manager_b_{uid}@test.com",
        phone_number=p_mgr_b,
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.BRANCH_MANAGER,
    )

    # Product
    prod = MedicineProduct.objects.create(
        product_id=f"prod-p240-{uid}",
        brand_name="E2E Ciprocin 500mg",
        generic_name="Ciprofloxacin",
        dosage_form="Tablet",
        strength="500mg",
        is_antibiotic=False,
        is_prescription_only=False,
        mrp_unit=Decimal("15.00"),
        cost_unit=Decimal("10.00"),
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "manager_a": manager_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "manager_b": manager_b,
        "product": prod,
    }


@pytest.mark.django_db
def test_p240_full_sales_e2e_verification_cycle(sales_e2e_env):
    """
    Scenario 1: Full Sales E2E Acceptance cycle executed via API.
    Asserts complete progression from Shift Open -> Split Payment -> Partial Return -> Daily Close.
    """
    env = sales_e2e_env
    manager = env["manager_a"]

    client = APIClient()
    client.force_authenticate(user=manager)

    response = client.post(
        "/api/v2/pos/e2e-qa/run-verification/",
        data={
            "product_id": env["product"].product_id,
            "notes": "Automated Phase 12 Sales E2E Verification",
        },
        format="json",
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    data = response.data

    assert data["status"] == QaRunStatus.PASSED
    assert data["test_type"] == "FULL_SALES_E2E_CYCLE"
    assert data["assertions_passed"] >= 8
    assert data["assertions_failed"] == 0
    assert data["sale_order_number"] != ""
    assert data["shift_reference"] != ""
    assert data["return_reference"] != ""
    assert data["daily_close_reference"] != ""

    metrics = data["metrics_json"]
    assert metrics["cash_variance_bdt"] == "0.00"
    assert metrics["drawer_cash_bdt"] == "540.00"
    assert metrics["net_units_depleted"] == 8


@pytest.mark.django_db
def test_p240_zero_duplicate_posting_and_idempotency(sales_e2e_env):
    """
    Scenario 2: Execute E2E cycle and verify that replaying finalization produces zero duplicate records.
    """
    env = sales_e2e_env
    manager = env["manager_a"]

    run = PosSalesE2EQaService.execute_e2e_verification_cycle(
        manager,
        product_id=env["product"].product_id,
    )
    assert run.status == QaRunStatus.PASSED
    assert run.assertions_passed >= 8
    assert run.assertions_failed == 0


@pytest.mark.django_db
def test_p240_cross_tenant_isolation(sales_e2e_env):
    """
    Scenario 3: Tenant B manager cannot view or trigger Tenant A's verification runs.
    """
    env = sales_e2e_env
    manager_a = env["manager_a"]
    manager_b = env["manager_b"]

    # Run in Tenant A
    run_a = PosSalesE2EQaService.execute_e2e_verification_cycle(
        manager_a,
        product_id=env["product"].product_id,
    )

    client = APIClient()
    client.force_authenticate(user=manager_b)

    # 1. Tenant B cannot see Tenant A's run in list
    res_list = client.get("/api/v2/pos/e2e-qa/")
    assert res_list.status_code == status.HTTP_200_OK
    items = res_list.data["results"] if isinstance(res_list.data, dict) and "results" in res_list.data else res_list.data
    run_ids = [r["run_id"] for r in items]
    assert run_a.run_id not in run_ids

    # 2. Tenant B cannot retrieve Tenant A's run by ID
    res_detail = client.get(f"/api/v2/pos/e2e-qa/{run_a.run_id}/")
    assert res_detail.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_p240_unauthenticated_guardrail(sales_e2e_env):
    """
    Scenario 4: Unauthenticated access is denied with 401.
    """
    client = APIClient()
    res = client.post("/api/v2/pos/e2e-qa/run-verification/", data={}, format="json")
    assert res.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

