"""
REST API ViewSets for Manufacturer & Supplier Catalogs, Merges and Archival.
Architecture Decision: ADR-0127 (P127).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.manufacturer_supplier_service import ManufacturerSupplierService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger("pharmacy.core.manufacturer_supplier.views")


def _get_request_tenant_id(request) -> str:
    """Extracts tenant ID from authenticated user or request tenant."""
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "tenant_id", None):
        return tenant.tenant_id
    user = getattr(request, "user", None)
    if user and getattr(user, "tenant_id", None):
        return user.tenant_id
    header = request.META.get("HTTP_X_TENANT_ID")
    if header:
        return header
    return ""


def _check_tenant_suspended(request):
    """Blocks suspended tenants."""
    tenant = getattr(request, "tenant", None)
    if tenant and getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED:
        raise PermissionDenied(
            {
                "error_code": "ERR_TENANT_SUSPENDED",
                "message_en": "Tenant account is suspended. Operation blocked.",
                "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত আছে। প্রবেশাধিকার সংরক্ষিত।",
            }
        )


class ManufacturerViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for global shared manufacturer reference catalog.
    Platform Superadmins can curate and merge; tenant users have read-only access.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/catalog/manufacturers/"""
        _check_tenant_suspended(request)
        search = request.query_params.get("search")
        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        results = ManufacturerSupplierService.list_manufacturers(search=search, is_active=is_active)
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/catalog/manufacturers/{pk}/"""
        _check_tenant_suspended(request)
        try:
            m = ManufacturerSupplierService.get_manufacturer(manufacturer_id=pk)
            return Response(m, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/catalog/manufacturers/ (Platform Superadmin only)"""
        _check_tenant_suspended(request)
        try:
            created = ManufacturerSupplierService.create_manufacturer(
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="merge")
    def merge(self, request, pk=None) -> Response:
        """POST /api/v2/catalog/manufacturers/{pk}/merge/ (Platform Superadmin only)"""
        _check_tenant_suspended(request)
        target_id = request.data.get("target_id")
        reason = request.data.get("reason", "Consolidation of duplicate manufacturer entries")

        if not target_id:
            return Response(
                {
                    "error_code": "ERR_MISSING_TARGET_ID",
                    "message_en": "target_id is required to merge manufacturer.",
                    "message_bn": "মার্জ করার জন্য লক্ষ্য প্রতিষ্ঠানের আইডি (target_id) আবশ্যক।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = ManufacturerSupplierService.merge_manufacturers(
                actor_user=request.user,
                source_id=pk,
                target_id=target_id,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)


class SupplierViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for tenant-scoped pharmaceutical supplier directory.
    Enforces strict tenant boundary, role-based merge & archive permissions.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/suppliers/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        search = request.query_params.get("search")
        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        results = ManufacturerSupplierService.list_suppliers(
            tenant_id=tenant_id,
            search=search,
            is_active=is_active,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/suppliers/{pk}/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            s = ManufacturerSupplierService.get_supplier(tenant_id=tenant_id, supplier_id=pk)
            return Response(s, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/suppliers/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            created = ManufacturerSupplierService.create_supplier(
                tenant_id=tenant_id,
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="merge")
    def merge(self, request, pk=None) -> Response:
        """POST /api/v2/suppliers/{pk}/merge/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        target_id = request.data.get("target_id")
        reason = request.data.get("reason", "Consolidation of duplicate supplier entries")

        if not target_id:
            return Response(
                {
                    "error_code": "ERR_MISSING_TARGET_ID",
                    "message_en": "target_id is required to merge supplier.",
                    "message_bn": "মার্জ করার জন্য লক্ষ্য সরবরাহকারীর আইডি (target_id) আবশ্যক।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = ManufacturerSupplierService.merge_suppliers(
                tenant_id=tenant_id,
                actor_user=request.user,
                source_id=pk,
                target_id=target_id,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None) -> Response:
        """POST /api/v2/suppliers/{pk}/archive/"""
        _check_tenant_suspended(request)
        tenant_id = _get_request_tenant_id(request)
        try:
            archived = ManufacturerSupplierService.archive_supplier(
                tenant_id=tenant_id,
                actor_user=request.user,
                supplier_id=pk,
            )
            return Response(archived, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
