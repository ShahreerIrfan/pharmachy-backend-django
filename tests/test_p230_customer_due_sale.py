"""
backend/tests/test_p230_customer_due_sale.py
Architecture Decision: ADR-0230 (P230).
Phase 12: POS, Sales Orders, Checkout & Returns.

End-to-end verification of:
1. Approved customer credit sales, explicit approval/suspend workflows & zero/exceeded limit blocks
2. Deliverable receivable postings (`CustomerDueRecord`), receipt snapshot, partial return & reversal parity
3. Later cash & verified digital due collection (`CustomerDueCollection`) WITHOUT double revenue
4. Cashier Close Reconciliation drawer cash & digital due-collection separation
5. RBAC, cross-tenant non-enumeration, BASIC plan lock, suspended tenant lock & concurrent limit safety
"""

from __future__ import annotations

import datetime
import threading
import uuid
from decimal import Decimal

import pytest
from django.db import close_old_connections
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    CashierRegisterAssignment,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    CustomerDueRecord,
    DueStatus,
    MedicineProduct,
    PaymentMethod,
    PlanTier,
    PosCustomer,
    PosCustomerCreditStatus,
    PosPaymentJournalEntry,
    PosPaymentJournalEntryType,
    PosSaleLifecycleState,
    RegisterAssignmentStatus,
    SaleInvoice,
    StaffMember,
    StaffRole,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole

_SQLITE_TX_LOCK = threading.Lock()


def _mk_user(tenant: Tenant, branch: Branch | None, role: str, tag: str) -> PharmacyUser:
    uid = uuid.uuid4().hex[:8]
    digits = "".join(str(ord(c) % 10) for c in uid)
    return PharmacyUser.objects.create_user(
        email=f"{tag}-{uid}@pharmacy.bd",
        phone_number=f"+88017{digits}",
        password="TestPass!123",
        full_name=f"{tag.title()} User",
        role=role,
        tenant_id=tenant.tenant_id,
        branch_id=branch.branch_id if branch else None,
    )


def _create_approved_sale_fixture(api_client, branch_id, product_id, qty, customer_id=None, reg_id=None):
    payload = {
        "branch": branch_id,
        "lines": [{"product": product_id, "quantity_units": qty, "unit_price_bdt": "10.00"}],
    }
    if customer_id:
        payload["customer"] = customer_id
    if reg_id:
        payload["register_assignment"] = reg_id
    res = api_client.post("/api/v2/pos/sales/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED, res.data
    return res.data["sale_id"]


@pytest.fixture
def p230_env(db):
    today = timezone.now().date()
    uid_a = uuid.uuid4().hex[:6]
    ten_a = Tenant.objects.create(
        tenant_id=f"ten-230a-{uid_a}",
        name="Dhaka Care Pharmacy",
        subdomain=f"dhaka-care-{uid_a}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_a1 = Branch.objects.create(
        branch_id=f"br-230a1-{uid_a}",
        tenant=ten_a,
        name="Dhanmondi Branch",
        license_number=f"DGDA-DHM-{uid_a}",
        is_active=True,
    )
    uid_b = uuid.uuid4().hex[:6]
    ten_b = Tenant.objects.create(
        tenant_id=f"ten-230b-{uid_b}",
        name="Other Pharmacy",
        subdomain=f"other-pharm-{uid_b}",
        plan_tier=PlanTier.STANDARD,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_b1 = Branch.objects.create(
        branch_id=f"br-230b1-{uid_b}",
        tenant=ten_b,
        name="Uttara Branch",
        license_number=f"DGDA-UTT-{uid_b}",
        is_active=True,
    )
    uid_bas = uuid.uuid4().hex[:6]
    ten_basic = Tenant.objects.create(
        tenant_id=f"ten-230bas-{uid_bas}",
        name="Starter Med",
        subdomain=f"starter-med-{uid_bas}",
        plan_tier=PlanTier.BASIC,
        status=TenantLifecycleStatus.ACTIVE,
    )
    br_basic = Branch.objects.create(
        branch_id=f"br-230bas1-{uid_bas}",
        tenant=ten_basic,
        name="Basic Branch",
        license_number=f"DGDA-BAS-{uid_bas}",
        is_active=True,
    )

    owner_a = _mk_user(ten_a, br_a1, UserRole.PHARMACY_OWNER, "own230a")
    manager_a = _mk_user(ten_a, br_a1, UserRole.BRANCH_MANAGER, "mgr230a")
    cashier_a = _mk_user(ten_a, br_a1, UserRole.CASHIER, "cash230a")
    owner_b = _mk_user(ten_b, br_b1, UserRole.PHARMACY_OWNER, "own230b")
    owner_basic = _mk_user(ten_basic, br_basic, UserRole.PHARMACY_OWNER, "own230bas")

    staff_cash_a = StaffMember.objects.create(
        staff_id=f"stf-{uuid.uuid4().hex[:8]}",
        tenant=ten_a,
        branch=br_a1,
        username=cashier_a.email,
        full_name=cashier_a.full_name,
        role=StaffRole.CASHIER,
        mobile_number=cashier_a.phone_number,
    )
    reg_a = CashierRegisterAssignment.objects.create(
        assignment_id=f"cra-{uuid.uuid4().hex[:8]}",
        tenant=ten_a,
        branch=br_a1,
        cashier=staff_cash_a,
        register_id="POS-REG-230",
        opening_float_amount=Decimal("500.00"),
        status=RegisterAssignmentStatus.ACTIVE,
    )

    prod_a = MedicineProduct.objects.create(
        product_id=f"med-230-{uuid.uuid4().hex[:6]}",
        brand_name="Napa Extend",
        generic_name="Paracetamol",
        strength="665mg",
        dosage_form="Tablet",
        manufacturer="Beximco",
        barcode=f"894230{uuid.uuid4().hex[:6]}",
        mrp_unit=Decimal("10.00"),
        cost_unit=Decimal("6.00"),
        is_prescription_only=False,
    )
    BatchStock.objects.create(
        batch_id=f"bat-230a-{uuid.uuid4().hex[:6]}",
        tenant=ten_a,
        branch=br_a1,
        product=prod_a,
        batch_number="BAT-230-A",
        expiry_date=today + datetime.timedelta(days=365),
        purchase_date=today - datetime.timedelta(days=10),
        quantity_base_units=500,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("6.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )
    BatchStock.objects.create(
        batch_id=f"bat-230bas-{uuid.uuid4().hex[:6]}",
        tenant=ten_basic,
        branch=br_basic,
        product=prod_a,
        batch_number="BAT-230-BAS",
        expiry_date=today + datetime.timedelta(days=365),
        purchase_date=today - datetime.timedelta(days=10),
        quantity_base_units=100,
        reserved_quantity_base_units=0,
        cost_per_unit=Decimal("6.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )

    return {
        "ten_a": ten_a,
        "br_a1": br_a1,
        "ten_b": ten_b,
        "br_b1": br_b1,
        "ten_basic": ten_basic,
        "br_basic": br_basic,
        "owner_a": owner_a,
        "manager_a": manager_a,
        "cashier_a": cashier_a,
        "owner_b": owner_b,
        "owner_basic": owner_basic,
        "prod_a": prod_a,
        "reg_a": reg_a,
    }


@pytest.mark.django_db
class TestP230CustomerDueSale:
    def test_valid_approved_due_sale_receipt_collections_without_double_revenue_and_close_recon(self, p230_env):
        api_client = APIClient()
        owner_a = p230_env["owner_a"]
        cashier_a = p230_env["cashier_a"]
        br_a1 = p230_env["br_a1"]
        ten_a = p230_env["ten_a"]
        prod_a = p230_env["prod_a"]
        reg_a = p230_env["reg_a"]

        # 1) Create pending customer, then Manager/Owner approves credit limit 1000.00 (15 days terms)
        api_client.force_authenticate(user=owner_a)
        c_res = api_client.post(
            "/api/v2/pos/customers/",
            {
                "primary_branch": br_a1.branch_id,
                "full_name": "Rafiqul Islam",
                "phone": "01711002233",
                "credit_limit_bdt": "1000.00",
                "credit_status": PosCustomerCreditStatus.PENDING_APPROVAL,
            },
            format="json",
        )
        assert c_res.status_code == status.HTTP_201_CREATED, c_res.data
        cust_id = c_res.data["customer_id"]
        assert c_res.data["is_credit_approved"] is False

        app_res = api_client.post(
            f"/api/v2/pos/customers/{cust_id}/approve-credit/",
            {"credit_limit_bdt": "1000.00", "credit_terms_days": 15},
            format="json",
        )
        assert app_res.status_code == status.HTTP_200_OK, app_res.data
        assert app_res.data["is_credit_approved"] is True
        assert app_res.data["credit_status"] == PosCustomerCreditStatus.APPROVED
        assert app_res.data["available_credit_bdt"] == "1000.00"

        # 2) Cashier opens sale (30 units = 300.00 BDT), attaches customer, splits 150 Cash tendered (100 net + 50 change) + 200 DUE
        api_client.force_authenticate(user=cashier_a)
        sale_id = _create_approved_sale_fixture(
            api_client, br_a1.branch_id, prod_a.product_id, qty=30, reg_id=reg_a.assignment_id
        )
        attach_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/attach-customer/",
            {"customer_id": cust_id},
            format="json",
        )
        assert attach_res.status_code == status.HTTP_200_OK, attach_res.data
        assert attach_res.data["customer_id"] == cust_id

        split_res = api_client.post(
            f"/api/v2/pos/sales/{sale_id}/split-payments/",
            {
                "splits": [
                    {"tender_type": PaymentMethod.CASH, "amount_bdt": "150.00"},
                    {"tender_type": PaymentMethod.DUE, "amount_bdt": "200.00"},
                ],
                "batch_idempotency_key": f"p230-sp-{uuid.uuid4().hex[:6]}",
            },
            format="json",
        )
        assert split_res.status_code == status.HTTP_201_CREATED, split_res.data
        assert split_res.data["sale"]["paid_amount_bdt"] == "100.00"
        assert split_res.data["sale"]["due_amount_bdt"] == "200.00"
        assert split_res.data["sale"]["change_returned_bdt"] == "50.00"

        fin_res = api_client.post(f"/api/v2/pos/sales/{sale_id}/finalize/", {}, format="json")
        assert fin_res.status_code == status.HTTP_200_OK, fin_res.data
        assert fin_res.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED

        # Check CustomerDueRecord linkage & PosCustomer due balance
        cust_obj = PosCustomer.objects.get(pk=cust_id)
        assert cust_obj.current_due_bdt == Decimal("200.00")
        due_rec = CustomerDueRecord.objects.get(tenant=ten_a, sale_id=sale_id)
        assert due_rec.customer_id == cust_id
        assert due_rec.total_due_bdt == Decimal("200.00")
        assert due_rec.remaining_due_bdt == Decimal("200.00")
        assert due_rec.status == DueStatus.OUTSTANDING
        assert due_rec.due_date == timezone.now().date() + datetime.timedelta(days=15)

        # Check printable receipt shows due snapshot
        rcpt = api_client.get(f"/api/v2/pos/sales/{sale_id}/receipt/")
        assert rcpt.status_code == status.HTTP_200_OK
        assert rcpt.data["totals"]["due_amount_bdt"] == "200.00"
        assert rcpt.data["customer_due_snapshot"]["due_record_id"] == due_rec.due_id
        assert rcpt.data["customer_due_snapshot"]["credit_status"] == PosCustomerCreditStatus.APPROVED
        assert rcpt.data["customer_due_snapshot"]["remaining_due_bdt"] == "200.00"

        # Baseline revenue before collection: 1 Finalized invoice = 300.00, Journal Cr POS_SALES_REVENUE = 300.00
        assert SaleInvoice.objects.filter(tenant=ten_a, status="FINALIZED").count() == 1
        rev_sum_before = PosPaymentJournalEntry.objects.filter(
            tenant=ten_a, credit_account="POS_SALES_REVENUE"
        ).aggregate(s=Sum("amount_bdt"))["s"]
        assert rev_sum_before == Decimal("300.00")

        # 3) Later Due Collection #1: Partial CASH 120.00 (150.00 tendered, 30.00 change) + idempotent replay
        col_idem = f"col-idem-{uuid.uuid4().hex[:8]}"
        c1 = api_client.post(
            f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
            {
                "tender_type": PaymentMethod.CASH,
                "amount_bdt": "120.00",
                "cash_tendered_bdt": "150.00",
                "idempotency_key": col_idem,
                "notes": "First installment",
            },
            format="json",
        )
        assert c1.status_code == status.HTTP_201_CREATED, c1.data
        assert c1.data["revenue_impact_bdt"] == "0.00"
        assert c1.data["collection"]["debit_account"] == "CASH_DRAWER"
        assert c1.data["collection"]["credit_account"] == "AR_CUSTOMER_DUE"
        assert c1.data["collection"]["change_given_bdt"] == "30.00"
        assert c1.data["due_record"]["remaining_due_bdt"] == "80.00"
        assert c1.data["due_record"]["status"] == DueStatus.PARTIAL

        c1_replay = api_client.post(
            f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
            {
                "tender_type": PaymentMethod.CASH,
                "amount_bdt": "120.00",
                "cash_tendered_bdt": "150.00",
                "idempotency_key": col_idem,
            },
            format="json",
        )
        assert c1_replay.status_code == status.HTTP_200_OK
        assert c1_replay.data["idempotent_replay"] is True

        # 4) Later Due Collection #2: Remaining 80.00 via verified BKASH
        c2 = api_client.post(
            f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
            {
                "tender_type": PaymentMethod.BKASH,
                "amount_bdt": "80.00",
                "gateway_reference": "BK-DUE-9921",
                "idempotency_key": f"col-bk-{uuid.uuid4().hex[:6]}",
            },
            format="json",
        )
        assert c2.status_code == status.HTTP_201_CREATED, c2.data
        assert c2.data["due_record"]["remaining_due_bdt"] == "0.00"
        assert c2.data["due_record"]["status"] == DueStatus.SETTLED
        assert c2.data["collection"]["debit_account"] == "BKASH_CLEARING"
        assert c2.data["collection"]["credit_account"] == "AR_CUSTOMER_DUE"

        # CRITICAL P230 PROOF: Zero double revenue & exact AR balance == 0.00
        assert SaleInvoice.objects.filter(tenant=ten_a, status="FINALIZED").count() == 1
        rev_sum_after = PosPaymentJournalEntry.objects.filter(
            tenant=ten_a, credit_account="POS_SALES_REVENUE"
        ).aggregate(s=Sum("amount_bdt"))["s"]
        assert rev_sum_after == Decimal("300.00")
        ar_debits = PosPaymentJournalEntry.objects.filter(
            tenant=ten_a, debit_account="AR_CUSTOMER_DUE"
        ).aggregate(s=Sum("amount_bdt"))["s"] or Decimal("0.00")
        ar_credits = PosPaymentJournalEntry.objects.filter(
            tenant=ten_a, credit_account="AR_CUSTOMER_DUE"
        ).aggregate(s=Sum("amount_bdt"))["s"] or Decimal("0.00")
        assert ar_debits == Decimal("200.00")
        assert ar_credits == Decimal("200.00")

        cust_obj.refresh_from_db()
        assert cust_obj.current_due_bdt == Decimal("0.00")

        ledger = api_client.get(f"/api/v2/pos/customers/{cust_id}/ledger/")
        assert ledger.status_code == status.HTTP_200_OK
        assert ledger.data["available_credit_bdt"] == "1000.00"
        assert ledger.data["double_revenue_detected"] is False
        assert ledger.data["revenue_recognized_bdt"] == "300.00"
        assert ledger.data["total_due_collected_bdt"] == "200.00"

        # 5) Close Reconciliation includes +120.00 cash due collection in drawer WITHOUT incrementing sales_count
        prev = api_client.get(
            f"/api/v2/pos/sales/close-reconciliation/?branch={br_a1.branch_id}&register_assignment={reg_a.assignment_id}"
        )
        assert prev.status_code == status.HTTP_200_OK
        # opening(500) + net_cash_sales(100) + due_collected_cash(120) = 720.00
        assert prev.data["net_cash_collected_bdt"] == "100.00"
        assert prev.data["due_collected_cash_bdt"] == "120.00"
        assert prev.data["due_collected_digital_bdt"] == "80.00"
        assert prev.data["expected_drawer_cash_bdt"] == "720.00"
        assert prev.data["sales_count"] == 1

        close_sub = api_client.post(
            "/api/v2/pos/sales/close-reconciliation/",
            {
                "branch": br_a1.branch_id,
                "register_assignment": reg_a.assignment_id,
                "counted_cash_bdt": "720.00",
            },
            format="json",
        )
        assert close_sub.status_code == status.HTTP_201_CREATED
        assert close_sub.data["status"] == "BALANCED"
        assert close_sub.data["due_collected_cash_bdt"] == "120.00"

    def test_invalid_inputs_rbac_partial_return_collection_reversal_cross_tenant_and_plans(self, p230_env):
        api_client = APIClient()
        owner_a = p230_env["owner_a"]
        manager_a = p230_env["manager_a"]
        cashier_a = p230_env["cashier_a"]
        owner_b = p230_env["owner_b"]
        owner_basic = p230_env["owner_basic"]
        br_a1 = p230_env["br_a1"]
        br_b1 = p230_env["br_b1"]
        br_basic = p230_env["br_basic"]
        prod_a = p230_env["prod_a"]

        # 1) Unapproved customer (0 limit / Cashier created) & walk-in blocked from DUE
        api_client.force_authenticate(user=cashier_a)
        walkin_sale = _create_approved_sale_fixture(api_client, br_a1.branch_id, prod_a.product_id, qty=5)
        no_cust_due = api_client.post(
            f"/api/v2/pos/sales/{walkin_sale}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "50.00"}]},
            format="json",
        )
        assert no_cust_due.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_APPROVED_CUSTOMER_REQUIRED" in str(no_cust_due.data)

        c_cash = api_client.post(
            "/api/v2/pos/customers/",
            {"primary_branch": br_a1.branch_id, "full_name": "Walkin To Credit", "phone": "01811000001"},
            format="json",
        )
        assert c_cash.status_code == status.HTTP_201_CREATED
        unapproved_id = c_cash.data["customer_id"]

        # Cashier CANNOT approve credit (403)
        cashier_approve = api_client.post(
            f"/api/v2/pos/customers/{unapproved_id}/approve-credit/",
            {"credit_limit_bdt": "500.00"},
            format="json",
        )
        assert cashier_approve.status_code == status.HTTP_403_FORBIDDEN

        # Unapproved customer blocked from DUE even for small amount
        api_client.post(
            f"/api/v2/pos/sales/{walkin_sale}/attach-customer/",
            {"customer_id": unapproved_id},
            format="json",
        )
        unapp_due = api_client.post(
            f"/api/v2/pos/sales/{walkin_sale}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "50.00"}]},
            format="json",
        )
        assert unapp_due.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_CUSTOMER_CREDIT_NOT_APPROVED" in str(unapp_due.data)

        # Offline DUE blocked cleanly
        offline_due = api_client.post(
            f"/api/v2/pos/sales/{walkin_sale}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "50.00"}], "is_offline": True},
            format="json",
        )
        assert offline_due.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_OFFLINE_CREDIT_UNAVAILABLE" in str(offline_due.data)

        # 2) Manager approves 100.00 limit -> 120.00 exceeds limit -> 50.00 succeeds -> then suspend blocks next sale
        api_client.force_authenticate(user=manager_a)
        assert (
            api_client.post(
                f"/api/v2/pos/customers/{unapproved_id}/approve-credit/",
                {"credit_limit_bdt": "100.00"},
                format="json",
            ).status_code
            == status.HTTP_200_OK
        )

        api_client.force_authenticate(user=cashier_a)
        sale_over = _create_approved_sale_fixture(
            api_client, br_a1.branch_id, prod_a.product_id, qty=12, customer_id=unapproved_id
        )
        over_res = api_client.post(
            f"/api/v2/pos/sales/{sale_over}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "120.00"}]},
            format="json",
        )
        assert over_res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_CUSTOMER_CREDIT_LIMIT_EXCEEDED" in str(over_res.data)

        # Complete valid 50.00 due sale + test partial return + collection errors + collection reversal
        s_valid = _create_approved_sale_fixture(
            api_client, br_a1.branch_id, prod_a.product_id, qty=5, customer_id=unapproved_id
        )
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{s_valid}/split-payments/",
                {"splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "50.00"}]},
                format="json",
            ).status_code
            == status.HTTP_201_CREATED
        )
        assert api_client.post(f"/api/v2/pos/sales/{s_valid}/finalize/", {}, format="json").status_code == status.HTTP_200_OK
        due_rec = CustomerDueRecord.objects.get(sale_id=s_valid)

        # Invalid collection cases: DUE tender, overpayment, offline, missing bKash TrxID
        for bad_payload, code in [
            ({"tender_type": PaymentMethod.DUE, "amount_bdt": "10.00"}, "ERR_CANNOT_COLLECT_DUE_WITH_DUE"),
            ({"tender_type": PaymentMethod.CASH, "amount_bdt": "90.00"}, "ERR_DUE_OVERPAYMENT"),
            ({"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00", "is_offline": True}, "ERR_OFFLINE_UNAVAILABLE"),
            ({"tender_type": PaymentMethod.BKASH, "amount_bdt": "10.00"}, "ERR_DIGITAL_TENDER_REFERENCE_REQUIRED"),
        ]:
            bad_r = api_client.post(
                f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
                bad_payload,
                format="json",
            )
            assert bad_r.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
            assert code in str(bad_r.data)

        # Collect 20.00 Cash, then Cashier CANNOT reverse collection (403); Manager reverses -> due back to 50.00
        col_ok = api_client.post(
            f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "20.00"},
            format="json",
        )
        assert col_ok.status_code == status.HTTP_201_CREATED
        col_id = col_ok.data["collection"]["collection_id"]

        cash_rev = api_client.post(
            f"/api/v2/pos/customer-dues/collections/{col_id}/reverse/",
            {"reason": "Wrong receipt"},
            format="json",
        )
        assert cash_rev.status_code == status.HTTP_403_FORBIDDEN

        api_client.force_authenticate(user=manager_a)
        mgr_rev = api_client.post(
            f"/api/v2/pos/customer-dues/collections/{col_id}/reverse/",
            {"reason": "Wrong customer account posted"},
            format="json",
        )
        assert mgr_rev.status_code == status.HTTP_200_OK
        assert mgr_rev.data["collection"]["status"] == CustomerDueCollectionStatus.REVERSED
        assert mgr_rev.data["due_record"]["remaining_due_bdt"] == "50.00"

        # Partial return of 10.00 against DUE reduces both CustomerDueRecord & PosCustomer
        part_ret = api_client.post(
            f"/api/v2/pos/sales/{s_valid}/refund-split/",
            {"refund_splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "10.00"}], "reason": "Returned 1 strip"},
            format="json",
        )
        assert part_ret.status_code == status.HTTP_200_OK
        due_rec.refresh_from_db()
        cust_chk = PosCustomer.objects.get(pk=unapproved_id)
        assert due_rec.remaining_due_bdt == Decimal("40.00")
        assert cust_chk.current_due_bdt == Decimal("40.00")

        # Suspend credit -> next DUE blocked
        assert (
            api_client.post(
                f"/api/v2/pos/customers/{unapproved_id}/suspend-credit/",
                {"reason": "Payment overdue"},
                format="json",
            ).status_code
            == status.HTTP_200_OK
        )
        susp_sale = _create_approved_sale_fixture(
            api_client, br_a1.branch_id, prod_a.product_id, qty=2, customer_id=unapproved_id
        )
        susp_due = api_client.post(
            f"/api/v2/pos/sales/{susp_sale}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "20.00"}]},
            format="json",
        )
        assert susp_due.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "ERR_CUSTOMER_CREDIT_SUSPENDED" in str(susp_due.data)

        # Cross-tenant B cannot read or collect Tenant A due or attach Tenant A customer
        api_client.force_authenticate(user=owner_b)
        assert api_client.get(f"/api/v2/pos/customer-dues/{due_rec.due_id}/").status_code == status.HTTP_404_NOT_FOUND
        assert (
            api_client.post(
                f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
                {"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00"},
                format="json",
            ).status_code
            == status.HTTP_404_NOT_FOUND
        )
        b_sale = _create_approved_sale_fixture(api_client, br_b1.branch_id, prod_a.product_id, qty=1)
        assert (
            api_client.post(
                f"/api/v2/pos/sales/{b_sale}/attach-customer/",
                {"customer_id": unapproved_id},
                format="json",
            ).status_code
            == status.HTTP_403_FORBIDDEN
        )

        # BASIC plan blocked from CUSTOMER_LEDGER endpoints (403 ERR_SUBSCRIPTION_FEATURE_LOCKED)
        api_client.force_authenticate(user=owner_basic)
        basic_dues = api_client.get(f"/api/v2/pos/customer-dues/?branch={br_basic.branch_id}")
        assert basic_dues.status_code == status.HTTP_403_FORBIDDEN
        assert "ERR_SUBSCRIPTION_FEATURE_LOCKED" in str(basic_dues.data)

        # Suspended tenant blocked from due collection (403 ERR_SUBSCRIPTION_SUSPENDED)
        ten_a = p230_env["ten_a"]
        ten_a.status = TenantLifecycleStatus.SUSPENDED
        ten_a.save(update_fields=["status"])
        api_client.force_authenticate(user=owner_a)
        susp_collect = api_client.post(
            f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00"},
            format="json",
        )
        assert susp_collect.status_code == status.HTTP_403_FORBIDDEN
        assert "SUSPENDED" in susp_collect.content.decode("utf-8").upper()


@pytest.mark.django_db(transaction=True)
def test_concurrent_credit_limit_lock_and_due_collection_idempotency(p230_env):
    owner_a = p230_env["owner_a"]
    cashier_a = p230_env["cashier_a"]
    br_a1 = p230_env["br_a1"]
    ten_a = p230_env["ten_a"]
    prod_a = p230_env["prod_a"]

    cust = PosCustomer.objects.create(
        customer_id=f"cust-conc-{uuid.uuid4().hex[:6]}",
        tenant=ten_a,
        primary_branch=br_a1,
        full_name="Concurrent Credit Cust",
        phone="01999887766",
        credit_limit_bdt=Decimal("500.00"),
        credit_status=PosCustomerCreditStatus.APPROVED,
        is_credit_approved=True,
    )

    c_setup = APIClient()
    c_setup.force_authenticate(user=cashier_a)
    sale_ids = []
    for _ in range(2):
        sid = _create_approved_sale_fixture(c_setup, br_a1.branch_id, prod_a.product_id, qty=40, customer_id=cust.customer_id)
        sp = c_setup.post(
            f"/api/v2/pos/sales/{sid}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.DUE, "amount_bdt": "400.00"}]},
            format="json",
        )
        assert sp.status_code == status.HTTP_201_CREATED
        sale_ids.append(sid)

    barrier = threading.Barrier(2)
    results = []

    def _finalize_worker(sid):
        client = APIClient()
        client.force_authenticate(user=owner_a)
        barrier.wait(timeout=5)
        with _SQLITE_TX_LOCK:
            close_old_connections()
            r = client.post(f"/api/v2/pos/sales/{sid}/finalize/", {}, format="json")
            results.append((r.status_code, r.data))
            close_old_connections()

    threads = [threading.Thread(target=_finalize_worker, args=(sid,)) for sid in sale_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    ok_codes = [c for c, _ in results if c == status.HTTP_200_OK]
    err_codes = [c for c, _ in results if c == status.HTTP_422_UNPROCESSABLE_ENTITY]
    assert len(ok_codes) == 1
    assert len(err_codes) == 1
    assert "ERR_CUSTOMER_CREDIT_LIMIT_EXCEEDED" in str(results)
    cust.refresh_from_db()
    assert cust.current_due_bdt == Decimal("400.00")

    # Concurrent idempotent due collection on the winning sale -> only ONE collection created
    due_rec = CustomerDueRecord.objects.get(customer=cust, status=DueStatus.OUTSTANDING)
    col_key = f"conc-col-{uuid.uuid4().hex[:6]}"
    col_barrier = threading.Barrier(2)
    col_results = []

    def _collect_worker():
        client = APIClient()
        client.force_authenticate(user=cashier_a)
        col_barrier.wait(timeout=5)
        with _SQLITE_TX_LOCK:
            close_old_connections()
            r = client.post(
                f"/api/v2/pos/customer-dues/{due_rec.due_id}/collect/",
                {"tender_type": PaymentMethod.CASH, "amount_bdt": "250.00", "idempotency_key": col_key},
                format="json",
            )
            col_results.append((r.status_code, r.data.get("collection", {}).get("collection_id")))
            close_old_connections()

    c_threads = [threading.Thread(target=_collect_worker) for _ in range(2)]
    for t in c_threads:
        t.start()
    for t in c_threads:
        t.join(timeout=15)

    assert sorted(c for c, _ in col_results) == [status.HTTP_200_OK, status.HTTP_201_CREATED]
    assert len({cid for _, cid in col_results}) == 1
    assert CustomerDueCollection.objects.filter(due_record=due_rec).count() == 1
    due_rec.refresh_from_db()
    assert due_rec.remaining_due_bdt == Decimal("150.00")
    assert PosPaymentJournalEntry.objects.filter(
        tenant=ten_a, entry_type=PosPaymentJournalEntryType.DUE_COLLECTION
    ).count() == 1
