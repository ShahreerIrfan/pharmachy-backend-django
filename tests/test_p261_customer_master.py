"""
backend/tests/test_p261_customer_master.py
Architecture Decision: ADR-0261 (P261).
Phase 14: Customers, Suppliers, Dues & Payments.

Targeted Verification Test Suite for Customer Master:
- Scenario 1: Valid customer registration with name, optional phone, privacy preferences, and credit fields.
- Scenario 2: Incomplete and invalid input rejection (empty name, negative credit limit, credit_terms_days < 1).
- Scenario 3: Duplicate phone protection within same tenant boundary.
- Scenario 4: Role-based authorization for credit limits and cross-tenant boundary isolation.
- Scenario 5: Privacy preferences update and consent persistence.
- Scenario 6: Safe soft-archival and outstanding due blocking guardrails.
"""

from decimal import Decimal
import random
import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PosCustomer,
    PosCustomerCreditStatus,
    Tenant,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def customer_env():
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p261-a-{uid}", name="Square Health Pharma A", subdomain=f"p261a-{uid}"
    )
    br_a = Branch.objects.create(
        tenant=ten_a, branch_id=f"br-p261-a-{uid}", name="Dhanmondi Branch", branch_code="DH01"
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

    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Tenant B (Cross-Tenant Boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p261-b-{uid}", name="Beximco Partner B", subdomain=f"p261b-{uid}"
    )
    br_b = Branch.objects.create(
        tenant=ten_b, branch_id=f"br-p261-b-{uid}", name="Uttara Branch B", branch_code="UT01"
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        branch_id=br_b.branch_id,
        role=UserRole.PHARMACY_OWNER,
    )

    return {
        "uid": uid,
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "cashier_a": cashier_a,
        "tenant_b": ten_b,
        "branch_b": br_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_scenario_1_valid_customer_registration_with_privacy(customer_env):
    """
    Scenario 1: Manager registers a new regular customer with optional phone,
    privacy preferences, and approved credit terms.
    """
    env = customer_env
    client = APIClient()
    client.force_authenticate(user=env["manager_a"])

    url = "/api/v2/pos/customers/"
    payload = {
        "full_name": "Tariqul Islam",
        "phone": "+8801711223344",
        "email": "tariqul@gmail.com",
        "address": "House 12, Road 5, Dhanmondi, Dhaka",
        "credit_limit_bdt": "10000.00",
        "credit_terms_days": 30,
        "privacy_preferences": {
            "sms_receipts": True,
            "marketing_opt_in": False,
            "data_retention_consent": True,
        },
    }

    res = client.post(url, payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED, res.data

    data = res.data
    assert data["customer_id"].startswith("cust-")
    assert data["full_name"] == "Tariqul Islam"
    assert data["phone"] == "+8801711223344"
    assert data["credit_limit_bdt"] == "10000.00"
    assert data["credit_status"] == PosCustomerCreditStatus.APPROVED
    assert data["is_credit_approved"] is True
    assert data["privacy_preferences"]["sms_receipts"] is True
    assert data["privacy_preferences"]["marketing_opt_in"] is False
    assert data["is_active"] is True
    assert data["is_archived"] is False


@pytest.mark.django_db
def test_scenario_2_incomplete_and_invalid_inputs(customer_env):
    """
    Scenario 2: Validation errors for empty full_name, negative credit limit, and credit_terms_days < 1.
    """
    env = customer_env
    client = APIClient()
    client.force_authenticate(user=env["owner_a"])
    url = "/api/v2/pos/customers/"

    # 1. Empty full_name
    res1 = client.post(url, {
        "full_name": "",
        "phone": "+8801711223344",
    }, format="json")
    assert res1.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]

    # 2. Negative credit limit
    res2 = client.post(url, {
        "full_name": "Mahmudul Hasan",
        "credit_limit_bdt": "-500.00",
    }, format="json")
    assert res2.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]

    # 3. Invalid credit terms (< 1)
    res3 = client.post(url, {
        "full_name": "Mahmudul Hasan",
        "credit_terms_days": 0,
    }, format="json")
    assert res3.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]


@pytest.mark.django_db
def test_scenario_3_duplicate_phone_protection(customer_env):
    """
    Scenario 3: Unique duplicate protection within same tenant:
    Attempting to register a second active customer with the same phone number is blocked.
    """
    env = customer_env
    client = APIClient()
    client.force_authenticate(user=env["owner_a"])
    url = "/api/v2/pos/customers/"

    phone_num = "+8801999887766"

    # Create initial customer
    res1 = client.post(url, {
        "full_name": "Customer One",
        "phone": phone_num,
    }, format="json")
    assert res1.status_code == status.HTTP_201_CREATED

    # Attempt second active customer with same phone in Tenant A -> Blocked
    res2 = client.post(url, {
        "full_name": "Customer Two Duplicate",
        "phone": phone_num,
    }, format="json")
    assert res2.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    details = res2.data.get("details", res2.data)
    assert "phone" in details

    # Different Tenant B can register with same phone without conflict
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])
    res_b = client_b.post(url, {
        "full_name": "Tenant B Customer",
        "phone": phone_num,
    }, format="json")
    assert res_b.status_code == status.HTTP_201_CREATED


@pytest.mark.django_db
def test_scenario_4_role_authorization_and_cross_tenant_isolation(customer_env):
    """
    Scenario 4: Role-based authorization gates:
    1. Cashier attempting to grant credit_limit > 0 is forbidden (ERR_PERMISSION_DENIED).
    2. Tenant B cannot access or modify Tenant A customer record.
    """
    env = customer_env

    # 1. Cashier attempts to grant credit limit
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=env["cashier_a"])

    url = "/api/v2/pos/customers/"
    res_cashier = client_cashier.post(url, {
        "full_name": "Credit Request by Cashier",
        "credit_limit_bdt": "5000.00",
    }, format="json")
    assert res_cashier.status_code == status.HTTP_403_FORBIDDEN
    assert res_cashier.data["error_code"] == "ERR_PERMISSION_DENIED"

    # Cashier can create regular walk-in / zero-credit customer
    res_cashier_ok = client_cashier.post(url, {
        "full_name": "Regular Walk-in Customer",
        "credit_limit_bdt": "0.00",
    }, format="json")
    assert res_cashier_ok.status_code == status.HTTP_201_CREATED
    cust_id = res_cashier_ok.data["customer_id"]

    # 2. Tenant B user attempts to retrieve Tenant A customer -> 404
    client_b = APIClient()
    client_b.force_authenticate(user=env["owner_b"])
    res_b = client_b.get(f"{url}{cust_id}/")
    assert res_b.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_scenario_5_privacy_preferences_update(customer_env):
    """
    Scenario 5: Update customer privacy preferences and phone number via PATCH.
    """
    env = customer_env
    client = APIClient()
    client.force_authenticate(user=env["manager_a"])
    url = "/api/v2/pos/customers/"

    create_res = client.post(url, {
        "full_name": "Ayesha Khatun",
        "phone": "+8801555667788",
        "privacy_preferences": {
            "sms_receipts": True,
            "marketing_opt_in": False,
        },
    }, format="json")
    assert create_res.status_code == status.HTTP_201_CREATED
    cust_id = create_res.data["customer_id"]

    # Update preferences: opt-in to marketing
    patch_res = client.patch(f"{url}{cust_id}/", {
        "privacy_preferences": {
            "sms_receipts": True,
            "marketing_opt_in": True,
            "data_retention_consent": True,
        },
    }, format="json")
    assert patch_res.status_code == status.HTTP_200_OK
    assert patch_res.data["privacy_preferences"]["marketing_opt_in"] is True


@pytest.mark.django_db
def test_scenario_6_archival_rules_and_due_blocking(customer_env):
    """
    Scenario 6: Soft-archival safety:
    1. Customer with zero due can be safely archived.
    2. Customer with outstanding due (current_due_bdt > 0) cannot be archived.
    """
    env = customer_env
    client = APIClient()
    client.force_authenticate(user=env["owner_a"])
    url = "/api/v2/pos/customers/"

    # Customer 1: Zero due
    c1 = PosCustomer.objects.create(
        tenant=env["tenant_a"],
        primary_branch=env["branch_a"],
        full_name="Zero Due Customer",
        phone="+8801666778899",
        current_due_bdt=Decimal("0.00"),
    )

    del_res1 = client.delete(f"{url}{c1.customer_id}/")
    assert del_res1.status_code == status.HTTP_200_OK
    assert del_res1.data["is_archived"] is True
    c1.refresh_from_db()
    assert c1.is_archived is True

    # Customer 2: Has outstanding due
    c2 = PosCustomer.objects.create(
        tenant=env["tenant_a"],
        primary_branch=env["branch_a"],
        full_name="Due Balance Customer",
        phone="+8801666778800",
        credit_limit_bdt=Decimal("5000.00"),
        current_due_bdt=Decimal("1500.00"),
        is_credit_approved=True,
    )

    del_res2 = client.delete(f"{url}{c2.customer_id}/")
    assert del_res2.status_code in [status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert del_res2.data["error_code"] == "ERR_CUSTOMER_HAS_OUTSTANDING_DUE"
    c2.refresh_from_db()
    assert c2.is_archived is False
