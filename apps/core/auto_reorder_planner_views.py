"""
backend/apps/core/auto_reorder_planner_views.py
Architecture Decision: ADR-0194 (P194).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing Automatic Reorder Planner:
- Recommends replenishment quantities using lead time, safety stock, FEFO exposure, and demand history.
- Never orders silently: generates structured proposals requiring explicit human authorization.
- Provides managerial approval workflows and formal conversion into real Purchase Orders.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.auto_reorder_planner_service import AutoReorderPlannerService
from apps.core.models import TenantLifecycleStatus

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
                "detail": "Tenant account subscription is suspended. Reorder planning locked.",
            }
        )


class AutoReorderPlannerViewSet(viewsets.ViewSet):
    """
    REST API ViewSet governing Automatic Reorder Planner.
    Routes:
      - GET    /api/v2/procurement/reorder-plans/
      - POST   /api/v2/procurement/reorder-plans/
      - GET    /api/v2/procurement/reorder-plans/{pk}/
      - POST   /api/v2/procurement/reorder-plans/{pk}/approve/
      - POST   /api/v2/procurement/reorder-plans/{pk}/convert-to-po/
      - POST   /api/v2/procurement/reorder-plans/{pk}/dismiss/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/reorder-plans/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.query_params.get("branch_id") or _get_request_branch_id(request)
        status_filter = request.query_params.get("status")

        try:
            plans = AutoReorderPlannerService.list_reorder_plans(
                tenant_id=tenant_id,
                branch_id=branch_id,
                status_filter=status_filter,
            )
            return Response({"count": len(plans), "results": plans}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/reorder-plans/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            plan = AutoReorderPlannerService.get_reorder_plan(
                tenant_id=tenant_id,
                proposal_id=pk,
            )
            return Response(plan, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/reorder-plans/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        branch_id = request.data.get("branch_id") or _get_request_branch_id(request)
        horizon = int(request.data.get("planning_horizon_days", 14))
        expiry_days = int(request.data.get("expiry_threshold_days", 60))
        notes = request.data.get("notes", "")

        try:
            plan = AutoReorderPlannerService.generate_reorder_plan(
                tenant_id=tenant_id,
                actor_user=request.user,
                branch_id=branch_id,
                horizon_days=horizon,
                expiry_threshold_days=expiry_days,
                notes=notes,
            )
            return Response(plan, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error generating reorder plan: %s", exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/reorder-plans/{pk}/approve/
        Explicit human managerial authorization of recommended reorder quantities.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        review_notes = request.data.get("review_notes", "")
        item_adjustments = request.data.get("item_adjustments")
        try:
            plan = AutoReorderPlannerService.approve_reorder_plan(
                tenant_id=tenant_id,
                actor_user=request.user,
                proposal_id=pk,
                review_notes=review_notes,
                item_adjustments=item_adjustments,
            )
            return Response(plan, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error approving reorder plan %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="convert-to-po")
    def convert_to_po(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/reorder-plans/{pk}/convert-to-po/
        Converts approved reorder proposal items into actual Purchase Orders.
        Guarded: fails if plan is not in APPROVED status. Never orders silently.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            result = AutoReorderPlannerService.convert_to_purchase_orders(
                tenant_id=tenant_id,
                actor_user=request.user,
                proposal_id=pk,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error converting reorder plan %s to PO: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="dismiss")
    def dismiss(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/reorder-plans/{pk}/dismiss/
        Dismisses/rejects reorder proposal with reason.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "")
        try:
            plan = AutoReorderPlannerService.dismiss_reorder_plan(
                tenant_id=tenant_id,
                actor_user=request.user,
                proposal_id=pk,
                reason=reason,
            )
            return Response(plan, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error dismissing reorder plan %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
