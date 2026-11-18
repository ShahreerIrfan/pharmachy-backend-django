"""
backend/apps/core/party_import_export_views.py
Architecture Decision: ADR-0278 (P278).
Phase 14: Customers, Suppliers, Dues & Payments.

REST API ViewSet for Party (Customer & Supplier) Import/Export with Preview,
Deduplication, Scoped Permissions and Privacy Review.
"""

from __future__ import annotations

import logging
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.party_import_export_service import PartyImportExportService

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


def _check_tenant_suspended(request):
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Party import/export blocked.",
                "message_en": "Tenant account subscription is suspended. Party import/export blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। পার্টি ইমপোর্ট/এক্সপোর্ট বন্ধ করা হয়েছে।",
            }
        )


class PartyImportExportViewSet(viewsets.ViewSet):
    """
    ViewSet governing party (Customer & Supplier) bulk import/export.
    Endpoints:
    - POST /api/v2/parties/export/
    - POST /api/v2/parties/import-preview/
    - POST /api/v2/parties/import-commit/
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="export")
    def export(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        party_type = request.data.get("party_type", "CUSTOMER")
        include_sensitive = bool(request.data.get("include_sensitive", False))

        try:
            records = PartyImportExportService.export_parties(
                actor_user=request.user,
                tenant_id=tenant_id,
                party_type=party_type,
                include_sensitive=include_sensitive,
            )
            return Response(
                {
                    "party_type": party_type.upper(),
                    "count": len(records),
                    "privacy_masked": not (PartyImportExportService.can_view_unmasked_privacy(request.user) and include_sensitive),
                    "parties": records,
                },
                status=status.HTTP_200_OK,
            )
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="import-preview")
    def import_preview(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        party_type = request.data.get("party_type", "CUSTOMER")
        records = request.data.get("records", [])

        try:
            preview = PartyImportExportService.preview_import(
                actor_user=request.user,
                tenant_id=tenant_id,
                party_type=party_type,
                records=records,
            )
            return Response(preview, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="import-commit")
    def import_commit(self, request) -> Response:
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        party_type = request.data.get("party_type", "CUSTOMER")
        records = request.data.get("records", [])
        duplicate_resolution = request.data.get("duplicate_resolution", "SKIP")

        try:
            result = PartyImportExportService.commit_import(
                actor_user=request.user,
                tenant_id=tenant_id,
                party_type=party_type,
                records=records,
                duplicate_resolution=duplicate_resolution,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)
