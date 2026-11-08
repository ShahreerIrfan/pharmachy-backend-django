"""
backend/apps/core/supplier_payment_schedule_views.py
Architecture Decision: ADR-0193 (P193).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing Supplier Payment Schedules:
- Plans due dates and priority queues for vendor invoice payments.
- Disburses partial / full settlement installments and posts AP SupplierLedgerEntry debits.
- Enforces anti-overpayment safeguards and audited due date rescheduling.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.supplier_payment_schedule_service import SupplierPaymentScheduleService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return str(user.tenant_id)
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return str(tenant.tenant_id)
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return str(header)
    return ""


def _get_request_branch_id(request) -> str:
    user = getattr(request, "user", None)
    if user and getattr(user, "branch_id", None):
        return str(user.branch_id)
    header = request.META.get("HTTP_X_BRANCH_ID")
    if header:
        return str(header)
    return ""


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Payment scheduling locked.",
            }
        )


class SupplierPaymentScheduleViewSet(viewsets.ViewSet):
    """
    REST API ViewSet governing Supplier Payment Schedules.
    Routes:
      - GET    /api/v2/procurement/payment-schedules/
      - POST   /api/v2/procurement/payment-schedules/
      - GET    /api/v2/procurement/payment-schedules/{pk}/
      - POST   /api/v2/procurement/payment-schedules/{pk}/record-payment/
      - POST   /api/v2/procurement/payment-schedules/{pk}/reschedule/
      - POST   /api/v2/procurement/payment-schedules/{pk}/cancel/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/payment-schedules/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.query_params.get("supplier_id")
        status_filter = request.query_params.get("status")
        priority = request.query_params.get("priority")
        branch_id = request.query_params.get("branch_id") or _get_request_branch_id(request)

        try:
            schedules = SupplierPaymentScheduleService.list_schedules(
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                status_filter=status_filter,
                priority=priority,
                branch_id=branch_id,
            )
            return Response({"count": len(schedules), "results": schedules}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/payment-schedules/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            sched = SupplierPaymentScheduleService.get_schedule(
                tenant_id=tenant_id,
                schedule_id=pk,
            )
            return Response(sched, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/payment-schedules/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            sched = SupplierPaymentScheduleService.create_schedule(
                tenant_id=tenant_id,
                actor_user=request.user,
                data=request.data,
            )
            return Response(sched, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error creating payment schedule: %s", exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/payment-schedules/{pk}/record-payment/
        Disburses partial or full settlement installment and posts AP SupplierLedgerEntry debit.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            sched = SupplierPaymentScheduleService.record_installment_payment(
                tenant_id=tenant_id,
                actor_user=request.user,
                schedule_id=pk,
                data=request.data,
            )
            return Response(sched, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error recording schedule payment %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="reschedule")
    def reschedule(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/payment-schedules/{pk}/reschedule/
        Updates due date with mandatory justification reason.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        new_due_date = request.data.get("due_date")
        reason = request.data.get("reason", "")
        try:
            sched = SupplierPaymentScheduleService.reschedule(
                tenant_id=tenant_id,
                actor_user=request.user,
                schedule_id=pk,
                new_due_date_raw=new_due_date,
                reason=reason,
            )
            return Response(sched, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error rescheduling payment %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/payment-schedules/{pk}/cancel/
        Cancels un-disbursed schedule.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "")
        try:
            sched = SupplierPaymentScheduleService.cancel_schedule(
                tenant_id=tenant_id,
                actor_user=request.user,
                schedule_id=pk,
                reason=reason,
            )
            return Response(sched, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error cancelling schedule %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
