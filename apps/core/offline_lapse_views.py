"""
backend/apps/core/offline_lapse_views.py
Architecture Decision: ADR-0005, ADR-0015, ADR-0072, ADR-0082, ADR-0092 & ADR-0094
Phase 05: Basic/Standard/Pro Subscription & Billing (P094)
Requirement Trace: FR-072 (Do not silently discard valid pending events if plan lapses)

DRF ViewSet for Offline Plan Lapse Handling & Reconciliation Queue:
- Ingests offline outbox events pushed while plan was lapsed without silent discards.
- Returns explicit HTTP 202 Accepted with canonical acknowledgment.
- Provides owner reconciliation dashboard and authorized maker-checker resolution.
- Enforces strict multi-tenant boundaries and role-based permissions.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.models import LapsedSubscriptionHoldQueue
from apps.core.offline_lapse_service import OfflinePlanLapseReconciliationService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.billing.offline_lapse")


class LapsedPlanReconciliationViewSet(viewsets.ViewSet):
    """
    Manages offline plan lapse hold queue intake, inspection, and authorized reconciliation.
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

    def _is_owner_or_admin(self, user: Any) -> bool:
        if self._is_platform_admin(user):
            return True
        role = str(getattr(user, "role", "")).upper()
        return role in ["PHARMACY_OWNER", "OWNER", "ADMIN", "PHARMACY_MANAGER"]

    def _resolve_tenant_id(self, request: Any, target_tenant_id: str | None = None) -> str:
        user = request.user
        user_tid = str(getattr(user, "tenant_id", "")).strip()

        if target_tenant_id:
            clean_target = str(target_tenant_id).strip()
            if not self._is_platform_admin(user) and user_tid and user_tid != clean_target:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot access tenant '{clean_target}'."
                )
            return clean_target

        if not user_tid:
            raise ValidationError("Tenant context could not be resolved from authenticated user.")
        return user_tid

    @action(detail=False, methods=["post"], url_path="push")
    def push_offline_event(self, request: Any) -> Response:
        """
        POST /api/v2/billing/offline-reconciliation/push/
        Ingests an offline transaction emitted by a terminal during plan lapse or stale lease.
        Returns HTTP 202 Accepted with canonical acknowledgment.
        """
        try:
            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            branch_id = request.data.get("branch_id", "")
            device_id = request.data.get("device_id", "")
            offline_event_id = request.data.get("offline_event_id", "")
            event_type = request.data.get("event_type", "OFFLINE_SALE")
            occurred_at = request.data.get("occurred_at")
            payload = request.data.get("payload", {})

            result = OfflinePlanLapseReconciliationService.intake_offline_event(
                tenant_id=tenant_id,
                branch_id=branch_id,
                device_id=device_id,
                offline_event_id=offline_event_id,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
                actor_user=request.user,
            )

            # Return 202 Accepted to signal receipt and hold
            return Response(result, status=status.HTTP_202_ACCEPTED)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error ingesting offline transaction into hold queue: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["get"], url_path="queue")
    def list_queue(self, request: Any) -> Response:
        """
        GET /api/v2/billing/offline-reconciliation/queue/
        Returns summary of held transactions and list of items awaiting reconciliation.
        """
        try:
            target_tenant = request.query_params.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)
            branch_id = request.query_params.get("branch_id")

            summary = OfflinePlanLapseReconciliationService.get_queue_summary(
                tenant_id=tenant_id,
                branch_id=branch_id,
            )

            # Fetch up to 50 queue items
            qs = LapsedSubscriptionHoldQueue.objects.filter(tenant_id=tenant_id).order_by("-occurred_at")
            if branch_id:
                qs = qs.filter(branch_id=str(branch_id).strip())

            hold_status_filter = request.query_params.get("status")
            if hold_status_filter:
                qs = qs.filter(hold_status=str(hold_status_filter).strip().upper())

            items = [OfflinePlanLapseReconciliationService._serialize_hold_record(r) for r in qs[:50]]
            summary["items"] = items

            return Response(summary, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception as err:
            logger.exception("Error listing hold queue: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="reconcile")
    def reconcile(self, request: Any, pk: str | None = None) -> Response:
        """
        POST /api/v2/billing/offline-reconciliation/{hold_id}/reconcile/
        Reconciles a specific held event into authoritative PostgreSQL records.
        Requires Owner/Manager privilege.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can reconcile held offline transactions.")

            hold_id = pk or request.data.get("hold_id")
            if not hold_id:
                return Response(
                    {"error": "VALIDATION_ERROR", "detail": "hold_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Cross-tenant check on record
            try:
                record = LapsedSubscriptionHoldQueue.objects.get(hold_id=str(hold_id).strip())
            except LapsedSubscriptionHoldQueue.DoesNotExist:
                return Response(
                    {"error": "NOT_FOUND", "detail": f"Hold record '{hold_id}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            user_tid = str(getattr(request.user, "tenant_id", "")).strip()
            if not self._is_platform_admin(request.user) and user_tid and user_tid != record.tenant_id:
                raise CrossTenantViolationError(
                    f"User from tenant '{user_tid}' cannot reconcile hold record belonging to tenant '{record.tenant_id}'."
                )

            resolution_notes = request.data.get("resolution_notes", "Reconciled via management console")
            result = OfflinePlanLapseReconciliationService.reconcile_single_event(
                hold_id=record.hold_id,
                resolution_notes=resolution_notes,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as err:
            detail = err.messages if hasattr(err, "messages") else str(err)
            if isinstance(detail, list):
                detail = " ".join(detail)
            return Response(
                {"error": "VALIDATION_ERROR", "detail": detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as err:
            logger.exception("Error reconciling held offline transaction: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="reconcile-all")
    def reconcile_all(self, request: Any) -> Response:
        """
        POST /api/v2/billing/offline-reconciliation/reconcile-all/
        Reconciles all pending held items for the tenant.
        """
        try:
            if not self._is_owner_or_admin(request.user):
                raise PermissionError("Only pharmacy owners or managers can reconcile held offline transactions.")

            target_tenant = request.data.get("tenant_id")
            tenant_id = self._resolve_tenant_id(request, target_tenant)

            result = OfflinePlanLapseReconciliationService.auto_reconcile_tenant_held_queue(
                tenant_id=tenant_id,
                actor_user=request.user,
            )
            return Response(result, status=status.HTTP_200_OK)

        except CrossTenantViolationError as err:
            return Response(
                {"error": "ERR_CROSS_TENANT_VIOLATION", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except PermissionError as err:
            return Response(
                {"error": "ERR_PERMISSION_DENIED", "detail": str(err)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except Exception as err:
            logger.exception("Error reconciling all held items: %s", err)
            return Response(
                {"error": "INTERNAL_SERVER_ERROR", "detail": str(err)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

