"""
backend/apps/core/pos_payment_service.py
Architecture Decision: ADR-0229 (P229).
Phase 12: POS, Sales Orders, Checkout & Returns.

Single authoritative service for:
- Cash, bKash, Nagad, Rocket, Upay, Card, Bank Transfer & Due split payments
- Strict digital tender reference validation, duplicate TrxID prevention & pre-final void/verify transitions
- Net-of-change cash settlement attribution (`PaymentTransaction` == exact invoice settled total)
- Balanced immutable `PosPaymentJournalEntry` posting across checkout, partial return & sale reversal
- Audited Cashier Shift & Register Close Reconciliation (`PosCashierCloseReconciliation`)
"""

from __future__ import annotations

import re
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.models import (
    CashierRegisterAssignment,
    PaymentMethod,
    PaymentTransaction,
    PosCashierCloseReconciliation,
    PosCloseReconciliationStatus,
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
    PaymentMethod.DUE: "AR_CUSTOMER_DUE",
}

TENDER_REFUND_CLEARING_ACCOUNTS = {
    PaymentMethod.CASH: "CASH_DRAWER_REFUND",
    PaymentMethod.BKASH: "BKASH_REFUND_CLEARING",
    PaymentMethod.NAGAD: "NAGAD_REFUND_CLEARING",
    PaymentMethod.ROCKET: "ROCKET_REFUND_CLEARING",
    PaymentMethod.UPAY: "UPAY_REFUND_CLEARING",
    PaymentMethod.CARD: "CARD_REFUND_CLEARING",
    PaymentMethod.BANK_TRANSFER: "BANK_TRANSFER_REFUND_CLEARING",
    PaymentMethod.DUE: "AR_CUSTOMER_DUE",
}

CARD_LAST4_RE = re.compile(r"^\d{4}$")


def q2(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else "0.00")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def parse_positive_money(raw: Any, field_name: str = "amount_bdt") -> Decimal:
    try:
        val = q2(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(
            {
                "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                field_name: f"Invalid decimal monetary value for {field_name}.",
            }
        ) from exc
    if val <= Decimal("0.00"):
        raise ValidationError(
            {
                "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                field_name: f"{field_name} must be strictly greater than 0.00 BDT.",
            }
        )
    return val


class PosPaymentSplitService:
    """Integrates directly with PosDomainService to enforce split-tender accounting & reconciliation."""

    @staticmethod
    def _ensure_sale_mutable_for_payment(sale: PosSaleOrder) -> None:
        if sale.lifecycle_state in (
            PosSaleLifecycleState.FINALIZED,
            PosSaleLifecycleState.VOIDED,
            PosSaleLifecycleState.REVERSED,
            PosSaleLifecycleState.PARTIALLY_RETURNED,
            PosSaleLifecycleState.FULLY_RETURNED,
        ):
            raise ValidationError(
                {
                    "error_code": "ERR_SALE_PAYMENT_LOCKED",
                    "detail": f"Cannot mutate checkout payment attempts on sale in '{sale.lifecycle_state}' state.",
                }
            )

    @staticmethod
    def _validate_tender_payload(
        *,
        sale: PosSaleOrder,
        data: Dict[str, Any],
        require_verified: bool,
        is_offline: bool,
        seen_batch_refs: Optional[set[tuple[str, str]]] = None,
        prior_batch_due_bdt: Decimal = Decimal("0.00"),
    ) -> Dict[str, Any]:
        tender_type = str(data.get("tender_type") or PaymentMethod.CASH).upper().strip()
        if tender_type not in PaymentMethod.values:
            raise ValidationError(
                {
                    "error_code": "ERR_UNSUPPORTED_TENDER_METHOD",
                    "tender_type": f"Unsupported payment tender '{tender_type}'.",
                }
            )

        attempt_status = str(data.get("status") or PosPaymentAttemptStatus.CAPTURED).upper().strip()
        if attempt_status not in PosPaymentAttemptStatus.values:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_STATUS",
                    "status": f"Invalid payment attempt status '{attempt_status}'.",
                }
            )

        amount_bdt = parse_positive_money(data.get("amount_bdt"), "amount_bdt")
        gateway_ref = str(data.get("gateway_reference") or "").strip()
        payer_ref = str(data.get("payer_reference") or "").strip()
        card_last4 = str(data.get("card_last4") or "").strip()
        verification_channel = str(data.get("verification_channel") or "").strip()
        raw_ver_status = str(data.get("verification_status") or "").upper().strip()

        if card_last4 and not CARD_LAST4_RE.match(card_last4):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_CARD_LAST4",
                    "card_last4": "Card last 4 digits must be exactly 4 numeric digits.",
                }
            )

        if tender_type == PaymentMethod.CASH:
            verification_status = raw_ver_status or PosPaymentVerificationStatus.NOT_REQUIRED
            if verification_status not in PosPaymentVerificationStatus.values:
                verification_status = PosPaymentVerificationStatus.NOT_REQUIRED
            verification_channel = verification_channel or ("OFFLINE_CASH_DRAWER" if is_offline else "CASH_DRAWER")

        elif tender_type == PaymentMethod.DUE:
            from apps.core.pos_customer_due_service import PosCustomerDueService

            if is_offline:
                raise ValidationError(
                    {
                        "error_code": "ERR_OFFLINE_CREDIT_UNAVAILABLE",
                        "detail": (
                            "Offline limitation: customer credit/DUE checkout requires online connectivity "
                            "to verify credit approval and live credit limit."
                        ),
                    }
                )
            if attempt_status == PosPaymentAttemptStatus.CAPTURED:
                existing_sale_due = sum(
                    (
                        a.amount_bdt
                        for a in sale.payment_attempts.filter(
                            status=PosPaymentAttemptStatus.CAPTURED,
                            tender_type=PaymentMethod.DUE,
                        )
                    ),
                    Decimal("0.00"),
                )
                PosCustomerDueService.assert_customer_due_eligible_in_tx(
                    tenant=sale.tenant,
                    sale=sale,
                    additional_due_bdt=q2(existing_sale_due + prior_batch_due_bdt + amount_bdt),
                )
            verification_status = PosPaymentVerificationStatus.VERIFIED
            verification_channel = verification_channel or "CUSTOMER_CREDIT_LEDGER"

        else:
            # Digital tenders: bKash, Nagad, Rocket, Upay, Card, Bank Transfer
            if raw_ver_status and raw_ver_status not in PosPaymentVerificationStatus.values:
                raise ValidationError(
                    {
                        "error_code": "ERR_INVALID_VERIFICATION_STATUS",
                        "verification_status": f"Invalid verification_status '{raw_ver_status}'.",
                    }
                )

            if is_offline:
                if raw_ver_status == PosPaymentVerificationStatus.PENDING_GATEWAY or len(gateway_ref) < 4:
                    raise ValidationError(
                        {
                            "error_code": "ERR_OFFLINE_DIGITAL_PAYMENT_REQUIRES_REF",
                            "detail": (
                                f"Offline limitation: {tender_type} cannot verify live gateway while offline "
                                "without a terminal TrxID/auth reference (>= 4 chars)."
                            ),
                        }
                    )
                verification_status = PosPaymentVerificationStatus.OFFLINE_PENDING_VERIFY
                verification_channel = verification_channel or "OFFLINE_TERMINAL_REF"
            elif raw_ver_status == PosPaymentVerificationStatus.PENDING_GATEWAY or attempt_status in (
                PosPaymentAttemptStatus.PENDING,
                PosPaymentAttemptStatus.AUTHORIZED,
                PosPaymentAttemptStatus.INITIATED,
            ):
                attempt_status = PosPaymentAttemptStatus.PENDING
                verification_status = PosPaymentVerificationStatus.PENDING_GATEWAY
                verification_channel = verification_channel or f"{tender_type}_GATEWAY"
            else:
                if require_verified and len(gateway_ref) < 4:
                    raise ValidationError(
                        {
                            "error_code": "ERR_DIGITAL_TENDER_REFERENCE_REQUIRED",
                            "gateway_reference": (
                                f"Verified {tender_type} payment requires gateway_reference / TrxID (>= 4 chars)."
                            ),
                        }
                    )
                verification_status = (
                    raw_ver_status
                    or (PosPaymentVerificationStatus.VERIFIED if gateway_ref else PosPaymentVerificationStatus.NOT_REQUIRED)
                )
                verification_channel = verification_channel or f"{tender_type}_VERIFIED_REF"

            if gateway_ref and attempt_status == PosPaymentAttemptStatus.CAPTURED:
                ref_key = (tender_type, gateway_ref.lower())
                if seen_batch_refs is not None:
                    if ref_key in seen_batch_refs:
                        raise ValidationError(
                            {
                                "error_code": "ERR_DUPLICATE_GATEWAY_REFERENCE",
                                "gateway_reference": f"Duplicate {tender_type} transaction reference '{gateway_ref}' in split batch.",
                            }
                        )
                    seen_batch_refs.add(ref_key)
                dup_exists = (
                    PosPaymentAttempt.objects.filter(
                        tenant=sale.tenant,
                        tender_type=tender_type,
                        gateway_reference__iexact=gateway_ref,
                        status__in=[
                            PosPaymentAttemptStatus.CAPTURED,
                            PosPaymentAttemptStatus.PENDING,
                            PosPaymentAttemptStatus.AUTHORIZED,
                        ],
                    )
                    .exists()
                )
                if dup_exists:
                    raise ValidationError(
                        {
                            "error_code": "ERR_DUPLICATE_GATEWAY_REFERENCE",
                            "gateway_reference": f"{tender_type} reference '{gateway_ref}' was already used on another payment.",
                        }
                    )

        return {
            "tender_type": tender_type,
            "status": attempt_status,
            "amount_bdt": amount_bdt,
            "gateway_reference": gateway_ref,
            "payer_reference": payer_ref,
            "card_last4": card_last4,
            "verification_status": verification_status,
            "verification_channel": verification_channel,
            "failure_reason_code": str(data.get("failure_reason_code") or ""),
            "failure_message": str(data.get("failure_message") or ""),
            "idempotency_key": str(data.get("idempotency_key") or "").strip(),
        }

    @classmethod
    def recalculate_sale_payment_totals(cls, sale: PosSaleOrder) -> PosSaleOrder:
        """
        Recomputes per-attempt `change_given_bdt` & `net_settled_amount_bdt`, then updates
        `paid_amount_bdt`, `change_returned_bdt`, `due_amount_bdt`, and `lifecycle_state`.
        Enforces zero non-cash overpayment and rejects redundant cash after full settlement.
        """
        captured = list(
            sale.payment_attempts.filter(status=PosPaymentAttemptStatus.CAPTURED)
            .select_for_update()
            .order_by("attempt_number")
        )
        non_cash_sum = Decimal("0.00")
        due_sum = Decimal("0.00")
        cash_attempts: List[PosPaymentAttempt] = []
        cash_sum = Decimal("0.00")

        for att in captured:
            if att.tender_type == PaymentMethod.DUE:
                due_sum = q2(due_sum + att.amount_bdt)
            elif att.tender_type == PaymentMethod.CASH:
                cash_attempts.append(att)
                cash_sum = q2(cash_sum + att.amount_bdt)
            else:
                non_cash_sum = q2(non_cash_sum + att.amount_bdt)

        grand_total = q2(sale.grand_total_bdt)
        non_cash_plus_due = q2(non_cash_sum + due_sum)
        if non_cash_plus_due > grand_total:
            raise ValidationError(
                {
                    "error_code": "ERR_NON_CASH_OVERPAYMENT",
                    "detail": (
                        f"Non-cash + credit tenders ({non_cash_plus_due:.2f} BDT) cannot exceed "
                        f"sale grand total ({grand_total:.2f} BDT)."
                    ),
                }
            )

        remaining_cash_needed = max(Decimal("0.00"), q2(grand_total - non_cash_plus_due))
        change_returned = max(Decimal("0.00"), q2(cash_sum - remaining_cash_needed))

        # Assign net_settled_amount_bdt for non-cash & due attempts
        for att in captured:
            if att.tender_type != PaymentMethod.CASH:
                if att.change_given_bdt != Decimal("0.00") or att.net_settled_amount_bdt != att.amount_bdt:
                    att.change_given_bdt = Decimal("0.00")
                    att.net_settled_amount_bdt = att.amount_bdt
                    att.save(update_fields=["change_given_bdt", "net_settled_amount_bdt"])

        # Attribute physical change returned strictly onto CASH attempts (last-in first-change)
        rem_change = change_returned
        for att in reversed(cash_attempts):
            if rem_change > Decimal("0.00"):
                give = min(att.amount_bdt, rem_change)
                net = q2(att.amount_bdt - give)
                rem_change = q2(rem_change - give)
            else:
                give = Decimal("0.00")
                net = att.amount_bdt
            if net <= Decimal("0.00") and cash_sum > remaining_cash_needed and len(cash_attempts) > 1:
                raise ValidationError(
                    {
                        "error_code": "ERR_REDUNDANT_CASH_OVERPAYMENT",
                        "detail": "Redundant cash payment attempt adds zero net settlement after prior tenders.",
                    }
                )
            if att.change_given_bdt != give or att.net_settled_amount_bdt != net:
                att.change_given_bdt = give
                att.net_settled_amount_bdt = net
                att.save(update_fields=["change_given_bdt", "net_settled_amount_bdt"])

        effective_cash = min(cash_sum, remaining_cash_needed)
        effective_paid = q2(non_cash_sum + effective_cash)
        sale.paid_amount_bdt = effective_paid
        sale.change_returned_bdt = change_returned
        sale.due_amount_bdt = due_sum

        if sale.lifecycle_state in (
            PosSaleLifecycleState.DRAFT,
            PosSaleLifecycleState.CONFIRMED,
            PosSaleLifecycleState.HELD,
        ):
            if Decimal("0.00") < q2(effective_paid + sale.due_amount_bdt) < grand_total:
                sale.lifecycle_state = PosSaleLifecycleState.PARTIALLY_PAID

        sale.save(
            update_fields=[
                "paid_amount_bdt",
                "change_returned_bdt",
                "due_amount_bdt",
                "lifecycle_state",
                "updated_at",
            ]
        )
        return sale

    @classmethod
    @transaction.atomic
    def record_single_attempt(cls, user: Any, sale: PosSaleOrder, data: Dict[str, Any]) -> tuple[PosPaymentAttempt, bool]:
        from apps.core.pos_domain_service import PosDomainService

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)
        cls._ensure_sale_mutable_for_payment(sale)

        idem_key = str(data.get("idempotency_key") or "").strip()
        if idem_key:
            existing = PosPaymentAttempt.objects.filter(tenant=sale.tenant, idempotency_key=idem_key).first()
            if existing:
                return existing, False

        validated = cls._validate_tender_payload(
            sale=sale,
            data=data,
            require_verified=bool(data.get("require_verified", False)),
            is_offline=bool(data.get("is_offline", False)),
        )
        next_no = (sale.payment_attempts.count() or 0) + 1
        attempt = PosPaymentAttempt.objects.create(
            attempt_id=f"payatt-{uuid.uuid4().hex[:12]}",
            tenant=sale.tenant,
            branch=sale.branch,
            sale=sale,
            attempt_number=next_no,
            tender_type=validated["tender_type"],
            amount_bdt=validated["amount_bdt"],
            net_settled_amount_bdt=(
                validated["amount_bdt"] if validated["status"] == PosPaymentAttemptStatus.CAPTURED else Decimal("0.00")
            ),
            status=validated["status"],
            idempotency_key=validated["idempotency_key"],
            gateway_reference=validated["gateway_reference"],
            payer_reference=validated["payer_reference"],
            card_last4=validated["card_last4"],
            verification_status=validated["verification_status"],
            verification_channel=validated["verification_channel"],
            failure_reason_code=validated["failure_reason_code"],
            failure_message=validated["failure_message"],
            processed_by=user,
            verified_by=(
                user if validated["verification_status"] == PosPaymentVerificationStatus.VERIFIED else None
            ),
            verified_at=(
                timezone.now() if validated["verification_status"] == PosPaymentVerificationStatus.VERIFIED else None
            ),
            settled_at=timezone.now() if validated["status"] == PosPaymentAttemptStatus.CAPTURED else None,
        )

        old_state = sale.lifecycle_state
        cls.recalculate_sale_payment_totals(sale)
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="PAYMENT_ATTEMPT_RECORDED",
            user=user,
            reason=f"{attempt.tender_type} {attempt.amount_bdt} BDT [{attempt.status}]",
            metadata={
                "attempt_id": attempt.attempt_id,
                "tender_type": attempt.tender_type,
                "amount_bdt": str(attempt.amount_bdt),
                "verification_status": attempt.verification_status,
            },
        )
        return attempt, True

    @classmethod
    @transaction.atomic
    def record_split_batch(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> tuple[PosSaleOrder, List[PosPaymentAttempt], bool]:
        from apps.core.pos_domain_service import PosDomainService

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)
        cls._ensure_sale_mutable_for_payment(sale)

        raw_splits = data.get("splits")
        if not isinstance(raw_splits, list) or not raw_splits:
            raise ValidationError(
                {
                    "error_code": "ERR_EMPTY_SPLIT_PAYMENTS",
                    "splits": "At least one payment split leg is required in `splits`.",
                }
            )

        batch_key = str(data.get("batch_idempotency_key") or data.get("idempotency_key") or "").strip()
        if batch_key:
            existing_batch = list(
                PosPaymentAttempt.objects.filter(
                    tenant=sale.tenant,
                    sale=sale,
                    split_batch_key=batch_key,
                ).order_by("attempt_number")
            )
            if existing_batch:
                return sale, existing_batch, False

        require_verified = bool(data.get("require_verified", True))
        is_offline = bool(data.get("is_offline", False))

        # If caller explicitly sets replace_existing=True on pre-final sale, void prior unfinalized attempts cleanly
        if bool(data.get("replace_existing", False)):
            sale.payment_attempts.filter(
                status__in=[
                    PosPaymentAttemptStatus.CAPTURED,
                    PosPaymentAttemptStatus.PENDING,
                    PosPaymentAttemptStatus.AUTHORIZED,
                    PosPaymentAttemptStatus.INITIATED,
                ]
            ).update(
                status=PosPaymentAttemptStatus.VOIDED,
                voided_by=user,
                voided_at=timezone.now(),
                void_reason="Replaced by new split payment batch",
            )

        seen_refs: set[tuple[str, str]] = set()
        batch_due_acc = Decimal("0.00")
        validated_legs: List[Dict[str, Any]] = []
        for idx, leg in enumerate(raw_splits, start=1):
            if not isinstance(leg, dict):
                raise ValidationError({"splits": f"Split leg #{idx} must be an object."})
            leg_idem = str(leg.get("idempotency_key") or "").strip()
            if leg_idem and PosPaymentAttempt.objects.filter(tenant=sale.tenant, idempotency_key=leg_idem).exists():
                raise ValidationError(
                    {
                        "error_code": "ERR_DUPLICATE_SPLIT_IDEMPOTENCY",
                        "detail": f"Split leg #{idx} idempotency_key '{leg_idem}' was already submitted.",
                    }
                )
            v_leg = cls._validate_tender_payload(
                sale=sale,
                data=leg,
                require_verified=require_verified,
                is_offline=is_offline,
                seen_batch_refs=seen_refs,
                prior_batch_due_bdt=batch_due_acc,
            )
            if v_leg["tender_type"] == PaymentMethod.DUE and v_leg["status"] == PosPaymentAttemptStatus.CAPTURED:
                batch_due_acc = q2(batch_due_acc + v_leg["amount_bdt"])
            validated_legs.append(v_leg)

        next_no = (sale.payment_attempts.count() or 0) + 1
        created_attempts: List[PosPaymentAttempt] = []
        now = timezone.now()
        for idx, v in enumerate(validated_legs):
            att = PosPaymentAttempt.objects.create(
                attempt_id=f"payatt-{uuid.uuid4().hex[:12]}",
                tenant=sale.tenant,
                branch=sale.branch,
                sale=sale,
                attempt_number=next_no + idx,
                tender_type=v["tender_type"],
                amount_bdt=v["amount_bdt"],
                net_settled_amount_bdt=(
                    v["amount_bdt"] if v["status"] == PosPaymentAttemptStatus.CAPTURED else Decimal("0.00")
                ),
                status=v["status"],
                idempotency_key=v["idempotency_key"],
                split_batch_key=batch_key,
                gateway_reference=v["gateway_reference"],
                payer_reference=v["payer_reference"],
                card_last4=v["card_last4"],
                verification_status=v["verification_status"],
                verification_channel=v["verification_channel"],
                failure_reason_code=v["failure_reason_code"],
                failure_message=v["failure_message"],
                processed_by=user,
                verified_by=user if v["verification_status"] == PosPaymentVerificationStatus.VERIFIED else None,
                verified_at=now if v["verification_status"] == PosPaymentVerificationStatus.VERIFIED else None,
                settled_at=now if v["status"] == PosPaymentAttemptStatus.CAPTURED else None,
            )
            created_attempts.append(att)

        old_state = sale.lifecycle_state
        cls.recalculate_sale_payment_totals(sale)
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="SPLIT_PAYMENTS_RECORDED",
            user=user,
            reason=f"Recorded {len(created_attempts)} split tender leg(s)",
            metadata={
                "split_batch_key": batch_key,
                "legs": [
                    {
                        "attempt_id": a.attempt_id,
                        "tender_type": a.tender_type,
                        "amount_bdt": str(a.amount_bdt),
                        "net_settled_amount_bdt": str(a.net_settled_amount_bdt),
                    }
                    for a in created_attempts
                ],
                "change_returned_bdt": str(sale.change_returned_bdt),
            },
        )
        return sale, created_attempts, True

    @classmethod
    @transaction.atomic
    def verify_payment_attempt(cls, user: Any, sale: PosSaleOrder, data: Dict[str, Any]) -> PosSaleOrder:
        from apps.core.pos_domain_service import PosDomainService

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)
        cls._ensure_sale_mutable_for_payment(sale)

        attempt_id = str(data.get("attempt_id") or "").strip()
        attempt = PosPaymentAttempt.objects.select_for_update().filter(
            tenant=sale.tenant, sale=sale, attempt_id=attempt_id
        ).first()
        if not attempt:
            raise ValidationError(
                {
                    "error_code": "ERR_PAYMENT_ATTEMPT_NOT_FOUND",
                    "attempt_id": "Payment attempt not found on this sale order.",
                }
            )
        if attempt.status in (PosPaymentAttemptStatus.VOIDED, PosPaymentAttemptStatus.REFUNDED):
            raise ValidationError(
                {
                    "error_code": "ERR_PAYMENT_ATTEMPT_LOCKED",
                    "detail": f"Cannot verify payment attempt in '{attempt.status}' state.",
                }
            )

        gw_ref = str(data.get("gateway_reference") or attempt.gateway_reference or "").strip()
        if attempt.tender_type in DIGITAL_TENDERS and len(gw_ref) < 4:
            raise ValidationError(
                {
                    "error_code": "ERR_DIGITAL_TENDER_REFERENCE_REQUIRED",
                    "gateway_reference": f"Verified {attempt.tender_type} confirmation requires reference >= 4 chars.",
                }
            )
        if gw_ref:
            dup = (
                PosPaymentAttempt.objects.filter(
                    tenant=sale.tenant,
                    tender_type=attempt.tender_type,
                    gateway_reference__iexact=gw_ref,
                    status__in=[PosPaymentAttemptStatus.CAPTURED, PosPaymentAttemptStatus.PENDING],
                )
                .exclude(attempt_id=attempt.attempt_id)
                .exists()
            )
            if dup:
                raise ValidationError(
                    {
                        "error_code": "ERR_DUPLICATE_GATEWAY_REFERENCE",
                        "gateway_reference": f"{attempt.tender_type} reference '{gw_ref}' is already assigned.",
                    }
                )

        old_state = sale.lifecycle_state
        now = timezone.now()
        attempt.gateway_reference = gw_ref
        if data.get("payer_reference"):
            attempt.payer_reference = str(data["payer_reference"]).strip()
        attempt.status = PosPaymentAttemptStatus.CAPTURED
        attempt.verification_status = PosPaymentVerificationStatus.VERIFIED
        attempt.verified_by = user
        attempt.verified_at = now
        attempt.settled_at = attempt.settled_at or now
        attempt.net_settled_amount_bdt = attempt.amount_bdt
        attempt.save()

        cls.recalculate_sale_payment_totals(sale)
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="PAYMENT_ATTEMPT_VERIFIED",
            user=user,
            reason=f"Verified {attempt.tender_type} ref {gw_ref}",
            metadata={"attempt_id": attempt.attempt_id, "gateway_reference": gw_ref},
        )
        return sale

    @classmethod
    @transaction.atomic
    def void_payment_attempt(cls, user: Any, sale: PosSaleOrder, data: Dict[str, Any]) -> PosSaleOrder:
        from apps.core.pos_domain_service import PosDomainService

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(user, branch_id=sale.branch_id, require_write=True)
        cls._ensure_sale_mutable_for_payment(sale)

        attempt_id = str(data.get("attempt_id") or "").strip()
        reason = str(data.get("reason") or "").strip()
        if len(reason) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_VOID_REASON_REQUIRED",
                    "reason": "A documented reason (>= 3 chars) is mandatory to void a payment attempt.",
                }
            )
        attempt = PosPaymentAttempt.objects.select_for_update().filter(
            tenant=sale.tenant, sale=sale, attempt_id=attempt_id
        ).first()
        if not attempt:
            raise ValidationError(
                {
                    "error_code": "ERR_PAYMENT_ATTEMPT_NOT_FOUND",
                    "attempt_id": "Payment attempt not found on this sale order.",
                }
            )
        if attempt.status in (PosPaymentAttemptStatus.VOIDED, PosPaymentAttemptStatus.REFUNDED):
            raise ValidationError(
                {
                    "error_code": "ERR_PAYMENT_ATTEMPT_ALREADY_VOIDED",
                    "detail": f"Payment attempt is already '{attempt.status}'.",
                }
            )

        old_state = sale.lifecycle_state
        attempt.status = PosPaymentAttemptStatus.VOIDED
        attempt.change_given_bdt = Decimal("0.00")
        attempt.net_settled_amount_bdt = Decimal("0.00")
        attempt.voided_by = user
        attempt.voided_at = timezone.now()
        attempt.void_reason = reason[:255]
        attempt.save(
            update_fields=[
                "status",
                "change_given_bdt",
                "net_settled_amount_bdt",
                "voided_by",
                "voided_at",
                "void_reason",
            ]
        )
        cls.recalculate_sale_payment_totals(sale)
        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="PAYMENT_ATTEMPT_VOIDED",
            user=user,
            reason=reason,
            metadata={"attempt_id": attempt.attempt_id, "tender_type": attempt.tender_type},
        )
        return sale

    @classmethod
    def assert_payments_ready_for_finalize(cls, sale: PosSaleOrder) -> List[PosPaymentAttempt]:
        pending_attempts = list(
            sale.payment_attempts.filter(
                status__in=[
                    PosPaymentAttemptStatus.PENDING,
                    PosPaymentAttemptStatus.AUTHORIZED,
                    PosPaymentAttemptStatus.INITIATED,
                ]
            )
        )
        if pending_attempts:
            raise ValidationError(
                {
                    "error_code": "ERR_UNVERIFIED_DIGITAL_PAYMENT",
                    "detail": (
                        f"Cannot finalize sale while {len(pending_attempts)} digital payment attempt(s) "
                        "await gateway confirmation."
                    ),
                }
            )
        cls.recalculate_sale_payment_totals(sale)
        settled_sum = q2(sale.paid_amount_bdt + sale.due_amount_bdt)
        if settled_sum != q2(sale.grand_total_bdt):
            raise ValidationError(
                {
                    "error_code": "ERR_PAYMENT_RECONCILIATION_MISMATCH",
                    "detail": (
                        f"Tender settlement mismatch: paid ({sale.paid_amount_bdt}) + due ({sale.due_amount_bdt}) = "
                        f"{settled_sum} BDT, expected grand_total = {sale.grand_total_bdt} BDT."
                    ),
                }
            )
        return list(
            sale.payment_attempts.filter(status=PosPaymentAttemptStatus.CAPTURED).order_by("attempt_number")
        )

    @classmethod
    def post_finalized_payment_ledger_and_journal_in_tx(
        cls,
        *,
        user: Any,
        sale: PosSaleOrder,
        invoice: SaleInvoice,
        captured_attempts: List[PosPaymentAttempt],
        is_offline_sync: bool = False,
    ) -> List[PosPaymentJournalEntry]:
        posted_entries: List[PosPaymentJournalEntry] = []
        for att in captured_attempts:
            net_amt = att.net_settled_amount_bdt if att.net_settled_amount_bdt > Decimal("0.00") else att.amount_bdt
            if net_amt <= Decimal("0.00"):
                continue
            tx = None
            if att.tender_type != PaymentMethod.DUE:
                tx = PaymentTransaction.objects.create(
                    transaction_id=f"pay_{uuid.uuid4().hex[:16]}",
                    tenant=sale.tenant,
                    branch=sale.branch,
                    invoice=invoice,
                    tender_type=att.tender_type,
                    gateway_reference=att.gateway_reference,
                    amount_bdt=net_amt,
                    status="SUCCESS",
                    initiated_by=sale.cashier_staff,
                )
                att.linked_payment_tx = tx
                att.save(update_fields=["linked_payment_tx"])

            entry_type = (
                PosPaymentJournalEntryType.DUE_RECEIVABLE
                if att.tender_type == PaymentMethod.DUE
                else PosPaymentJournalEntryType.SALE_SETTLEMENT
            )
            debit_acct = TENDER_CLEARING_ACCOUNTS.get(att.tender_type, "POS_OTHER_CLEARING")
            cash_impact = net_amt if att.tender_type == PaymentMethod.CASH else Decimal("0.00")
            ver_status = (
                PosPaymentVerificationStatus.OFFLINE_PENDING_VERIFY
                if (is_offline_sync and att.tender_type in DIGITAL_TENDERS)
                else att.verification_status
            )
            entry = PosPaymentJournalEntry.objects.create(
                journal_id=f"pjr-{uuid.uuid4().hex[:12]}",
                tenant=sale.tenant,
                branch=sale.branch,
                sale=sale,
                invoice=invoice,
                payment_attempt=att,
                payment_tx=tx,
                register_assignment=sale.register_assignment,
                entry_type=entry_type,
                tender_type=att.tender_type,
                debit_account=debit_acct,
                credit_account="POS_SALES_REVENUE",
                amount_bdt=net_amt,
                cash_register_impact_bdt=cash_impact,
                gateway_reference=att.gateway_reference,
                verification_status=ver_status,
                notes=f"Invoice {invoice.invoice_number} split tender #{att.attempt_number}",
                posted_by=user,
                is_offline_sync=bool(is_offline_sync),
            )
            posted_entries.append(entry)
        return posted_entries

    @classmethod
    def post_reversal_journal_and_refund_in_tx(
        cls,
        *,
        user: Any,
        sale: PosSaleOrder,
        reason: str,
    ) -> List[PosPaymentJournalEntry]:
        if sale.linked_invoice_id:
            PaymentTransaction.objects.filter(
                tenant=sale.tenant, invoice_id=sale.linked_invoice_id, status="SUCCESS"
            ).update(status="REVERSED")

        orig_entries = list(
            sale.payment_journal_entries.filter(
                entry_type__in=[
                    PosPaymentJournalEntryType.SALE_SETTLEMENT,
                    PosPaymentJournalEntryType.DUE_RECEIVABLE,
                ]
            ).order_by("posted_at", "journal_id")
        )
        prior_refunded_by_tender: Dict[str, Decimal] = {}
        for pr in sale.payment_journal_entries.filter(
            entry_type__in=[
                PosPaymentJournalEntryType.REFUND_REVERSAL,
                PosPaymentJournalEntryType.PARTIAL_RETURN_REFUND,
            ]
        ):
            prior_refunded_by_tender[pr.tender_type] = q2(
                prior_refunded_by_tender.get(pr.tender_type, Decimal("0.00")) + pr.amount_bdt
            )

        reversals: List[PosPaymentJournalEntry] = []
        for orig in orig_entries:
            already = prior_refunded_by_tender.get(orig.tender_type, Decimal("0.00"))
            if already >= orig.amount_bdt:
                prior_refunded_by_tender[orig.tender_type] = q2(already - orig.amount_bdt)
                continue
            rem = q2(orig.amount_bdt - already)
            if already > Decimal("0.00"):
                prior_refunded_by_tender[orig.tender_type] = Decimal("0.00")
            credit_acct = TENDER_REFUND_CLEARING_ACCOUNTS.get(orig.tender_type, "POS_REFUND_CLEARING")
            cash_impact = -rem if orig.tender_type == PaymentMethod.CASH else Decimal("0.00")
            rev_entry = PosPaymentJournalEntry.objects.create(
                journal_id=f"pjr-{uuid.uuid4().hex[:12]}",
                tenant=sale.tenant,
                branch=sale.branch,
                sale=sale,
                invoice=orig.invoice,
                payment_attempt=orig.payment_attempt,
                payment_tx=orig.payment_tx,
                register_assignment=sale.register_assignment,
                reversed_entry=orig,
                entry_type=PosPaymentJournalEntryType.REFUND_REVERSAL,
                tender_type=orig.tender_type,
                debit_account="SALES_RETURN_CONTRA",
                credit_account=credit_acct,
                amount_bdt=rem,
                cash_register_impact_bdt=cash_impact,
                gateway_reference=orig.gateway_reference,
                verification_status=PosPaymentVerificationStatus.VERIFIED,
                notes=f"Reversal: {reason[:120]}",
                posted_by=user,
            )
            reversals.append(rev_entry)
        return reversals

    @classmethod
    @transaction.atomic
    def post_partial_return_refund(
        cls,
        user: Any,
        sale: PosSaleOrder,
        data: Dict[str, Any],
    ) -> tuple[PosSaleOrder, List[PosPaymentJournalEntry]]:
        from apps.core.pos_domain_service import PosDomainService

        sale = PosSaleOrder.objects.select_for_update().get(pk=sale.pk)
        PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=sale.branch_id,
            require_write=True,
            allowed_roles=PosDomainService.POS_ELEVATED_ROLES,
        )
        if sale.lifecycle_state not in (PosSaleLifecycleState.FINALIZED, PosSaleLifecycleState.PARTIALLY_RETURNED):
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_LIFECYCLE_TRANSITION",
                    "detail": f"Partial return split refund only allowed on FINALIZED/PARTIALLY_RETURNED sales (got '{sale.lifecycle_state}').",
                }
            )
        reason = str(data.get("reason") or "").strip()
        if len(reason) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_REFUND_REASON_REQUIRED",
                    "reason": "Documented reason (>= 3 chars) is mandatory for refund posting.",
                }
            )

        raw_splits = data.get("refund_splits")
        if not isinstance(raw_splits, list) or not raw_splits:
            raise ValidationError(
                {
                    "error_code": "ERR_EMPTY_REFUND_SPLITS",
                    "refund_splits": "Provide `refund_splits` array with `{tender_type, amount_bdt}`.",
                }
            )

        # Compute net refundable per tender on this sale
        net_settled_by_tender: Dict[str, Decimal] = {}
        for j in sale.payment_journal_entries.all():
            if j.entry_type in (
                PosPaymentJournalEntryType.SALE_SETTLEMENT,
                PosPaymentJournalEntryType.DUE_RECEIVABLE,
            ):
                net_settled_by_tender[j.tender_type] = q2(
                    net_settled_by_tender.get(j.tender_type, Decimal("0.00")) + j.amount_bdt
                )
            elif j.entry_type in (
                PosPaymentJournalEntryType.REFUND_REVERSAL,
                PosPaymentJournalEntryType.PARTIAL_RETURN_REFUND,
            ):
                net_settled_by_tender[j.tender_type] = q2(
                    net_settled_by_tender.get(j.tender_type, Decimal("0.00")) - j.amount_bdt
                )

        entries_created: List[PosPaymentJournalEntry] = []
        non_due_refund_total = Decimal("0.00")
        due_refund_total = Decimal("0.00")

        for leg in raw_splits:
            if not isinstance(leg, dict):
                raise ValidationError({"refund_splits": "Each refund split entry must be an object."})
            tender = str(leg.get("tender_type") or "").upper().strip()
            if tender not in PaymentMethod.values:
                raise ValidationError({"error_code": "ERR_UNSUPPORTED_TENDER_METHOD", "tender_type": f"Invalid tender '{tender}'."})
            amt = parse_positive_money(leg.get("amount_bdt"), "amount_bdt")
            avail = net_settled_by_tender.get(tender, Decimal("0.00"))
            if amt > avail:
                raise ValidationError(
                    {
                        "error_code": "ERR_REFUND_EXCEEDS_TENDER_SETTLED",
                        "detail": f"Refund of {amt:.2f} BDT exceeds remaining settled {tender} balance ({avail:.2f} BDT).",
                    }
                )
            net_settled_by_tender[tender] = q2(avail - amt)
            cash_impact = -amt if tender == PaymentMethod.CASH else Decimal("0.00")
            credit_acct = TENDER_REFUND_CLEARING_ACCOUNTS.get(tender, "POS_REFUND_CLEARING")
            j_entry = PosPaymentJournalEntry.objects.create(
                journal_id=f"pjr-{uuid.uuid4().hex[:12]}",
                tenant=sale.tenant,
                branch=sale.branch,
                sale=sale,
                invoice=sale.linked_invoice,
                register_assignment=sale.register_assignment,
                entry_type=PosPaymentJournalEntryType.PARTIAL_RETURN_REFUND,
                tender_type=tender,
                debit_account="SALES_RETURN_CONTRA",
                credit_account=credit_acct,
                amount_bdt=amt,
                cash_register_impact_bdt=cash_impact,
                gateway_reference=str(leg.get("gateway_reference") or "").strip(),
                verification_status=PosPaymentVerificationStatus.VERIFIED,
                notes=f"Partial return refund: {reason[:120]}",
                posted_by=user,
            )
            entries_created.append(j_entry)
            if tender == PaymentMethod.DUE:
                due_refund_total = q2(due_refund_total + amt)
            else:
                non_due_refund_total = q2(non_due_refund_total + amt)
                PaymentTransaction.objects.create(
                    transaction_id=f"pay_ref_{uuid.uuid4().hex[:12]}",
                    tenant=sale.tenant,
                    branch=sale.branch,
                    invoice=sale.linked_invoice,
                    tender_type=tender,
                    gateway_reference=str(leg.get("gateway_reference") or "").strip(),
                    amount_bdt=amt,
                    status="REFUNDED",
                    initiated_by=sale.cashier_staff,
                )

        if due_refund_total > Decimal("0.00"):
            from apps.core.pos_customer_due_service import PosCustomerDueService

            PosCustomerDueService.sync_due_on_partial_return_in_tx(
                sale=sale,
                due_credit_bdt=due_refund_total,
                reason=reason,
            )

        old_state = sale.lifecycle_state
        sale.refunded_amount_bdt = q2(sale.refunded_amount_bdt + non_due_refund_total)
        remaining_total = q2(sale.grand_total_bdt - sale.refunded_amount_bdt - due_refund_total)
        sale.lifecycle_state = (
            PosSaleLifecycleState.FULLY_RETURNED
            if remaining_total <= Decimal("0.00") and sale.refunded_amount_bdt >= sale.paid_amount_bdt
            else PosSaleLifecycleState.PARTIALLY_RETURNED
        )
        sale.version += 1
        sale.updated_by = user
        sale.save(update_fields=["refunded_amount_bdt", "lifecycle_state", "version", "updated_by", "updated_at"])

        PosDomainService._record_lifecycle_audit(
            sale=sale,
            from_state=old_state,
            to_state=sale.lifecycle_state,
            action="PARTIAL_RETURN_SPLIT_REFUND",
            user=user,
            reason=reason,
            metadata={
                "non_due_refund_bdt": str(non_due_refund_total),
                "due_credit_note_bdt": str(due_refund_total),
                "journal_ids": [e.journal_id for e in entries_created],
            },
        )
        return sale, entries_created

    @classmethod
    def build_close_reconciliation_preview(
        cls,
        user: Any,
        *,
        branch_id: str,
        register_assignment_id: str = "",
        cashier_user_id: str = "",
    ) -> Dict[str, Any]:
        from apps.core.pos_domain_service import PosDomainService

        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=False,
            allowed_roles=PosDomainService.POS_READ_ROLES,
        )
        reg: Optional[CashierRegisterAssignment] = None
        if register_assignment_id:
            reg = CashierRegisterAssignment.objects.filter(
                tenant=tenant, branch=branch, assignment_id=register_assignment_id
            ).first()
            if not reg:
                raise ValidationError(
                    {
                        "error_code": "ERR_REGISTER_NOT_FOUND",
                        "register_assignment": "Register assignment not found in this branch.",
                    }
                )
        else:
            reg = (
                CashierRegisterAssignment.objects.filter(
                    tenant=tenant, branch=branch, status=RegisterAssignmentStatus.ACTIVE
                )
                .order_by("-assigned_at")
                .first()
            )

        opening_float = q2(reg.opening_float_amount) if reg else Decimal("0.00")

        sales_qs = PosSaleOrder.objects.filter(tenant=tenant, branch=branch)
        journal_qs = PosPaymentJournalEntry.objects.filter(tenant=tenant, branch=branch)
        if register_assignment_id and reg:
            sales_qs = sales_qs.filter(register_assignment=reg)
            journal_qs = journal_qs.filter(register_assignment=reg)
        if cashier_user_id:
            sales_qs = sales_qs.filter(cashier_user_id=cashier_user_id)
            journal_qs = journal_qs.filter(sale__cashier_user_id=cashier_user_id)

        gross_cash_tendered = Decimal("0.00")
        cash_change_returned = Decimal("0.00")
        cash_refunded = Decimal("0.00")
        tender_collected: Dict[str, Decimal] = {m: Decimal("0.00") for m in PaymentMethod.values}
        tender_refunded: Dict[str, Decimal] = {m: Decimal("0.00") for m in PaymentMethod.values}
        offline_pending_verify_count = 0

        # Calculate gross cash tendered & change directly from captured attempts on finalized/returned/reversed sales
        finalized_sales = list(
            sales_qs.filter(
                lifecycle_state__in=[
                    PosSaleLifecycleState.FINALIZED,
                    PosSaleLifecycleState.PARTIALLY_RETURNED,
                    PosSaleLifecycleState.FULLY_RETURNED,
                    PosSaleLifecycleState.REVERSED,
                ]
            )
        )
        sales_count = sum(
            1
            for s in finalized_sales
            if s.lifecycle_state in (PosSaleLifecycleState.FINALIZED, PosSaleLifecycleState.PARTIALLY_RETURNED)
        )
        reversed_count = sum(1 for s in finalized_sales if s.lifecycle_state == PosSaleLifecycleState.REVERSED)
        sale_ids = [s.sale_id for s in finalized_sales]

        for att in PosPaymentAttempt.objects.filter(
            tenant=tenant,
            sale_id__in=sale_ids,
            tender_type=PaymentMethod.CASH,
            status__in=[PosPaymentAttemptStatus.CAPTURED, PosPaymentAttemptStatus.REFUNDED],
        ):
            gross_cash_tendered = q2(gross_cash_tendered + att.amount_bdt)
            cash_change_returned = q2(cash_change_returned + att.change_given_bdt)

        total_debits = Decimal("0.00")
        total_credits = Decimal("0.00")
        due_collected_cash = Decimal("0.00")
        due_collected_digital = Decimal("0.00")
        for j in journal_qs:
            total_debits = q2(total_debits + j.amount_bdt)
            total_credits = q2(total_credits + j.amount_bdt)
            if j.verification_status == PosPaymentVerificationStatus.OFFLINE_PENDING_VERIFY:
                offline_pending_verify_count += 1
            if j.entry_type in (
                PosPaymentJournalEntryType.SALE_SETTLEMENT,
                PosPaymentJournalEntryType.DUE_RECEIVABLE,
            ):
                tender_collected[j.tender_type] = q2(
                    tender_collected.get(j.tender_type, Decimal("0.00")) + j.amount_bdt
                )
            elif j.entry_type == PosPaymentJournalEntryType.DUE_COLLECTION:
                if j.tender_type == PaymentMethod.CASH:
                    due_collected_cash = q2(due_collected_cash + j.amount_bdt)
                elif j.tender_type in DIGITAL_TENDERS:
                    due_collected_digital = q2(due_collected_digital + j.amount_bdt)
            elif j.entry_type == PosPaymentJournalEntryType.DUE_COLLECTION_REVERSAL:
                if j.tender_type == PaymentMethod.CASH:
                    due_collected_cash = q2(due_collected_cash - j.amount_bdt)
                elif j.tender_type in DIGITAL_TENDERS:
                    due_collected_digital = q2(due_collected_digital - j.amount_bdt)
            elif j.entry_type in (
                PosPaymentJournalEntryType.REFUND_REVERSAL,
                PosPaymentJournalEntryType.PARTIAL_RETURN_REFUND,
            ):
                tender_refunded[j.tender_type] = q2(
                    tender_refunded.get(j.tender_type, Decimal("0.00")) + j.amount_bdt
                )
                if j.tender_type == PaymentMethod.CASH:
                    cash_refunded = q2(cash_refunded + j.amount_bdt)

        net_cash_collected = q2(tender_collected[PaymentMethod.CASH])
        # Fallback parity if gross_cash_tendered wasn't incremented on legacy sales
        if gross_cash_tendered == Decimal("0.00") and net_cash_collected > Decimal("0.00"):
            gross_cash_tendered = net_cash_collected
        due_collected_total = q2(due_collected_cash + due_collected_digital)
        expected_drawer_cash = q2(opening_float + net_cash_collected + due_collected_cash - cash_refunded)

        digital_total = sum(
            (q2(tender_collected[m] - tender_refunded[m]) for m in DIGITAL_TENDERS),
            Decimal("0.00"),
        )
        due_net = q2(tender_collected[PaymentMethod.DUE] - tender_refunded[PaymentMethod.DUE])

        pending_unverified_count = PosPaymentAttempt.objects.filter(
            tenant=tenant,
            branch=branch,
            status__in=[PosPaymentAttemptStatus.PENDING, PosPaymentAttemptStatus.AUTHORIZED],
        ).count()

        tender_totals = {
            m: {
                "collected_bdt": f"{tender_collected[m]:.2f}",
                "refunded_bdt": f"{tender_refunded[m]:.2f}",
                "net_bdt": f"{q2(tender_collected[m] - tender_refunded[m]):.2f}",
            }
            for m in PaymentMethod.values
            if tender_collected[m] > Decimal("0.00")
            or tender_refunded[m] > Decimal("0.00")
            or m in (PaymentMethod.CASH, PaymentMethod.BKASH, PaymentMethod.NAGAD, PaymentMethod.CARD, PaymentMethod.DUE)
        }

        return {
            "tenant_id": tenant.tenant_id,
            "branch_id": branch.branch_id,
            "branch_name": branch.name,
            "register_assignment_id": reg.assignment_id if reg else "",
            "opening_float_bdt": f"{opening_float:.2f}",
            "gross_cash_tendered_bdt": f"{gross_cash_tendered:.2f}",
            "cash_change_returned_bdt": f"{cash_change_returned:.2f}",
            "net_cash_collected_bdt": f"{net_cash_collected:.2f}",
            "cash_refunded_bdt": f"{cash_refunded:.2f}",
            "expected_drawer_cash_bdt": f"{expected_drawer_cash:.2f}",
            "digital_total_bdt": f"{digital_total:.2f}",
            "due_issued_bdt": f"{due_net:.2f}",
            "due_collected_cash_bdt": f"{due_collected_cash:.2f}",
            "due_collected_digital_bdt": f"{due_collected_digital:.2f}",
            "due_collected_total_bdt": f"{due_collected_total:.2f}",
            "sales_count": sales_count,
            "reversed_sales_count": reversed_count,
            "pending_unverified_attempts": pending_unverified_count,
            "offline_pending_verify_entries": offline_pending_verify_count,
            "journal_balanced": total_debits == total_credits,
            "tender_totals": tender_totals,
        }

    @classmethod
    @transaction.atomic
    def submit_close_reconciliation(
        cls,
        user: Any,
        data: Dict[str, Any],
    ) -> PosCashierCloseReconciliation:
        from apps.core.pos_domain_service import PosDomainService

        branch_id = str(data.get("branch") or data.get("branch_id") or getattr(user, "branch_id", "") or "").strip()
        if not branch_id:
            raise ValidationError({"branch": "branch is required for cashier close reconciliation."})
        tenant, branch = PosDomainService.resolve_user_tenant_and_branch(
            user,
            branch_id=branch_id,
            require_write=True,
        )

        reg_id = str(data.get("register_assignment") or data.get("register_assignment_id") or "").strip()
        cashier_id = str(data.get("cashier_user_id") or "").strip()

        raw_counted = data.get("counted_cash_bdt")
        if raw_counted is None or str(raw_counted).strip() == "":
            raise ValidationError(
                {
                    "error_code": "ERR_COUNTED_CASH_REQUIRED",
                    "counted_cash_bdt": "counted_cash_bdt is mandatory to submit cashier close reconciliation.",
                }
            )
        try:
            counted_cash = q2(raw_counted)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError(
                {
                    "error_code": "ERR_INVALID_PAYMENT_AMOUNT",
                    "counted_cash_bdt": "counted_cash_bdt must be a valid decimal amount.",
                }
            ) from exc
        if counted_cash < Decimal("0.00"):
            raise ValidationError(
                {
                    "error_code": "ERR_NEGATIVE_COUNTED_CASH",
                    "counted_cash_bdt": "counted_cash_bdt cannot be negative.",
                }
            )

        preview = cls.build_close_reconciliation_preview(
            user,
            branch_id=branch.branch_id,
            register_assignment_id=reg_id,
            cashier_user_id=cashier_id,
        )
        expected_cash = q2(preview["expected_drawer_cash_bdt"])
        variance = q2(counted_cash - expected_cash)
        notes = str(data.get("notes") or "").strip()

        if variance != Decimal("0.00") and len(notes) < 3:
            raise ValidationError(
                {
                    "error_code": "ERR_VARIANCE_NOTES_REQUIRED",
                    "notes": f"Variance of {variance:+.2f} BDT requires a documented explanation (>= 3 chars).",
                }
            )

        if variance == Decimal("0.00"):
            rec_status = PosCloseReconciliationStatus.BALANCED
        elif variance < Decimal("0.00"):
            rec_status = PosCloseReconciliationStatus.SHORTAGE
        else:
            rec_status = PosCloseReconciliationStatus.SURPLUS

        reg_obj: Optional[CashierRegisterAssignment] = None
        eff_reg_id = reg_id or str(preview.get("register_assignment_id") or "")
        if eff_reg_id:
            reg_obj = CashierRegisterAssignment.objects.select_for_update().filter(
                tenant=tenant, branch=branch, assignment_id=eff_reg_id
            ).first()
            if reg_obj and reg_obj.status == RegisterAssignmentStatus.ACTIVE:
                reg_obj.closing_float_amount = counted_cash
                reg_obj.status = RegisterAssignmentStatus.CLOSED
                reg_obj.closed_at = timezone.now()
                if notes:
                    reg_obj.notes = f"{reg_obj.notes} | Close: {notes[:120]}".strip(" |")
                reg_obj.save(update_fields=["closing_float_amount", "status", "closed_at", "notes"])

        rec = PosCashierCloseReconciliation.objects.create(
            reconciliation_id=f"pcr-{uuid.uuid4().hex[:12]}",
            tenant=tenant,
            branch=branch,
            register_assignment=reg_obj,
            cashier_user=user,
            opening_float_bdt=q2(preview["opening_float_bdt"]),
            gross_cash_tendered_bdt=q2(preview["gross_cash_tendered_bdt"]),
            cash_change_returned_bdt=q2(preview["cash_change_returned_bdt"]),
            net_cash_collected_bdt=q2(preview["net_cash_collected_bdt"]),
            cash_refunded_bdt=q2(preview["cash_refunded_bdt"]),
            expected_drawer_cash_bdt=expected_cash,
            counted_cash_bdt=counted_cash,
            variance_bdt=variance,
            digital_total_bdt=q2(preview["digital_total_bdt"]),
            due_issued_bdt=q2(preview["due_issued_bdt"]),
            due_collected_cash_bdt=q2(preview.get("due_collected_cash_bdt", "0.00")),
            due_collected_digital_bdt=q2(preview.get("due_collected_digital_bdt", "0.00")),
            tender_totals_json=preview["tender_totals"],
            sales_count=int(preview["sales_count"]),
            reversed_sales_count=int(preview["reversed_sales_count"]),
            status=rec_status,
            notes=notes,
            closed_by=user,
        )
        return rec
