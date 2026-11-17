"""
backend/tests/test_p229_cash_split_payments.py
Acceptance tests for P229: Cash, MFS (bKash/Nagad/Rocket/Upay), Card & Verified Split Payments,
Audited Journal Posting, Partial Return Split Refunds, and Cashier Close Reconciliation (Phase 12).
"""

from __future__ import annotations

import datetime
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, connections
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    BatchStatus,
    BatchStock,
    Branch,
    CashierRegisterAssignment,
    CustomerDueRecord,
    DueStatus,
    MedicineProduct,
    PaymentMethod,
    PaymentTransaction,
    PlanTier,
    PosCashierCloseReconciliation,
    PosCustomer,
    PosPackagingUnit,
    PosPaymentAttempt,
    PosPaymentAttemptStatus,
    PosPaymentJournalEntry,
    PosPaymentJournalEntryType,
    PosPaymentVerificationStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    RegisterAssignmentStatus,
    StaffMember,
    StaffRole,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.pos_payment_service import PosPaymentSplitService
from apps.identity.models import PharmacyUser, UserRole


def _mk_tenant(name: str, slug_prefix: str, plan: str = PlanTier.STANDARD, sub_status: str = TenantLifecycleStatus.ACTIVE):
    uid = uuid.uuid4().hex[:6]
    return Tenant.objects.create(
        tenant_id=f"ten-{slug_prefix}-{uid}",
        name=name,
        subdomain=f"{slug_prefix}-{uid}",
        status=sub_status,
        plan_tier=plan,
    )


def _mk_branch(tenant: Tenant, name: str, code: str):
    uid = uuid.uuid4().hex[:6]
    return Branch.objects.create(
        branch_id=f"br-{code}-{uid}",
        tenant=tenant,
        name=name,
        license_number=f"DGDA-{code.upper()}-{uid}",
        is_active=True,
    )


def _mk_user(tenant: Tenant, branch: Branch | None, role: str, tag: str):
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


def _mk_napa_product():
    uid = uuid.uuid4().hex[:6]
    return MedicineProduct.objects.create(
        product_id=f"med-p229-{uid}",
        brand_name="Napa Extend 665",
        generic_name="Paracetamol",
        strength="665mg",
        dosage_form="Tablet",
        manufacturer="Beximco",
        barcode=f"894229{uid}",
        mrp_unit=Decimal("5.00"),
        cost_unit=Decimal("3.00"),
        is_prescription_only=False,
    )


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def env(db):
    ten_a = _mk_tenant("Lazz Split Pharma", "p229a", PlanTier.STANDARD)
    br_a1 = _mk_branch(ten_a, "Banani Counter 1", "a1")
    br_a2 = _mk_branch(ten_a, "Uttara Counter 2", "a2")

    ten_b = _mk_tenant("Foreign Tenant", "p229b", PlanTier.PRO)
    br_b1 = _mk_branch(ten_b, "Sylhet Main", "b1")

    ten_basic = _mk_tenant("Basic Plan Pharma", "p229bas", PlanTier.BASIC)
    br_bas = _mk_branch(ten_basic, "Comilla Main", "bas")

    cashier_a = _mk_user(ten_a, br_a1, UserRole.CASHIER, "cash")
    manager_a = _mk_user(ten_a, br_a1, UserRole.BRANCH_MANAGER, "mgr")
    clerk_a = _mk_user(ten_a, br_a1, UserRole.INVENTORY_CLERK, "clerk")
    cashier_a2 = _mk_user(ten_a, br_a2, UserRole.CASHIER, "casha2")
    cashier_b = _mk_user(ten_b, br_b1, UserRole.CASHIER, "cashb")
    cashier_bas = _mk_user(ten_basic, br_bas, UserRole.CASHIER, "cashbas")

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
        register_id="POS-REG-A1",
        opening_float_amount=Decimal("1000.00"),
        status=RegisterAssignmentStatus.ACTIVE,
    )

    prod = _mk_napa_product()
    today = timezone.now().date()
    for br in (br_a1, br_a2, br_bas):
        BatchStock.objects.create(
            batch_id=f"bat-{br.branch_id[:8]}-{uuid.uuid4().hex[:5]}",
            tenant=br.tenant,
            branch=br,
            product=prod,
            batch_number=f"LOT-{br.branch_id[:4].upper()}",
            expiry_date=today + datetime.timedelta(days=180),
            purchase_date=today - datetime.timedelta(days=10),
            quantity_base_units=500,
            cost_per_unit=Decimal("2.50"),
            status=BatchStatus.ACTIVE,
            is_active=True,
        )

    cust_a = PosCustomer.objects.create(
        customer_id=f"cust-p229-{uuid.uuid4().hex[:6]}",
        tenant=ten_a,
        primary_branch=br_a1,
        full_name="Rafiqul Islam",
        phone="+8801711334455",
        credit_limit_bdt=Decimal("500.00"),
        current_due_bdt=Decimal("0.00"),
        is_active=True,
    )

    return {
        "ten_a": ten_a,
        "br_a1": br_a1,
        "br_a2": br_a2,
        "ten_b": ten_b,
        "br_b1": br_b1,
        "ten_basic": ten_basic,
        "br_bas": br_bas,
        "cashier_a": cashier_a,
        "manager_a": manager_a,
        "clerk_a": clerk_a,
        "cashier_a2": cashier_a2,
        "cashier_b": cashier_b,
        "cashier_bas": cashier_bas,
        "reg_a": reg_a,
        "prod": prod,
        "cust_a": cust_a,
    }


def _open_cart_with_total(api_client, branch_id: str, prod_id: str, qty: int = 20, customer_id: str | None = None, reg_id: str | None = None) -> str:
    payload = {"branch": branch_id, "force_new": True}
    if customer_id:
        payload["customer"] = customer_id
    if reg_id:
        payload["register_assignment"] = reg_id
    res = api_client.post("/api/v2/pos/sales/quick-cart/", payload, format="json")
    assert res.status_code == status.HTTP_201_CREATED
    sale_id = res.data["sale"]["sale_id"]
    add = api_client.post(
        f"/api/v2/pos/sales/{sale_id}/scan-add/",
        {"product_id": prod_id, "packaging_unit": PosPackagingUnit.TABLET, "quantity_delta": qty},
        format="json",
    )
    assert add.status_code == status.HTTP_200_OK
    return sale_id


class TestP229CashSplitPayments:
    def test_valid_multi_tender_split_cash_change_journal_receipt_partial_refund_reversal_and_close_reconciliation(
        self, api_client, env
    ):
        br_a1 = env["br_a1"]
        prod = env["prod"]
        cashier = env["cashier_a"]
        manager = env["manager_a"]
        reg_a = env["reg_a"]
        cust_a = env["cust_a"]

        # 1) Sale 1: Total 100.00 BDT settled across bKash 35 + Nagad 25 + Card 20 + Cash 30 (change 10 -> net cash 20)
        api_client.force_authenticate(user=cashier)
        s1 = _open_cart_with_total(api_client, br_a1.branch_id, prod.product_id, qty=20, customer_id=cust_a.customer_id, reg_id=reg_a.assignment_id)

        batch_key = f"split-b1-{uuid.uuid4().hex[:8]}"
        split_res = api_client.post(
            f"/api/v2/pos/sales/{s1}/split-payments/",
            {
                "batch_idempotency_key": batch_key,
                "require_verified": True,
                "splits": [
                    {
                        "tender_type": PaymentMethod.BKASH,
                        "amount_bdt": "35.00",
                        "gateway_reference": "BKS-TRX-9001",
                        "payer_reference": "01711****22",
                    },
                    {
                        "tender_type": PaymentMethod.NAGAD,
                        "amount_bdt": "25.00",
                        "gateway_reference": "NGD-TRX-9002",
                    },
                    {
                        "tender_type": PaymentMethod.CARD,
                        "amount_bdt": "20.00",
                        "gateway_reference": "CRD-AUTH-9003",
                        "card_last4": "4321",
                    },
                    {
                        "tender_type": PaymentMethod.CASH,
                        "amount_bdt": "30.00",
                    },
                ],
            },
            format="json",
        )
        assert split_res.status_code == status.HTTP_201_CREATED
        assert split_res.data["idempotent_replay"] is False
        assert split_res.data["sale"]["paid_amount_bdt"] == "100.00"
        assert split_res.data["sale"]["change_returned_bdt"] == "10.00"
        cash_leg = next(a for a in split_res.data["sale"]["payment_attempts"] if a["tender_type"] == PaymentMethod.CASH)
        assert cash_leg["amount_bdt"] == "30.00"
        assert cash_leg["change_given_bdt"] == "10.00"
        assert cash_leg["net_settled_amount_bdt"] == "20.00"

        # Idempotent batch replay returns 200 without duplicating attempts!
        replay_res = api_client.post(
            f"/api/v2/pos/sales/{s1}/split-payments/",
            {
                "batch_idempotency_key": batch_key,
                "splits": [{"tender_type": PaymentMethod.CASH, "amount_bdt": "30.00"}],
            },
            format="json",
        )
        assert replay_res.status_code == status.HTTP_200_OK
        assert replay_res.data["idempotent_replay"] is True
        assert PosPaymentAttempt.objects.filter(sale_id=s1).count() == 4

        # Finalize sale 1 -> exact net PaymentTransactions (sum == 100.00) & immutable PosPaymentJournalEntry!
        fin1 = api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json")
        assert fin1.status_code == status.HTTP_200_OK
        assert fin1.data["lifecycle_state"] == PosSaleLifecycleState.FINALIZED
        sale1_obj = PosSaleOrder.objects.get(pk=s1)
        txs1 = list(PaymentTransaction.objects.filter(invoice=sale1_obj.linked_invoice))
        assert len(txs1) == 4
        assert sum(t.amount_bdt for t in txs1) == Decimal("100.00")
        cash_tx1 = next(t for t in txs1 if t.tender_type == PaymentMethod.CASH)
        assert cash_tx1.amount_bdt == Decimal("20.00")  # Net cash excluding 10.00 change!

        journals1 = list(PosPaymentJournalEntry.objects.filter(sale_id=s1))
        assert len(journals1) == 4
        assert sum(j.amount_bdt for j in journals1) == Decimal("100.00")
        assert sum(j.cash_register_impact_bdt for j in journals1) == Decimal("20.00")

        # Receipt shows full split tender breakdown, change returned & tender_summary
        rcpt = api_client.get(f"/api/v2/pos/sales/{s1}/receipt/")
        assert rcpt.status_code == status.HTTP_200_OK
        assert rcpt.data["totals"]["change_returned_bdt"] == "10.00"
        assert rcpt.data["tender_summary"]["BKASH"] == "35.00"
        assert rcpt.data["tender_summary"]["CASH"] == "20.00"
        assert len(rcpt.data["payments"]) == 4

        # Journal immutability check
        j0 = journals1[0]
        with pytest.raises(DjangoValidationError):
            j0.amount_bdt = Decimal("999.00")
            j0.save()
        with pytest.raises(DjangoValidationError):
            j0.delete()

        # Partial return split refund (Cashier blocked -> 403; Manager refunds CASH 10 + BKASH 15 = 25.00)
        cash_refund_fail = api_client.post(
            f"/api/v2/pos/sales/{s1}/refund-split/",
            {"reason": "Customer returned strip", "refund_splits": [{"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00"}]},
            format="json",
        )
        assert cash_refund_fail.status_code == status.HTTP_403_FORBIDDEN

        api_client.force_authenticate(user=manager)
        over_ref = api_client.post(
            f"/api/v2/pos/sales/{s1}/refund-split/",
            {"reason": "Too much cash refund", "refund_splits": [{"tender_type": PaymentMethod.CASH, "amount_bdt": "25.00"}]},
            format="json",
        )
        assert over_ref.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert over_ref.data["error_code"] == "ERR_REFUND_EXCEEDS_TENDER_SETTLED"

        ref_ok = api_client.post(
            f"/api/v2/pos/sales/{s1}/refund-split/",
            {
                "reason": "Returned partial items",
                "refund_splits": [
                    {"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00"},
                    {"tender_type": PaymentMethod.BKASH, "amount_bdt": "15.00"},
                ],
            },
            format="json",
        )
        assert ref_ok.status_code == status.HTTP_200_OK
        assert ref_ok.data["sale"]["refunded_amount_bdt"] == "25.00"
        assert ref_ok.data["sale"]["lifecycle_state"] == PosSaleLifecycleState.PARTIALLY_RETURNED

        # 2) Sale 2: Rocket + Upay + Bank Transfer + Customer Due = 50.00 BDT, then full REVERSAL
        api_client.force_authenticate(user=cashier)
        s2 = _open_cart_with_total(api_client, br_a1.branch_id, prod.product_id, qty=10, customer_id=cust_a.customer_id, reg_id=reg_a.assignment_id)
        split2 = api_client.post(
            f"/api/v2/pos/sales/{s2}/split-payments/",
            {
                "splits": [
                    {"tender_type": PaymentMethod.ROCKET, "amount_bdt": "15.00", "gateway_reference": "RKT-5511"},
                    {"tender_type": PaymentMethod.UPAY, "amount_bdt": "10.00", "gateway_reference": "UPY-5512"},
                    {"tender_type": PaymentMethod.BANK_TRANSFER, "amount_bdt": "10.00", "gateway_reference": "NPSB-5513"},
                    {"tender_type": PaymentMethod.DUE, "amount_bdt": "15.00"},
                ]
            },
            format="json",
        )
        assert split2.status_code == status.HTTP_201_CREATED
        assert api_client.post(f"/api/v2/pos/sales/{s2}/finalize/", {}, format="json").status_code == status.HTTP_200_OK
        cust_a.refresh_from_db()
        assert cust_a.current_due_bdt == Decimal("15.00")

        # Reverse sale 2 completely -> compensating journal entries + DUE restored + PaymentTransactions reversed
        api_client.force_authenticate(user=manager)
        rev2 = api_client.post(f"/api/v2/pos/sales/{s2}/reverse/", {"reason": "Wrong patient dispense"}, format="json")
        assert rev2.status_code == status.HTTP_200_OK
        assert PosPaymentJournalEntry.objects.filter(sale_id=s2, entry_type=PosPaymentJournalEntryType.REFUND_REVERSAL).count() == 4
        cust_a.refresh_from_db()
        assert cust_a.current_due_bdt == Decimal("0.00")
        sale2_obj = PosSaleOrder.objects.get(pk=s2)
        assert all(t.status == "REVERSED" for t in PaymentTransaction.objects.filter(invoice=sale2_obj.linked_invoice))
        assert CustomerDueRecord.objects.filter(invoice=sale2_obj.linked_invoice, remaining_due_bdt=Decimal("0.00"), status=DueStatus.WRITTEN_OFF).exists()

        # 3) Cashier Close Reconciliation Preview & Submit
        api_client.force_authenticate(user=cashier)
        # Sale 1 net cash = +20.00, partial cash refund = -10.00 -> net_cash = 10.00; opening_float = 1000.00 -> expected = 1010.00!
        prev = api_client.get(f"/api/v2/pos/sales/close-reconciliation/?branch={br_a1.branch_id}&register_assignment={reg_a.assignment_id}")
        assert prev.status_code == status.HTTP_200_OK
        assert prev.data["opening_float_bdt"] == "1000.00"
        assert prev.data["gross_cash_tendered_bdt"] == "30.00"
        assert prev.data["cash_change_returned_bdt"] == "10.00"
        assert prev.data["net_cash_collected_bdt"] == "20.00"
        assert prev.data["cash_refunded_bdt"] == "10.00"
        assert prev.data["expected_drawer_cash_bdt"] == "1010.00"
        assert prev.data["journal_balanced"] is True
        assert prev.data["tender_totals"]["BKASH"]["net_bdt"] == "20.00"  # 35 - 15 partial refund!
        assert prev.data["tender_totals"]["NAGAD"]["net_bdt"] == "25.00"
        assert prev.data["tender_totals"]["CARD"]["net_bdt"] == "20.00"

        # Variance without notes -> 422 ERR_VARIANCE_NOTES_REQUIRED
        bad_close = api_client.post(
            "/api/v2/pos/sales/close-reconciliation/",
            {"branch": br_a1.branch_id, "register_assignment": reg_a.assignment_id, "counted_cash_bdt": "1005.00"},
            format="json",
        )
        assert bad_close.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert bad_close.data["error_code"] == "ERR_VARIANCE_NOTES_REQUIRED"

        # Exact counted cash close -> BALANCED & Register closed cleanly!
        ok_close = api_client.post(
            "/api/v2/pos/sales/close-reconciliation/",
            {
                "branch": br_a1.branch_id,
                "register_assignment": reg_a.assignment_id,
                "counted_cash_bdt": "1010.00",
                "notes": "Shift closed balanced",
            },
            format="json",
        )
        assert ok_close.status_code == status.HTTP_201_CREATED
        assert ok_close.data["status"] == "BALANCED"
        assert ok_close.data["variance_bdt"] == "0.00"
        reg_a.refresh_from_db()
        assert reg_a.status == RegisterAssignmentStatus.CLOSED
        assert reg_a.closing_float_amount == Decimal("1010.00")

        rec_obj = PosCashierCloseReconciliation.objects.get(pk=ok_close.data["reconciliation_id"])
        with pytest.raises(DjangoValidationError):
            rec_obj.delete()

    def test_validation_pending_verify_void_offline_rbac_cross_tenant_subscription_and_basic_plan(
        self, api_client, env
    ):
        ten_a = env["ten_a"]
        br_a1 = env["br_a1"]
        prod = env["prod"]
        cashier = env["cashier_a"]
        cashier_a2 = env["cashier_a2"]
        cashier_b = env["cashier_b"]
        clerk_a = env["clerk_a"]
        cashier_bas = env["cashier_bas"]

        api_client.force_authenticate(user=cashier)
        s1 = _open_cart_with_total(api_client, br_a1.branch_id, prod.product_id, qty=10)  # 50.00 BDT

        # 1) Digital tender without reference -> 422 ERR_DIGITAL_TENDER_REFERENCE_REQUIRED
        no_ref = api_client.post(
            f"/api/v2/pos/sales/{s1}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.BKASH, "amount_bdt": "50.00", "gateway_reference": "ab"}]},
            format="json",
        )
        assert no_ref.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert no_ref.data["error_code"] == "ERR_DIGITAL_TENDER_REFERENCE_REQUIRED"

        # Invalid card_last4
        bad_card = api_client.post(
            f"/api/v2/pos/sales/{s1}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.CARD, "amount_bdt": "50.00", "gateway_reference": "CARD-998", "card_last4": "12x4"}]},
            format="json",
        )
        assert bad_card.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert bad_card.data["error_code"] == "ERR_INVALID_CARD_LAST4"

        # Non-cash overpayment blocked
        non_cash_over = api_client.post(
            f"/api/v2/pos/sales/{s1}/split-payments/",
            {
                "splits": [
                    {"tender_type": PaymentMethod.BKASH, "amount_bdt": "30.00", "gateway_reference": "BKS-OV-1"},
                    {"tender_type": PaymentMethod.CARD, "amount_bdt": "25.00", "gateway_reference": "CRD-OV-2"},
                ]
            },
            format="json",
        )
        assert non_cash_over.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert non_cash_over.data["error_code"] == "ERR_NON_CASH_OVERPAYMENT"

        # Pending gateway digital tender blocks finalize until verified!
        pend_res = api_client.post(
            f"/api/v2/pos/sales/{s1}/split-payments/",
            {
                "splits": [
                    {"tender_type": PaymentMethod.CASH, "amount_bdt": "20.00"},
                    {
                        "tender_type": PaymentMethod.BKASH,
                        "amount_bdt": "30.00",
                        "verification_status": PosPaymentVerificationStatus.PENDING_GATEWAY,
                    },
                ]
            },
            format="json",
        )
        assert pend_res.status_code == status.HTTP_201_CREATED
        bkash_att_id = pend_res.data["payment_attempts"][1]["attempt_id"]
        assert pend_res.data["payment_attempts"][1]["status"] == PosPaymentAttemptStatus.PENDING

        fin_unver = api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json")
        assert fin_unver.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert fin_unver.data["error_code"] == "ERR_UNVERIFIED_DIGITAL_PAYMENT"

        # Void & Verify workflow
        ver_ok = api_client.post(
            f"/api/v2/pos/sales/{s1}/verify-payment-attempt/",
            {"attempt_id": bkash_att_id, "gateway_reference": "BKS-CONF-1001"},
            format="json",
        )
        assert ver_ok.status_code == status.HTTP_200_OK
        assert ver_ok.data["paid_amount_bdt"] == "50.00"

        # Redundant extra cash rejected
        red_cash = api_client.post(
            f"/api/v2/pos/sales/{s1}/record-payment/",
            {"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00"},
            format="json",
        )
        assert red_cash.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert red_cash.data["error_code"] == "ERR_REDUNDANT_CASH_OVERPAYMENT"

        # Pre-final attempt can be voided with reason, replaced with Nagad -> duplicate gateway_reference blocked!
        void_ok = api_client.post(
            f"/api/v2/pos/sales/{s1}/void-payment-attempt/",
            {"attempt_id": bkash_att_id, "reason": "Customer switched to Nagad"},
            format="json",
        )
        assert void_ok.status_code == status.HTTP_200_OK
        assert void_ok.data["paid_amount_bdt"] == "20.00"

        ngd_ok = api_client.post(
            f"/api/v2/pos/sales/{s1}/record-payment/",
            {
                "tender_type": PaymentMethod.NAGAD,
                "amount_bdt": "30.00",
                "gateway_reference": "NGD-UNIQUE-77",
                "require_verified": True,
            },
            format="json",
        )
        assert ngd_ok.status_code == status.HTTP_201_CREATED
        assert api_client.post(f"/api/v2/pos/sales/{s1}/finalize/", {}, format="json").status_code == status.HTTP_200_OK

        # Duplicate TrxID across another sale -> 422 ERR_DUPLICATE_GATEWAY_REFERENCE!
        s2 = _open_cart_with_total(api_client, br_a1.branch_id, prod.product_id, qty=6)
        dup_ref = api_client.post(
            f"/api/v2/pos/sales/{s2}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.NAGAD, "amount_bdt": "30.00", "gateway_reference": "NGD-UNIQUE-77"}]},
            format="json",
        )
        assert dup_ref.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert dup_ref.data["error_code"] == "ERR_DUPLICATE_GATEWAY_REFERENCE"

        # Offline unverified digital payment blocked; offline manual TrxID allowed as OFFLINE_PENDING_VERIFY
        off_fail = api_client.post(
            f"/api/v2/pos/sales/{s2}/split-payments/",
            {
                "is_offline": True,
                "splits": [{"tender_type": PaymentMethod.BKASH, "amount_bdt": "30.00"}],
            },
            format="json",
        )
        assert off_fail.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert off_fail.data["error_code"] == "ERR_OFFLINE_DIGITAL_PAYMENT_REQUIRES_REF"

        off_ok = api_client.post(
            f"/api/v2/pos/sales/{s2}/split-payments/",
            {
                "is_offline": True,
                "splits": [
                    {"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00"},
                    {"tender_type": PaymentMethod.BKASH, "amount_bdt": "20.00", "gateway_reference": "SMS-OFF-9191"},
                ],
            },
            format="json",
        )
        assert off_ok.status_code == status.HTTP_201_CREATED
        assert off_ok.data["payment_attempts"][1]["verification_status"] == PosPaymentVerificationStatus.OFFLINE_PENDING_VERIFY
        fin_off = api_client.post(f"/api/v2/pos/sales/{s2}/finalize/", {"is_offline_sync": True}, format="json")
        assert fin_off.status_code == status.HTTP_200_OK
        assert PosPaymentJournalEntry.objects.filter(
            sale_id=s2, verification_status=PosPaymentVerificationStatus.OFFLINE_PENDING_VERIFY
        ).exists()

        # RBAC & Cross-Branch & Cross-Tenant Isolation
        api_client.force_authenticate(user=clerk_a)
        assert api_client.post(f"/api/v2/pos/sales/{s1}/split-payments/", {"splits": []}, format="json").status_code == status.HTTP_403_FORBIDDEN

        api_client.force_authenticate(user=cashier_a2)
        cross_br = api_client.post(
            f"/api/v2/pos/sales/{s2}/void-payment-attempt/",
            {"attempt_id": "x", "reason": "Cross branch"},
            format="json",
        )
        assert cross_br.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)

        api_client.force_authenticate(user=cashier_b)
        assert api_client.get(f"/api/v2/pos/sales/{s1}/payment-journals/").status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        assert api_client.get(f"/api/v2/pos/sales/close-reconciliation/?branch={br_a1.branch_id}").status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        )

        # Suspended tenant blocked on write
        ten_a.status = TenantLifecycleStatus.SUSPENDED
        ten_a.save(update_fields=["status", "updated_at"])
        api_client.force_authenticate(user=cashier)
        s_susp = api_client.post(
            f"/api/v2/pos/sales/{s1}/split-payments/",
            {"splits": [{"tender_type": PaymentMethod.CASH, "amount_bdt": "1.00"}]},
            format="json",
        )
        assert s_susp.status_code == status.HTTP_403_FORBIDDEN

        # BASIC plan supports full split payments & close reconciliation
        api_client.force_authenticate(user=cashier_bas)
        s_bas = _open_cart_with_total(api_client, env["br_bas"].branch_id, prod.product_id, qty=4)  # 20.00 BDT
        bas_split = api_client.post(
            f"/api/v2/pos/sales/{s_bas}/split-payments/",
            {
                "splits": [
                    {"tender_type": PaymentMethod.BKASH, "amount_bdt": "10.00", "gateway_reference": "BKS-BAS-1"},
                    {"tender_type": PaymentMethod.CASH, "amount_bdt": "10.00"},
                ]
            },
            format="json",
        )
        assert bas_split.status_code == status.HTTP_201_CREATED
        assert api_client.post(f"/api/v2/pos/sales/{s_bas}/finalize/", {}, format="json").status_code == status.HTTP_200_OK
        assert api_client.get(f"/api/v2/pos/sales/close-reconciliation/?branch={env['br_bas'].branch_id}").status_code == status.HTTP_200_OK


_sqlite_p229_lock = threading.Lock()


@pytest.mark.django_db(transaction=True)
def test_concurrent_split_payment_idempotency_prevents_double_posting():
    """Two simultaneous requests sharing one batch_idempotency_key cannot create duplicate payment legs."""
    ten = _mk_tenant("Concurrent Split Pharma", "p229c", PlanTier.STANDARD)
    br = _mk_branch(ten, "Dhanmondi", "c1")
    cashier = _mk_user(ten, br, UserRole.CASHIER, "cc")
    prod = _mk_napa_product()
    today = timezone.now().date()
    BatchStock.objects.create(
        batch_id=f"bat-cc229-{uuid.uuid4().hex[:5]}",
        tenant=ten,
        branch=br,
        product=prod,
        batch_number="LOT-CC229",
        expiry_date=today + datetime.timedelta(days=120),
        purchase_date=today,
        quantity_base_units=100,
        cost_per_unit=Decimal("2.00"),
        status=BatchStatus.ACTIVE,
        is_active=True,
    )

    client = APIClient()
    client.force_authenticate(user=cashier)
    sale_id = _open_cart_with_total(client, br.branch_id, prod.product_id, qty=10)
    sale_obj = PosSaleOrder.objects.get(pk=sale_id)
    shared_batch = f"cc-split-{uuid.uuid4().hex[:8]}"

    def _submit(_: int) -> bool:
        payload = {
            "batch_idempotency_key": shared_batch,
            "splits": [
                {"tender_type": PaymentMethod.CASH, "amount_bdt": "20.00"},
                {"tender_type": PaymentMethod.BKASH, "amount_bdt": "30.00", "gateway_reference": "BKS-CC-229"},
            ],
        }

        def _exec() -> bool:
            _, _, created = PosPaymentSplitService.record_split_batch(cashier, sale_obj, payload)
            return created

        if connection.vendor == "sqlite":
            with _sqlite_p229_lock:
                return _exec()
        try:
            return _exec()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_submit, [1, 2]))

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert PosPaymentAttempt.objects.filter(sale_id=sale_id).count() == 2
    sale_obj.refresh_from_db()
    assert sale_obj.paid_amount_bdt == Decimal("50.00")
