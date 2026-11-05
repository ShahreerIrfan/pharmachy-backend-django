"""
backend/apps/core/supplier_organization_views.py
Architecture Decision: ADR-0181 (P181).
Phase 10: Suppliers, Purchase Orders & Supply Chain.

REST API ViewSet governing tenant-scoped supplier organizations, legal credentials,
accounts payable (AP) ledger postings, and authentic origin verification.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import TenantLifecycleStatus
from apps.core.supplier_organization_service import SupplierOrganizationService

logger = logging.getLogger(__name__)


def _get_request_tenant_id(request) -> str:
    """Extracts tenant ID from authenticated user, request tenant, or header."""
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
    """Blocks suspended tenants from mutating procurement resources."""
    tenant = getattr(request, "tenant", None)
    if tenant and (
        getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED
        or getattr(tenant, "subscription_status", None) == "SUSPENDED"
    ):
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "detail": "Tenant account subscription is suspended. Procurement access blocked.",
                "message_en": "Tenant account subscription is suspended. Procurement access blocked.",
                "message_bn": "ফার্মেসি সাবস্ক্রিপশন স্থগিত আছে। সরবরাহকারী মডিউল বন্ধ।",
            }
        )


class SupplierOrganizationViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for tenant-scoped pharmaceutical supplier organization directory.
    Enforces strict tenant isolation, role-based procurement RBAC, and AP ledger auditability.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/procurement/suppliers/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        search = request.query_params.get("search")
        region = request.query_params.get("region")
        status_filter = request.query_params.get("status")
        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        try:
            results = SupplierOrganizationService.list_suppliers(
                tenant_id=tenant_id,
                search=search,
                region=region,
                status=status_filter,
                is_active=is_active,
            )
            return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/suppliers/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            s = SupplierOrganizationService.get_supplier(tenant_id=tenant_id, supplier_id=pk)
            return Response(s, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/procurement/suppliers/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        # If payload provides tenant_id, use payload's tenant_id to enable cross-tenant checks
        target_tenant = request.data.get("tenant_id") or tenant_id
        try:
            created = SupplierOrganizationService.create_supplier(
                tenant_id=target_tenant,
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def partial_update(self, request, pk=None) -> Response:
        """PATCH /api/v2/procurement/suppliers/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            updated = SupplierOrganizationService.update_supplier(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=pk,
                data=request.data,
            )
            return Response(updated, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, pk=None) -> Response:
        """DELETE /api/v2/procurement/suppliers/{pk}/ (Soft-delete / archive)"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            archived = SupplierOrganizationService.archive_or_delete_supplier(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=pk,
            )
            return Response(archived, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="post-ledger")
    def post_ledger(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/suppliers/{pk}/post-ledger/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            entry = SupplierOrganizationService.post_ap_ledger_entry(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=pk,
                data=request.data,
            )
            return Response(entry, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["get"], url_path="ledger")
    def list_ledger(self, request, pk=None) -> Response:
        """GET /api/v2/procurement/suppliers/{pk}/ledger/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            entries = SupplierOrganizationService.list_ledger_entries(
                tenant_id=tenant_id,
                supplier_id=pk,
            )
            return Response({"count": len(entries), "results": entries}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["post"], url_path="verify-origin")
    def verify_origin(self, request, pk=None) -> Response:
        """POST /api/v2/procurement/suppliers/{pk}/verify-origin/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        notes = request.data.get("notes", "")
        try:
            verified = SupplierOrganizationService.verify_proof_of_origin(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=pk,
                notes=notes,
            )
            return Response(verified, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    # =========================================================================
    # SUPPLIER CONTACT PERSONS (P182)
    # =========================================================================

    @action(detail=True, methods=["get", "post"], url_path="contacts")
    def contacts(self, request, pk=None) -> Response:
        """
        GET /api/v2/procurement/suppliers/{pk}/contacts/
        POST /api/v2/procurement/suppliers/{pk}/contacts/
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        if request.method.upper() == "GET":
            role = request.query_params.get("role")
            is_active_param = request.query_params.get("is_active")
            is_active = None
            if is_active_param is not None:
                is_active = is_active_param.lower() in ("true", "1")

            try:
                results = SupplierOrganizationService.list_supplier_contacts(
                    tenant_id=tenant_id,
                    supplier_id=pk,
                    role=role,
                    is_active=is_active,
                )
                return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
            except PermissionDenied as exc:
                return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
            except NotFound as exc:
                return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

        # POST create contact
        try:
            created = SupplierOrganizationService.create_supplier_contact(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=pk,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except NotFound as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["get", "patch", "delete"], url_path=r"contacts/(?P<contact_id>[^/.]+)")
    def contact_detail(self, request, pk=None, contact_id=None) -> Response:
        """
        GET /api/v2/procurement/suppliers/{pk}/contacts/{contact_id}/
        PATCH /api/v2/procurement/suppliers/{pk}/contacts/{contact_id}/
        DELETE /api/v2/procurement/suppliers/{pk}/contacts/{contact_id}/
        """
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)

        if request.method.upper() == "GET":
            try:
                contact = SupplierOrganizationService.get_supplier_contact(
                    tenant_id=tenant_id,
                    supplier_id=pk,
                    contact_id=contact_id,
                )
                return Response(contact, status=status.HTTP_200_OK)
            except PermissionDenied as exc:
                return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
            except NotFound as exc:
                return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

        elif request.method.upper() == "PATCH":
            try:
                updated = SupplierOrganizationService.update_supplier_contact(
                    tenant_id=tenant_id,
                    actor_user=request.user,
                    supplier_id=pk,
                    contact_id=contact_id,
                    data=request.data,
                )
                return Response(updated, status=status.HTTP_200_OK)
            except PermissionDenied as exc:
                return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
            except ValidationError as exc:
                return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
            except NotFound as exc:
                return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

        elif request.method.upper() == "DELETE":
            try:
                archived = SupplierOrganizationService.archive_supplier_contact(
                    tenant_id=tenant_id,
                    actor_user=request.user,
                    supplier_id=pk,
                    contact_id=contact_id,
                )
                return Response(archived, status=status.HTTP_200_OK)
            except PermissionDenied as exc:
                return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
            except NotFound as exc:
                return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

