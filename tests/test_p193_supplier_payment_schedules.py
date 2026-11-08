"""
backend/tests/test_p193_supplier_payment_schedules.py
Architecture Decision: ADR-0193 (P193).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Integration tests for P193 Supplier Payment Schedules & AP Ledger Consistency:
1. Create payment schedule -> SCHEDULED status, settled=0, remaining=planned.
2. Record partial installment -> PARTIALLY_SETTLED, AP ledger debited.
3. Record final installment -> SETTLED, remaining=0.
4. Overpayment guard -> payment exceeding remaining due raises HTTP 400 ERR_EXCEEDS_SCHEDULED_DUE.
5. Cannot disburse against settled schedule -> HTTP 400 ERR_SCHEDULE_NOT_PAYABLE.
6. Audited reschedule due date -> updates date, records reschedule reason.
7. Reschedule requires mandatory reason -> HTTP 400 ERR_RESCHEDULE_REASON_REQUIRED.
8. Cancel undisbursed schedule -> transitions to CANCELLED.
9. Cannot cancel disbursed schedule -> HTTP 400 ERR_CANNOT_CANCEL_DISBURSED_SCHEDULE.
10. RBAC and cross-tenant isolation -> Cashier blocked (403), foreign tenant blocked (404).
"""

from __future__ import annotations

import datetime
import random
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.core.models import (
    Branch,
    PaymentMethodPreference,
    PaymentSchedulePriority,
    PaymentScheduleStatus,
    Supplier,
    SupplierLedgerEntry,
    SupplierPaymentInstallment,
    SupplierPaymentSchedule,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import PharmacyUser, UserRole


@pytest.mark.django_db(transaction=True)
class TestSupplierPaymentScheduleIntegration:

    def setup_method(self):
        self.client = APIClient()
        self.suffix = uuid.uuid4().hex[:6]

        # 1. Primary Tenant & Branch
        self.tenant = Tenant.objects.create(
            tenant_id=f"ten-p193-{self.suffix}",
            name="Square Health Care",
            subdomain=f"square-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.branch = Branch.objects.create(
            branch_id=f"br-sq-{self.suffix}",
            tenant=self.tenant,
            name="Main Dispensary",
        )

        # 2. Foreign Tenant & Branch
        self.foreign_tenant = Tenant.objects.create(
            tenant_id=f"ten-foreign-{self.suffix}",
            name="Popular Pharma Ltd",
            subdomain=f"popular-{self.suffix}",
            status=TenantLifecycleStatus.ACTIVE,
        )
        self.foreign_branch = Branch.objects.create(
            branch_id=f"br-pop-{self.suffix}",
            tenant=self.foreign_tenant,
            name="Popular Central",
        )

        # 3. Users
        self.manager = PharmacyUser.objects.create_user(
            email=f"mgr_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Procurement Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.cashier = PharmacyUser.objects.create_user(
            email=f"cashier_{self.suffix}@square.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Front Desk Cashier",
            password="StrongPassword123!",
            role=UserRole.CASHIER,
            tenant_id=self.tenant.tenant_id,
            branch_id=self.branch.branch_id,
        )

        self.foreign_manager = PharmacyUser.objects.create_user(
            email=f"foreign_{self.suffix}@popular.com",
            phone_number=f"+880171{random.randint(1000000, 9999999)}",
            full_name="Foreign Manager",
            password="StrongPassword123!",
            role=UserRole.BRANCH_MANAGER,
            tenant_id=self.foreign_tenant.tenant_id,
            branch_id=self.foreign_branch.branch_id,
        )

        # 4. Supplier
        self.supplier = Supplier.objects.create(
            supplier_id=f"sup-sq-{self.suffix}",
            tenant=self.tenant,
            name="Square Pharmaceuticals Ltd",
            contact_person="M. Rahman",
            phone="+8801700000000",
            email="supply@squarepharma.com.bd",
            is_active=True,
        )

        # Pre-seed AP ledger balance with ৳80,000 from previous goods receipt invoice
        SupplierLedgerEntry.objects.create(
            posting_id=f"sup-init-{self.suffix}",
            tenant=self.tenant,
            branch=self.branch,
            supplier=self.supplier,
            posting_type=SupplierPostingType.GRN_INVOICE,
            reference_number="INV-INIT-193",
            debit_amount=Decimal("0.0000"),
            credit_amount=Decimal("80000.0000"),
            balance_after=Decimal("80000.0000"),
            currency="BDT",
        )

    def test_01_create_payment_schedule(self):
        """Creates a supplier payment schedule with planned due date and priority."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=14)).date().isoformat()
        payload = {
            "supplier_id": self.supplier.supplier_id,
            "branch_id": self.branch.branch_id,
            "due_date": due_date,
            "planned_amount": "50000.0000",
            "priority": "HIGH",
            "payment_method_preference": "BANK_TRANSFER",
            "reference_note": "Invoice INV-SQ-2026-99",
            "notes": "Critical settlement for insulin supply continuity",
        }

        resp = self.client.post("/api/v2/procurement/payment-schedules/", data=payload, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()

        assert data["status"] == "SCHEDULED"
        assert Decimal(data["planned_amount"]) == Decimal("50000.0000")
        assert Decimal(data["settled_amount"]) == Decimal("0.0000")
        assert Decimal(data["remaining_amount"]) == Decimal("50000.0000")
        assert data["priority"] == "HIGH"
        assert data["payment_method_preference"] == "BANK_TRANSFER"

    def test_02_record_partial_installment_payment(self):
        """Disbursing a partial installment transitions schedule to PARTIALLY_SETTLED and reduces AP balance."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date, "planned_amount": "50000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        # Disburse ৳20,000 partial payment
        pay_payload = {
            "amount_paid": "20000.0000",
            "payment_method": "BANK_TRANSFER",
            "payment_reference": f"EFT-SQ-{self.suffix.upper()}",
            "notes": "First installment via BEFTN",
        }
        pay_resp = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/record-payment/",
            data=pay_payload,
            format="json",
        )
        assert pay_resp.status_code == status.HTTP_200_OK
        data = pay_resp.json()

        assert data["status"] == "PARTIALLY_SETTLED"
        assert Decimal(data["settled_amount"]) == Decimal("20000.0000")
        assert Decimal(data["remaining_amount"]) == Decimal("30000.0000")
        assert len(data["installments"]) == 1

        # Verify AP ledger balance: 80,000 - 20,000 = 60,000
        ledger = SupplierLedgerEntry.objects.filter(
            tenant=self.tenant,
            supplier=self.supplier,
            posting_type=SupplierPostingType.PAYMENT,
        ).first()

        assert ledger is not None
        assert ledger.debit_amount == Decimal("20000.0000")
        assert ledger.balance_after == Decimal("60000.0000")

    def test_03_record_full_settlement_payment(self):
        """Second installment settling the remaining balance transitions status to SETTLED."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date, "planned_amount": "50000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        # 1st installment: ৳20,000
        self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/record-payment/",
            data={"amount_paid": "20000.0000", "payment_reference": f"P1-{self.suffix}"},
            format="json",
        )

        # 2nd installment: ৳30,000 (remaining balance)
        pay_resp2 = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/record-payment/",
            data={"amount_paid": "30000.0000", "payment_reference": f"P2-{self.suffix}"},
            format="json",
        )
        assert pay_resp2.status_code == status.HTTP_200_OK
        data2 = pay_resp2.json()

        assert data2["status"] == "SETTLED"
        assert Decimal(data2["settled_amount"]) == Decimal("50000.0000")
        assert Decimal(data2["remaining_amount"]) == Decimal("0.0000")
        assert len(data2["installments"]) == 2

    def test_04_overpayment_guard(self):
        """Attempting to disburse an amount exceeding remaining due raises ERR_EXCEEDS_SCHEDULED_DUE."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date, "planned_amount": "10000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        # Attempt to disburse ৳15,000 against ৳10,000 due
        resp = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/record-payment/",
            data={"amount_paid": "15000.0000", "payment_reference": "ERR-OVER"},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error_code"] == "ERR_EXCEEDS_SCHEDULED_DUE"

    def test_05_cannot_pay_settled_schedule(self):
        """Attempting to disburse payment against an already SETTLED schedule throws ERR_SCHEDULE_NOT_PAYABLE."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date, "planned_amount": "5000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        # Full settlement
        self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/record-payment/",
            data={"amount_paid": "5000.0000", "payment_reference": "FULL-PAY"},
            format="json",
        )

        # Attempt additional disbursement
        resp_again = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/record-payment/",
            data={"amount_paid": "1000.0000", "payment_reference": "EXTRA-FAIL"},
            format="json",
        )
        assert resp_again.status_code == status.HTTP_400_BAD_REQUEST
        assert resp_again.json()["error_code"] == "ERR_SCHEDULE_NOT_PAYABLE"

    def test_06_audited_reschedule_due_date(self):
        """Audited reschedule updates due date, increments counter, and stores reason."""
        self.client.force_authenticate(user=self.manager)
        due_date1 = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date1, "planned_amount": "25000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        # Reschedule to 14 days later
        due_date2 = (timezone.now() + datetime.timedelta(days=21)).date().isoformat()
        resched_resp = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/reschedule/",
            data={"due_date": due_date2, "reason": "Agreement with territory manager to align with monthly cycle"},
            format="json",
        )
        assert resched_resp.status_code == status.HTTP_200_OK
        data = resched_resp.json()

        assert data["due_date"] == due_date2
        assert data["reschedule_count"] == 1
        assert "monthly cycle" in data["last_reschedule_reason"]
        assert data["last_rescheduled_at"] is not None

    def test_07_reschedule_requires_reason(self):
        """Rescheduling without a justification reason throws ERR_RESCHEDULE_REASON_REQUIRED."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date, "planned_amount": "10000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        resp = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/reschedule/",
            data={"due_date": (timezone.now() + datetime.timedelta(days=10)).date().isoformat(), "reason": ""},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error_code"] == "ERR_RESCHEDULE_REASON_REQUIRED"

    def test_08_cancel_undisbursed_schedule(self):
        """Cancels an undisbursed payment schedule."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date, "planned_amount": "15000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        cancel_resp = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/cancel/",
            data={"reason": "Cancelled due to supplier invoice adjustment"},
            format="json",
        )
        assert cancel_resp.status_code == status.HTTP_200_OK
        assert cancel_resp.json()["status"] == "CANCELLED"

    def test_09_cannot_cancel_disbursed_schedule(self):
        """Attempting to cancel a schedule that already has disbursed payments throws ERR_CANNOT_CANCEL_DISBURSED_SCHEDULE."""
        self.client.force_authenticate(user=self.manager)
        due_date = (timezone.now() + datetime.timedelta(days=7)).date().isoformat()
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": due_date, "planned_amount": "20000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        # Disburse ৳5,000
        self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/record-payment/",
            data={"amount_paid": "5000.0000", "payment_reference": "DISB-1"},
            format="json",
        )

        # Attempt to cancel
        cancel_resp = self.client.post(
            f"/api/v2/procurement/payment-schedules/{schedule_id}/cancel/",
            data={"reason": "Should fail"},
            format="json",
        )
        assert cancel_resp.status_code == status.HTTP_400_BAD_REQUEST
        assert cancel_resp.json()["error_code"] == "ERR_CANNOT_CANCEL_DISBURSED_SCHEDULE"

    def test_10_rbac_and_cross_tenant_isolation(self):
        """Cashier role is blocked from payment schedules, and foreign tenants cannot access records."""
        # 1. Cashier attempts to create schedule -> 403 Forbidden
        self.client.force_authenticate(user=self.cashier)
        resp_csh = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": "2026-11-20", "planned_amount": "1000.0000"},
            format="json",
        )
        assert resp_csh.status_code == status.HTTP_403_FORBIDDEN
        assert resp_csh.json()["error_code"] == "ERR_STAFF_ROLE_DENIED"

        # 2. Manager creates schedule
        self.client.force_authenticate(user=self.manager)
        create_resp = self.client.post(
            "/api/v2/procurement/payment-schedules/",
            data={"supplier_id": self.supplier.supplier_id, "due_date": "2026-11-20", "planned_amount": "1000.0000"},
            format="json",
        )
        schedule_id = create_resp.json()["schedule_id"]

        # 3. Foreign tenant attempts to access schedule -> 404 Not Found
        self.client.force_authenticate(user=self.foreign_manager)
        resp_foreign = self.client.get(f"/api/v2/procurement/payment-schedules/{schedule_id}/")
        assert resp_foreign.status_code == status.HTTP_404_NOT_FOUND
