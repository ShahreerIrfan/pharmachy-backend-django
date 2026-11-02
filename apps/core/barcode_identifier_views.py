"""
backend/apps/core/barcode_identifier_views.py
Architecture Decision: ADR-0136 (P136).
REST API ViewSet for Primary, Secondary, and Alternate Barcode Identifiers:
Multi-Tier Packaging GTINs, Contextual Uniqueness, and Scan Prioritization Resolution.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from apps.core.barcode_identifier_service import BarcodeIdentifierService
from apps.core.models import (
    BarcodeAlternateIdentifier,
    Tenant,
    TenantLifecycleStatus,
)
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.barcode_identifier.views")


class BarcodeAlternateIdentifierViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for Barcode & Alternate Identifiers.
    Supports primary, secondary, and alternate barcodes across packaging levels
    (UNIT, STRIP, BOX, SHIPPER_CARTON), contextual uniqueness, and high-speed POS scan prioritization.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _require_inventory_role(self, request):
        """Cashiers cannot add, update priority, or archive barcode identifiers."""
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication credentials were not provided.")

        if self._is_platform_operator(user):
            return

        role = str(getattr(user, "role", "")).upper().strip()
        allowed = {
            UserRole.PHARMACY_OWNER,
            UserRole.BRANCH_MANAGER,
            UserRole.STAFF_PHARMACIST,
            "OWNER",
            "MANAGER",
            "PHARMACIST",
        }
        if role not in allowed:
            raise PermissionDenied(
                detail={
                    "error_code": "ERR_STAFF_ROLE_DENIED",
                    "message_en": "Cashiers cannot register or mutate barcode identifiers. Contact store manager.",
                    "message_bn": "ক্যাশিয়াররা নতুন বারকোড নিবন্ধন বা পরিবর্তন করতে পারবেন না। স্টোর ম্যানেজারের সাথে যোগাযোগ করুন।",
                }
            )

    def _check_tenant_status(self, request):
        """Guard against suspended tenants mutating barcode identifiers."""
        user = getattr(request, "user", None)
        if self._is_platform_operator(user):
            return

        tenant_id = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        if tenant_id:
            tenant = Tenant.objects.filter(tenant_id=tenant_id).first()
            if tenant and tenant.status == TenantLifecycleStatus.SUSPENDED:
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_TENANT_SUSPENDED",
                        "message_en": "Tenant account is suspended. Barcode modification access denied.",
                        "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত থাকায় বারকোড পরিবর্তন করা বন্ধ রয়েছে।",
                    }
                )

    def create(self, request) -> Response:
        """
        POST /api/v2/catalog/barcodes/
        Registers a new barcode identifier across packaging hierarchy.
        """
        self._require_inventory_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = (
            request.data.get("tenant_id")
            if self._is_platform_operator(user) and request.data.get("tenant_id")
            else getattr(user, "tenant_id", "platform")
        )

        data = request.data or {}
        try:
            result = BarcodeIdentifierService.register_barcode(
                barcode_value=data.get("barcode_value", ""),
                identifier_type=data.get("identifier_type", ""),
                packaging_level=data.get("packaging_level", ""),
                product_id=data.get("product_id"),
                non_medicine_item_id=data.get("non_medicine_item_id"),
                variant_id=data.get("variant_id"),
                priority_order=data.get("priority_order"),
                pack_quantity_multiplier=data.get("pack_quantity_multiplier"),
                description=data.get("description", ""),
                actor=user,
                tenant_id=tenant_id,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            msg_dict = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            return Response(msg_dict, status=status.HTTP_400_BAD_REQUEST)

    def list(self, request) -> Response:
        """
        GET /api/v2/catalog/barcodes/
        Lists barcodes with filtering. Accessible to all staff including cashiers for checkout scan lookup.
        """
        user = request.user
        tenant_id = (
            request.query_params.get("tenant_id")
            if self._is_platform_operator(user) and request.query_params.get("tenant_id")
            else getattr(user, "tenant_id", None)
        )

        product_id = request.query_params.get("product_id")
        non_medicine_item_id = request.query_params.get("non_medicine_item_id")
        variant_id = request.query_params.get("variant_id")
        is_active_raw = request.query_params.get("is_active")
        is_active = is_active_raw.lower() in ("true", "1") if is_active_raw is not None else None
        search = request.query_params.get("search")

        results = BarcodeIdentifierService.list_barcodes(
            product_id=product_id,
            non_medicine_item_id=non_medicine_item_id,
            variant_id=variant_id,
            tenant_id=tenant_id,
            is_active=is_active,
            search=search,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/catalog/barcodes/{id}/
        Retrieves a single barcode identifier.
        """
        record = BarcodeAlternateIdentifier.objects.filter(identifier_id=str(pk).strip()).first()
        if not record:
            raise NotFound({"detail": f"Barcode identifier '{pk}' not found."})

        return Response(BarcodeIdentifierService.serialize_identifier(record), status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="lookup")
    def lookup(self, request) -> Response:
        """
        GET /api/v2/catalog/barcodes/lookup/?code=...
        High-speed scan prioritization resolution endpoint.
        Returns matched product, packaging level, and quantity multiplier.
        Accessible to all staff including cashiers.
        """
        code = request.query_params.get("code") or request.query_params.get("barcode") or request.query_params.get("barcode_value")
        if not code:
            return Response(
                {"error_code": "ERR_MISSING_BARCODE_PARAM", "detail": "Query parameter 'code' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        tenant_id = getattr(user, "tenant_id", None)
        result = BarcodeIdentifierService.resolve_barcode(barcode_value=code, tenant_id=tenant_id)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="priority")
    def priority(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/barcodes/{id}/priority/
        Amends the priority order of a barcode identifier.
        """
        self._require_inventory_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = getattr(user, "tenant_id", None) if not self._is_platform_operator(user) else None

        new_priority = request.data.get("priority_order") or request.data.get("priority")
        if new_priority is None:
            return Response(
                {"priority_order": "Field 'priority_order' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = BarcodeIdentifierService.update_priority(
                identifier_id=pk,
                new_priority=int(new_priority),
                actor=user,
                tenant_id=tenant_id,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            msg_dict = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            return Response(msg_dict, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/barcodes/{id}/archive/
        Soft-archives a barcode identifier.
        """
        self._require_inventory_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = getattr(user, "tenant_id", None) if not self._is_platform_operator(user) else None
        reason = request.data.get("reason", "")

        try:
            result = BarcodeIdentifierService.archive_barcode(
                identifier_id=pk,
                actor=user,
                tenant_id=tenant_id,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            msg_dict = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            return Response(msg_dict, status=status.HTTP_400_BAD_REQUEST)
