"""
REST API ViewSet for Manager and Supervisor Approvals (Maker-Checker Workflow).
Architecture Decision: ADR-0005, ADR-0104, ADR-0108 (P108).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.

Enforces Dual-Control (Maker != Checker) segregation across:
- HIGH_DISCOUNT: High retail discounts
- REFUND: Sales returns and refunds
- STOCK_WRITE_OFF: Stock breakage and write-offs
- SENSITIVE_EXPENSE: Operating expense disbursements
- CLINICAL_ESCALATION: Prescribing interaction and clinical overrides
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.approval_service import (
    ApprovalWorkflowService,
    InsufficientApprovalAuthorityError,
    MakerCheckerViolationError,
    SubscriptionSuspendedError,
)
from apps.core.postgres_service import CrossTenantOperationError

logger = logging.getLogger("pharmacy.approvals.views")


class ApprovalRequestViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for dual-control Maker-Checker approvals.
    Allows staff to submit requests and supervisors/managers/owners to review, approve, or reject.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant_and_role(self, request) -> Tuple[str, str, Optional[str]]:
        user = request.user
        role = str(getattr(user, "role", "")).upper()

        if not user or not user.is_authenticated:
            raise PermissionDenied(
                {
                    "error_code": "ERR_AUTHENTICATION_REQUIRED",
                    "message_en": "Authentication credentials were not provided.",
                    "message_bn": "প্রবেশাধিকারের জন্য অথেনটিকেশন প্রয়োজন।",
                }
            )

        tenant_id = (
            getattr(user, "tenant_id", None)
            or getattr(user, "tenant", None)
            or request.headers.get("X-Tenant-ID")
            or request.query_params.get("tenant_id")
        )
        if hasattr(tenant_id, "tenant_id"):
            tenant_id = tenant_id.tenant_id

        if not tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_NOT_IDENTIFIED",
                    "message_en": "Tenant identification missing from user session or header.",
                    "message_bn": "টেন্যান্ট চিহ্নিত করা যায়নি।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        if hasattr(user_branch_id, "branch_id"):
            user_branch_id = user_branch_id.branch_id

        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None

    def list(self, request) -> Response:
        """Lists approval requests filtered by status, type, urgency, or branch."""
        tenant_id, role, user_branch_id = self._resolve_tenant_and_role(request)

        branch_id = request.query_params.get("branch_id")
        # If Branch Manager, default filter to assigned branch
        if role == "BRANCH_MANAGER" and user_branch_id and not branch_id:
            branch_id = user_branch_id

        status_filter = request.query_params.get("status")
        type_filter = request.query_params.get("approval_type")
        urgency_filter = request.query_params.get("urgency")

        try:
            results = ApprovalWorkflowService.list_requests(
                tenant_id=tenant_id,
                branch_id=branch_id,
                status=status_filter,
                approval_type=type_filter,
                urgency=urgency_filter,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            return Response(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

    def create(self, request) -> Response:
        """Submits a new Maker-Checker approval request (MAKER)."""
        tenant_id, _, user_branch_id = self._resolve_tenant_and_role(request)
        data = request.data
        ip_addr = request.META.get("REMOTE_ADDR", "")

        branch_id = data.get("branch_id") or user_branch_id
        if not branch_id:
            return Response(
                {"error_code": "BRANCH_REQUIRED", "message": "Branch identifier is required for approval requests."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = ApprovalWorkflowService.submit_request(
                tenant_id=tenant_id,
                branch_id=branch_id,
                maker_user=request.user,
                approval_type=data.get("approval_type"),
                title=data.get("title"),
                reason=data.get("reason"),
                amount_bdt=data.get("amount_bdt", 0),
                discount_percent=data.get("discount_percent", 0),
                urgency=data.get("urgency", "ROUTINE"),
                payload=data.get("payload", {}),
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None) -> Response:
        """Retrieves details of an approval request with audit log."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        try:
            detail = ApprovalWorkflowService.get_request_detail(tenant_id=tenant_id, request_id=pk)
            return Response(detail, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            return Response(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None) -> Response:
        """Approves a pending request (CHECKER). Enforces Maker != Checker."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        decision_notes = request.data.get("decision_notes", "")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = ApprovalWorkflowService.approve_request(
                tenant_id=tenant_id,
                request_id=pk,
                checker_user=request.user,
                decision_notes=decision_notes,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except MakerCheckerViolationError as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except InsufficientApprovalAuthorityError as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None) -> Response:
        """Rejects a pending request (CHECKER). Enforces Maker != Checker."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        reason = request.data.get("rejection_reason") or request.data.get("reason", "")
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = ApprovalWorkflowService.reject_request(
                tenant_id=tenant_id,
                request_id=pk,
                checker_user=request.user,
                rejection_reason=reason,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except MakerCheckerViolationError as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except InsufficientApprovalAuthorityError as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None) -> Response:
        """Cancels a pending request (MAKER)."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        ip_addr = request.META.get("REMOTE_ADDR", "")

        try:
            result = ApprovalWorkflowService.cancel_request(
                tenant_id=tenant_id,
                request_id=pk,
                maker_user=request.user,
                ip_address=ip_addr,
            )
            return Response(result, status=status.HTTP_200_OK)
        except SubscriptionSuspendedError as exc:
            return Response({"error_code": "ERR_SUBSCRIPTION_SUSPENDED", "message_en": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except CrossTenantOperationError as exc:
            return Response(
                {"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="verify-token")
    def verify_token(self, request) -> Response:
        """Verifies if an approval token is valid, active, and approved for consumption."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        token = request.data.get("approval_token", "")
        verification = ApprovalWorkflowService.verify_token(tenant_id=tenant_id, approval_token=token)
        if verification.get("valid"):
            return Response(verification, status=status.HTTP_200_OK)
        return Response(verification, status=status.HTTP_400_BAD_REQUEST)
