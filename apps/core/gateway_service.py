"""
backend/apps/core/gateway_service.py
Architecture Decision: ADR-0015, ADR-0033 & ADR-0089
Phase 05: Basic/Standard/Pro Subscription & Billing (P089)

Payment Gateway Orchestration Service:
- Manages checkout initiation via pluggable gateway adapters.
- Validates signed HMAC-SHA256 webhook callbacks.
- Enforces strict zero-stored-secrets rule.
- Guarantees idempotent deduplication preventing double billing.
- Reconciles payments and activates subscriptions.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit_logging.models import AuditActionStatus, AuditLogEntry
from apps.core.gateway_adapters import PaymentGatewayRegistry
from apps.core.models import (
    GatewayWebhookStatus,
    PaymentGatewayWebhookLog,
    RenewalInvoiceStatus,
    SubscriptionRenewalInvoice,
    Tenant,
)
from apps.core.renewal_invoice_service import RenewalInvoiceService

logger = logging.getLogger("pharmacy.billing.gateway_service")


class PaymentGatewayService:
    @classmethod
    def initiate_checkout(
        cls,
        tenant_id: str,
        invoice_id: str,
        gateway_name: str = "BKASH",
        customer_phone: str = "",
        customer_email: str = "",
        return_url: str = "",
        cancel_url: str = "",
        actor_user: Any = None,
    ) -> dict[str, Any]:
        """Initiates a checkout session through the configured gateway adapter."""
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
                f"Invoice '{invoice.invoice_number}' is in status '{invoice.status}' and cannot initiate checkout."
            )

        adapter = PaymentGatewayRegistry.get_adapter(gateway_name)
        session = adapter.initiate_checkout(
            invoice=invoice,
            customer_phone=customer_phone,
            customer_email=customer_email,
            return_url=return_url or "https://pharmasaas.local/billing/return",
            cancel_url=cancel_url or "https://pharmasaas.local/billing/cancel",
        )

        AuditLogEntry.objects.create(
            action="GATEWAY_CHECKOUT_INITIATED",
            resource_type="BILLING_INVOICE",
            resource_id=invoice.invoice_number,
            tenant_id=tenant.tenant_id,
            user_id=str(getattr(actor_user, "email", None) or getattr(actor_user, "user_id", None) or "system"),
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "gateway_name": session.gateway_name,
                "gateway_payment_id": session.gateway_payment_id,
                "amount_bdt": str(session.amount_bdt),
            },
        )

        return {
            "gateway_name": session.gateway_name,
            "checkout_url": session.checkout_url,
            "gateway_payment_id": session.gateway_payment_id,
            "invoice_number": session.invoice_number,
            "amount_bdt": str(session.amount_bdt),
            "currency": session.currency,
        }

    @classmethod
    def handle_webhook(
        cls,
        gateway_name: str,
        raw_body_bytes: bytes,
        headers: dict[str, str],
        payload_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Validates signed webhook, checks idempotency, and settles invoice on payment completion."""
        adapter = PaymentGatewayRegistry.get_adapter(gateway_name)
        result = adapter.parse_webhook(payload_dict, raw_body_bytes, headers)
        payload_hash = hashlib.sha256(raw_body_bytes).hexdigest()
        event_id = f"evt-{uuid.uuid4().hex[:12]}"
        now = timezone.now()

        # 1. Signature Verification Barrier
        if not result.is_valid_signature:
            PaymentGatewayWebhookLog.objects.create(
                event_id=event_id,
                gateway_name=adapter.gateway_name,
                event_type=result.event_type,
                idempotency_key=result.idempotency_key or f"idemp-invalid-{uuid.uuid4().hex[:8]}",
                gateway_payment_id=result.gateway_payment_id or "UNKNOWN",
                invoice_number=result.invoice_number or "UNKNOWN",
                amount_bdt=result.amount_bdt,
                currency=result.currency,
                payload_hash=payload_hash,
                payload_json=payload_dict,
                status=GatewayWebhookStatus.SIGNATURE_INVALID,
                signature_verified=False,
                error_message="Invalid cryptographic HMAC-SHA256 signature.",
            )
            logger.warning(f"Rejected forged webhook from gateway '{adapter.gateway_name}'.")
            raise ValidationError("Invalid gateway webhook HMAC signature.")

        # 2. Idempotency Check: Prevent duplicate webhook execution
        existing_log = PaymentGatewayWebhookLog.objects.filter(
            gateway_name=adapter.gateway_name,
            idempotency_key=result.idempotency_key,
            status=GatewayWebhookStatus.PROCESSED,
        ).first()

        if existing_log:
            PaymentGatewayWebhookLog.objects.create(
                event_id=event_id,
                gateway_name=adapter.gateway_name,
                event_type=result.event_type,
                idempotency_key=result.idempotency_key,
                gateway_payment_id=result.gateway_payment_id,
                invoice_number=result.invoice_number,
                tenant=existing_log.tenant,
                amount_bdt=result.amount_bdt,
                currency=result.currency,
                payload_hash=payload_hash,
                payload_json=payload_dict,
                status=GatewayWebhookStatus.DUPLICATE,
                signature_verified=True,
                error_message="Duplicate event ignored via idempotency barrier.",
                processed_at=now,
            )
            logger.info(f"Duplicate webhook event '{result.idempotency_key}' ignored safely.")
            return {
                "event_id": event_id,
                "gateway_name": adapter.gateway_name,
                "event_type": result.event_type,
                "idempotency_key": result.idempotency_key,
                "gateway_payment_id": result.gateway_payment_id,
                "invoice_number": result.invoice_number,
                "amount_bdt": str(result.amount_bdt),
                "currency": result.currency,
                "status": "DUPLICATE",
                "signature_verified": True,
                "timestamp": now.isoformat(),
                "error_message": "Duplicate event ignored via idempotency barrier.",
            }

        # 3. Invoice Matching & Amount Verification
        invoice = SubscriptionRenewalInvoice.objects.filter(invoice_number=result.invoice_number).first()
        if not invoice:
            PaymentGatewayWebhookLog.objects.create(
                event_id=event_id,
                gateway_name=adapter.gateway_name,
                event_type=result.event_type,
                idempotency_key=result.idempotency_key,
                gateway_payment_id=result.gateway_payment_id,
                invoice_number=result.invoice_number,
                amount_bdt=result.amount_bdt,
                currency=result.currency,
                payload_hash=payload_hash,
                payload_json=payload_dict,
                status=GatewayWebhookStatus.FAILED,
                signature_verified=True,
                error_message=f"Invoice '{result.invoice_number}' not found.",
            )
            raise ValidationError(f"Renewal invoice '{result.invoice_number}' not found.")

        if result.amount_bdt != invoice.total_bdt:
            PaymentGatewayWebhookLog.objects.create(
                event_id=event_id,
                gateway_name=adapter.gateway_name,
                event_type=result.event_type,
                idempotency_key=result.idempotency_key,
                gateway_payment_id=result.gateway_payment_id,
                invoice_number=result.invoice_number,
                tenant=invoice.tenant,
                amount_bdt=result.amount_bdt,
                currency=result.currency,
                payload_hash=payload_hash,
                payload_json=payload_dict,
                status=GatewayWebhookStatus.FAILED,
                signature_verified=True,
                error_message=f"Webhook amount {result.amount_bdt} does not match invoice total {invoice.total_bdt}.",
            )
            raise ValidationError(
                f"Received webhook amount {result.amount_bdt} BDT does not match invoice total {invoice.total_bdt} BDT."
            )

        # 4. Settle Invoice & Extend Subscription
        if result.event_type == "PAYMENT_COMPLETED":
            RenewalInvoiceService.settle_renewal_invoice(
                invoice_id=invoice.invoice_id,
                payment_method=adapter.gateway_name,
                payment_reference=result.gateway_payment_id,
                actor_user=None,
            )

        # 5. Record Processed Log
        webhook_log = PaymentGatewayWebhookLog.objects.create(
            event_id=event_id,
            gateway_name=adapter.gateway_name,
            event_type=result.event_type,
            idempotency_key=result.idempotency_key,
            gateway_payment_id=result.gateway_payment_id,
            invoice_number=result.invoice_number,
            tenant=invoice.tenant,
            amount_bdt=result.amount_bdt,
            currency=result.currency,
            payload_hash=payload_hash,
            payload_json=payload_dict,
            status=GatewayWebhookStatus.PROCESSED,
            signature_verified=True,
            processed_at=now,
        )

        AuditLogEntry.objects.create(
            action="GATEWAY_WEBHOOK_PAYMENT_SETTLED",
            resource_type="GATEWAY_WEBHOOK",
            resource_id=result.gateway_payment_id,
            tenant_id=invoice.tenant.tenant_id,
            user_id="gateway-webhook",
            status=AuditActionStatus.SUCCESS,
            metadata_json={
                "gateway_name": adapter.gateway_name,
                "invoice_number": invoice.invoice_number,
                "amount_bdt": str(result.amount_bdt),
                "idempotency_key": result.idempotency_key,
            },
        )

        logger.info(f"Gateway webhook processed successfully for invoice '{invoice.invoice_number}' via {adapter.gateway_name}.")
        return cls.serialize_webhook_log(webhook_log)

    @classmethod
    def reconcile_invoice(cls, invoice_id: str, actor_user: Any = None) -> dict[str, Any]:
        """Reconciles payment gateway status for an invoice."""
        clean_inv_id = str(invoice_id).strip()
        invoice = (
            SubscriptionRenewalInvoice.objects.filter(invoice_id=clean_inv_id).select_related("tenant", "subscription").first()
            or SubscriptionRenewalInvoice.objects.filter(invoice_number=clean_inv_id).select_related("tenant", "subscription").first()
        )
        if not invoice:
            raise ValidationError(f"Renewal invoice '{clean_inv_id}' not found.")

        processed_log = PaymentGatewayWebhookLog.objects.filter(
            invoice_number=invoice.invoice_number,
            status=GatewayWebhookStatus.PROCESSED,
        ).first()

        is_reconciled = invoice.status == RenewalInvoiceStatus.PAID
        return {
            "invoice_id": invoice.invoice_id,
            "invoice_number": invoice.invoice_number,
            "tenant_id": invoice.tenant.tenant_id,
            "status": invoice.status,
            "total_bdt": str(invoice.total_bdt),
            "is_reconciled": is_reconciled,
            "gateway_name": processed_log.gateway_name if processed_log else (invoice.payment_method or None),
            "gateway_payment_id": processed_log.gateway_payment_id if processed_log else (invoice.payment_reference or None),
            "reconciled_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        }

    @classmethod
    def serialize_webhook_log(cls, log: PaymentGatewayWebhookLog) -> dict[str, Any]:
        """Serializes log matching gateway_webhook.schema.json."""
        return {
            "event_id": log.event_id,
            "gateway_name": log.gateway_name,
            "event_type": log.event_type,
            "idempotency_key": log.idempotency_key,
            "gateway_payment_id": log.gateway_payment_id,
            "invoice_number": log.invoice_number,
            "amount_bdt": str(log.amount_bdt),
            "currency": log.currency,
            "status": log.status,
            "signature_verified": log.signature_verified,
            "timestamp": (log.processed_at or log.created_at).isoformat(),
            "error_message": log.error_message or None,
        }
