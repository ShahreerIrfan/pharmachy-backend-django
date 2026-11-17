"""
backend/tests/test_p220_inventory_fault_stress.py
Acceptance tests for P220: Inventory fault and stress test (Phase 11 Capstone).

Tests covered:
1. End-to-end movement conservation pipeline across StockLedger, InventoryCount,
   StockMovementCorrection, InterBranchTransfer, ColdChainExcursion, Snapshot, and Valuation.
2. High-volume stock ledger stress (100+ rapid postings, zero drift, SHA-256 verification).
3. Multi-threaded concurrent stock deduction contention (oversell rejection, zero negative stock).
4. Fault injection (zero-delta, over-deduction, ledger delete) + strict branch & tenant isolation.
5. Invalid inputs (HTTP 400/422), Cashier RBAC denial (HTTP 403), Suspended subscription lock (HTTP 403),
   and audit record immutability (API HTTP 405 & ORM ValidationError).
"""

from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.inventory_fault_stress_service import InventoryFaultStressService
from apps.core.models import (
    Branch,
    InventoryFaultStressRun,
    InventoryStressTestType,
    PermissionDecision,
    QaRunStatus,
    StockPermissionAuditLog,
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
        tenant_id=f"ten-qa-a-{uid}",
        name="United Care Hospital Pharmacy",
        subdomain=f"united-qa-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-qa-b-{uid}",
        name="Evercare Chittagong Pharmacy",
        subdomain=f"evercare-qa-{uid}",
        status=TenantLifecycleStatus.ACTIVE,
    )


@pytest.fixture
def tenant_suspended(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-qa-susp-{uid}",
        name="Suspended Dispensary Ltd.",
        subdomain=f"susp-qa-{uid}",
        status=TenantLifecycleStatus.SUSPENDED,
    )


@pytest.fixture
def branch_a(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-qa-a-{uid}",
        tenant=tenant_a,
        name="Dhanmondi Central Warehouse",
        is_active=True,
    )


@pytest.fixture
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-qa-a2-{uid}",
        tenant=tenant_a,
        name="Uttara Sector 11 Dispensary",
        is_active=True,
    )


@pytest.fixture
def branch_b(tenant_b):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-qa-b-{uid}",
        tenant=tenant_b,
        name="Chittagong Agrabad Branch",
        is_active=True,
    )


@pytest.fixture
def branch_suspended(tenant_suspended):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-qa-susp-{uid}",
        tenant=tenant_suspended,
        name="Suspended Branch",
        is_active=True,
    )


@pytest.fixture
def owner_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email=f"owner_{uuid.uuid4().hex[:6]}@united.com",
        password="StrongPassword123!",
        role=UserRole.PHARMACY_OWNER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def pharmacist_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email=f"pharma_{uuid.uuid4().hex[:6]}@united.com",
        password="StrongPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email=f"cashier_{uuid.uuid4().hex[:6]}@united.com",
        password="StrongPassword123!",
        role=UserRole.CASHIER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def owner_b(tenant_b, branch_b):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email=f"owner_b_{uuid.uuid4().hex[:6]}@evercare.com",
        password="StrongPassword123!",
        role=UserRole.PHARMACY_OWNER,
        tenant_id=tenant_b.tenant_id,
        branch_id=branch_b.branch_id,
        is_active=True,
    )


@pytest.fixture
def owner_suspended(tenant_suspended, branch_suspended):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email=f"owner_s_{uuid.uuid4().hex[:6]}@suspended.com",
        password="StrongPassword123!",
        role=UserRole.PHARMACY_OWNER,
        tenant_id=tenant_suspended.tenant_id,
        branch_id=branch_suspended.branch_id,
        is_active=True,
    )


@pytest.mark.django_db(transaction=True)
def test_01_end_to_end_conservation_pipeline(
    api_client, owner_a, branch_a, branch_a2
):
    """
    Scenario 1: Integrated Phase 11 stock conservation pipeline via DRF API.
    Exercises receipt -> count -> reversal -> interbranch split transfer -> cold chain excursion -> snapshot/valuation.
    """
    api_client.force_authenticate(user=owner_a)
    res = api_client.post(
        "/api/v2/inventory/qa-stress/verify-conservation/",
        {
            "branch": branch_a.branch_id,
            "dest_branch": branch_a2.branch_id,
            "initial_units": 200,
        },
        format="json",
    )
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data
    assert data["test_type"] == InventoryStressTestType.CONSERVATION_PIPELINE
    assert data["status"] == QaRunStatus.PASSED
    assert data["conservation_verified"] is True
    assert data["branch_isolation_verified"] is True
    assert data["initial_stock_units"] == 240  # 200 ambient + 40 cold chain
    assert data["final_stock_units"] == 120  # (200 - 5 + 5 - 80) + 0 disposed
    assert data["net_ledger_delta_units"] == 120
    assert data["rejected_faults"] >= 1
    assert data["metrics_payload"]["transfer_reconciled_units"] == 80
    assert len(data["verification_hash"]) == 64

    # Verify persisted DB integrity & hash verification
    run_obj = InventoryFaultStressRun.objects.get(run_id=data["run_id"])
    assert run_obj.verification_hash == run_obj.compute_hash()

    # Verify list & retrieve endpoints
    res_list = api_client.get(f"/api/v2/inventory/qa-stress/?branch={branch_a.branch_id}")
    assert res_list.status_code == status.HTTP_200_OK
    res_detail = api_client.get(f"/api/v2/inventory/qa-stress/{run_obj.run_id}/")
    assert res_detail.status_code == status.HTTP_200_OK
    assert res_detail.data["run_id"] == run_obj.run_id


@pytest.mark.django_db(transaction=True)
def test_02_high_volume_ledger_stress(api_client, owner_a, branch_a):
    """
    Scenario 2: High-volume stock ledger stress (100 rapid inward/outward postings + seed = 101 entries).
    Proves zero discrepancy, snapshot balance, and exact ledger delta equality.
    """
    api_client.force_authenticate(user=owner_a)
    res = api_client.post(
        "/api/v2/inventory/qa-stress/run-high-volume/",
        {"branch": branch_a.branch_id, "operation_count": 100},
        format="json",
    )
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data
    assert data["test_type"] == InventoryStressTestType.HIGH_VOLUME_STRESS
    assert data["status"] == QaRunStatus.PASSED
    assert data["total_operations"] == 101
    assert data["successful_operations"] == 101
    assert data["conservation_verified"] is True
    assert data["metrics_payload"]["ledger_entries_verified"] == 101
    assert data["metrics_payload"]["discrepancy"] == 0
    assert data["final_stock_units"] == data["net_ledger_delta_units"]
    assert len(data["verification_hash"]) == 64


@pytest.mark.django_db(transaction=True)
def test_03_concurrent_update_oversell_guard(api_client, owner_a, branch_a):
    """
    Scenario 3: Concurrent update stress test proving pessimistic locking, exact oversell rejection,
    and zero negative stock under multi-threaded demand contention.
    """
    api_client.force_authenticate(user=owner_a)
    res = api_client.post(
        "/api/v2/inventory/qa-stress/run-concurrency/",
        {
            "branch": branch_a.branch_id,
            "initial_units": 50,
            "concurrency_level": 20,
            "units_per_request": 5,
        },
        format="json",
    )
    assert res.status_code == status.HTTP_201_CREATED
    data = res.data
    assert data["test_type"] == InventoryStressTestType.CONCURRENT_MUTATION
    assert data["status"] == QaRunStatus.PASSED
    assert data["total_operations"] == 20
    assert data["successful_operations"] == 10
    assert data["rejected_faults"] == 10
    assert data["final_stock_units"] == 0
    assert data["net_ledger_delta_units"] == 0
    assert data["conservation_verified"] is True

    # Non-divisible remainder concurrency verification directly via service (33 units / 5 per request = 6 ok, 4 rejected, 3 left)
    rem_run = InventoryFaultStressService.run_concurrent_mutation_stress(
        user=owner_a,
        branch_id=branch_a.branch_id,
        initial_units=33,
        concurrency_level=10,
        units_per_request=5,
    )
    assert rem_run.status == QaRunStatus.PASSED
    assert rem_run.successful_operations == 6
    assert rem_run.rejected_faults == 4
    assert rem_run.final_stock_units == 3
    assert rem_run.net_ledger_delta_units == 3
    assert rem_run.conservation_verified is True


@pytest.mark.django_db(transaction=True)
def test_04_fault_injection_and_branch_tenant_isolation(
    api_client, pharmacist_a, owner_b, branch_a, branch_a2
):
    """
    Scenario 4:
    - Controlled fault injection (zero-delta, over-deduction, ledger delete, sibling branch probe).
    - Branch-bound staff denied from sibling branch (HTTP 403 ERR_CROSS_BRANCH_ACCESS_DENIED).
    - Cross-tenant probe blocked with HTTP 404 NotFound to prevent tenant enumeration.
    """
    # 1. Fault & isolation suite by branch-bound pharmacist
    api_client.force_authenticate(user=pharmacist_a)
    res_fault = api_client.post(
        "/api/v2/inventory/qa-stress/verify-fault-isolation/",
        {"branch": branch_a.branch_id, "sibling_branch": branch_a2.branch_id},
        format="json",
    )
    assert res_fault.status_code == status.HTTP_201_CREATED
    fdata = res_fault.data
    assert fdata["test_type"] == InventoryStressTestType.FAULT_AND_ISOLATION
    assert fdata["status"] == QaRunStatus.PASSED
    assert fdata["total_operations"] == 4
    assert fdata["rejected_faults"] == 4
    assert fdata["final_stock_units"] == 25
    assert fdata["conservation_verified"] is True
    assert fdata["branch_isolation_verified"] is True

    cross_branch_audit = StockPermissionAuditLog.objects.filter(
        branch=branch_a2,
        actor=pharmacist_a,
        decision=PermissionDecision.DENIED,
        error_code="ERR_CROSS_BRANCH_ACCESS_DENIED",
    ).first()
    assert cross_branch_audit is not None

    # 2. Direct cross-branch invocation denied (HTTP 403 ERR_CROSS_BRANCH_ACCESS_DENIED)
    res_cross_br = api_client.post(
        "/api/v2/inventory/qa-stress/run-high-volume/",
        {"branch": branch_a2.branch_id, "operation_count": 10},
        format="json",
    )
    assert res_cross_br.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_CROSS_BRANCH_ACCESS_DENIED" in str(res_cross_br.data)

    # 3. Cross-tenant isolation (Owner B targeting Tenant A branch -> HTTP 404 NotFound)
    api_client.force_authenticate(user=owner_b)
    res_cross_ten = api_client.post(
        "/api/v2/inventory/qa-stress/run-concurrency/",
        {"branch": branch_a.branch_id, "initial_units": 20, "concurrency_level": 4},
        format="json",
    )
    assert res_cross_ten.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db(transaction=True)
def test_05_validation_rbac_subscription_and_immutability(
    api_client, owner_a, cashier_a, owner_suspended, branch_a, branch_suspended
):
    """
    Scenario 5:
    - Invalid payload bounds rejected (HTTP 400/422).
    - Cashier RBAC denied across GET and POST (HTTP 403 ERR_PERMISSION_DENIED).
    - Suspended tenant subscription locked (HTTP 403 ERR_TENANT_SUSPENDED).
    - Audit evidence strictly immutable (PUT/PATCH/DELETE -> HTTP 405, ORM edit/delete -> ValidationError).
    """
    # 1. Validation rejections
    api_client.force_authenticate(user=owner_a)
    res_bad_vol = api_client.post(
        "/api/v2/inventory/qa-stress/run-high-volume/",
        {"branch": branch_a.branch_id, "operation_count": 2},
        format="json",
    )
    assert res_bad_vol.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    res_bad_type = api_client.post(
        "/api/v2/inventory/qa-stress/run-high-volume/",
        {"branch": branch_a.branch_id, "operation_count": "not-an-int"},
        format="json",
    )
    assert res_bad_type.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # 2. Create a valid run for immutability check later
    res_ok = api_client.post(
        "/api/v2/inventory/qa-stress/verify-fault-isolation/",
        {"branch": branch_a.branch_id},
        format="json",
    )
    assert res_ok.status_code == status.HTTP_201_CREATED
    run_id = res_ok.data["run_id"]

    # 3. Cashier RBAC denial
    api_client.force_authenticate(user=cashier_a)
    res_cash_list = api_client.get("/api/v2/inventory/qa-stress/")
    assert res_cash_list.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_PERMISSION_DENIED" in str(res_cash_list.data)

    res_cash_post = api_client.post(
        "/api/v2/inventory/qa-stress/verify-conservation/",
        {"branch": branch_a.branch_id},
        format="json",
    )
    assert res_cash_post.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_PERMISSION_DENIED" in str(res_cash_post.data)

    # 4. Suspended tenant subscription lock (middleware or service)
    api_client.force_authenticate(user=owner_suspended)
    res_susp = api_client.post(
        "/api/v2/inventory/qa-stress/verify-fault-isolation/",
        {"branch": branch_suspended.branch_id},
        format="json",
    )
    assert res_susp.status_code == status.HTTP_403_FORBIDDEN
    susp_payload = res_susp.data if hasattr(res_susp, "data") else res_susp.json()
    assert susp_payload.get("error_code") in (
        "ERR_TENANT_SUSPENDED",
        "ERR_SUBSCRIPTION_UNENTITLED_WRITE_BLOCKED",
    )

    # 5. API & ORM Immutability
    api_client.force_authenticate(user=owner_a)
    assert api_client.put(f"/api/v2/inventory/qa-stress/{run_id}/", {}, format="json").status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert api_client.patch(f"/api/v2/inventory/qa-stress/{run_id}/", {}, format="json").status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert api_client.delete(f"/api/v2/inventory/qa-stress/{run_id}/").status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    run_obj = InventoryFaultStressRun.objects.get(run_id=run_id)
    run_obj.status = QaRunStatus.FAILED
    with pytest.raises(DjangoValidationError):
        run_obj.save()
    with pytest.raises(DjangoValidationError):
        run_obj.delete()
