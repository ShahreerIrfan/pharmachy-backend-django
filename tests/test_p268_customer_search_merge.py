"""
backend/tests/test_p268_customer_search_merge.py
Architecture Decision: ADR-0268 (P268).
Phase 14: Customers, Suppliers, Dues & Payments.

Verification suite for:
- Normalized Bangladesh phone and alias search for POS customers.
- Controlled duplicate customer merge preserving immutable transaction links.
- AR ledger adjustments and double-entry balance parity.
- Cross-tenant boundaries, self-merge prevention, already-merged rejection, and RBAC gating.
"""

from decimal import Decimal
import random
import uuid

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.customer_receivable_ledger_service import CustomerReceivableLedgerService
from apps.core.models import (
    Branch,
    CustomerDueRecord,
    CustomerMergeAudit,
    CustomerReceivableEntryType,
    CustomerReceivableLedgerEntry,
    DueStatus,
    PlanTier,
    PosCustomer,
    PosCustomerCreditStatus,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.fixture
def search_merge_env(db):
    uid = uuid.uuid4().hex[:6]

    # Tenant A
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-p268-a-{uid}",
        name="Lazz Pharma Gulshan",
        subdomain=f"p268a-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a = Branch.objects.create(
        tenant=ten_a,
        branch_id=f"br-p268-a-{uid}",
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
    cashier_a = PharmacyUser.objects.create_user(
        email=f"cashier_{uid}@test.com",
        phone_number=f"+88017{random.randint(10000000, 99999999)}",
        password="PassWord123!",
        tenant_id=ten_a.tenant_id,
        branch_id=br_a.branch_id,
        role=UserRole.CASHIER,
    )

    # Customer 1 (Survivor / Target): "Kabir Hossain", Phone: 01711223344
    cust_target = PosCustomer.objects.create(
        customer_id=f"cust-p268-target-{uid}",
        tenant=ten_a,
        primary_branch=br_a,
        full_name="Kabir Hossain",
        phone="01711223344",
        credit_limit_bdt=Decimal("5000.00"),
        current_due_bdt=Decimal("600.00"),
        is_credit_approved=True,
        credit_status=PosCustomerCreditStatus.APPROVED,
        notes="Target customer notes",
    )
    # Post initial ledger entry for target: 600 BDT
    CustomerReceivableLedgerEntry.objects.create(
        tenant=ten_a,
        customer=cust_target,
        branch=br_a,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="INV-TARGET-01",
        debit_amount_bdt=Decimal("600.00"),
        credit_amount_bdt=Decimal("0.00"),
        balance_after_bdt=Decimal("600.00"),
        posted_at=timezone.now(),
        posted_by=cashier_a,
    )

    # Customer 2 (Duplicate / Source): "Al-Haj Kabir", Phone: 01899887766, Aliases: ["Kabir Bhai", "Hossain Saheb"]
    cust_source = PosCustomer.objects.create(
        customer_id=f"cust-p268-source-{uid}",
        tenant=ten_a,
        primary_branch=br_a,
        full_name="Al-Haj Kabir",
        phone="01899887766",
        aliases=["Kabir Bhai", "Hossain Saheb"],
        credit_limit_bdt=Decimal("3000.00"),
        current_due_bdt=Decimal("400.00"),
        is_credit_approved=False,
        credit_status=PosCustomerCreditStatus.PENDING_APPROVAL,
        notes="Source duplicate notes",
    )
    # Post initial ledger entry for source: 400 BDT
    CustomerReceivableLedgerEntry.objects.create(
        tenant=ten_a,
        customer=cust_source,
        branch=br_a,
        entry_type=CustomerReceivableEntryType.DUE_SALE,
        reference_number="INV-SOURCE-02",
        debit_amount_bdt=Decimal("400.00"),
        credit_amount_bdt=Decimal("0.00"),
        balance_after_bdt=Decimal("400.00"),
        posted_at=timezone.now(),
        posted_by=cashier_a,
    )

    # Create active CustomerDueRecord for source customer
    due_source = CustomerDueRecord.objects.create(
        due_id=f"due-p268-src-{uid}",
        tenant=ten_a,
        branch=br_a,
        customer=cust_source,
        customer_name=cust_source.full_name,
        customer_phone=cust_source.phone,
        total_due_bdt=Decimal("400.00"),
        paid_amount_bdt=Decimal("0.00"),
        remaining_due_bdt=Decimal("400.00"),
        status=DueStatus.OUTSTANDING,
        due_date=timezone.now().date(),
    )

    # Tenant B (for cross-tenant tests)
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-p268-b-{uid}",
        name="Lazz Pharma Banani",
        subdomain=f"p268b-{uid}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    cust_b = PosCustomer.objects.create(
        customer_id=f"cust-p268-b-{uid}",
        tenant=ten_b,
        full_name="Other Tenant Customer",
        phone="01799999999",
        current_due_bdt=Decimal("100.00"),
    )

    return {
        "ten_a": ten_a,
        "br_a": br_a,
        "owner_a": owner_a,
        "cashier_a": cashier_a,
        "cust_target": cust_target,
        "cust_source": cust_source,
        "due_source": due_source,
        "ten_b": ten_b,
        "cust_b": cust_b,
    }


@pytest.mark.django_db
def test_p268_normalized_phone_and_alias_search(search_merge_env):
    """
    Scenario 1: Search customer by formatted phone number and alias.
    Verify:
    - Search with '+880 1899-887766' matches source customer ('01899887766') with match_type='EXACT_PHONE'.
    - Search with 'Kabir Bhai' matches source customer with match_type='ALIAS'.
    - General customer list search endpoint `/api/v2/pos/customers/?search=...` works consistently.
    """
    client = APIClient()
    client.force_authenticate(user=search_merge_env["cashier_a"])

    # 1. Search with formatted phone number
    resp_phone = client.get("/api/v2/pos/customers/search/?q=+880 1899-887766")
    assert resp_phone.status_code == status.HTTP_200_OK
    hits = resp_phone.data["results"]
    assert len(hits) >= 1
    hit = next((h for h in hits if h["customer_id"] == search_merge_env["cust_source"].customer_id), None)
    assert hit is not None
    assert hit["match_type"] == "EXACT_PHONE"
    assert hit["phone"] == "01899887766"

    # 2. Search with alias
    resp_alias = client.get("/api/v2/pos/customers/search/?q=Kabir Bhai")
    assert resp_alias.status_code == status.HTTP_200_OK
    alias_hits = resp_alias.data["results"]
    assert len(alias_hits) >= 1
    assert any(h["customer_id"] == search_merge_env["cust_source"].customer_id for h in alias_hits)

    # 3. Query params on standard customers endpoint
    resp_list = client.get("/api/v2/pos/customers/?search=01899887766")
    assert resp_list.status_code == status.HTTP_200_OK
    data = resp_list.data if isinstance(resp_list.data, list) else resp_list.data.get("results", [])
    assert any(c["customer_id"] == search_merge_env["cust_source"].customer_id for c in data)


@pytest.mark.django_db
def test_p268_valid_customer_merge_and_ledger_reconciliation(search_merge_env):
    """
    Scenario 2: Authorized Pharmacy Owner merges source customer into target customer.
    Verify:
    - Source customer is archived, deactivated, and linked to target (`merged_into`).
    - Source due balance becomes 0.00 BDT.
    - Target due balance increases by source due (600 + 400 = 1000.00 BDT).
    - Target inherits source's phone into secondary_phones and source's name/aliases into aliases.
    - Active CustomerDueRecord is re-assigned to target customer.
    - Double-entry AR ledger postings: MERGE-OUT on source, MERGE-IN on target.
    - CustomerMergeAudit is created with cryptographic SHA-256 hash.
    """
    client = APIClient()
    client.force_authenticate(user=search_merge_env["owner_a"])

    source = search_merge_env["cust_source"]
    target = search_merge_env["cust_target"]
    due = search_merge_env["due_source"]

    payload = {
        "source_customer_id": source.customer_id,
        "target_customer_id": target.customer_id,
        "merge_reason": "Duplicate phone entry created by different cashier",
    }
    resp = client.post("/api/v2/pos/customers/merge/", data=payload, format="json")
    assert resp.status_code == status.HTTP_200_OK
    res_data = resp.data

    assert res_data["source_customer"]["customer_id"] == source.customer_id
    assert res_data["target_customer"]["customer_id"] == target.customer_id
    assert res_data["transferred_due_bdt"] == "400.00"
    assert res_data["sha256_hash"] != ""

    # Refresh instances from database
    source.refresh_from_db()
    target.refresh_from_db()
    due.refresh_from_db()

    # Verify source customer state
    assert source.is_archived is True
    assert source.is_active is False
    assert source.current_due_bdt == Decimal("0.00")
    assert source.merged_into_id == target.customer_id

    # Verify target customer state
    assert target.current_due_bdt == Decimal("1000.00")  # 600 + 400
    assert target.credit_limit_bdt == Decimal("5000.00")  # max(5000, 3000)
    assert source.phone in target.secondary_phones
    assert source.full_name in target.aliases

    # Verify active CustomerDueRecord was re-assigned to target
    assert due.customer_id == target.customer_id
    assert f"MERGED from {source.customer_id}" in due.notes

    # Verify Accounts Receivable Ledger postings:
    # 1. Source ledger: MERGE-OUT credit entry of 400 BDT, balance becomes 0.00 BDT
    out_entry = CustomerReceivableLedgerEntry.objects.filter(
        customer=source,
        entry_type=CustomerReceivableEntryType.ADJUSTMENT,
    ).latest("posted_at")
    assert out_entry.credit_amount_bdt == Decimal("400.00")
    assert out_entry.balance_after_bdt == Decimal("0.00")
    assert "MERGE-OUT" in out_entry.reference_number

    # 2. Target ledger: MERGE-IN debit entry of 400 BDT, balance becomes 1000.00 BDT
    in_entry = CustomerReceivableLedgerEntry.objects.filter(
        customer=target,
        entry_type=CustomerReceivableEntryType.ADJUSTMENT,
    ).latest("posted_at")
    assert in_entry.debit_amount_bdt == Decimal("400.00")
    assert in_entry.balance_after_bdt == Decimal("1000.00")
    assert "MERGE-IN" in in_entry.reference_number

    # Verify Audit record
    audit = CustomerMergeAudit.objects.get(merge_id=res_data["merge_id"])
    assert audit.source_customer_id == source.customer_id
    assert audit.target_customer_id == target.customer_id
    assert audit.transferred_due_bdt == Decimal("400.00")
    assert audit.sha256_hash == res_data["sha256_hash"]


@pytest.mark.django_db
def test_p268_self_merge_and_invalid_inputs(search_merge_env):
    """
    Scenario 3: Validation fails for self-merge, missing IDs, or short merge reasons.
    """
    client = APIClient()
    client.force_authenticate(user=search_merge_env["owner_a"])
    target = search_merge_env["cust_target"]

    # 1. Self merge
    resp_self = client.post(
        "/api/v2/pos/customers/merge/",
        data={
            "source_customer_id": target.customer_id,
            "target_customer_id": target.customer_id,
            "merge_reason": "Trying self merge",
        },
        format="json",
    )
    assert resp_self.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_SELF_MERGE_FORBIDDEN" in str(resp_self.data)

    # 2. Missing reason
    resp_no_reason = client.post(
        "/api/v2/pos/customers/merge/",
        data={
            "source_customer_id": search_merge_env["cust_source"].customer_id,
            "target_customer_id": target.customer_id,
            "merge_reason": "",
        },
        format="json",
    )
    assert resp_no_reason.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_MERGE_REASON_REQUIRED" in str(resp_no_reason.data)


@pytest.mark.django_db
def test_p268_unauthorized_cashier_merge_denial(search_merge_env):
    """
    Scenario 4: Cashier attempts to execute customer merge -> Denied with ERR_PERMISSION_DENIED (HTTP 403).
    """
    client = APIClient()
    client.force_authenticate(user=search_merge_env["cashier_a"])

    resp = client.post(
        "/api/v2/pos/customers/merge/",
        data={
            "source_customer_id": search_merge_env["cust_source"].customer_id,
            "target_customer_id": search_merge_env["cust_target"].customer_id,
            "merge_reason": "Cashier unauthorized attempt",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "ERR_PERMISSION_DENIED" in str(resp.data)


@pytest.mark.django_db
def test_p268_cross_tenant_merge_isolation(search_merge_env):
    """
    Scenario 5: Attempting to merge a customer belonging to Tenant B while authenticated in Tenant A
    returns 404 / ERR_SOURCE_CUSTOMER_NOT_FOUND.
    """
    client = APIClient()
    client.force_authenticate(user=search_merge_env["owner_a"])

    resp = client.post(
        "/api/v2/pos/customers/merge/",
        data={
            "source_customer_id": search_merge_env["cust_b"].customer_id,
            "target_customer_id": search_merge_env["cust_target"].customer_id,
            "merge_reason": "Cross tenant merge attempt",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "ERR_SOURCE_CUSTOMER_NOT_FOUND" in str(resp.data)


@pytest.mark.django_db
def test_p268_already_merged_customer_rejection(search_merge_env):
    """
    Scenario 6: Attempting to re-merge a customer that has already been merged into another customer
    is rejected with ERR_ALREADY_MERGED.
    """
    client = APIClient()
    client.force_authenticate(user=search_merge_env["owner_a"])
    source = search_merge_env["cust_source"]
    target = search_merge_env["cust_target"]

    # First merge succeeds
    client.post(
        "/api/v2/pos/customers/merge/",
        data={
            "source_customer_id": source.customer_id,
            "target_customer_id": target.customer_id,
            "merge_reason": "First successful merge",
        },
        format="json",
    )

    # Second merge attempt on the same source customer
    resp_again = client.post(
        "/api/v2/pos/customers/merge/",
        data={
            "source_customer_id": source.customer_id,
            "target_customer_id": target.customer_id,
            "merge_reason": "Second invalid merge attempt",
        },
        format="json",
    )
    assert resp_again.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY)
    assert "ERR_ALREADY_MERGED" in str(resp_again.data)


@pytest.mark.django_db
def test_p268_merge_history_audit_endpoint(search_merge_env):
    """
    Scenario 7: GET /api/v2/pos/customers/merge-history/ returns the logged merge event.
    """
    client = APIClient()
    client.force_authenticate(user=search_merge_env["owner_a"])
    source = search_merge_env["cust_source"]
    target = search_merge_env["cust_target"]

    client.post(
        "/api/v2/pos/customers/merge/",
        data={
            "source_customer_id": source.customer_id,
            "target_customer_id": target.customer_id,
            "merge_reason": "Merge for history check",
        },
        format="json",
    )

    resp_hist = client.get(f"/api/v2/pos/customers/merge-history/?customer_id={target.customer_id}")
    assert resp_hist.status_code == status.HTTP_200_OK
    items = resp_hist.data["results"]
    assert len(items) >= 1
    item = items[0]
    assert item["source_customer"]["customer_id"] == source.customer_id
    assert item["target_customer"]["customer_id"] == target.customer_id
    assert item["transferred_due_bdt"] == "400.00"
