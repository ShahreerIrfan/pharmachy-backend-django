"""
backend/tests/test_p270_customer_loyalty.py
Architecture Decision: ADR-0270 (P270).
Phase 14: Customers, Suppliers, Dues & Payments.

Verification suite for:
- Optional customer loyalty points, tiered offers, and balance tracking.
- Regulatory safety exclusions: Prescription medicines and antibiotics excluded from points accrual and discounts.
- Privacy consent enforcement: Loyalty program respects customer promotional opt-out.
- Subscription plan feature gate: Gated behind CUSTOMER_LOYALTY entitlement.
- Double-entry points ledger with SHA-256 integrity hashes.
- Multi-tenant isolation and input boundary validations.
"""

from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.customer_loyalty_service import CustomerLoyaltyService
from apps.core.models import (
    Branch,
    CustomerLoyaltyEntryType,
    CustomerLoyaltyLedgerEntry,
    CustomerLoyaltyProgramConfig,
    CustomerLoyaltyTier,
    PlanTier,
    PosCustomer,
    PosSaleOrder,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def loyalty_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A (STANDARD Tier -> Has CUSTOMER_LOYALTY entitlement)
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p270-a-{uid}",
        name="Lazz Pharma Dhanmondi Loyalty",
        subdomain=f"p270a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p270-a-{uid}",
        name="Dhanmondi Branch",
        branch_code=f"D{uid[:3].upper()}",
    )

    owner_a = PharmacyUser.objects.create_user(
        email=f"owner_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        role=UserRole.PHARMACY_OWNER,
    )
    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Enrolled Customer with 50 existing loyalty points
    cust_a = PosCustomer.objects.create(
        customer_id=f"cust-p270-a-{uid}",
        tenant=ten_a,
        primary_branch=br_a,
        full_name="Tanvir Ahmed",
        phone="01712001122",
        loyalty_tier=CustomerLoyaltyTier.BRONZE,
        loyalty_points_balance=50,
        lifetime_loyalty_points_earned=50,
        lifetime_loyalty_spend_bdt=Decimal("5000.00"),
        is_loyalty_enrolled=True,
        privacy_preferences={"promotional_campaigns": "GRANTED"},
    )

    # Customer with revoked promotional/loyalty consent
    cust_revoked = PosCustomer.objects.create(
        customer_id=f"cust-p270-rev-{uid}",
        tenant=ten_a,
        primary_branch=br_a,
        full_name="Farhana Yasmin",
        phone="01713002233",
        loyalty_points_balance=30,
        is_loyalty_enrolled=True,
        privacy_preferences={"promotional_campaigns": "REVOKED"},
    )

    # Tenant B (BASIC Tier -> No CUSTOMER_LOYALTY entitlement)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p270-b-{uid}",
        name="Small Pharmacy Basic",
        subdomain=f"p270b-{uid}",
        plan_tier=PlanTier.BASIC,
        status=TenantLifecycleStatus.ACTIVE,
    )
    cashier_b = PharmacyUser.objects.create_user(
        email=f"cashier_b_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_b.tenant_id,
        role=UserRole.CASHIER,
    )
    cust_b = PosCustomer.objects.create(
        customer_id=f"cust-p270-b-{uid}",
        tenant=ten_b,
        full_name="Basic Tier Customer",
        phone="01714003344",
    )

    return {
        "ten_a": ten_a,
        "br_a": br_a,
        "owner_a": owner_a,
        "cashier_a": cashier_a,
        "cust_a": cust_a,
        "cust_revoked": cust_revoked,
        "ten_b": ten_b,
        "cashier_b": cashier_b,
        "cust_b": cust_b,
    }


@pytest.mark.django_db
def test_p270_loyalty_profile_and_safety_exclusion_spend_calculation(loyalty_env):
    """
    Scenario 1: Fetch loyalty profile and verify safety exclusion spend calculation.
    - Prescription and antibiotic items are strictly excluded from points accrual.
    - 400 BDT OTC + 600 BDT Antibiotic -> eligible spend is 400 BDT, points earnable is 4.
    """
    client = APIClient()
    client.force_authenticate(user=loyalty_env["cashier_a"])
    cust = loyalty_env["cust_a"]

    # 1. Fetch profile
    resp = client.get(f"/api/v2/pos/customers/{cust.customer_id}/loyalty/")
    assert resp.status_code == status.HTTP_200_OK
    data = resp.data
    assert data["customer_id"] == cust.customer_id
    assert data["points_balance"] == 50
    assert data["loyalty_tier"] == CustomerLoyaltyTier.BRONZE
    assert data["has_promotional_consent"] is True
    assert data["program_config"]["safety_exclusions"]["prescription_drugs_excluded"] is True

    # 2. Test safety exclusion spend breakdown directly in service
    config = CustomerLoyaltyService.get_or_create_config(loyalty_env["ten_a"])
    cart_items = [
        {"product_name": "Paracetamol 500mg", "line_total_bdt": "400.00", "is_prescription_required": False},
        {"product_name": "Cefixime 200mg", "line_total_bdt": "600.00", "is_prescription_required": True, "is_antibiotic": True},
    ]
    breakdown = CustomerLoyaltyService.calculate_spend_breakdown(config=config, cart_items=cart_items)
    assert breakdown["eligible_spend_bdt"] == Decimal("400.00")
    assert breakdown["excluded_spend_bdt"] == Decimal("600.00")
    assert breakdown["points_earnable"] == 4  # 400 // 100 BDT per point


@pytest.mark.django_db
def test_p270_points_redemption_and_ledger_entry(loyalty_env):
    """
    Scenario 2: Customer with 50 points redeems 20 points (20 BDT discount) on eligible cart items.
    Verify:
    - Customer points balance drops to 30.
    - CustomerLoyaltyLedgerEntry records POINTS_REDEEMED with SHA-256 hash.
    - Loyalty ledger history endpoint returns the audit entry.
    """
    client = APIClient()
    client.force_authenticate(user=loyalty_env["cashier_a"])
    cust = loyalty_env["cust_a"]

    payload = {
        "points_to_redeem": 20,
        "cart_items": [
            {"product_name": "Vitamin C chewable", "line_total_bdt": "500.00", "is_prescription_required": False},
        ],
    }
    resp = client.post(f"/api/v2/pos/customers/{cust.customer_id}/loyalty-redeem/", data=payload, format="json")
    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["points_redeemed"] == 20
    assert resp.data["discount_amount_bdt"] == "20.00"
    assert resp.data["new_points_balance"] == 30

    cust.refresh_from_db()
    assert cust.loyalty_points_balance == 30

    # Verify ledger entry
    entry = CustomerLoyaltyLedgerEntry.objects.filter(
        customer=cust,
        entry_type=CustomerLoyaltyEntryType.POINTS_REDEEMED,
    ).latest("created_at")
    assert entry.points_debit == 20
    assert entry.balance_after == 30
    assert entry.monetary_equivalent_bdt == Decimal("20.00")
    assert entry.sha256_hash != ""

    # Verify ledger history endpoint
    resp_hist = client.get(f"/api/v2/pos/customers/{cust.customer_id}/loyalty-ledger/")
    assert resp_hist.status_code == status.HTTP_200_OK
    assert len(resp_hist.data["results"]) >= 1


@pytest.mark.django_db
def test_p270_regulatory_exclusion_rejects_rx_only_redemption(loyalty_env):
    """
    Scenario 3: Attempting to redeem points on a cart containing only restricted prescription/antibiotic items
    is strictly rejected with ERR_NO_ELIGIBLE_ITEMS.
    """
    client = APIClient()
    client.force_authenticate(user=loyalty_env["cashier_a"])
    cust = loyalty_env["cust_a"]

    payload = {
        "points_to_redeem": 20,
        "cart_items": [
            {"product_name": "Meropenem IV Injection", "line_total_bdt": "1500.00", "is_prescription_required": True, "is_antibiotic": True},
        ],
    }
    resp = client.post(f"/api/v2/pos/customers/{cust.customer_id}/loyalty-redeem/", data=payload, format="json")
    assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_NO_ELIGIBLE_ITEMS" in str(resp.data)


@pytest.mark.django_db
def test_p270_privacy_opt_out_blocks_redemption(loyalty_env):
    """
    Scenario 4: Customer who revoked promotional/loyalty consent cannot redeem points (ERR_LOYALTY_CONSENT_REVOKED).
    """
    client = APIClient()
    client.force_authenticate(user=loyalty_env["cashier_a"])
    cust_rev = loyalty_env["cust_revoked"]

    payload = {
        "points_to_redeem": 10,
        "cart_items": [
            {"product_name": "OTC Cough Syrup", "line_total_bdt": "200.00", "is_prescription_required": False},
        ],
    }
    resp = client.post(f"/api/v2/pos/customers/{cust_rev.customer_id}/loyalty-redeem/", data=payload, format="json")
    assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_LOYALTY_CONSENT_REVOKED" in str(resp.data)


@pytest.mark.django_db
def test_p270_subscription_plan_feature_lock(loyalty_env):
    """
    Scenario 5: Tenant on BASIC plan tier does not have CUSTOMER_LOYALTY entitlement
    and is denied with ERR_SUBSCRIPTION_FEATURE_LOCKED (HTTP 403).
    """
    client = APIClient()
    client.force_authenticate(user=loyalty_env["cashier_b"])
    cust_b = loyalty_env["cust_b"]

    resp = client.get(f"/api/v2/pos/customers/{cust_b.customer_id}/loyalty/")
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_SUBSCRIPTION_FEATURE_LOCKED" in str(resp.data)


@pytest.mark.django_db
def test_p270_redemption_validation_limits(loyalty_env):
    """
    Scenario 6: Validation rejects requests that exceed balance, below min points, or exceed max discount percentage cap.
    """
    client = APIClient()
    client.force_authenticate(user=loyalty_env["cashier_a"])
    cust = loyalty_env["cust_a"]

    # 1. Below min threshold (min is 10 points)
    resp_min = client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/loyalty-redeem/",
        data={"points_to_redeem": 5, "cart_items": [{"line_total_bdt": "500.00", "is_prescription_required": False}]},
        format="json",
    )
    assert resp_min.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_MIN_POINTS_THRESHOLD" in str(resp_min.data)

    # 2. Exceeds customer points balance (balance is 50, requests 100)
    resp_bal = client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/loyalty-redeem/",
        data={"points_to_redeem": 100, "cart_items": [{"line_total_bdt": "500.00", "is_prescription_required": False}]},
        format="json",
    )
    assert resp_bal.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_INSUFFICIENT_LOYALTY_POINTS" in str(resp_bal.data)
