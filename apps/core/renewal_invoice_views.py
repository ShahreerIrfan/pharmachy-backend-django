"""
backend/apps/core/renewal_invoice_views.py
Architecture Decision: ADR-0015, ADR-0033 & ADR-0087
Phase 05: Basic/Standard/Pro Subscription & Billing (P087)

REST API Endpoints for Subscription Renewal Invoices:
- GET /api/v2/billing/invoices/
- GET /api/v2/billing/invoices/<pk>/
- POST /api/v2/billing/invoices/generate/
- POST /api/v2/billing/invoices/<pk>/pay/
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import SubscriptionRenewalInvoice
from apps.core.renewal_invoice_service import RenewalInvoiceService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.renewal_views")


class RenewalInvoiceViewSet(viewsets.ViewSet):
    """
    Manages generation, inspection, and payment of SaaS subscription renewal invoices.
    """

    permission_classes = [permissions.IsAuthenticated]

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
                f"Cross-tenant access denied: You cannot view or pay renewal invoices for tenant '{target_tid}'."
            )

    def list(self, request):
        """GET /api/v2/billing/invoices/"""
        user = request.user
        param_tid = request.query_params.get("tenant_id")
        if self._is_superadmin(user):
            if param_tid:
                invoices = SubscriptionRenewalInvoice.objects.filter(tenant_id=param_tid.strip()).order_by("-created_at")
            else:
                invoices = SubscriptionRenewalInvoice.objects.all().order_by("-created_at")
            data = [RenewalInvoiceService.serialize_invoice(inv) for inv in invoices]
            return Response(
                {
                    "success": True,
                    "code": "RENEWAL_INVOICES_RETRIEVED",
                    "message_en": f"Retrieved {len(data)} renewal invoice(s).",
                    "message_bn": f"{len(data)}টি নবায়ন ইনভয়েস পাওয়া গেছে।",
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
        invoices = SubscriptionRenewalInvoice.objects.filter(tenant_id=target_tid).order_by("-created_at")
        data = [RenewalInvoiceService.serialize_invoice(inv) for inv in invoices]
        return Response(
            {
                "success": True,
                "code": "RENEWAL_INVOICES_RETRIEVED",
                "message_en": f"Retrieved {len(data)} renewal invoice(s).",
                "message_bn": f"{len(data)}টি নবায়ন ইনভয়েস পাওয়া গেছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request, pk=None):
        """GET /api/v2/billing/invoices/<invoice_id>/"""
        invoice = (
            SubscriptionRenewalInvoice.objects.filter(invoice_id=pk).select_related("tenant", "subscription").first()
            or SubscriptionRenewalInvoice.objects.filter(invoice_number=pk).select_related("tenant", "subscription").first()
        )
        if not invoice:
            return Response(
                {"success": False, "error_code": "ERR_INVOICE_NOT_FOUND", "message": f"Renewal invoice '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        self._enforce_tenant_ownership(request, invoice.tenant.tenant_id)
        return Response(
            {
                "success": True,
                "code": "RENEWAL_INVOICE_DETAILS",
                "message_en": "Renewal invoice details retrieved successfully.",
                "message_bn": "নবায়ন ইনভয়েসের তথ্য সফলভাবে প্রাপ্ত হয়েছে।",
                "data": RenewalInvoiceService.serialize_invoice(invoice),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["POST"], url_path="generate")
    def generate_invoice(self, request):
        """POST /api/v2/billing/invoices/generate/"""
        data = request.data or {}
        target_tid = data.get("tenant_id") or getattr(request, "tenant_id", None) or getattr(request.user, "tenant_id", None)
        if not target_tid:
            return Response(
                {"success": False, "error_code": "ERR_TENANT_REQUIRED", "message": "tenant_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        self._enforce_tenant_ownership(request, target_tid)

        billing_cycle = data.get("billing_cycle")
        raw_tax = data.get("tax_rate") if data.get("tax_rate") is not None else data.get("tax_rate_percent", "5.00")
        try:
            tax_rate = Decimal(str(raw_tax))
        except Exception:
            return Response(
                {"success": False, "error_code": "ERR_INVALID_INVOICE_REQUEST", "message": "Invalid tax rate specified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        operator = str(request.user.email or request.user.user_id)

        try:
            invoice, created = RenewalInvoiceService.generate_renewal_invoice(
                tenant_id=target_tid,
                billing_cycle=billing_cycle,
                tax_rate_percent=tax_rate,
                operator=operator,
            )
            return Response(
                {
                    "success": True,
                    "code": "RENEWAL_INVOICE_GENERATED" if created else "RENEWAL_INVOICE_EXISTING",
                    "message_en": f"Renewal invoice '{invoice.invoice_number}' {'generated' if created else 'retrieved'} successfully.",
                    "message_bn": f"নবায়ন ইনভয়েস '{invoice.invoice_number}' সফলভাবে {'তৈরি' if created else 'পুনরুদ্ধার'} হয়েছে।",
                    "data": RenewalInvoiceService.serialize_invoice(invoice),
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_INVALID_INVOICE_REQUEST", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=["POST"], url_path="pay")
    def pay_invoice(self, request, pk=None):
        """POST /api/v2/billing/invoices/<invoice_id>/pay/"""
        invoice = (
            SubscriptionRenewalInvoice.objects.filter(invoice_id=pk).select_related("tenant", "subscription").first()
            or SubscriptionRenewalInvoice.objects.filter(invoice_number=pk).select_related("tenant", "subscription").first()
        )
        if not invoice:
            return Response(
                {"success": False, "error_code": "ERR_INVOICE_NOT_FOUND", "message": f"Renewal invoice '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        self._enforce_tenant_ownership(request, invoice.tenant.tenant_id)

        data = request.data or {}
        payment_method = data.get("payment_method", "BKASH")
        payment_reference = data.get("payment_reference") or data.get("transaction_ref")

        if not payment_reference:
            return Response(
                {"success": False, "error_code": "ERR_PAYMENT_REF_REQUIRED", "message": "payment_reference is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            settled_invoice = RenewalInvoiceService.settle_renewal_invoice(
                invoice_id=invoice.invoice_id,
                payment_method=payment_method,
                payment_reference=payment_reference,
                actor_user=request.user,
            )
            return Response(
                {
                    "success": True,
                    "code": "RENEWAL_INVOICE_PAID",
                    "message_en": f"Renewal invoice '{settled_invoice.invoice_number}' settled successfully.",
                    "message_bn": f"নবায়ন ইনভয়েস '{settled_invoice.invoice_number}' সফলভাবে পরিশোধ হয়েছে।",
                    "data": RenewalInvoiceService.serialize_invoice(settled_invoice),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_PAYMENT_SETTLEMENT_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )
