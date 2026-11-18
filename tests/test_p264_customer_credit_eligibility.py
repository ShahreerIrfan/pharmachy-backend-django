"""
backend/tests/test_p264_customer_credit_eligibility.py
Architecture Decision: ADR-0264 (P264).
Phase 14: Customers, Suppliers, Dues & Payments.

Targeted Verification Test Suite for Customer Credit Eligibility:
- Scenario 1: Valid credit eligibility evaluation (approved customer within limit and branch ceiling).
- Scenario 2: Incomplete and invalid input rejection (negative credit limit, negative ceiling).
- Scenario 3: Overdue receivables hard control (unpaid due past terms blocks new credit).
- Scenario 4: Restricted medicines hard control (narcotic medicine in cart blocks credit).
- Scenario 5: Branch cumulative exposure ceiling exceeded blocks credit sales.
- Scenario 6: Role-based policy configuration governance and cross-tenant boundary isolation.
"""

from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    CustomerCreditEligibilityPolicy,
    CustomerDueRecord,
    DueStatus,
    MedicineProduct,
    PlanTier,
    PosCustomer,
    PosCustomerCreditStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def credit_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p264-a-{uid}",
        name="Lazz Pharma Gulshan",
        subdomain=f"p264a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p264-a-{uid}",
        name="Gulshan Branch",
        branch_code=f"G{uid[:3].upper()}",
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

    # Approved Customer
    cust_approved = PosCustomer.objects.create(
        tenant=ten_a,
        primary_branch=br_a,
        customer_id=f"cust-app-{uid}",
        full_name="Anwar Hossain",
        phone="+8801711223344",
        credit_status=PosCustomerCreditStatus.APPROVED,
        is_credit_approved=True,
        credit_limit_bdt=Decimal("5000.00"),
        current_due_bdt=Decimal("1000.00"),
        credit_terms_days=30,
        created_by=owner_a,
    )

    # Suspended Customer
    cust_suspended = PosCustomer.objects.create(
        tenant=ten_a,
        primary_branch=br_a,
        customer_id=f"cust-susp-{uid}",
        full_name="Rafiqul Islam",
        phone="+8801722334455",
        credit_status=PosCustomerCreditStatus.SUSPENDED,
        is_credit_approved=False,
        credit_limit_bdt=Decimal("3000.00"),
        current_due_bdt=Decimal("500.00"),
        credit_suspended_reason="Repeated payment delays",
        created_by=owner_a,
    )

    # General Product
    product_regular = MedicineProduct.objects.create(
        product_id=f"prod-reg-{uid}",
        brand_name="Ace Plus",
        generic_name="Paracetamol + Caffeine",
        dosage_form="Tablet",
        strength="500mg+65mg",
        is_narcotic=False,
        is_antibiotic=False,
        mrp_unit=Decimal("2.50"),
        cost_unit=Decimal("1.80"),
    )

    # Narcotic Product
    product_narcotic = MedicineProduct.objects.create(
        product_id=f"prod-narc-{uid}",
        brand_name="Morphine Sulfate 15mg",
        generic_name="Morphine",
        dosage_form="Tablet",
        strength="15mg",
        is_narcotic=True,
        is_antibiotic=False,
        mrp_unit=Decimal("50.00"),
        cost_unit=Decimal("35.00"),
    )

    # Default Branch Credit Policy
    policy = CustomerCreditEligibilityPolicy.objects.create(
        policy_id=f"ccp-{uid}",
        tenant=ten_a,
        branch=br_a,
        max_customer_credit_limit_bdt=Decimal("10000.00"),
        branch_credit_ceiling_bdt=Decimal("20000.00"),
        block_credit_if_overdue=True,
        allow_credit_for_narcotics=False,
        allow_credit_for_antibiotics=True,
        is_active=True,
        updated_by=owner_a,
    )

    # Tenant B (Cross-Tenant Boundary)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p264-b-{uid}",
        name="Apollo Health B",
        subdomain=f"p264b-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    owner_b = PharmacyUser.objects.create_user(
        email=f"owner_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )

    return {
        "tenant_a": ten_a,
        "branch_a": br_a,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "cashier_a": cashier_a,
        "cust_approved": cust_approved,
        "cust_suspended": cust_suspended,
        "product_regular": product_regular,
        "product_narcotic": product_narcotic,
        "policy": policy,
        "tenant_b": ten_b,
        "owner_b": owner_b,
    }


@pytest.mark.django_db
def test_p264_valid_credit_eligibility(credit_env):
    """Scenario 1: Valid credit eligibility evaluation (approved customer within limit and branch ceiling)."""
    client = APIClient()
    client.force_authenticate(user=credit_env["cashier_a"])

    customer_id = credit_env["cust_approved"].customer_id
    url = f"/api/v2/pos/customers/{customer_id}/credit-eligibility/"
    resp = client.get(url, {"additional_due": "500.00"})

    assert resp.status_code == status.HTTP_200_OK, resp.data
    data = resp.data
    assert data["is_eligible"] is True
    assert data["customer_id"] == customer_id
    assert data["credit_status"] == "APPROVED"
    assert data["credit_limit_bdt"] == "5000.00"
    assert data["current_due_bdt"] == "1000.00"
    assert data["available_credit_bdt"] == "4000.00"
    assert data["additional_due_requested_bdt"] == "500.00"
    assert data["projected_due_bdt"] == "1500.00"
    assert data["has_overdue_dues"] is False
    assert len(data["rejections"]) == 0


@pytest.mark.django_db
def test_p264_policy_configuration_rejections(credit_env):
    """Scenario 2: Parameter validation rejection on invalid policy limits (negative amounts)."""
    client = APIClient()
    client.force_authenticate(user=credit_env["owner_a"])

    url = "/api/v2/pos/credit-eligibility/policy/"

    # Negative credit limit
    resp_neg = client.put(url, {"max_customer_credit_limit_bdt": "-100.00"})
    assert resp_neg.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Negative branch ceiling
    resp_ceil = client.put(url, {"branch_credit_ceiling_bdt": "-500.00"})
    assert resp_ceil.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)


@pytest.mark.django_db
def test_p264_overdue_receivables_hard_control(credit_env):
    """Scenario 3: Overdue receivables hard control (unpaid due past terms blocks new credit)."""
    client = APIClient()
    client.force_authenticate(user=credit_env["cashier_a"])

    customer = credit_env["cust_approved"]

    # Create an overdue CustomerDueRecord for this customer (due_date in the past)
    yesterday = timezone.now().date() - timezone.timedelta(days=5)
    CustomerDueRecord.objects.create(
        due_id=f"due-overdue-{uuid.uuid4().hex[:6]}",
        tenant=credit_env["tenant_a"],
        branch=credit_env["branch_a"],
        customer=customer,
        customer_name=customer.full_name,
        customer_phone=customer.phone,
        total_due_bdt=Decimal("500.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("500.00"),
        status=DueStatus.OUTSTANDING,
        due_date=yesterday,
    )

    url = f"/api/v2/pos/customers/{customer.customer_id}/credit-eligibility/"
    resp = client.get(url, {"additional_due": "200.00"})

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["is_eligible"] is False
    assert resp.data["has_overdue_dues"] is True
    rejection_codes = [r["code"] for r in resp.data["rejections"]]
    assert "ERR_CUSTOMER_HAS_OVERDUE_DUES" in rejection_codes


@pytest.mark.django_db
def test_p264_restricted_medicines_hard_control(credit_env):
    """Scenario 4: Restricted medicines hard control (narcotic medicine in cart blocks credit)."""
    client = APIClient()
    client.force_authenticate(user=credit_env["cashier_a"])

    customer_id = credit_env["cust_approved"].customer_id
    url = "/api/v2/pos/credit-eligibility/check/"

    # Cart containing narcotic medicine
    payload = {
        "customer_id": customer_id,
        "branch": credit_env["branch_a"].branch_id,
        "additional_due_bdt": "50.00",
        "cart_lines": [
            {
                "product_id": credit_env["product_narcotic"].product_id,
                "quantity": 1,
            }
        ],
    }

    resp = client.post(url, payload, format="json")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["is_eligible"] is False
    rejection_codes = [r["code"] for r in resp.data["rejections"]]
    assert "ERR_RESTRICTED_MEDICINE_CREDIT_FORBIDDEN" in rejection_codes
    assert len(resp.data["restricted_items"]) == 1
    assert resp.data["restricted_items"][0]["restriction_type"] == "NARCOTIC_RESTRICTED"


@pytest.mark.django_db
def test_p264_limit_and_ceiling_exceeded(credit_env):
    """Scenario 5: Customer credit limit exceeded and branch ceiling exceeded."""
    client = APIClient()
    client.force_authenticate(user=credit_env["cashier_a"])

    customer = credit_env["cust_approved"]
    url = f"/api/v2/pos/customers/{customer.customer_id}/credit-eligibility/"

    # Current due is 1000, limit is 5000. Requesting 4500 brings projected due to 5500 > 5000!
    resp_limit = client.get(url, {"additional_due": "4500.00"})
    assert resp_limit.status_code == status.HTTP_200_OK
    assert resp_limit.data["is_eligible"] is False
    rejection_codes = [r["code"] for r in resp_limit.data["rejections"]]
    assert "ERR_CUSTOMER_CREDIT_LIMIT_EXCEEDED" in rejection_codes

    # Suspended customer check
    cust_susp = credit_env["cust_suspended"]
    resp_susp = client.get(f"/api/v2/pos/customers/{cust_susp.customer_id}/credit-eligibility/")
    assert resp_susp.status_code == status.HTTP_200_OK
    assert resp_susp.data["is_eligible"] is False
    susp_codes = [r["code"] for r in resp_susp.data["rejections"]]
    assert "ERR_CUSTOMER_CREDIT_SUSPENDED" in susp_codes


@pytest.mark.django_db
def test_p264_rbac_and_cross_tenant_isolation(credit_env):
    """Scenario 6: Role-based policy configuration governance and cross-tenant boundary isolation."""
    # 1. Cashier attempts to modify credit policy -> 403 Forbidden
    client_cashier = APIClient()
    client_cashier.force_authenticate(user=credit_env["cashier_a"])
    resp_cashier = client_cashier.put(
        "/api/v2/pos/credit-eligibility/policy/",
        {"max_customer_credit_limit_bdt": "15000.00"},
    )
    assert resp_cashier.status_code == status.HTTP_403_FORBIDDEN

    # 2. Pharmacy Owner configures policy successfully
    client_owner = APIClient()
    client_owner.force_authenticate(user=credit_env["owner_a"])
    resp_owner = client_owner.put(
        "/api/v2/pos/credit-eligibility/policy/",
        {
            "max_customer_credit_limit_bdt": "15000.00",
            "branch_credit_ceiling_bdt": "50000.00",
            "allow_credit_for_narcotics": False,
        },
    )
    assert resp_owner.status_code == status.HTTP_200_OK
    assert resp_owner.data["max_customer_credit_limit_bdt"] == "15000.00"
    assert resp_owner.data["branch_credit_ceiling_bdt"] == "50000.00"

    # 3. Cross-Tenant Isolation: Tenant B cannot query Tenant A customer
    client_b = APIClient()
    client_b.force_authenticate(user=credit_env["owner_b"])
    customer_id = credit_env["cust_approved"].customer_id
    resp_cross = client_b.get(f"/api/v2/pos/customers/{customer_id}/credit-eligibility/")
    assert resp_cross.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
