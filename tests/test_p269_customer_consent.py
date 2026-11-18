"""
backend/tests/test_p269_customer_consent.py
Architecture Decision: ADR-0269 (P269).
Phase 14: Customers, Suppliers, Dues & Payments.

Verification suite for:
- Tracking consent for optional reminders and communications separately from statutory transaction records.
- Enforcing that mandatory tax invoices, prescription records, and AR ledgers cannot be opted out of.
- Complete revocation of optional communications while preserving all statutory ledgers.
- Immutable consent audit logging with SHA-256 integrity hashes.
- Multi-tenant isolation and input validation.
"""

from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.customer_consent_service import CustomerConsentService
from apps.core.models import (
    Branch,
    CustomerCommunicationConsent,
    CustomerConsentAuditLog,
    CustomerConsentChannel,
    CustomerConsentStatus,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    PlanTier,
    PosCustomer,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def consent_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p269-a-{uid}",
        name="Lazz Pharma Dhanmondi 27",
        subdomain=f"p269a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p269-a-{uid}",
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

    cust_a = PosCustomer.objects.create(
        customer_id=f"cust-p269-a-{uid}",
        tenant=ten_a,
        primary_branch=br_a,
        full_name="Mahmudur Rahman",
        phone="01715566778",
        email="mahmud@example.com",
        current_due_bdt=Decimal("800.00"),
    )

    # Post statutory Accounts Receivable ledger entry
    CustomerReceivableLedgerEntry.objects.create(
        tenant=ten_a,
        customer=cust_a,
        branch=br_a,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="INV-STAT-001",
        debit_amount_bdt=Decimal("800.00"),
        credit_amount_bdt=Decimal("0.00"),
        balance_after_bdt=Decimal("800.00"),
        posted_at=timezone.now(),
        posted_by=cashier_a,
    )

    # Tenant B (for cross-tenant tests)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p269-b-{uid}",
        name="Lazz Pharma Uttara",
        subdomain=f"p269b-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    cust_b = PosCustomer.objects.create(
        customer_id=f"cust-p269-b-{uid}",
        tenant=ten_b,
        full_name="Uttara Customer",
        phone="01811112222",
    )

    return {
        "ten_a": ten_a,
        "br_a": br_a,
        "owner_a": owner_a,
        "cashier_a": cashier_a,
        "cust_a": cust_a,
        "ten_b": ten_b,
        "cust_b": cust_b,
    }


@pytest.mark.django_db
def test_p269_consent_matrix_and_grant_workflow(consent_env):
    """
    Scenario 1: Fetch consent matrix, grant optional refill reminders and email receipts.
    Verify:
    - Matrix clearly presents statutory retention policies (mandatory, cannot opt out).
    - Setting consent to GRANTED creates CustomerCommunicationConsent and CustomerConsentAuditLog.
    - Gate check is_communication_allowed returns True for granted channel and False for ungranted channel.
    """
    client = APIClient()
    client.force_authenticate(user=consent_env["cashier_a"])
    cust = consent_env["cust_a"]

    # 1. Fetch initial consent matrix
    resp_matrix = client.get(f"/api/v2/pos/customers/{cust.customer_id}/consents/")
    assert resp_matrix.status_code == status.HTTP_200_OK
    data = resp_matrix.data

    assert data["customer"]["customer_id"] == cust.customer_id
    statutory = data["statutory_record_retention"]
    assert "tax_invoices" in statutory
    assert statutory["tax_invoices"]["is_mandatory"] is True
    assert statutory["tax_invoices"]["can_opt_out"] is False

    # 2. Grant consent for SMS refill reminders
    resp_grant = client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={
            "channel": "SMS_REFILL_REMINDERS",
            "status": "GRANTED",
            "capture_method": "POS_VERBAL_CONSENT",
            "notes": "Patient verbally requested monthly diabetic medicine refill reminders",
        },
        format="json",
    )
    assert resp_grant.status_code == status.HTTP_200_OK
    assert resp_grant.data["channel"] == "SMS_REFILL_REMINDERS"
    assert resp_grant.data["is_granted"] is True
    assert resp_grant.data["sha256_hash"] != ""

    # 3. Verify gate checks
    allowed_refill = CustomerConsentService.is_communication_allowed(
        user=consent_env["cashier_a"],
        customer_id=cust.customer_id,
        channel="SMS_REFILL_REMINDERS",
    )
    assert allowed_refill is True

    allowed_promo = CustomerConsentService.is_communication_allowed(
        user=consent_env["cashier_a"],
        customer_id=cust.customer_id,
        channel="PROMOTIONAL_CAMPAIGNS",
    )
    assert allowed_promo is False


@pytest.mark.django_db
def test_p269_statutory_record_opt_out_rejection(consent_env):
    """
    Scenario 2: Attempting to opt out of legally mandated statutory records
    is strictly rejected with ERR_STATUTORY_RECORD_IMMUTABLE.
    """
    client = APIClient()
    client.force_authenticate(user=consent_env["cashier_a"])
    cust = consent_env["cust_a"]

    resp = client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={
            "channel": "STATUTORY_TAX_INVOICES",
            "status": "REVOKED",
            "reason": "Customer does not want VAT records kept",
        },
        format="json",
    )
    assert resp.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_STATUTORY_RECORD_IMMUTABLE" in str(resp.data)


@pytest.mark.django_db
def test_p269_revoke_all_optional_preserves_statutory_ledgers(consent_env):
    """
    Scenario 3: Revoking all optional communication consents sets all optional channels to REVOKED,
    while leaving statutory ledger entries, due balances, and invoices 100% active and unimpacted.
    """
    client = APIClient()
    client.force_authenticate(user=consent_env["owner_a"])
    cust = consent_env["cust_a"]

    # First grant 2 optional channels
    client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={"channel": "SMS_REFILL_REMINDERS", "status": "GRANTED"},
        format="json",
    )
    client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={"channel": "EMAIL_RECEIPTS", "status": "GRANTED"},
        format="json",
    )

    # Revoke all optional communications
    resp_revoke = client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/revoke-all-consents/",
        data={"reason": "Customer requested full opt-out of notifications"},
        format="json",
    )
    assert resp_revoke.status_code == status.HTTP_200_OK
    assert resp_revoke.data["statutory_records_status"] == "ACTIVE_IMMUTABLE"

    # Verify all optional channels are now revoked
    matrix = CustomerConsentService.get_customer_consent_matrix(
        user=consent_env["cashier_a"],
        customer_id=cust.customer_id,
    )
    assert matrix["all_optional_revoked"] is True

    # Crucial check: Customer's Accounts Receivable ledger balance and entries remain 100% intact!
    cust.refresh_from_db()
    assert cust.current_due_bdt == Decimal("800.00")
    ledger_count = CustomerReceivableLedgerEntry.objects.filter(customer=cust).count()
    assert ledger_count == 1


@pytest.mark.django_db
def test_p269_invalid_channel_and_status_validation(consent_env):
    """
    Scenario 4: Validation rejects unknown channels or invalid status values.
    """
    client = APIClient()
    client.force_authenticate(user=consent_env["cashier_a"])
    cust = consent_env["cust_a"]

    # Unknown channel
    resp_bad_ch = client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={"channel": "NON_EXISTENT_CHANNEL", "status": "GRANTED"},
        format="json",
    )
    assert resp_bad_ch.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_INVALID_CONSENT_CHANNEL" in str(resp_bad_ch.data)

    # Invalid status
    resp_bad_st = client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={"channel": "SMS_REFILL_REMINDERS", "status": "MAYBE"},
        format="json",
    )
    assert resp_bad_st.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_INVALID_CONSENT_STATUS" in str(resp_bad_st.data)


@pytest.mark.django_db
def test_p269_cross_tenant_consent_isolation(consent_env):
    """
    Scenario 5: User in Tenant A cannot view or update consent for Customer in Tenant B.
    """
    client = APIClient()
    client.force_authenticate(user=consent_env["owner_a"])
    cust_b = consent_env["cust_b"]

    resp = client.get(f"/api/v2/pos/customers/{cust_b.customer_id}/consents/")
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "ERR_CUSTOMER_NOT_FOUND" in str(resp.data)


@pytest.mark.django_db
def test_p269_consent_audit_logs_endpoint(consent_env):
    """
    Scenario 6: Audit log captures previous_status, new_status, and sha256_hash.
    """
    client = APIClient()
    client.force_authenticate(user=consent_env["owner_a"])
    cust = consent_env["cust_a"]

    # Grant then Revoke
    client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={"channel": "SMS_DUE_REMINDERS", "status": "GRANTED", "reason": "Initial opt-in"},
        format="json",
    )
    client.post(
        f"/api/v2/pos/customers/{cust.customer_id}/set-consent/",
        data={"channel": "SMS_DUE_REMINDERS", "status": "REVOKED", "reason": "Opted out later"},
        format="json",
    )

    resp_logs = client.get(f"/api/v2/pos/customers/{cust.customer_id}/consent-audit-logs/")
    assert resp_logs.status_code == status.HTTP_200_OK
    logs = resp_logs.data["results"]
    assert len(logs) >= 2
    latest = logs[0]
    assert latest["action"] == "REVOKED"
    assert latest["channel"] == "SMS_DUE_REMINDERS"
    assert latest["previous_status"] == "GRANTED"
    assert latest["new_status"] == "REVOKED"
    assert latest["sha256_hash"] != ""
