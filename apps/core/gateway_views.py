"""
backend/apps/core/gateway_views.py
Architecture Decision: ADR-0015, ADR-0033 & ADR-0089
Phase 05: Basic/Standard/Pro Subscription & Billing (P089)

REST API Endpoints for Payment Gateway Integration & Signed Webhooks:
- POST /api/v2/billing/gateway/checkout/
- POST /api/v2/billing/gateway/webhook/<gateway_name>/
- POST /api/v2/billing/gateway/reconcile/<invoice_id>/
- GET /api/v2/billing/gateway/events/
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.gateway_service import PaymentGatewayService
from apps.core.models import PaymentGatewayWebhookLog, SubscriptionRenewalInvoice
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.gateway_views")


class PaymentGatewayViewSet(viewsets.ViewSet):
    """
    Manages payment gateway checkout sessions, signed webhook processing,
    and automated transaction reconciliation.
    """

    def get_permissions(self):
        if self.action == "process_webhook":
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def _is_superadmin(self, user: Any) -> bool:
        if not user or not user.is_authenticated:
            return False
        return (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) == UserRole.SUPERADMIN
            or str(getattr(user, "role", "")).upper() in [
                "SUPERADMIN",
                "SUPER_ADMIN",
                "PLATFORM_SUPER_ADMIN",
                "PLATFORM_OPERATOR",
            ]
        )

    def _enforce_tenant_ownership(self, request: Any, target_tenant_id: str) -> None:
        user = request.user
        if self._is_superadmin(user):
            return
        user_tid = str(getattr(user, "tenant_id", "")).strip()
        target_tid = str(target_tenant_id).strip()
        if user_tid and user_tid != target_tid:
            raise CrossTenantViolationError(
                f"Cross-tenant access denied: You cannot initiate checkout or reconcile for tenant '{target_tid}'."
            )

    @action(detail=False, methods=["POST"], url_path="checkout")
    def checkout(self, request):
        """POST /api/v2/billing/gateway/checkout/"""
        data = request.data or {}
        target_tid = data.get("tenant_id") or getattr(request, "tenant_id", None) or getattr(request.user, "tenant_id", None)
        if not target_tid:
            return Response(
                {"success": False, "error_code": "ERR_TENANT_REQUIRED", "message": "tenant_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        self._enforce_tenant_ownership(request, target_tid)

        invoice_id = data.get("invoice_id")
        if not invoice_id:
            return Response(
                {"success": False, "error_code": "ERR_INVOICE_REQUIRED", "message": "invoice_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gateway_name = data.get("gateway_name", "BKASH")

        try:
            session = PaymentGatewayService.initiate_checkout(
                tenant_id=target_tid,
                invoice_id=invoice_id,
                gateway_name=gateway_name,
                customer_phone=data.get("customer_phone", ""),
                customer_email=data.get("customer_email", ""),
                return_url=data.get("return_url", ""),
                cancel_url=data.get("cancel_url", ""),
                actor_user=request.user,
            )
            return Response(
                {
                    "success": True,
                    "code": "GATEWAY_CHECKOUT_INITIATED",
                    "message_en": f"Checkout session initiated via {session['gateway_name']}.",
                    "message_bn": f"{session['gateway_name']} এর মাধ্যমে পেমেন্ট সেশন শুরু হয়েছে।",
                    "data": session,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_CHECKOUT_INITIATION_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=["POST"], url_path=r"webhook/(?P<gateway_name>[^/.]+)")
    def process_webhook(self, request, gateway_name=None):
        """POST /api/v2/billing/gateway/webhook/<gateway_name>/"""
        clean_gateway = str(gateway_name or "BKASH").upper().strip()
        raw_body_bytes = request.body

        # Parse JSON payload
        try:
            if isinstance(request.data, dict) and request.data:
                payload_dict = request.data
            else:
                payload_dict = json.loads(raw_body_bytes.decode("utf-8")) if raw_body_bytes else {}
        except Exception:
            payload_dict = {}

        headers = {k: v for k, v in request.headers.items()}
        # Normalize Django META headers
        for k, v in request.META.items():
            if k.startswith("HTTP_"):
                headers[k] = v

        try:
            result = PaymentGatewayService.handle_webhook(
                gateway_name=clean_gateway,
                raw_body_bytes=raw_body_bytes,
                headers=headers,
                payload_dict=payload_dict,
            )
            return Response(
                {
                    "success": True,
                    "code": "WEBHOOK_PROCESSED",
                    "message_en": "Webhook event processed successfully.",
                    "message_bn": "ওয়েবহুক ইভেন্ট সফলভাবে সম্পন্ন হয়েছে।",
                    "data": result,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            if "signature" in msg.lower():
                return Response(
                    {"success": False, "error_code": "ERR_INVALID_WEBHOOK_SIGNATURE", "message": msg},
                    status=status.HTTP_403_FORBIDDEN,
                )
            return Response(
                {"success": False, "error_code": "ERR_WEBHOOK_PROCESSING_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=["POST"], url_path="reconcile")
    def reconcile(self, request, pk=None):
        """POST /api/v2/billing/gateway/<invoice_id>/reconcile/"""
        invoice = (
            SubscriptionRenewalInvoice.objects.filter(invoice_id=pk).select_related("tenant").first()
            or SubscriptionRenewalInvoice.objects.filter(invoice_number=pk).select_related("tenant").first()
        )
        if not invoice:
            return Response(
                {"success": False, "error_code": "ERR_INVOICE_NOT_FOUND", "message": f"Renewal invoice '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        self._enforce_tenant_ownership(request, invoice.tenant.tenant_id)

        try:
            reconciliation = PaymentGatewayService.reconcile_invoice(invoice_id=invoice.invoice_id, actor_user=request.user)
            return Response(
                {
                    "success": True,
                    "code": "PAYMENT_RECONCILED",
                    "message_en": "Invoice reconciliation completed.",
                    "message_bn": "ইনভয়েস রিকনসিলিয়েশন সম্পন্ন হয়েছে।",
                    "data": reconciliation,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_RECONCILIATION_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=["GET"], url_path="events")
    def list_events(self, request):
        """GET /api/v2/billing/gateway/events/"""
        user = request.user
        param_tid = request.query_params.get("tenant_id")

        if self._is_superadmin(user):
            if param_tid:
                logs = PaymentGatewayWebhookLog.objects.filter(tenant_id=param_tid.strip()).order_by("-created_at")
            else:
                logs = PaymentGatewayWebhookLog.objects.all().order_by("-created_at")
            data = [PaymentGatewayService.serialize_webhook_log(log) for log in logs]
            return Response(
                {
                    "success": True,
                    "code": "GATEWAY_EVENTS_RETRIEVED",
                    "message_en": f"Retrieved {len(data)} webhook event(s).",
                    "message_bn": f"{len(data)}টি ওয়েবহুক ইভেন্ট পাওয়া গেছে।",
                    "data": data,
                },
                status=status.HTTP_200_OK,
            )

        target_tid = getattr(request, "tenant_id", None) or getattr(user, "tenant_id", None)
        if not target_tid:
            return Response(
                {"success": False, "error_code": "ERR_TENANT_REQUIRED", "message": "tenant_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        self._enforce_tenant_ownership(request, target_tid)
        logs = PaymentGatewayWebhookLog.objects.filter(tenant_id=target_tid).order_by("-created_at")
        data = [PaymentGatewayService.serialize_webhook_log(log) for log in logs]
        return Response(
            {
                "success": True,
                "code": "GATEWAY_EVENTS_RETRIEVED",
                "message_en": f"Retrieved {len(data)} webhook event(s).",
                "message_bn": f"{len(data)}টি ওয়েবহুক ইভেন্ট পাওয়া গেছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

