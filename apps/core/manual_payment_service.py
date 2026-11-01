"""
backend/apps/core/manual_payment_service.py
Architecture Decision: ADR-0015, ADR-0033, ADR-0087 & ADR-0088
Phase 05: Basic/Standard/Pro Subscription & Billing (P088)

Manual Payment Verification Service:
- Submits verifiable manual payment claims with evidence (bKash, Nagad, Bank Transfer, etc.).
- Enforces strict Maker-Checker separation (Maker cannot self-approve claims).
- Validates claimed amount precision against invoice total in BDT.
- Atomically settles invoices and extends subscriptions upon checker approval.
- Enforces mandatory audit rejection reasons.
- Produces immutable activation outcomes and audit log records.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.models import (
    ManualPaymentClaim,
    ManualPaymentClaimStatus,
    ManualPaymentMethod,
    RenewalInvoiceStatus,
    SubscriptionRecord,
    SubscriptionRenewalInvoice,
    Tenant,
)
from apps.core.renewal_invoice_service import RenewalInvoiceService

logger = logging.getLogger("pharmacy.billing.manual_payments")


class ManualPaymentService:
    @classmethod
    def submit_claim(
        cls,
        tenant_id: str,
        invoice_id: str,
        payment_method: str,
        transaction_reference: str,
        sender_account_number: str,
        claimed_amount_bdt: Decimal | str | float,
        evidence_file_url: str,
        evidence_notes: str = "",
        maker_user: Any = None,
    ) -> ManualPaymentClaim:
        """Submits a manual payment verification claim with evidence."""
        clean_tid = str(tenant_id).strip()
        tenant = Tenant.objects.filter(tenant_id=clean_tid).first()
        if not tenant:
            raise ValidationError(f"Tenant '{clean_tid}' not found.")

        clean_inv_id = str(invoice_id).strip()
        invoice = (
            SubscriptionRenewalInvoice.objects.filter(invoice_id=clean_inv_id, tenant=tenant).first()
            or SubscriptionRenewalInvoice.objects.filter(invoice_number=clean_inv_id, tenant=tenant).first()
        )
        if not invoice:
            raise ValidationError(f"Renewal invoice '{clean_inv_id}' not found for tenant '{clean_tid}'.")

        if invoice.status not in [RenewalInvoiceStatus.ISSUED, RenewalInvoiceStatus.PAST_DUE]:
            raise ValidationError(
                f"Invoice '{invoice.invoice_number}' is in status '{invoice.status}' and cannot accept manual payment claims."
            )

        try:
            claimed_dec = Decimal(str(claimed_amount_bdt)).quantize(Decimal("0.01"))
        except Exception:
            raise ValidationError("Invalid claimed amount specified.")

        if claimed_dec <= Decimal("0.00"):
            raise ValidationError("Claimed payment amount must be greater than zero.")

        if claimed_dec != invoice.total_bdt:
            raise ValidationError(
                f"Claimed amount {claimed_dec} BDT does not match invoice total payable {invoice.total_bdt} BDT."
            )

        clean_method = str(payment_method or "BKASH").upper().strip()
        if clean_method not in ManualPaymentMethod.values:
            raise ValidationError(
                f"Invalid payment method '{clean_method}'. Valid options: {', '.join(ManualPaymentMethod.values)}"
            )

        clean_ref = str(transaction_reference or "").strip()
        if len(clean_ref) < 4:
            raise ValidationError("A valid transaction reference (minimum 4 characters) is required.")

        clean_sender = str(sender_account_number or "").strip()
        if len(clean_sender) < 3:
            raise ValidationError("A valid sender phone number or bank account number is required.")

        clean_evidence = str(evidence_file_url or "").strip()
        if len(clean_evidence) < 4:
            raise ValidationError("Evidence document URL or file reference is required.")

        # Check for active pending claims on the same invoice
        existing_claim = ManualPaymentClaim.objects.filter(
            invoice=invoice,
            status=ManualPaymentClaimStatus.PENDING_VERIFICATION,
        ).first()
        if existing_claim:
            raise ValidationError(
                f"A pending payment verification claim '{existing_claim.claim_number}' already exists for invoice '{invoice.invoice_number}'."
            )

        now = timezone.now()
        clean_suffix = clean_tid.split("-")[-1].upper() if "-" in clean_tid else clean_tid[-4:].upper()
        unique_claim_num = f"PAY-CLM-{now.strftime('%Y%m')}-{clean_suffix}-{uuid.uuid4().hex[:6].upper()}"

        maker_id = str(getattr(maker_user, "user_id", None) or getattr(maker_user, "id", None) or "system")
        maker_email = str(getattr(maker_user, "email", None) or "owner@pharma.local")

        claim = ManualPaymentClaim.objects.create(
            claim_id=f"clm-{uuid.uuid4().hex[:12]}",
            claim_number=unique_claim_num,
            tenant=tenant,
            invoice=invoice,
            payment_method=clean_method,
            transaction_reference=clean_ref,
            sender_account_number=clean_sender,
            claimed_amount_bdt=claimed_dec,
            evidence_file_url=clean_evidence,
            evidence_notes=evidence_notes.strip(),
            status=ManualPaymentClaimStatus.PENDING_VERIFICATION,
            maker_user_id=maker_id,
            maker_user_email=maker_email,
        )

        AuditLogEntry.objects.create(
            action="MANUAL_PAYMENT_CLAIM_SUBMITTED",
            resource_type="MANUAL_PAYMENT_CLAIM",
            resource_id=claim.claim_number,
            tenant_id=tenant.tenant_id,
            user_id=maker_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "claim_number": claim.claim_number,
                "invoice_number": invoice.invoice_number,
                "amount_bdt": str(claimed_dec),
                "payment_method": clean_method,
                "transaction_reference": clean_ref,
                "maker_email": maker_email,
            },
        )

        logger.info(f"Manual payment claim '{claim.claim_number}' submitted by '{maker_email}' for {claimed_dec} BDT.")
        return claim

    @classmethod
    def approve_claim(
        cls,
        claim_id: str,
        checker_user: Any,
        checker_notes: str = "",
    ) -> ManualPaymentClaim:
        """Approves a manual payment claim, settles the invoice, and activates subscription."""
        clean_id = str(claim_id).strip()
        claim = (
            ManualPaymentClaim.objects.filter(claim_id=clean_id).select_related("tenant", "invoice").first()
            or ManualPaymentClaim.objects.filter(claim_number=clean_id).select_related("tenant", "invoice").first()
        )
        if not claim:
            raise ValidationError(f"Manual payment claim '{clean_id}' not found.")

        if claim.status != ManualPaymentClaimStatus.PENDING_VERIFICATION:
            raise ValidationError(
                f"Manual payment claim '{claim.claim_number}' is in status '{claim.status}' and cannot be approved."
            )

        checker_id = str(getattr(checker_user, "user_id", None) or getattr(checker_user, "id", None) or "system")
        checker_email = str(getattr(checker_user, "email", None) or "checker@platform.internal")

        # Maker-Checker Separation Invariant
        if str(checker_id) == str(claim.maker_user_id):
            raise ValidationError("Maker-checker violation: Maker cannot self-approve their own manual payment claim.")

        # Settle Invoice via RenewalInvoiceService
        settled_invoice = RenewalInvoiceService.settle_renewal_invoice(
            invoice_id=claim.invoice.invoice_id,
            payment_method=claim.payment_method,
            payment_reference=claim.transaction_reference,
            actor_user=checker_user,
        )

        now = timezone.now()
        sub = settled_invoice.subscription or SubscriptionRecord.objects.filter(tenant=claim.tenant).first()

        activation_outcomes = {
            "invoice_id": settled_invoice.invoice_id,
            "invoice_number": settled_invoice.invoice_number,
            "settled_at": now.isoformat(),
            "amount_bdt": str(settled_invoice.total_bdt),
            "plan_tier": settled_invoice.plan_tier,
            "billing_cycle": settled_invoice.billing_cycle,
            "previous_renewal_date": settled_invoice.period_start.isoformat(),
            "new_renewal_date": settled_invoice.period_end.isoformat(),
            "subscription_status": sub.status if sub else "ACTIVE",
            "checker_user_id": checker_id,
            "checker_email": checker_email,
        }

        claim.status = ManualPaymentClaimStatus.APPROVED
        claim.checker_user_id = checker_id
        claim.checker_user_email = checker_email
        claim.checker_notes = checker_notes.strip()
        claim.verified_at = now
        claim.activation_outcomes_json = activation_outcomes
        claim.save(update_fields=["status", "checker_user_id", "checker_user_email", "checker_notes", "verified_at", "activation_outcomes_json", "updated_at"])

        AuditLogEntry.objects.create(
            action="MANUAL_PAYMENT_APPROVED_AND_ACTIVATED",
            resource_type="MANUAL_PAYMENT_CLAIM",
            resource_id=claim.claim_number,
            tenant_id=claim.tenant.tenant_id,
            user_id=checker_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json=activation_outcomes,
        )

        logger.info(f"Claim '{claim.claim_number}' approved by checker '{checker_email}'. Subscription renewal extended to '{settled_invoice.period_end}'.")
        return claim

    @classmethod
    def reject_claim(
        cls,
        claim_id: str,
        checker_user: Any,
        rejection_reason: str,
        checker_notes: str = "",
    ) -> ManualPaymentClaim:
        """Rejects a manual payment claim with mandatory business audit reason."""
        clean_id = str(claim_id).strip()
        claim = (
            ManualPaymentClaim.objects.filter(claim_id=clean_id).select_related("tenant", "invoice").first()
            or ManualPaymentClaim.objects.filter(claim_number=clean_id).select_related("tenant", "invoice").first()
        )
        if not claim:
            raise ValidationError(f"Manual payment claim '{clean_id}' not found.")

        if claim.status != ManualPaymentClaimStatus.PENDING_VERIFICATION:
            raise ValidationError(
                f"Manual payment claim '{claim.claim_number}' is in status '{claim.status}' and cannot be rejected."
            )

        clean_reason = str(rejection_reason or "").strip()
        if not clean_reason:
            raise ValidationError("A clear rejection reason is mandatory when rejecting a payment claim.")

        checker_id = str(getattr(checker_user, "user_id", None) or getattr(checker_user, "id", None) or "system")
        checker_email = str(getattr(checker_user, "email", None) or "checker@platform.internal")
        now = timezone.now()

        claim.status = ManualPaymentClaimStatus.REJECTED
        claim.checker_user_id = checker_id
        claim.checker_user_email = checker_email
        claim.checker_notes = checker_notes.strip()
        claim.rejection_reason = clean_reason
        claim.verified_at = now
        claim.save(update_fields=["status", "checker_user_id", "checker_user_email", "checker_notes", "rejection_reason", "verified_at", "updated_at"])

        AuditLogEntry.objects.create(
            action="MANUAL_PAYMENT_REJECTED",
            resource_type="MANUAL_PAYMENT_CLAIM",
            resource_id=claim.claim_number,
            tenant_id=claim.tenant.tenant_id,
            user_id=checker_id,
            status=AuditActionStatus.DENIED,
            metadata_json={
                "claim_number": claim.claim_number,
                "invoice_number": claim.invoice.invoice_number,
                "rejection_reason": clean_reason,
                "checker_email": checker_email,
            },
        )

        logger.info(f"Claim '{claim.claim_number}' rejected by '{checker_email}'. Reason: {clean_reason}")
        return claim

    @classmethod
    def cancel_claim(cls, claim_id: str, actor_user: Any) -> ManualPaymentClaim:
        """Maker cancels their own pending verification claim."""
        clean_id = str(claim_id).strip()
        claim = (
            ManualPaymentClaim.objects.filter(claim_id=clean_id).select_related("tenant", "invoice").first()
            or ManualPaymentClaim.objects.filter(claim_number=clean_id).select_related("tenant", "invoice").first()
        )
        if not claim:
            raise ValidationError(f"Manual payment claim '{clean_id}' not found.")

        if claim.status != ManualPaymentClaimStatus.PENDING_VERIFICATION:
            raise ValidationError(
                f"Manual payment claim '{claim.claim_number}' is in status '{claim.status}' and cannot be cancelled."
            )

        actor_id = str(getattr(actor_user, "user_id", None) or getattr(actor_user, "id", None) or "system")
        claim.status = ManualPaymentClaimStatus.CANCELLED
        claim.save(update_fields=["status", "updated_at"])

        AuditLogEntry.objects.create(
            action="MANUAL_PAYMENT_CLAIM_CANCELLED",
            resource_type="MANUAL_PAYMENT_CLAIM",
            resource_id=claim.claim_number,
            tenant_id=claim.tenant.tenant_id,
            user_id=actor_id,
            status=AuditActionStatus.SUCCESS,
            metadata_json={"claim_number": claim.claim_number},
        )
        return claim

    @classmethod
    def serialize_claim(cls, claim: ManualPaymentClaim) -> dict[str, Any]:
        """Serializes claim matching manual_payment_claim.schema.json."""
        return {
            "claim_id": claim.claim_id,
            "claim_number": claim.claim_number,
            "tenant_id": claim.tenant.tenant_id,
            "invoice_id": claim.invoice.invoice_id,
            "invoice_number": claim.invoice.invoice_number,
            "payment_method": claim.payment_method,
            "transaction_reference": claim.transaction_reference,
            "sender_account_number": claim.sender_account_number,
            "claimed_amount_bdt": str(claim.claimed_amount_bdt),
            "evidence_file_url": claim.evidence_file_url,
            "evidence_notes": claim.evidence_notes or None,
            "status": claim.status,
            "maker_user_id": claim.maker_user_id,
            "maker_user_email": claim.maker_user_email,
            "checker_user_id": claim.checker_user_id or None,
            "checker_user_email": claim.checker_user_email or None,
            "checker_notes": claim.checker_notes or None,
            "rejection_reason": claim.rejection_reason or None,
            "submitted_at": claim.submitted_at.isoformat(),
            "verified_at": claim.verified_at.isoformat() if claim.verified_at else None,
            "activation_outcomes": claim.activation_outcomes_json or None,
        }
