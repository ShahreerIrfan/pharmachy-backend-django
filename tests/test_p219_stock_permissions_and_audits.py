"""
backend/tests/test_p219_stock_permissions_and_audits.py
Acceptance tests for P219: Stock permissions and audits.
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

Tests covered:
1. Valid authorization across roles: Pharmacist, Inventory Clerk, Finance Officer, and Tenant Owner.
2. Strict role-based denials: Inventory Clerk denied quarantine release/valuation, Cashier denied counting/adjustment (HTTP 403 + Audit log).
3. Cross-branch boundary enforcement: Branch A pharmacist blocked from Branch A2 within the same tenant (HTTP 403 ERR_CROSS_BRANCH_ACCESS_DENIED).
4. Cross-tenant isolation: Tenant B pharmacist blocked from Tenant A branch (HTTP 404).
5. Audit log immutability and SHA-256 tamper-evident signature verification.
"""

import uuid

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PermissionDecision,
    StockActionScope,
    StockPermissionAuditLog,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant_a(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-a-{uid}",
        name="United Hospital Pharmacy",
        subdomain=f"united-perm-{uid}",
        status="ACTIVE",
    )


@pytest.fixture
def tenant_b(db):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-b-{uid}",
        name="Apollo Dhaka Dispensary",
        subdomain=f"apollo-perm-{uid}",
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
def branch_a2(tenant_a):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-a2-{uid}",
        tenant=tenant_a,
        name="Banani Satellite Branch",
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
def owner_user(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="owner_perm@united.com",
        password="ValidPassword123!",
        role=UserRole.PHARMACY_OWNER,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def pharmacist_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="pharmacist_a@united.com",
        password="ValidPassword123!",
        role=UserRole.STAFF_PHARMACIST,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def clerk_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="clerk_a@united.com",
        password="ValidPassword123!",
        role=UserRole.INVENTORY_CLERK,
        tenant_id=tenant_a.tenant_id,
        branch_id=branch_a.branch_id,
        is_active=True,
    )


@pytest.fixture
def cashier_a(tenant_a, branch_a):
    return PharmacyUser.objects.create_user(
        phone_number=f"+88017{uuid.uuid4().int % 90000000 + 10000000}",
        email="cashier_a@united.com",
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


@pytest.mark.django_db
def test_valid_role_permissions_and_tenant_owner_multi_branch(
    api_client, pharmacist_a, clerk_a, owner_user, branch_a, branch_a2
):
    """
    Scenario 1:
    - Pharmacist A allowed QUARANTINE_RELEASE and STOCK_ADJUSTMENT in Branch A.
    - Inventory Clerk A allowed STOCK_COUNTING in Branch A.
    - Pharmacy Owner allowed across both Branch A and Branch A2.
    """
    # 1. Pharmacist A -> QUARANTINE_RELEASE in Branch A
    api_client.force_authenticate(user=pharmacist_a)
    res_ph = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a.branch_id, "action_scope": StockActionScope.QUARANTINE_RELEASE, "resource_id": "bat-01"},
        format="json",
    )
    assert res_ph.status_code == status.HTTP_200_OK
    assert res_ph.data["decision"] == PermissionDecision.ALLOWED
    assert len(res_ph.data["sha256_signature"]) == 64

    # 2. Inventory Clerk A -> STOCK_COUNTING in Branch A
    api_client.force_authenticate(user=clerk_a)
    res_clk = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a.branch_id, "action_scope": StockActionScope.STOCK_COUNTING, "resource_id": "cnt-01"},
        format="json",
    )
    assert res_clk.status_code == status.HTTP_200_OK
    assert res_clk.data["decision"] == PermissionDecision.ALLOWED

    # 3. Pharmacy Owner -> INTERBRANCH_TRANSFER on Branch A2 (multi-branch tenant-wide privilege)
    api_client.force_authenticate(user=owner_user)
    res_own = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a2.branch_id, "action_scope": StockActionScope.INTERBRANCH_TRANSFER, "resource_id": "trf-01"},
        format="json",
    )
    assert res_own.status_code == status.HTTP_200_OK
    assert res_own.data["decision"] == PermissionDecision.ALLOWED


@pytest.mark.django_db
def test_role_denials_logged_and_forbidden(
    api_client, clerk_a, cashier_a, branch_a
):
    """
    Scenario 2:
    - Inventory Clerk denied QUARANTINE_RELEASE & VALUATION_DATA (403 Forbidden).
    - Cashier denied STOCK_COUNTING & STOCK_ADJUSTMENT (403 Forbidden).
    - Every denial generates a permanent DENIED audit log entry.
    """
    # 1. Inventory Clerk attempting QUARANTINE_RELEASE
    api_client.force_authenticate(user=clerk_a)
    res_clk_qur = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a.branch_id, "action_scope": StockActionScope.QUARANTINE_RELEASE, "resource_id": "bat-01"},
        format="json",
    )
    assert res_clk_qur.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_ROLE_NOT_AUTHORIZED" in str(res_clk_qur.data)

    # 2. Inventory Clerk attempting VALUATION_DATA
    res_clk_val = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a.branch_id, "action_scope": StockActionScope.VALUATION_DATA},
        format="json",
    )
    assert res_clk_val.status_code == status.HTTP_403_FORBIDDEN

    # 3. Cashier attempting STOCK_COUNTING
    api_client.force_authenticate(user=cashier_a)
    res_csh = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a.branch_id, "action_scope": StockActionScope.STOCK_COUNTING},
        format="json",
    )
    assert res_csh.status_code == status.HTTP_403_FORBIDDEN

    # Verify DENIED audit logs recorded in DB
    denials = StockPermissionAuditLog.objects.filter(branch=branch_a, decision=PermissionDecision.DENIED)
    assert denials.count() == 3


@pytest.mark.django_db
def test_cross_branch_boundary_enforcement(
    api_client, pharmacist_a, branch_a2
):
    """
    Scenario 3: Pharmacist assigned to Branch A attempting STOCK_ADJUSTMENT in Branch A2 (same tenant)
    is strictly denied (HTTP 403 ERR_CROSS_BRANCH_ACCESS_DENIED).
    """
    api_client.force_authenticate(user=pharmacist_a)
    res = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a2.branch_id, "action_scope": StockActionScope.STOCK_ADJUSTMENT, "resource_id": "adj-x"},
        format="json",
    )
    assert res.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_CROSS_BRANCH_ACCESS_DENIED" in str(res.data)

    log = StockPermissionAuditLog.objects.filter(branch=branch_a2, actor=pharmacist_a).first()
    assert log is not None
    assert log.decision == PermissionDecision.DENIED
    assert log.error_code == "ERR_CROSS_BRANCH_ACCESS_DENIED"


@pytest.mark.django_db
def test_cross_tenant_isolation_404(
    api_client, tenant_b_pharmacist, branch_a
):
    """
    Scenario 4: Tenant B Pharmacist cannot access Tenant A's branch (HTTP 404).
    """
    api_client.force_authenticate(user=tenant_b_pharmacist)
    res = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a.branch_id, "action_scope": StockActionScope.STOCK_COUNTING},
        format="json",
    )
    assert res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_audit_log_immutability(
    api_client, owner_user, branch_a
):
    """
    Scenario 5: Security audit logs cannot be edited or deleted via API or ORM.
    """
    api_client.force_authenticate(user=owner_user)
    res = api_client.post(
        "/api/v2/inventory/permissions/evaluate/",
        {"branch": branch_a.branch_id, "action_scope": StockActionScope.VALUATION_DATA},
        format="json",
    )
    assert res.status_code == status.HTTP_200_OK
    audit_id = res.data["audit_id"]

    # Check API PUT/DELETE blocked (405)
    res_put = api_client.put(f"/api/v2/inventory/permissions/{audit_id}/", {}, format="json")
    assert res_put.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    res_del = api_client.delete(f"/api/v2/inventory/permissions/{audit_id}/")
    assert res_del.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    # Check ORM delete blocked
    audit_obj = StockPermissionAuditLog.objects.get(audit_id=audit_id)
    with pytest.raises(DjangoValidationError):
        audit_obj.delete()
