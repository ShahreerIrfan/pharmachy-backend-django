"""
backend/apps/core/dunning_views.py

Architecture Decision: ADR-0099
Phase 05: Basic/Standard/Pro Subscription & Billing (P099)
Requirement Trace: FR-044, FR-066, FR-068, FR-070

DRF ViewSet for Subscription Dunning Notifications:
- List & view localized dunning alerts for renewals, payment failures, grace periods.
- Pharmacy Owner acknowledgment and manual retry controls.
- Administrative and automated lifecycle dispatch with 24-hour de-duplication rules.
- Strict RBAC and cross-tenant isolation enforcement.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.dunning_service import DunningNotificationService
from apps.core.errors import CrossTenantViolationError
from apps.core.models import DunningNotification
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.dunning")


class DunningNotificationViewSet(viewsets.ViewSet):
    """
    API ViewSet for viewing, acknowledging, retrying, and dispatching
    subscription dunning notifications.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_admin(self, user: Any) -> bool:
        if not user or not user.is_authenticated:
            return False
        return (
            getattr(user, "is_superuser", False)
            or getattr(user, "role", None) in [UserRole.SUPERADMIN, UserRole.PLATFORM_OPERATOR]
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "PLATFORM_OPERATOR", "SUPER_ADMIN"]
        )

    def _require_owner_or_admin(self, user: Any) -> None:
        if self._is_platform_admin(user):
            return
        role = str(getattr(user, "role", "")).upper()
        if role not in ["PHARMACY_OWNER", "OWNER", "ADMIN", "PHARMACY_MANAGER"]:
            raise PermissionDenied(
                detail={
                    "error_code": "PERMISSION_DENIED",
                    "message_en": "Only pharmacy owners and administrators can access dunning notifications.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিক ও অ্যাডমিনিস্ট্রেটরদের ডানিং নোটিফিকেশন দেখার অনুমতি আছে।",
                }
            )

    def _resolve_tenant_id(self, request: Any, target_tenant_id: str | None = None) -> str:
        user = request.user
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        if target_tenant_id:
            clean_target = str(target_tenant_id).strip()
            if not self._is_platform_admin(user) and user_tid and user_tid != clean_target:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access notifications for tenant '{clean_target}'."
                )
            return clean_target

        if user_tid:
            return user_tid

        header_tid = str(request.headers.get("X-Tenant-ID", "")).strip()
        if header_tid:
            return header_tid

        raise ValidationError({"tenant_id": "Target tenant_id could not be resolved from authentication context."})

    def list(self, request: Any) -> Response:
        """Lists dunning notifications for authenticated pharmacy tenant."""
        self._require_owner_or_admin(request.user)
        target_tid = request.query_params.get("tenant_id")
        resolved_tid = self._resolve_tenant_id(request, target_tid)

        stage = request.query_params.get("stage")
        unack_only = request.query_params.get("unacknowledged_only", "false").lower() == "true"
        limit = int(request.query_params.get("limit", 50))

        data = DunningNotificationService.list_notifications(
            tenant_id=resolved_tid,
            stage=stage,
            unacknowledged_only=unack_only,
            limit=limit,
        )

        return Response(
            {
                "success": True,
                "code": "DUNNING_NOTIFICATIONS_RETRIEVED",
                "message_en": "Dunning notifications retrieved successfully.",
                "message_bn": "ডানিং নোটিফিকেশন তালিকা সফলভাবে প্রাপ্ত হয়েছে।",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    def retrieve(self, request: Any, pk: str = None) -> Response:
        """Retrieves details of a specific dunning notification."""
        self._require_owner_or_admin(request.user)
        try:
            notif = DunningNotification.objects.get(notification_id=pk)
        except DunningNotification.DoesNotExist:
            raise ValidationError({"notification_id": f"Notification '{pk}' not found."})

        self._resolve_tenant_id(request, notif.tenant_id)

        data = {
            "notification_id": notif.notification_id,
            "tenant_id": notif.tenant_id,
            "invoice_id": getattr(notif, "invoice_id", None) or (notif.invoice.invoice_id if notif.invoice else None),
            "stage": notif.stage,
            "channel": notif.channel,
            "delivery_status": notif.delivery_status,
            "dedup_key": notif.dedup_key,
            "title_en": notif.title_en,
            "title_bn": notif.title_bn,
            "body_en": notif.body_en,
            "body_bn": notif.body_bn,
            "amount_due_bdt": DunningNotificationService._format_decimal(notif.amount_due_bdt),
            "recipient_email": notif.recipient_email,
            "recipient_phone": notif.recipient_phone,
            "is_acknowledged": notif.is_acknowledged,
            "acknowledged_at": notif.acknowledged_at.isoformat() if notif.acknowledged_at else None,
            "retry_count": notif.retry_count,
            "max_retries": notif.max_retries,
            "created_at": notif.created_at.isoformat(),
        }

        return Response(
            {
                "success": True,
                "code": "DUNNING_NOTIFICATION_RETRIEVED",
                "data": data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="acknowledge")
    def acknowledge(self, request: Any, pk: str = None) -> Response:
        """Marks a dunning alert as acknowledged/read by pharmacy owner."""
        self._require_owner_or_admin(request.user)
        try:
            notif = DunningNotification.objects.get(notification_id=pk)
        except DunningNotification.DoesNotExist:
            raise ValidationError({"notification_id": f"Notification '{pk}' not found."})

        self._resolve_tenant_id(request, notif.tenant_id)

        try:
            updated = DunningNotificationService.acknowledge_notification(
                notification_id=pk,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "DUNNING_NOTIFICATION_ACKNOWLEDGED",
                "message_en": "Notification acknowledged successfully.",
                "message_bn": "নোটিফিকেশনটি সফলভাবে অ্যাকনলেজ করা হয়েছে।",
                "data": {
                    "notification_id": updated.notification_id,
                    "is_acknowledged": updated.is_acknowledged,
                    "acknowledged_at": updated.acknowledged_at.isoformat(),
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="retry")
    def retry(self, request: Any, pk: str = None) -> Response:
        """Retries delivery of a dunning notification subject to retry limit."""
        self._require_owner_or_admin(request.user)
        try:
            notif = DunningNotification.objects.get(notification_id=pk)
        except DunningNotification.DoesNotExist:
            raise ValidationError({"notification_id": f"Notification '{pk}' not found."})

        self._resolve_tenant_id(request, notif.tenant_id)

        try:
            retried = DunningNotificationService.retry_notification(
                notification_id=pk,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "DUNNING_NOTIFICATION_RETRIED",
                "message_en": "Notification retry dispatched successfully.",
                "message_bn": "নোটিফিকেশন পুনরায় প্রেরণ করা হয়েছে।",
                "data": {
                    "notification_id": retried.notification_id,
                    "delivery_status": retried.delivery_status,
                    "retry_count": retried.retry_count,
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="dispatch")
    def dispatch_notification(self, request: Any) -> Response:
        """Dispatches a dunning lifecycle notification with de-duplication rules."""
        self._require_owner_or_admin(request.user)
        target_tid = request.data.get("tenant_id")
        resolved_tid = self._resolve_tenant_id(request, target_tid)

        stage = request.data.get("stage")
        invoice_id = request.data.get("invoice_id")
        amount_bdt = request.data.get("amount_bdt")
        channel = request.data.get("channel", "IN_APP")
        force = bool(request.data.get("force", False))

        try:
            notif = DunningNotificationService.generate_and_dispatch_notification(
                tenant_id=resolved_tid,
                stage=stage,
                invoice_id=invoice_id,
                amount_bdt=amount_bdt,
                channel=channel,
                force=force,
                operator_user=request.user,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        item = {
            "notification_id": notif.notification_id,
            "tenant_id": notif.tenant_id,
            "invoice_id": getattr(notif, "invoice_id", None) or (notif.invoice.invoice_id if notif.invoice else None),
            "stage": notif.stage,
            "channel": notif.channel,
            "delivery_status": notif.delivery_status,
            "dedup_key": notif.dedup_key,
            "title_en": notif.title_en,
            "title_bn": notif.title_bn,
            "body_en": notif.body_en,
            "body_bn": notif.body_bn,
            "amount_due_bdt": DunningNotificationService._format_decimal(notif.amount_due_bdt),
            "recipient_email": notif.recipient_email,
            "recipient_phone": notif.recipient_phone,
            "is_acknowledged": notif.is_acknowledged,
            "acknowledged_at": notif.acknowledged_at.isoformat() if notif.acknowledged_at else None,
            "retry_count": notif.retry_count,
            "max_retries": notif.max_retries,
            "created_at": notif.created_at.isoformat(),
        }

        return Response(
            {
                "success": True,
                "code": "DUNNING_NOTIFICATION_DISPATCHED",
                "message_en": "Dunning notification generated and dispatched successfully.",
                "message_bn": "ডানিং নোটিফিকেশন সফলভাবে তৈরি ও প্রেরণ করা হয়েছে।",
                "data": item,
            },
            status=status.HTTP_201_CREATED,
        )
