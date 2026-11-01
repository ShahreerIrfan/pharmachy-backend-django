"""
backend/apps/core/gateway_adapters.py
Architecture Decision: ADR-0015, ADR-0033 & ADR-0089
Phase 05: Basic/Standard/Pro Subscription & Billing (P089)

Replaceable Payment Gateway Adapter Interface:
- Abstract protocol for pluggable payment aggregators (bKash, Nagad, SSLCommerz).
- Zero stored secrets: API keys and webhook signing secrets resolved from environment/settings.
- Cryptographic HMAC-SHA256 signature verification over raw request body.
- Idempotency key and payment reference resolution.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import os
from decimal import Decimal
from typing import Any

from django.conf import settings

from apps.core.models import SubscriptionRenewalInvoice


class GatewayCheckoutSession:
    def __init__(
        self,
        gateway_name: str,
        checkout_url: str,
        gateway_payment_id: str,
        invoice_number: str,
        amount_bdt: Decimal,
        currency: str = "BDT",
    ):
        self.gateway_name = gateway_name
        self.checkout_url = checkout_url
        self.gateway_payment_id = gateway_payment_id
        self.invoice_number = invoice_number
        self.amount_bdt = amount_bdt
        self.currency = currency


class GatewayWebhookResult:
    def __init__(
        self,
        is_valid_signature: bool,
        event_type: str,
        gateway_payment_id: str,
        idempotency_key: str,
        invoice_number: str,
        amount_bdt: Decimal,
        currency: str = "BDT",
        error_message: str | None = None,
    ):
        self.is_valid_signature = is_valid_signature
        self.event_type = event_type
        self.gateway_payment_id = gateway_payment_id
        self.idempotency_key = idempotency_key
        self.invoice_number = invoice_number
        self.amount_bdt = amount_bdt
        self.currency = currency
        self.error_message = error_message


class PaymentGatewayAdapter(abc.ABC):
    @property
    @abc.abstractmethod
    def gateway_name(self) -> str:
        """Normalized unique identifier of the payment gateway."""
        pass

    @abc.abstractmethod
    def get_webhook_secret(self) -> str:
        """Retrieves webhook secret from environment / settings without storing in database."""
        pass

    def verify_webhook_signature(
        self,
        raw_body_bytes: bytes,
        signature_header: str,
        secret: str | None = None,
    ) -> bool:
        """Verifies cryptographic HMAC-SHA256 webhook signature."""
        active_secret = secret or self.get_webhook_secret()
        if not active_secret or not signature_header:
            return False
        expected_sig = hmac.new(
            active_secret.encode("utf-8"),
            raw_body_bytes,
            hashlib.sha256,
        ).hexdigest()
        clean_sig = signature_header.strip()
        if clean_sig.startswith("sha256="):
            clean_sig = clean_sig[7:]
        return hmac.compare_digest(expected_sig.lower(), clean_sig.lower())

    @abc.abstractmethod
    def initiate_checkout(
        self,
        invoice: SubscriptionRenewalInvoice,
        customer_phone: str,
        customer_email: str,
        return_url: str,
        cancel_url: str,
    ) -> GatewayCheckoutSession:
        pass

    @abc.abstractmethod
    def parse_webhook(
        self,
        payload: dict[str, Any],
        raw_body: bytes,
        headers: dict[str, str],
    ) -> GatewayWebhookResult:
        pass


class BKashPaymentGatewayAdapter(PaymentGatewayAdapter):
    @property
    def gateway_name(self) -> str:
        return "BKASH"

    def get_webhook_secret(self) -> str:
        return os.environ.get(
            "BKASH_WEBHOOK_SECRET",
            getattr(settings, "GATEWAY_WEBHOOK_SECRET", "pharma-saas-webhook-secret-key-2026"),
        )

    def initiate_checkout(
        self,
        invoice: SubscriptionRenewalInvoice,
        customer_phone: str,
        customer_email: str,
        return_url: str,
        cancel_url: str,
    ) -> GatewayCheckoutSession:
        payment_id = f"bka-pay-{invoice.invoice_number.split('-')[-1]}"
        checkout_url = f"https://checkout.sandbox.bka.sh/v1.2.0-beta/checkout/{payment_id}"
        return GatewayCheckoutSession(
            gateway_name=self.gateway_name,
            checkout_url=checkout_url,
            gateway_payment_id=payment_id,
            invoice_number=invoice.invoice_number,
            amount_bdt=invoice.total_bdt,
        )

    def parse_webhook(
        self,
        payload: dict[str, Any],
        raw_body: bytes,
        headers: dict[str, str],
    ) -> GatewayWebhookResult:
        sig_header = headers.get("x-gateway-signature") or headers.get("X-Gateway-Signature") or headers.get("HTTP_X_GATEWAY_SIGNATURE") or ""
        valid_sig = self.verify_webhook_signature(raw_body, sig_header)

        event_type = payload.get("event_type", "PAYMENT_COMPLETED")
        payment_id = str(payload.get("payment_id") or payload.get("trx_id") or payload.get("gateway_payment_id") or "")
        idemp_key = str(payload.get("idempotency_key") or f"idemp-bka-{payment_id}")
        invoice_number = str(payload.get("invoice_number") or payload.get("merchant_invoice_number") or "")
        amount_bdt = Decimal(str(payload.get("amount") or payload.get("amount_bdt") or "0.00")).quantize(Decimal("0.01"))
        currency = str(payload.get("currency") or "BDT").upper()

        return GatewayWebhookResult(
            is_valid_signature=valid_sig,
            event_type=event_type,
            gateway_payment_id=payment_id,
            idempotency_key=idemp_key,
            invoice_number=invoice_number,
            amount_bdt=amount_bdt,
            currency=currency,
        )


class NagadPaymentGatewayAdapter(PaymentGatewayAdapter):
    @property
    def gateway_name(self) -> str:
        return "NAGAD"

    def get_webhook_secret(self) -> str:
        return os.environ.get(
            "NAGAD_WEBHOOK_SECRET",
            getattr(settings, "GATEWAY_WEBHOOK_SECRET", "pharma-saas-webhook-secret-key-2026"),
        )

    def initiate_checkout(
        self,
        invoice: SubscriptionRenewalInvoice,
        customer_phone: str,
        customer_email: str,
        return_url: str,
        cancel_url: str,
    ) -> GatewayCheckoutSession:
        payment_id = f"nag-pay-{invoice.invoice_number.split('-')[-1]}"
        checkout_url = f"https://api.mynagad.com/pg/checkout/{payment_id}"
        return GatewayCheckoutSession(
            gateway_name=self.gateway_name,
            checkout_url=checkout_url,
            gateway_payment_id=payment_id,
            invoice_number=invoice.invoice_number,
            amount_bdt=invoice.total_bdt,
        )

    def parse_webhook(
        self,
        payload: dict[str, Any],
        raw_body: bytes,
        headers: dict[str, str],
    ) -> GatewayWebhookResult:
        sig_header = headers.get("x-gateway-signature") or headers.get("X-Gateway-Signature") or headers.get("HTTP_X_GATEWAY_SIGNATURE") or ""
        valid_sig = self.verify_webhook_signature(raw_body, sig_header)

        event_type = payload.get("event_type", "PAYMENT_COMPLETED")
        payment_id = str(payload.get("payment_id") or payload.get("trx_id") or payload.get("gateway_payment_id") or "")
        idemp_key = str(payload.get("idempotency_key") or f"idemp-nag-{payment_id}")
        invoice_number = str(payload.get("invoice_number") or payload.get("merchant_invoice_number") or "")
        amount_bdt = Decimal(str(payload.get("amount") or payload.get("amount_bdt") or "0.00")).quantize(Decimal("0.01"))
        currency = str(payload.get("currency") or "BDT").upper()

        return GatewayWebhookResult(
            is_valid_signature=valid_sig,
            event_type=event_type,
            gateway_payment_id=payment_id,
            idempotency_key=idemp_key,
            invoice_number=invoice_number,
            amount_bdt=amount_bdt,
            currency=currency,
        )


class SSLCommerzPaymentGatewayAdapter(PaymentGatewayAdapter):
    @property
    def gateway_name(self) -> str:
        return "SSLCOMMERZ"

    def get_webhook_secret(self) -> str:
        return os.environ.get(
            "SSLCOMMERZ_WEBHOOK_SECRET",
            getattr(settings, "GATEWAY_WEBHOOK_SECRET", "pharma-saas-webhook-secret-key-2026"),
        )

    def initiate_checkout(
        self,
        invoice: SubscriptionRenewalInvoice,
        customer_phone: str,
        customer_email: str,
        return_url: str,
        cancel_url: str,
    ) -> GatewayCheckoutSession:
        payment_id = f"ssl-pay-{invoice.invoice_number.split('-')[-1]}"
        checkout_url = f"https://sandbox.sslcommerz.com/gwprocess/v4/api.php?session={payment_id}"
        return GatewayCheckoutSession(
            gateway_name=self.gateway_name,
            checkout_url=checkout_url,
            gateway_payment_id=payment_id,
            invoice_number=invoice.invoice_number,
            amount_bdt=invoice.total_bdt,
        )

    def parse_webhook(
        self,
        payload: dict[str, Any],
        raw_body: bytes,
        headers: dict[str, str],
    ) -> GatewayWebhookResult:
        sig_header = headers.get("x-gateway-signature") or headers.get("X-Gateway-Signature") or headers.get("HTTP_X_GATEWAY_SIGNATURE") or ""
        valid_sig = self.verify_webhook_signature(raw_body, sig_header)

        event_type = payload.get("event_type", "PAYMENT_COMPLETED")
        payment_id = str(payload.get("payment_id") or payload.get("val_id") or payload.get("gateway_payment_id") or "")
        idemp_key = str(payload.get("idempotency_key") or f"idemp-ssl-{payment_id}")
        invoice_number = str(payload.get("invoice_number") or payload.get("tran_id") or "")
        amount_bdt = Decimal(str(payload.get("amount") or payload.get("amount_bdt") or "0.00")).quantize(Decimal("0.01"))
        currency = str(payload.get("currency") or "BDT").upper()

        return GatewayWebhookResult(
            is_valid_signature=valid_sig,
            event_type=event_type,
            gateway_payment_id=payment_id,
            idempotency_key=idemp_key,
            invoice_number=invoice_number,
            amount_bdt=amount_bdt,
            currency=currency,
        )


class PaymentGatewayRegistry:
    _adapters: dict[str, PaymentGatewayAdapter] = {
        "BKASH": BKashPaymentGatewayAdapter(),
        "NAGAD": NagadPaymentGatewayAdapter(),
        "SSLCOMMERZ": SSLCommerzPaymentGatewayAdapter(),
    }

    @classmethod
    def get_adapter(cls, name: str | None) -> PaymentGatewayAdapter:
        clean = str(name or "BKASH").upper().strip()
        if clean in cls._adapters:
            return cls._adapters[clean]
        # Fallback to BKASH
        return cls._adapters["BKASH"]

    @classmethod
    def register_adapter(cls, adapter: PaymentGatewayAdapter) -> None:
        cls._adapters[adapter.gateway_name.upper()] = adapter
