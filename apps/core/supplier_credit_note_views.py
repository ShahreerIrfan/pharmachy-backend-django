"""
backend/apps/core/supplier_credit_note_views.py
Architecture Decision: ADR-0192 (P192).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing Supplier Credit Notes:
- Records supplier credit notes with traceable origin (Invoice, GRN, Return, PO).
- Posts AP debit notes to SupplierLedgerEntry reducing payable balance.
- Executes non-destructive audited reversals restoring supplier payable balance.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.supplier_credit_note_service import SupplierCreditNoteService

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
                "detail": "Tenant account subscription is suspended. Credit note processing locked.",
            }
        )


class SupplierCreditNoteViewSet(viewsets.ViewSet):
    """
    REST API ViewSet governing Supplier Credit Notes.
    Routes:
      - GET    /api/v2/procurement/supplier-credit-notes/
      - POST   /api/v2/procurement/supplier-credit-notes/
      - GET    /api/v2/procurement/supplier-credit-notes/{pk}/
      - POST   /api/v2/procurement/supplier-credit-notes/{pk}/post-credit-note/
      - POST   /api/v2/procurement/supplier-credit-notes/{pk}/reverse/
      - POST   /api/v2/procurement/supplier-credit-notes/{pk}/cancel/
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/supplier-credit-notes/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        supplier_id = request.query_params.get("supplier_id")
        status_filter = request.query_params.get("status")
        origin_type = request.query_params.get("origin_type")
        branch_id = request.query_params.get("branch_id") or _get_request_branch_id(request)

        try:
            notes = SupplierCreditNoteService.list_credit_notes(
                tenant_id=tenant_id,
                supplier_id=supplier_id,
                status_filter=status_filter,
                origin_type=origin_type,
                branch_id=branch_id,
            )
            return Response({"count": len(notes), "results": notes}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/supplier-credit-notes/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            note = SupplierCreditNoteService.get_credit_note(
                tenant_id=tenant_id,
                note_id=pk,
            )
            return Response(note, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/supplier-credit-notes/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        auto_post = request.query_params.get("auto_post", "").lower() in ("true", "1")
        try:
            note = SupplierCreditNoteService.create_credit_note(
                tenant_id=tenant_id,
                actor_user=request.user,
                data=request.data,
                auto_post=auto_post,
            )
            return Response(note, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error creating supplier credit note: %s", exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="post-credit-note")
    def post_credit_note(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/supplier-credit-notes/{pk}/post-credit-note/
        Finalizes credit note and posts AP debit entry to SupplierLedgerEntry.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            note = SupplierCreditNoteService.post_credit_note(
                tenant_id=tenant_id,
                actor_user=request.user,
                note_id=pk,
            )
            return Response(note, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error posting credit note %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/supplier-credit-notes/{pk}/reverse/
        Executes audited non-destructive reversal, restoring AP balance.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "")
        try:
            note = SupplierCreditNoteService.reverse_credit_note(
                tenant_id=tenant_id,
                actor_user=request.user,
                note_id=pk,
                reversal_reason=reason,
            )
            return Response(note, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error reversing credit note %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None) -> Response:
        """
        POST /api/v2/procurement/supplier-credit-notes/{pk}/cancel/
        Cancels an unposted draft credit note.
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        reason = request.data.get("reason", "")
        try:
            note = SupplierCreditNoteService.cancel_credit_note(
                tenant_id=tenant_id,
                actor_user=request.user,
                note_id=pk,
                reason=reason,
            )
            return Response(note, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error cancelling credit note %s: %s", pk, exc)
            return Response({"error_code": "ERR_INTERNAL", "detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

