"""
backend/apps/core/pos_customer_due_service.py
Architecture Decision: ADR-0230 (P230).
Phase 12: POS, Sales Orders, Checkout & Returns.

Single authoritative service for:
- Approved customer credit sales & credit limit enforcement (concurrency-safe SELECT FOR UPDATE)
- Credit approval / suspension workflows (Owner/Manager/Finance RBAC + CUSTOMER_LEDGER entitlement)
- Receivable (`CustomerDueRecord`) creation, reversal, and partial-return credit reconciliation
- Subsequent due collection (`CustomerDueCollection`) & audited collection reversal
  posting Dr Clearing/Cash -> Cr `AR_CUSTOMER_DUE` (guaranteeing ZERO double revenue)
"""

from __future__ import annotations

import datetime
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.models import (
    CashierRegisterAssignment,
    CustomerDueCollection,
    CustomerDueCollectionStatus,
    CustomerDueRecord,
    DueStatus,
    PaymentMethod,
    PosCustomer,
    PosCustomerCreditStatus,
    PosPaymentAttempt,
    PosPaymentAttemptStatus,
    PosPaymentJournalEntry,
    PosPaymentJournalEntryType,
    PosPaymentVerificationStatus,
    PosSaleLifecycleState,
    PosSaleOrder,
    RegisterAssignmentStatus,
    SaleInvoice,
)

MONEY_Q = Decimal("0.01")
CARD_LAST4_RE = re.compile(r"^\d{4}$")

DIGITAL_TENDERS = {
    PaymentMethod.BKASH,
    PaymentMethod.NAGAD,
    PaymentMethod.ROCKET,
    PaymentMethod.UPAY,
    PaymentMethod.CARD,
    PaymentMethod.BANK_TRANSFER,
}

TENDER_CLEARING_ACCOUNTS = {
    PaymentMethod.CASH: "CASH_DRAWER",
    PaymentMethod.BKASH: "BKASH_CLEARING",
    PaymentMethod.NAGAD: "NAGAD_CLEARING",
    PaymentMethod.ROCKET: "ROCKET_CLEARING",
    PaymentMethod.UPAY: "UPAY_CLEARING",
    PaymentMethod.CARD: "CARD_POS_CLEARING",
    PaymentMethod.BANK_TRANSFER: "BANK_TRANSFER_CLEARING",
}

TENDER_REFUND_CLEARING_ACCOUNTS = {
    PaymentMethod.CASH: "CASH_DRAWER_REFUND",
    PaymentMethod.BKASH: "BKASH_REFUND_CLEARING",
    PaymentMethod.NAGAD: "NAGAD_REFUND_CLEARING",
    PaymentMethod.ROCKET: "ROCKET_REFUND_CLEARING",
    PaymentMethod.UPAY: "UPAY_REFUND_CLEARING",
    PaymentMethod.CARD: "CARD_REFUND_CLEARING",
    PaymentMethod.BANK_TRANSFER: "BANK_TRANSFER_REFUND_CLEARING",
}


def q2(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else "0.00")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


class PosCustomerDueService:
    @classmethod
    def compute_available_credit(cls, customer: Optional[PosCustomer]) -> Decimal:
        if not customer or customer.is_archived or not customer.is_active:
            return Decimal("0.00")
        if not customer.is_credit_approved or customer.credit_status != PosCustomerCreditStatus.APPROVED:
            return Decimal("0.00")
        return max(Decimal("0.00"), q2(customer.credit_limit_bdt - customer.current_due_bdt))

    @classmethod
    def assert_customer_due_eligible_in_tx(
        cls,
        *,
        tenant: Any,
        sale: Optional[PosSaleOrder] = None,
        customer_id: str = "",
        additional_due_bdt: Decimal = Decimal("0.00"),
    ) -> PosCustomer:
        """
        Locks `PosCustomer` (`SELECT FOR UPDATE`), checks feature entitlement, approval status,
        and enforces `current_due + additional_due <= credit_limit_bdt` even for 0.00 credit limit.
        """
        from apps.core.pos_domain_service import PosDomainService

        PosDomainService.assert_tenant_active(tenant)
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "credit/due checkout")

        resolved_cust_id = str(customer_id or (sale.customer_id if sale else "") or "").strip()
        if not resolved_cust_id:
            raise ValidationError(
                {
                    "error_code": "ERR_APPROVED_CUSTOMER_REQUIRED",
                    "customer": "An approved PosCustomer account must be attached before recording a DUE credit sale.",
                }
            )

        cust = PosCustomer.objects.select_for_update().filter(
            customer_id=resolved_cust_id, tenant=tenant
        ).first()
        if not cust:
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_NOT_FOUND",
                    "customer": f"Customer '{resolved_cust_id}' was not found in this tenant.",
                }
            )
        if cust.is_archived or not cust.is_active:
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_INACTIVE_OR_ARCHIVED",
                    "customer": f"Customer '{cust.full_name}' is inactive or archived.",
                }
            )
        if cust.credit_status == PosCustomerCreditStatus.SUSPENDED:
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_CREDIT_SUSPENDED",
                    "customer": (
                        f"Customer '{cust.full_name}' credit facility is suspended"
                        f"{': ' + cust.credit_suspended_reason if cust.credit_suspended_reason else ''}."
                    ),
                }
            )
        if not cust.is_credit_approved or cust.credit_status != PosCustomerCreditStatus.APPROVED:
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_CREDIT_NOT_APPROVED",
                    "customer": (
                        f"Customer '{cust.full_name}' is not approved for credit/due sales "
                        f"(status: {cust.credit_status or PosCustomerCreditStatus.UNAPPROVED})."
                    ),
                }
            )

        projected_due = q2(cust.current_due_bdt + additional_due_bdt)
        if cust.credit_limit_bdt <= Decimal("0.00") or projected_due > cust.credit_limit_bdt:
            available = cls.compute_available_credit(cust)
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_CREDIT_LIMIT_EXCEEDED",
                    "detail": (
                        f"Credit amount exceeds approved limit for '{cust.full_name}': "
                        f"projected due {projected_due:.2f} BDT > limit {cust.credit_limit_bdt:.2f} BDT "
                        f"(current due {cust.current_due_bdt:.2f} BDT, available {available:.2f} BDT)."
                    ),
                }
            )
        return cust

    @classmethod
    @transaction.atomic
    def approve_customer_credit(cls, user: Any, customer: PosCustomer, data: Dict[str, Any]) -> PosCustomer:
        from apps.core.pos_domain_service import PosDomainService

        cust = PosCustomer.objects.select_for_update().get(pk=customer.pk)
        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=cust.primary_branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "customer credit approval")

        if cust.is_archived or not cust.is_active:
            raise ValidationError(
                {
                    "error_code": "ERR_CUSTOMER_INACTIVE_OR_ARCHIVED",
                    "customer": "Cannot approve credit for an inactive or archived customer.",
                }
            )

        raw_limit = data.get("credit_limit_bdt")
        if raw_limit is not None and str(raw_limit).strip() != "":
            try:
                new_limit = q2(raw_limit)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValidationError({"error_code": "ERR_INVALID_PAYMENT_AMOUNT", "credit_limit_bdt": "Invalid decimal credit limit."}) from exc
        else:
            new_limit = q2(cust.credit_limit_bdt)

        if new_limit <= Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_CREDIT_LIMIT_MUST_BE_POSITIVE",
                    "credit_limit_bdt": "Approved credit_limit_bdt must be strictly greater than 0.00 BDT.",
                }
            )

        raw_terms = data.get("credit_terms_days", cust.credit_terms_days or 30)
        try:
            terms_days = int(raw_terms)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"credit_terms_days": "credit_terms_days must be a positive integer."}) from exc
        if terms_days < 1 or terms_days > 365:
            raise ValidationError({"credit_terms_days": "credit_terms_days must be between 1 and 365 days."})

        cust.credit_limit_bdt = new_limit
        cust.credit_terms_days = terms_days
        cust.credit_status = PosCustomerCreditStatus.APPROVED
        cust.is_credit_approved = True
        cust.credit_approved_by = user
        cust.credit_approved_at = timezone.now()
        cust.credit_suspended_reason = ""
        cust.updated_by = user
        cust.save()
        return cust

    @classmethod
    @transaction.atomic
    def suspend_customer_credit(cls, user: Any, customer: PosCustomer, reason: str) -> PosCustomer:
        from apps.core.pos_domain_service import PosDomainService

        cust = PosCustomer.objects.select_for_update().get(pk=customer.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=cust.primary_branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_SUSPEND_REASON_REQUIRED",
                    "reason": "Reason (>= 3 chars) is mandatory to suspend customer credit.",
                }
            )
        cust.credit_status = PosCustomerCreditStatus.SUSPENDED
        cust.is_credit_approved = False
        cust.credit_suspended_reason = clean_reason[:255]
        cust.updated_by = user
        cust.save()
        return cust

    @classmethod
    @transaction.atomic
    def attach_customer_to_sale(cls, user: Any, sale: PosSaleOrder, data: Dict[str, Any]) -> PosSaleOrder:
        from apps.core.pos_domain_service import PosDomainService

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)
        if sale.lifecycle_state not in PosDomainService.MUTABLE_SALE_STATES and sale.lifecycle_state not in (
            PosSaleLifecycleState.CONFIRMED,
            PosSaleLifecycleState.PARTIALLY_PAID,
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_IMMUTABLE_SALE_STATE",
                    "detail": f"Cannot change customer on finalized/voided sale '{sale.order_number}'.",
                }
            )

        cust_id = str(data.get("customer") or data.get("customer_id") or "").strip()
        if not cust_id:
            raise ValidationError({"customer": "customer_id is required to attach customer."})

        cust = PosCustomer.objects.filter(
            customer_id=cust_id, tenant=sale.tenant, is_archived=False, is_active=True
        ).first()
        if not cust:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CUSTOMER_NOT_FOUND",
                    "detail": f"Customer '{cust_id}' was not found or belongs to another tenant.",
                }
            )

        sale.customer = cust
        sale.customer_name = cust.full_name
        sale.customer_phone = cust.phone
        sale.version += 1
        sale.updated_by = user
        sale.save(update_fields=["customer", "customer_name", "customer_phone", "version", "updated_by", "updated_at"])

        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=sale.lifecycle_state,
            to_state=sale.lifecycle_state,
            action="CUSTOMER_ATTACHED",
            user=user,
            reason=f"Attached customer {cust.full_name} ({cust.customer_id})",
            metadata={"customer_id": cust.customer_id, "credit_status": cust.credit_status},
        )
        return sale

    @classmethod
    def create_due_record_on_finalize_in_tx(
        cls,
        *,
        user: Any,
        sale: PosSaleOrder,
        invoice: SaleInvoice,
    ) -> Optional[CustomerDueRecord]:
        if sale.due_amount_bdt <= Decimal("0.00"):
            return None

        cust = cls.assert_customer_due_eligible_in_tx(
            tenant=sale.tenant,
            sale=sale,
            additional_due_bdt=sale.due_amount_bdt,
        )
        terms = max(1, int(cust.credit_terms_days or 30))
        due_date = timezone.now().date() + datetime.timedelta(days=terms)

        due_record = CustomerDueRecord.objects.create(
            due_id=f"due-{uuid.uuid4().hex[:12]}",
            tenant=sale.tenant,
            branch=sale.branch,
            customer=cust,
            sale=sale,
            invoice=invoice,
            customer_name=cust.full_name or sale.customer_name or "Customer",
            customer_phone=cust.phone or sale.customer_phone or "N/A",
            total_due_bdt=sale.due_amount_bdt,
            paid_amount_bdt=Decimal("0.00"),
            remaining_due_bdt=sale.due_amount_bdt,
            status=DueStatus.OUTSTANDING,
            due_date=due_date,
            notes=f"POS Credit Sale {sale.order_number} (Terms {terms}d)",
        )
        cust.current_due_bdt = q2(cust.current_due_bdt + sale.due_amount_bdt)
        cust.save(update_fields=["current_due_bdt", "updated_at"])
        return due_record

    @classmethod
    def sync_due_on_partial_return_in_tx(
        cls,
        *,
        sale: PosSaleOrder,
        due_credit_bdt: Decimal,
        reason: str,
    ) -> None:
        if due_credit_bdt <= Decimal("0.00"):
            return

        rem_to_apply = q2(due_credit_bdt)
        due_qs = CustomerDueRecord.objects.select_for_update().filter(
            tenant=sale.tenant,
            remaining_due_bdt__gt=Decimal("0.00"),
        )
        if sale.linked_invoice_id:
            due_qs = due_qs.filter(invoice_id=sale.linked_invoice_id)
        else:
            due_qs = due_qs.filter(sale=sale)

        for rec in due_qs.order_by("created_at", "due_id"):
            if rem_to_apply <= Decimal("0.00"):
                break
            reduce_amt = min(rec.remaining_due_bdt, rem_to_apply)
            rec.remaining_due_bdt = q2(rec.remaining_due_bdt - reduce_amt)
            rec.paid_amount_bdt = q2(rec.paid_amount_bdt + reduce_amt)
            rec.status = DueStatus.SETTLED if rec.remaining_due_bdt == Decimal("0.00") else DueStatus.PARTIAL
            note_suffix = f"Return credit -{reduce_amt:.2f}: {reason[:60]}"
            rec.notes = f"{rec.notes} | {note_suffix}".strip(" |")
            rec.save(update_fields=["paid_amount_bdt", "remaining_due_bdt", "status", "notes", "updated_at"])
            rem_to_apply = q2(rem_to_apply - reduce_amt)

        if sale.customer_id:
            cust = PosCustomer.objects.select_for_update().get(pk=sale.customer_id)
            cust.current_due_bdt = max(Decimal("0.00"), q2(cust.current_due_bdt - due_credit_bdt))
            cust.save(update_fields=["current_due_bdt", "updated_at"])

    @classmethod
    def sync_due_on_sale_reversal_in_tx(
        cls,
        *,
        sale: PosSaleOrder,
        reason: str,
    ) -> Decimal:
        due_qs = CustomerDueRecord.objects.select_for_update().filter(
            tenant=sale.tenant,
            remaining_due_bdt__gt=Decimal("0.00"),
        )
        if sale.linked_invoice_id:
            due_qs = due_qs.filter(invoice_id=sale.linked_invoice_id)
        else:
            due_qs = due_qs.filter(sale=sale)

        written_off_total = Decimal("0.00")
        for rec in due_qs:
            written_off_total = q2(written_off_total + rec.remaining_due_bdt)
            rec.remaining_due_bdt = Decimal("0.00")
            rec.status = DueStatus.WRITTEN_OFF
            rec.notes = f"Reversed via POS Sale {sale.order_number}: {reason[:80]}"
            rec.save(update_fields=["remaining_due_bdt", "status", "notes", "updated_at"])

        decrement_amt = written_off_total if written_off_total > Decimal("0.00") else q2(sale.due_amount_bdt)
        if decrement_amt > Decimal("0.00") and sale.customer_id:
            cust = PosCustomer.objects.select_for_update().get(pk=sale.customer_id)
            cust.current_due_bdt = max(Decimal("0.00"), q2(cust.current_due_bdt - decrement_amt))
            cust.save(update_fields=["current_due_bdt", "updated_at"])
        return decrement_amt

    @classmethod
    @transaction.atomic
    def collect_customer_due(
        cls,
        user: Any,
        due_record: CustomerDueRecord,
        data: Dict[str, Any],
    ) -> Tuple[CustomerDueCollection, bool]:
        """
        Collects full or partial repayment against an outstanding `CustomerDueRecord`
        using Cash or Verified Digital Tender (NEVER DUE).
        Posts `PosPaymentJournalEntryType.DUE_COLLECTION`:
          Dr CASH_DRAWER / <MFS>_CLEARING
          Cr AR_CUSTOMER_DUE
        ZERO impact on `POS_SALES_REVENUE` or `SaleInvoice` (zero double revenue).
        """
        from apps.core.pos_domain_service import PosDomainService

        due_rec = CustomerDueRecord.objects.select_for_update().select_related(
            "tenant", "branch", "customer", "sale", "invoice"
        ).get(pk=due_record.pk)

        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=due_rec.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_SALE_WRITE_ROLES | PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "customer due collection")

        if bool(data.get("is_offline", False)):
            raise ValidationError(
                {
                    "error_code": "ERR_OFFLINE_UNAVAILABLE",
                    "detail": (
                        "Offline limitation: customer receivable collection requires online cloud connectivity "
                        "to prevent double collection and verify AR ledger balance."
                    ),
                }
            )

        idem_key = str(data.get("idempotency_key") or "").strip()
        if idem_key:
            existing = CustomerDueCollection.objects.filter(tenant=tenant, idempotency_key=idem_key).first()
            if existing:
                if existing.due_record_id != due_rec.due_id:
                    raise ValidationError(
                        {
                            "error_code": "ERR_DUPLICATE_DUE_IDEMPOTENCY",
                            "idempotency_key": f"Idempotency key '{idem_key}' belongs to a different due record.",
                        }
                    )
                return existing, False

        if due_rec.status in (DueStatus.SETTLED, DueStatus.WRITTEN_OFF) or due_rec.remaining_due_bdt <= Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_ALREADY_SETTLED",
                    "due_id": f"Due record '{due_rec.due_id}' is already {due_rec.status} (remaining 0.00 BDT).",
                }
            )

        tender = str(data.get("tender_type") or PaymentMethod.CASH).upper().strip()
        if tender not in PaymentMethod.values:
            raise ValidationError(
                {
                    "error_code": "ERR_UNSUPPORTED_TENDER_METHOD",
                    "tender_type": f"Unsupported payment method '{tender}'.",
                }
            )
        if tender == PaymentMethod.DUE:
            raise ValidationError(
                {
                    "error_code": "ERR_CANNOT_COLLECT_DUE_WITH_DUE",
                    "tender_type": "Cannot settle a customer credit balance using DUE tender.",
                }
            )

        try:
            amount_bdt = q2(data.get("amount_bdt"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError({"error_code": "ERR_INVALID_PAYMENT_AMOUNT", "amount_bdt": "Invalid decimal amount_bdt."}) from exc
        if amount_bdt <= Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                    "amount_bdt": "Collection amount_bdt must be strictly greater than 0.00 BDT.",
                }
            )
        if amount_bdt > due_rec.remaining_due_bdt:
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_OVERPAYMENT",
                    "amount_bdt": (
                        f"Collection amount ({amount_bdt:.2f} BDT) exceeds remaining due "
                        f"({due_rec.remaining_due_bdt:.2f} BDT)."
                    ),
                }
            )

        raw_tendered = data.get("cash_tendered_bdt")
        if raw_tendered is not None and str(raw_tendered).strip() != "":
            try:
                cash_tendered = q2(raw_tendered)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValidationError({"error_code": "ERR_INVALID_PAYMENT_AMOUNT", "cash_tendered_bdt": "Invalid cash_tendered_bdt."}) from exc
        else:
            cash_tendered = amount_bdt if tender == PaymentMethod.CASH else Decimal("0.00")

        change_given = Decimal("0.00")
        if tender == PaymentMethod.CASH:
            if cash_tendered < amount_bdt:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                        "cash_tendered_bdt": f"cash_tendered_bdt ({cash_tendered:.2f}) cannot be less than applied amount ({amount_bdt:.2f}).",
                    }
                )
            change_given = q2(cash_tendered - amount_bdt)
        else:
            cash_tendered = Decimal("0.00")

        gateway_ref = str(data.get("gateway_reference") or "").strip()
        payer_ref = str(data.get("payer_reference") or "").strip()
        card_last4 = str(data.get("card_last4") or "").strip()
        if card_last4 and not CARD_LAST4_RE.match(card_last4):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_CARD_LAST4",
                    "card_last4": "Card last 4 digits must be exactly 4 numeric digits.",
                }
            )

        if tender in DIGITAL_TENDERS:
            if len(gateway_ref) < 4:
                raise ValidationError(
                    {
                        "error_code": "ERR_DIGITAL_TENDER_REFERENCE_REQUIRED",
                        "gateway_reference": f"{tender} due collection requires gateway TrxID / auth reference (>= 4 chars).",
                    }
                )
            dup_att = PosPaymentAttempt.objects.filter(
                tenant=tenant,
                tender_type=tender,
                gateway_reference=gateway_ref,
                status__in=[PosPaymentAttemptStatus.CAPTURED, PosPaymentAttemptStatus.PENDING, PosPaymentAttemptStatus.AUTHORIZED],
            ).exists()
            dup_col = CustomerDueCollection.objects.filter(
                tenant=tenant,
                tender_type=tender,
                gateway_reference=gateway_ref,
                status=CustomerDueCollectionStatus.CAPTURED,
            ).exists()
            if dup_att or dup_col:
                raise ValidationError(
                    {
                        "error_code": "ERR_DUPLICATE_GATEWAY_REFERENCE",
                        "gateway_reference": f"Gateway reference '{gateway_ref}' for {tender} has already been used in this tenant.",
                    }
                )
            ver_status = PosPaymentVerificationStatus.VERIFIED
            ver_channel = f"{tender}_VERIFIED_REF"
        else:
            ver_status = PosPaymentVerificationStatus.NOT_REQUIRED
            ver_channel = "CASH_DRAWER"

        reg: Optional[CashierRegisterAssignment] = None
        reg_id = str(data.get("register_assignment") or data.get("register_assignment_id") or "").strip()
        if reg_id:
            reg = CashierRegisterAssignment.objects.filter(
                tenant=tenant, branch=branch, assignment_id=reg_id
            ).first()
            if not reg:
                raise ValidationError({"error_code": "ERR_REGISTER_NOT_FOUND", "register_assignment": "Register assignment not found in this branch."})
        else:
            reg = (
                CashierRegisterAssignment.objects.filter(
                    tenant=tenant, branch=branch, status=RegisterAssignmentStatus.ACTIVE
                )
                .order_by("-assigned_at")
                .first()
            )

        debit_account = TENDER_CLEARING_ACCOUNTS.get(tender, "POS_OTHER_CLEARING")
        cash_impact = amount_bdt if tender == PaymentMethod.CASH else Decimal("0.00")
        notes = str(data.get("notes") or "").strip()

        journal = PosPaymentJournalEntry.objects.create(
            journal_id=f"pjr-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            sale=due_rec.sale,
            invoice=due_rec.invoice,
            register_assignment=reg,
            entry_type=PosPaymentJournalEntryType.DUE_COLLECTION,
            tender_type=tender,
            debit_account=debit_account,
            credit_account="AR_CUSTOMER_DUE",
            amount_bdt=amount_bdt,
            cash_register_impact_bdt=cash_impact,
            gateway_reference=gateway_ref,
            verification_status=ver_status,
            notes=f"Due collection {due_rec.due_id} ({tender})" + (f": {notes[:80]}" if notes else ""),
            posted_by=user,
            is_offline_sync=False,
        )

        target_customer: Optional[PosCustomer] = None
        if due_rec.customer_id:
            target_customer = PosCustomer.objects.select_for_update().get(pk=due_rec.customer_id)
        elif due_rec.customer_phone:
            target_customer = PosCustomer.objects.select_for_update().filter(
                tenant=tenant, phone=due_rec.customer_phone, is_archived=False
            ).first()

        collection = CustomerDueCollection.objects.create(
            collection_id=f"col-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            due_record=due_rec,
            customer=target_customer,
            sale=due_rec.sale,
            register_assignment=reg,
            journal_entry=journal,
            tender_type=tender,
            amount_bdt=amount_bdt,
            cash_tendered_bdt=cash_tendered,
            change_given_bdt=change_given,
            gateway_reference=gateway_ref,
            payer_reference=payer_ref,
            card_last4=card_last4,
            verification_status=ver_status,
            verification_channel=ver_channel,
            idempotency_key=idem_key,
            status=CustomerDueCollectionStatus.CAPTURED,
            notes=notes,
            collected_by=user,
        )

        due_rec.paid_amount_bdt = q2(due_rec.paid_amount_bdt + amount_bdt)
        due_rec.remaining_due_bdt = q2(due_rec.remaining_due_bdt - amount_bdt)
        due_rec.status = DueStatus.SETTLED if due_rec.remaining_due_bdt == Decimal("0.00") else DueStatus.PARTIAL
        due_rec.save(update_fields=["paid_amount_bdt", "remaining_due_bdt", "status", "updated_at"])

        if target_customer:
            target_customer.current_due_bdt = max(
                Decimal("0.00"), q2(target_customer.current_due_bdt - amount_bdt)
            )
            target_customer.save(update_fields=["current_due_bdt", "updated_at"])

        if due_rec.sale:
            PosDomainService._record_lifecycle_audit(
                sale=due_rec.sale,
                from_state=due_rec.sale.lifecycle_state,
                to_state=due_rec.sale.lifecycle_state,
                action="CUSTOMER_DUE_COLLECTED",
                user=user,
                reason=f"Collected {amount_bdt:.2f} BDT ({tender}) against {due_rec.due_id}",
                metadata={
                    "collection_id": collection.collection_id,
                    "due_id": due_rec.due_id,
                    "journal_id": journal.journal_id,
                    "debit_account": debit_account,
                    "credit_account": "AR_CUSTOMER_DUE",
                    "revenue_impact_bdt": "0.00",
                    "remaining_due_bdt": f"{due_rec.remaining_due_bdt:.2f}",
                },
            )
        return collection, True

    @classmethod
    @transaction.atomic
    def reverse_due_collection(
        cls,
        user: Any,
        collection: CustomerDueCollection,
        reason: str,
    ) -> CustomerDueCollection:
        from apps.core.pos_domain_service import PosDomainService

        col = CustomerDueCollection.objects.select_for_update().select_related(
            "due_record", "customer", "sale", "journal_entry", "register_assignment"
        ).get(pk=collection.pk)
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=col.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_VOID_REASON_REQUIRED",
                    "reason": "Reason (>= 3 chars) is mandatory to reverse a customer due collection.",
                }
            )
        if col.status == CustomerDueCollectionStatus.REVERSED:
            raise ValidationError(
                {
                    "error_code": "ERR_COLLECTION_ALREADY_REVERSED",
                    "collection_id": f"Due collection '{col.collection_id}' was already reversed.",
                }
            )

        due_rec = CustomerDueRecord.objects.select_for_update().get(pk=col.due_record_id)
        if due_rec.status == DueStatus.WRITTEN_OFF:
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_ALREADY_WRITTEN_OFF",
                    "due_id": "Cannot reverse collection after due record was written off.",
                }
            )

        credit_acct = TENDER_REFUND_CLEARING_ACCOUNTS.get(col.tender_type, "POS_REFUND_CLEARING")
        cash_impact = -col.amount_bdt if col.tender_type == PaymentMethod.CASH else Decimal("0.00")

        PosPaymentJournalEntry.objects.create(
            journal_id=f"pjr-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            sale=col.sale,
            invoice=due_rec.invoice,
            register_assignment=col.register_assignment,
            reversed_entry=col.journal_entry,
            entry_type=PosPaymentJournalEntryType.DUE_COLLECTION_REVERSAL,
            tender_type=col.tender_type,
            debit_account="AR_CUSTOMER_DUE",
            credit_account=credit_acct,
            amount_bdt=col.amount_bdt,
            cash_register_impact_bdt=cash_impact,
            gateway_reference=col.gateway_reference,
            verification_status=PosPaymentVerificationStatus.VERIFIED,
            notes=f"Reversed due collection {col.collection_id}: {clean_reason[:90]}",
            posted_by=user,
        )

        col.status = CustomerDueCollectionStatus.REVERSED
        col.reversed_by = user
        col.reversed_at = timezone.now()
        col.reversal_reason = clean_reason[:255]
        col.save(update_fields=["status", "reversed_by", "reversed_at", "reversal_reason"])

        due_rec.paid_amount_bdt = max(Decimal("0.00"), q2(due_rec.paid_amount_bdt - col.amount_bdt))
        due_rec.remaining_due_bdt = q2(due_rec.remaining_due_bdt + col.amount_bdt)
        due_rec.status = DueStatus.OUTSTANDING if due_rec.paid_amount_bdt == Decimal("0.00") else DueStatus.PARTIAL
        due_rec.save(update_fields=["paid_amount_bdt", "remaining_due_bdt", "status", "updated_at"])

        if col.customer_id:
            cust = PosCustomer.objects.select_for_update().get(pk=col.customer_id)
            cust.current_due_bdt = q2(cust.current_due_bdt + col.amount_bdt)
            cust.save(update_fields=["current_due_bdt", "updated_at"])

        return col

    @classmethod
    @transaction.atomic
    def write_off_customer_due(cls, user: Any, due_record: CustomerDueRecord, reason: str) -> CustomerDueRecord:
        from apps.core.pos_domain_service import PosDomainService

        due_rec = CustomerDueRecord.objects.select_for_update().get(pk=due_record.pk)
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=due_rec.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_CUSTOMER_ADMIN_ROLES,
        )
        clean_reason = str(reason or "").strip()
        if len(clean_reason) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_WRITEOFF_REASON_REQUIRED",
                    "reason": "Reason (>= 3 chars) is mandatory to write off an open due record.",
                }
            )
        if due_rec.status in (DueStatus.SETTLED, DueStatus.WRITTEN_OFF) or due_rec.remaining_due_bdt <= Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_DUE_ALREADY_SETTLED",
                    "due_id": f"Due record '{due_rec.due_id}' has no remaining open balance.",
                }
            )

        rem = q2(due_rec.remaining_due_bdt)
        PosPaymentJournalEntry.objects.create(
            journal_id=f"pjr-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            sale=due_rec.sale,
            invoice=due_rec.invoice,
            entry_type=PosPaymentJournalEntryType.DUE_COLLECTION_REVERSAL,
            tender_type=PaymentMethod.DUE,
            debit_account="BAD_DEBT_EXPENSE",
            credit_account="AR_CUSTOMER_DUE",
            amount_bdt=rem,
            cash_register_impact_bdt=Decimal("0.00"),
            verification_status=PosPaymentVerificationStatus.VERIFIED,
            notes=f"Write-off {due_rec.due_id}: {clean_reason[:90]}",
            posted_by=user,
        )
        due_rec.remaining_due_bdt = Decimal("0.00")
        due_rec.status = DueStatus.WRITTEN_OFF
        due_rec.notes = f"{due_rec.notes} | Write-off ({rem:.2f} BDT): {clean_reason[:80]}".strip(" |")
        due_rec.save(update_fields=["remaining_due_bdt", "status", "notes", "updated_at"])

        if due_rec.customer_id:
            cust = PosCustomer.objects.select_for_update().get(pk=due_rec.customer_id)
            cust.current_due_bdt = max(Decimal("0.00"), q2(cust.current_due_bdt - rem))
            cust.save(update_fields=["current_due_bdt", "updated_at"])
        return due_rec

    @classmethod
    def build_customer_ledger(cls, user: Any, customer: PosCustomer) -> Dict[str, Any]:
        from apps.core.pos_domain_service import PosDomainService

        tenant, _ = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=customer.primary_branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        PosDomainService.assert_feature_entitlement(tenant, "CUSTOMER_LEDGER", "customer credit ledger")
        today = timezone.now().date()
        dues = list(
            CustomerDueRecord.objects.filter(tenant=tenant, customer=customer)
            .select_related("branch", "invoice", "sale")
            .order_by("-created_at")
        )
        collections = list(
            CustomerDueCollection.objects.filter(tenant=tenant, customer=customer)
            .select_related("due_record", "collected_by", "journal_entry")
            .order_by("-collected_at")
        )
        overdue_bdt = sum(
            (
                d.remaining_due_bdt
                for d in dues
                if d.status in (DueStatus.OUTSTANDING, DueStatus.PARTIAL) and d.due_date and d.due_date < today
            ),
            Decimal("0.00"),
        )
        total_due_issued = sum((d.total_due_bdt for d in dues), Decimal("0.00"))
        total_collected = sum(
            (c.amount_bdt for c in collections if c.status == CustomerDueCollectionStatus.CAPTURED),
            Decimal("0.00"),
        )
        rev_entries = PosPaymentJournalEntry.objects.filter(
            tenant=tenant,
            sale__customer=customer,
            credit_account="POS_SALES_REVENUE",
        ).aggregate(s=Sum("amount_bdt"))["s"] or Decimal("0.00")
        col_rev_entries = PosPaymentJournalEntry.objects.filter(
            tenant=tenant,
            entry_type=PosPaymentJournalEntryType.DUE_COLLECTION,
            credit_account="POS_SALES_REVENUE",
        ).count()

        return {
            "customer_id": customer.customer_id,
            "full_name": customer.full_name,
            "phone": customer.phone,
            "credit_status": customer.credit_status,
            "is_credit_approved": customer.is_credit_approved,
            "credit_limit_bdt": f"{customer.credit_limit_bdt:.2f}",
            "current_due_bdt": f"{customer.current_due_bdt:.2f}",
            "available_credit_bdt": f"{cls.compute_available_credit(customer):.2f}",
            "credit_terms_days": int(customer.credit_terms_days or 30),
            "overdue_due_bdt": f"{overdue_bdt:.2f}",
            "total_due_issued_bdt": f"{total_due_issued:.2f}",
            "total_due_collected_bdt": f"{total_collected:.2f}",
            "revenue_recognized_bdt": f"{q2(rev_entries):.2f}",
            "double_revenue_detected": col_rev_entries > 0,
            "open_due_records_count": sum(
                1 for d in dues if d.status in (DueStatus.OUTSTANDING, DueStatus.PARTIAL)
            ),
            "dues": dues,
            "collections": collections,
        }
