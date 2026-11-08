"""
backend/apps/core/supplier_payment_schedule_service.py
Architecture Decision: ADR-0193 (P193).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

Service layer for Supplier Payment Schedules:
- Schedules supplier dues, settlement dates, priority queues, and partial payments.
- Disburses installment payments and posts immutable AP SupplierLedgerEntry debits.
- Enforces anti-overpayment guards and audited due date rescheduling.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    GoodsReceiptNote,
    PaymentMethodPreference,
    PaymentSchedulePriority,
    PaymentScheduleStatus,
    PurchaseOrder,
    Supplier,
    SupplierInvoice,
    SupplierLedgerEntry,
    SupplierPaymentInstallment,
    SupplierPaymentSchedule,
    SupplierPostingType,
    Tenant,
    TenantLifecycleStatus,
)


class SupplierPaymentScheduleService:

    _ALLOWED_PROCUREMENT_ROLES = {
        "BRANCH_MANAGER",
        "PHARMACY_OWNER",
        "SUPERADMIN",
        "STAFF_PHARMACIST",
        "PROCUREMENT_OFFICER",
        "INVENTORY_CLERK",
        "ACCOUNTANT",
        "AUDITOR",
    }

    @classmethod
    def _assert_procurement_role(cls, actor_user: Any) -> None:
        role = getattr(actor_user, "role", None)
        if not role:
            return
        role_str = str(role).upper()
        if role_str == "CASHIER" or role_str not in cls._ALLOWED_PROCUREMENT_ROLES:
            raise PermissionDenied(
                {
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "detail": "Staff role does not have permission to manage supplier payment schedules.",
                }
            )

    @classmethod
    def _assert_tenant_active(cls, tenant_id: str) -> Tenant:
        try:
            tenant = Tenant.objects.get(tenant_id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFound({"error_code": "ERR_TENANT_NOT_FOUND", "detail": f"Tenant {tenant_id} not found."})

        if tenant.status == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "detail": "Tenant account subscription is suspended. Payment scheduling locked.",
                }
            )
        return tenant

    @classmethod
    def serialize_installment(cls, inst: SupplierPaymentInstallment) -> Dict[str, Any]:
        return {
            "installment_id": inst.installment_id,
            "amount_paid": str(inst.amount_paid),
            "payment_method": inst.payment_method,
            "payment_reference": inst.payment_reference,
            "transaction_date": inst.transaction_date.isoformat() if inst.transaction_date else None,
            "ledger_entry_id": inst.ledger_entry_id,
            "notes": inst.notes,
            "disbursed_by": str(getattr(inst.disbursed_by, "user_id", inst.disbursed_by_id or "")) if inst.disbursed_by_id else None,
            "created_at": inst.created_at.isoformat() if inst.created_at else None,
        }

    @classmethod
    def serialize_schedule(cls, sps: SupplierPaymentSchedule) -> Dict[str, Any]:
        # Check overdue dynamically
        current_status = sps.status
        today = timezone.now().date()
        if current_status in (PaymentScheduleStatus.SCHEDULED, PaymentScheduleStatus.PARTIALLY_SETTLED) and sps.due_date < today:
            current_status = PaymentScheduleStatus.OVERDUE

        return {
            "schedule_id": sps.schedule_id,
            "schedule_number": sps.schedule_number,
            "tenant_id": sps.tenant_id,
            "branch_id": sps.branch_id,
            "supplier_id": sps.supplier_id,
            "supplier_name": sps.supplier_name or (sps.supplier.name if sps.supplier else ""),
            "supplier_invoice_id": sps.supplier_invoice_id,
            "purchase_order_id": sps.purchase_order_id,
            "goods_receipt_id": sps.goods_receipt_id,
            "reference_note": sps.reference_note,
            "due_date": sps.due_date.isoformat() if sps.due_date else None,
            "planned_amount": str(sps.planned_amount),
            "settled_amount": str(sps.settled_amount),
            "remaining_amount": str(sps.remaining_amount),
            "status": current_status,
            "priority": sps.priority,
            "payment_method_preference": sps.payment_method_preference,
            "cheque_reference": sps.cheque_reference,
            "cheque_date": sps.cheque_date.isoformat() if sps.cheque_date else None,
            "reschedule_count": sps.reschedule_count,
            "last_reschedule_reason": sps.last_reschedule_reason,
            "last_rescheduled_at": sps.last_rescheduled_at.isoformat() if sps.last_rescheduled_at else None,
            "notes": sps.notes,
            "created_by": str(getattr(sps.created_by, "user_id", sps.created_by_id or "")) if sps.created_by_id else None,
            "is_active": sps.is_active,
            "installments": [cls.serialize_installment(i) for i in sps.installments.all().order_by("-created_at")],
            "created_at": sps.created_at.isoformat() if sps.created_at else None,
            "updated_at": sps.updated_at.isoformat() if sps.updated_at else None,
        }

    @classmethod
    def create_schedule(
        cls,
        tenant_id: str,
        actor_user: Any,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        supplier_id = str(data.get("supplier_id", "")).strip()
        if not supplier_id:
            raise ValidationError({"error_code": "ERR_MISSING_SUPPLIER", "detail": "Supplier ID is required."})

        try:
            supplier = Supplier.objects.get(supplier_id=supplier_id, tenant_id=tenant_id)
        except Supplier.DoesNotExist:
            raise NotFound({"error_code": "ERR_SUPPLIER_NOT_FOUND", "detail": f"Supplier {supplier_id} not found."})

        due_date_raw = data.get("due_date")
        if not due_date_raw:
            raise ValidationError({"error_code": "ERR_MISSING_DUE_DATE", "detail": "Settlement due date is required."})

        if isinstance(due_date_raw, str):
            due_date = datetime.date.fromisoformat(due_date_raw)
        else:
            due_date = due_date_raw

        planned_amt = Decimal(str(data.get("planned_amount", "0.0000"))).quantize(Decimal("0.0001"))
        if planned_amt <= Decimal("0.0000"):
            raise ValidationError(
                {"error_code": "ERR_INVALID_PLANNED_AMOUNT", "detail": "Planned settlement due amount must be positive."}
            )

        branch_id = data.get("branch_id") or getattr(actor_user, "branch_id", None)
        branch = tenant.branches.filter(branch_id=branch_id).first() if branch_id else None

        # Links
        invoice = None
        inv_id = data.get("supplier_invoice_id")
        if inv_id:
            invoice = SupplierInvoice.objects.filter(invoice_id=inv_id, tenant_id=tenant_id).first()

        po = None
        po_id = data.get("purchase_order_id")
        if po_id:
            po = PurchaseOrder.objects.filter(po_id=po_id, tenant_id=tenant_id).first()

        grn = None
        grn_id = data.get("goods_receipt_id")
        if grn_id:
            grn = GoodsReceiptNote.objects.filter(grn_id=grn_id, tenant_id=tenant_id).first()

        ref_note = str(data.get("reference_note") or (invoice.invoice_number if invoice else "")).strip()

        priority = str(data.get("priority", PaymentSchedulePriority.MEDIUM)).upper()
        if priority not in PaymentSchedulePriority.values:
            priority = PaymentSchedulePriority.MEDIUM

        method_pref = str(data.get("payment_method_preference", PaymentMethodPreference.BANK_TRANSFER)).upper()
        if method_pref not in PaymentMethodPreference.values:
            method_pref = PaymentMethodPreference.BANK_TRANSFER

        now = timezone.now()
        sched_id = f"sps-{uuid.uuid4().hex[:12]}"
        sched_num = f"SPS-{now.strftime('%Y%m')}-{uuid.uuid4().hex[:4].upper()}"

        with transaction.atomic():
            sps = SupplierPaymentSchedule.objects.create(
                schedule_id=sched_id,
                schedule_number=sched_num,
                tenant=tenant,
                branch=branch,
                supplier=supplier,
                supplier_name=supplier.name,
                supplier_invoice=invoice,
                purchase_order=po,
                goods_receipt=grn,
                reference_note=ref_note,
                due_date=due_date,
                planned_amount=planned_amt,
                settled_amount=Decimal("0.0000"),
                remaining_amount=planned_amt,
                status=PaymentScheduleStatus.SCHEDULED,
                priority=priority,
                payment_method_preference=method_pref,
                cheque_reference=str(data.get("cheque_reference", "")).strip(),
                cheque_date=data.get("cheque_date"),
                notes=str(data.get("notes", "")).strip(),
                created_by=actor_user if hasattr(actor_user, "user_id") else None,
                is_active=True,
            )

        return cls.serialize_schedule(sps)

    @classmethod
    def record_installment_payment(
        cls,
        tenant_id: str,
        actor_user: Any,
        schedule_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            sps = SupplierPaymentSchedule.objects.select_for_update().get(schedule_id=schedule_id, tenant_id=tenant_id)
        except SupplierPaymentSchedule.DoesNotExist:
            raise NotFound({"error_code": "ERR_SCHEDULE_NOT_FOUND", "detail": f"Payment schedule {schedule_id} not found."})

        if sps.status in (PaymentScheduleStatus.SETTLED, PaymentScheduleStatus.CANCELLED):
            raise ValidationError(
                {
                    "error_code": "ERR_SCHEDULE_NOT_PAYABLE",
                    "detail": f"Cannot disburse payment against schedule in {sps.status} status.",
                }
            )

        amt_paid = Decimal(str(data.get("amount_paid", "0.0000"))).quantize(Decimal("0.0001"))
        if amt_paid <= Decimal("0.0000"):
            raise ValidationError({"error_code": "ERR_INVALID_PAYMENT_AMOUNT", "detail": "Disbursement amount must be positive."})

        if amt_paid > sps.remaining_amount:
            raise ValidationError(
                {
                    "error_code": "ERR_EXCEEDS_SCHEDULED_DUE",
                    "detail": f"Payment amount ৳{amt_paid} exceeds remaining scheduled due of ৳{sps.remaining_amount}.",
                }
            )

        pay_ref = str(data.get("payment_reference", "")).strip()
        if not pay_ref:
            raise ValidationError({"error_code": "ERR_MISSING_PAYMENT_REF", "detail": "Payment reference voucher / MR is required."})

        method = str(data.get("payment_method", sps.payment_method_preference)).upper()
        if method not in PaymentMethodPreference.values:
            method = PaymentMethodPreference.BANK_TRANSFER

        tx_date_raw = data.get("transaction_date")
        if tx_date_raw:
            tx_date = datetime.date.fromisoformat(str(tx_date_raw)) if isinstance(tx_date_raw, str) else tx_date_raw
        else:
            tx_date = timezone.now().date()

        with transaction.atomic():
            # 1. Post Accounts Payable Disbursement to SupplierLedgerEntry (reduces AP liability)
            last_entry = (
                SupplierLedgerEntry.objects.filter(tenant=tenant, supplier=sps.supplier)
                .order_by("-posted_at", "-posting_id")
                .first()
            )
            prev_balance = last_entry.balance_after if last_entry else Decimal("0.0000")
            new_balance = max(Decimal("0.0000"), prev_balance - amt_paid)

            posting_id = f"sup-pay-{uuid.uuid4().hex[:12]}"
            ledger_entry = SupplierLedgerEntry.objects.create(
                posting_id=posting_id,
                tenant=tenant,
                branch=sps.branch,
                supplier=sps.supplier,
                posting_type=SupplierPostingType.PAYMENT,
                reference_number=pay_ref,
                debit_amount=amt_paid,
                credit_amount=Decimal("0.0000"),
                balance_after=new_balance,
                currency="BDT",
                notes=f"Disbursement against Schedule {sps.schedule_number} (Ref: {pay_ref})",
                posted_by=actor_user if hasattr(actor_user, "user_id") else None,
            )

            # 2. Record Installment
            inst = SupplierPaymentInstallment.objects.create(
                installment_id=f"spi-{uuid.uuid4().hex[:12]}",
                schedule=sps,
                tenant=tenant,
                amount_paid=amt_paid,
                payment_method=method,
                payment_reference=pay_ref,
                transaction_date=tx_date,
                ledger_entry=ledger_entry,
                notes=str(data.get("notes", "")).strip(),
                disbursed_by=actor_user if hasattr(actor_user, "user_id") else None,
            )

            # 3. Update Schedule balances & status
            sps.settled_amount += amt_paid
            sps.remaining_amount -= amt_paid
            if sps.remaining_amount <= Decimal("0.0000"):
                sps.remaining_amount = Decimal("0.0000")
                sps.status = PaymentScheduleStatus.SETTLED
            else:
                sps.status = PaymentScheduleStatus.PARTIALLY_SETTLED

            sps.save(update_fields=["settled_amount", "remaining_amount", "status", "updated_at"])

        AuditLogEntry.objects.create(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            tenant_id=str(tenant.tenant_id),
            user_id=getattr(actor_user, "user_id", str(actor_user)),
            actor_email=str(getattr(actor_user, "email", "manager@pharma.com")),
            actor_role=str(getattr(actor_user, "role", "BRANCH_MANAGER")),
            action="SUPPLIER_PAYMENT_DISBURSED",
            resource_type="SupplierPaymentSchedule",
            resource_id=sps.schedule_id,
            ip_address="127.0.0.1",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "schedule_id": sps.schedule_id,
                "schedule_number": sps.schedule_number,
                "amount_paid": str(amt_paid),
                "remaining_amount": str(sps.remaining_amount),
                "posting_id": ledger_entry.posting_id,
            },
        )

        return cls.serialize_schedule(sps)

    @classmethod
    def reschedule(
        cls,
        tenant_id: str,
        actor_user: Any,
        schedule_id: str,
        new_due_date_raw: Any,
        reason: str,
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        reason_str = str(reason or "").strip()
        if not reason_str:
            raise ValidationError(
                {"error_code": "ERR_RESCHEDULE_REASON_REQUIRED", "detail": "Reschedule justification reason is mandatory."}
            )

        if not new_due_date_raw:
            raise ValidationError({"error_code": "ERR_MISSING_DUE_DATE", "detail": "New due date is required."})

        new_due_date = (
            datetime.date.fromisoformat(str(new_due_date_raw))
            if isinstance(new_due_date_raw, str)
            else new_due_date_raw
        )

        try:
            sps = SupplierPaymentSchedule.objects.select_for_update().get(schedule_id=schedule_id, tenant_id=tenant_id)
        except SupplierPaymentSchedule.DoesNotExist:
            raise NotFound({"error_code": "ERR_SCHEDULE_NOT_FOUND", "detail": f"Payment schedule {schedule_id} not found."})

        if sps.status in (PaymentScheduleStatus.SETTLED, PaymentScheduleStatus.CANCELLED):
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_RESCHEDULE_CLOSED",
                    "detail": f"Cannot reschedule a schedule in {sps.status} status.",
                }
            )

        sps.due_date = new_due_date
        sps.reschedule_count += 1
        sps.last_reschedule_reason = reason_str
        sps.last_rescheduled_at = timezone.now()
        sps.save(update_fields=["due_date", "reschedule_count", "last_reschedule_reason", "last_rescheduled_at", "updated_at"])

        return cls.serialize_schedule(sps)

    @classmethod
    def cancel_schedule(
        cls,
        tenant_id: str,
        actor_user: Any,
        schedule_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        cls._assert_procurement_role(actor_user)
        tenant = cls._assert_tenant_active(tenant_id)

        try:
            sps = SupplierPaymentSchedule.objects.select_for_update().get(schedule_id=schedule_id, tenant_id=tenant_id)
        except SupplierPaymentSchedule.DoesNotExist:
            raise NotFound({"error_code": "ERR_SCHEDULE_NOT_FOUND", "detail": f"Payment schedule {schedule_id} not found."})

        if sps.settled_amount > Decimal("0.0000"):
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_CANCEL_DISBURSED_SCHEDULE",
                    "detail": f"Cannot cancel schedule with ৳{sps.settled_amount} already disbursed. Disbursed payments must be reconciled.",
                }
            )

        sps.status = PaymentScheduleStatus.CANCELLED
        sps.notes = f"{sps.notes} | Cancelled: {reason}".strip(" |")
        sps.save(update_fields=["status", "notes", "updated_at"])
        return cls.serialize_schedule(sps)

    @classmethod
    def get_schedule(cls, tenant_id: str, schedule_id: str) -> Dict[str, Any]:
        cls._assert_tenant_active(tenant_id)
        try:
            sps = SupplierPaymentSchedule.objects.select_related(
                "supplier", "supplier_invoice", "purchase_order", "goods_receipt", "created_by"
            ).get(schedule_id=schedule_id, tenant_id=tenant_id)
        except SupplierPaymentSchedule.DoesNotExist:
            raise NotFound({"error_code": "ERR_SCHEDULE_NOT_FOUND", "detail": f"Payment schedule {schedule_id} not found."})

        return cls.serialize_schedule(sps)

    @classmethod
    def list_schedules(
        cls,
        tenant_id: str,
        supplier_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        priority: Optional[str] = None,
        branch_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cls._assert_tenant_active(tenant_id)
        qs = SupplierPaymentSchedule.objects.filter(tenant_id=tenant_id).select_related(
            "supplier", "supplier_invoice", "purchase_order", "goods_receipt", "created_by"
        )

        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if priority:
            qs = qs.filter(priority=priority.upper())
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        return [cls.serialize_schedule(s) for s in qs]
