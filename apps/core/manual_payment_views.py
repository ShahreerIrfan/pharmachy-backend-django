"""
backend/apps/core/manual_payment_views.py
Architecture Decision: ADR-0015, ADR-0033 & ADR-0088
Phase 05: Basic/Standard/Pro Subscription & Billing (P088)

REST API Endpoints for Manual Payment Verification:
- GET /api/v2/billing/payment-claims/
- GET /api/v2/billing/payment-claims/<pk>/
- POST /api/v2/billing/payment-claims/submit/
- POST /api/v2/billing/payment-claims/<pk>/approve/
- POST /api/v2/billing/payment-claims/<pk>/reject/
- POST /api/v2/billing/payment-claims/<pk>/cancel/
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.manual_payment_service import ManualPaymentService
from apps.core.models import ManualPaymentClaim
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.manual_payment_views")


class ManualPaymentClaimViewSet(viewsets.ViewSet):
    """
    Manages submission, maker-checker verification, approval, and rejection
    of manual subscription payment claims.
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
                f"Cross-tenant access denied: You cannot view or modify payment claims for tenant '{target_tid}'."
            )

    def list(self, request):
        """GET /api/v2/billing/payment-claims/"""
        user = request.user
        param_tid = request.query_params.get("tenant_id")

        if self._is_superadmin(user):
            if param_tid:
                claims = ManualPaymentClaim.objects.filter(tenant_id=param_tid.strip()).order_by("-created_at")
            else:
                claims = ManualPaymentClaim.objects.all().order_by("-created_at")
            data = [ManualPaymentService.serialize_claim(c) for c in claims]
            return Response(
                {
                    "success": True,
                    "code": "PAYMENT_CLAIMS_RETRIEVED",
                    "message_en": f"Retrieved {len(data)} payment claim(s).",
                    "message_bn": f"{len(data)}টি পেমেন্ট ভেরিফিকেশন আবেদন পাওয়া গেছে।",
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
        claims = ManualPaymentClaim.objects.filter(tenant_id=target_tid).order_by("-created_at")
        data = [ManualPaymentService.serialize_claim(c) for c in claims]
        return Response(
            {
                "success": True,
                "code": "PAYMENT_CLAIMS_RETRIEVED",
                "message_en": f"Retrieved {len(data)} payment claim(s).",
                "message_bn": f"{len(data)}টি পেমেন্ট ভেরিফিকেশন আবেদন পাওয়া গেছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request, pk=None):
        """GET /api/v2/billing/payment-claims/<claim_id>/"""
        claim = (
            ManualPaymentClaim.objects.filter(claim_id=pk).select_related("tenant", "invoice").first()
            or ManualPaymentClaim.objects.filter(claim_number=pk).select_related("tenant", "invoice").first()
        )
        if not claim:
            return Response(
                {"success": False, "error_code": "ERR_CLAIM_NOT_FOUND", "message": f"Payment claim '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        self._enforce_tenant_ownership(request, claim.tenant.tenant_id)
        return Response(
            {
                "success": True,
                "code": "PAYMENT_CLAIM_DETAILS",
                "message_en": "Payment claim details retrieved successfully.",
                "message_bn": "পেমেন্ট দাবির তথ্য সফলভাবে প্রাপ্ত হয়েছে।",
                "data": ManualPaymentService.serialize_claim(claim),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["POST"], url_path="submit")
    def submit_claim(self, request):
        """POST /api/v2/billing/payment-claims/submit/"""
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

        try:
            claim = ManualPaymentService.submit_claim(
                tenant_id=target_tid,
                invoice_id=invoice_id,
                payment_method=data.get("payment_method", "BKASH"),
                transaction_reference=data.get("transaction_reference", ""),
                sender_account_number=data.get("sender_account_number", ""),
                claimed_amount_bdt=data.get("claimed_amount_bdt", "0.00"),
                evidence_file_url=data.get("evidence_file_url", ""),
                evidence_notes=data.get("evidence_notes", ""),
                maker_user=request.user,
            )
            return Response(
                {
                    "success": True,
                    "code": "PAYMENT_CLAIM_SUBMITTED",
                    "message_en": f"Payment claim '{claim.claim_number}' submitted for verification.",
                    "message_bn": f"পেমেন্ট দাবি '{claim.claim_number}' ভেরিফিকেশনের জন্য সফলভাবে দাখিল হয়েছে।",
                    "data": ManualPaymentService.serialize_claim(claim),
                },
                status=status.HTTP_201_CREATED,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_PAYMENT_CLAIM_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=["POST"], url_path="approve")
    def approve_claim(self, request, pk=None):
        """POST /api/v2/billing/payment-claims/<claim_id>/approve/ (Checker action)"""
        if not self._is_superadmin(request.user):
            raise PermissionDenied("Only Platform Administrators can verify and approve manual payment claims.")

        claim = (
            ManualPaymentClaim.objects.filter(claim_id=pk).select_related("tenant", "invoice").first()
            or ManualPaymentClaim.objects.filter(claim_number=pk).select_related("tenant", "invoice").first()
        )
        if not claim:
            return Response(
                {"success": False, "error_code": "ERR_CLAIM_NOT_FOUND", "message": f"Payment claim '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        data = request.data or {}
        checker_notes = data.get("checker_notes", "")

        try:
            approved_claim = ManualPaymentService.approve_claim(
                claim_id=claim.claim_id,
                checker_user=request.user,
                checker_notes=checker_notes,
            )
            return Response(
                {
                    "success": True,
                    "code": "PAYMENT_CLAIM_APPROVED",
                    "message_en": f"Payment claim '{approved_claim.claim_number}' approved. Subscription renewed.",
                    "message_bn": f"পেমেন্ট দাবি '{approved_claim.claim_number}' অনুমোদিত হয়েছে। সাবস্ক্রিপশন সক্রিয় করা হয়েছে।",
                    "data": ManualPaymentService.serialize_claim(approved_claim),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_PAYMENT_APPROVAL_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=["POST"], url_path="reject")
    def reject_claim(self, request, pk=None):
        """POST /api/v2/billing/payment-claims/<claim_id>/reject/ (Checker action)"""
        if not self._is_superadmin(request.user):
            raise PermissionDenied("Only Platform Administrators can reject manual payment claims.")

        claim = (
            ManualPaymentClaim.objects.filter(claim_id=pk).select_related("tenant", "invoice").first()
            or ManualPaymentClaim.objects.filter(claim_number=pk).select_related("tenant", "invoice").first()
        )
        if not claim:
            return Response(
                {"success": False, "error_code": "ERR_CLAIM_NOT_FOUND", "message": f"Payment claim '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        data = request.data or {}
        rejection_reason = data.get("rejection_reason")
        checker_notes = data.get("checker_notes", "")

        if not rejection_reason or not str(rejection_reason).strip():
            return Response(
                {"success": False, "error_code": "ERR_REJECTION_REASON_REQUIRED", "message": "rejection_reason is mandatory."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            rejected_claim = ManualPaymentService.reject_claim(
                claim_id=claim.claim_id,
                checker_user=request.user,
                rejection_reason=rejection_reason,
                checker_notes=checker_notes,
            )
            return Response(
                {
                    "success": True,
                    "code": "PAYMENT_CLAIM_REJECTED",
                    "message_en": f"Payment claim '{rejected_claim.claim_number}' rejected.",
                    "message_bn": f"পেমেন্ট দাবি '{rejected_claim.claim_number}' প্রত্যাখ্যাত হয়েছে।",
                    "data": ManualPaymentService.serialize_claim(rejected_claim),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_PAYMENT_REJECTION_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=["POST"], url_path="cancel")
    def cancel_claim(self, request, pk=None):
        """POST /api/v2/billing/payment-claims/<claim_id>/cancel/ (Maker action)"""
        claim = (
            ManualPaymentClaim.objects.filter(claim_id=pk).select_related("tenant", "invoice").first()
            or ManualPaymentClaim.objects.filter(claim_number=pk).select_related("tenant", "invoice").first()
        )
        if not claim:
            return Response(
                {"success": False, "error_code": "ERR_CLAIM_NOT_FOUND", "message": f"Payment claim '{pk}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        self._enforce_tenant_ownership(request, claim.tenant.tenant_id)

        try:
            cancelled = ManualPaymentService.cancel_claim(claim_id=claim.claim_id, actor_user=request.user)
            return Response(
                {
                    "success": True,
                    "code": "PAYMENT_CLAIM_CANCELLED",
                    "message_en": f"Payment claim '{cancelled.claim_number}' cancelled successfully.",
                    "message_bn": f"পেমেন্ট দাবি '{cancelled.claim_number}' বাতিল করা হয়েছে।",
                    "data": ManualPaymentService.serialize_claim(cancelled),
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as e:
            msg = e.messages[0] if hasattr(e, "messages") and e.messages else str(e)
            return Response(
                {"success": False, "error_code": "ERR_PAYMENT_CANCEL_FAILED", "message": msg},
                status=status.HTTP_400_BAD_REQUEST,
            )

