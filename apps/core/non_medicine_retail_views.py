"""
backend/apps/core/non_medicine_retail_views.py
Architecture Decision: ADR-0135 (P135).
REST API ViewSet for Authorized Non-Medicine Retail Goods:
Separation of Dispensing Controls, Multi-Tenant Catalog Scoping, and Barcode Lookup.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import (
    NonMedicineCategory,
    NonMedicineRetailItem,
    Tenant,
    TenantLifecycleStatus,
)
from apps.core.non_medicine_retail_service import NonMedicineRetailService
from apps.identity.models import UserRole

logger = logging.getLogger("pharmacy.core.non_medicine_retail.views")


class NonMedicineRetailItemViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for Authorized Non-Medicine Retail Goods.
    Preserves strict separation from prescription and narcotic controls,
    while allowing high-speed cashier barcode checkout and store-level catalog management.
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
        """Cashiers cannot add, mutate, or archive retail items in the catalog."""
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
                    "message_en": "Cashiers cannot create or mutate non-medicine retail items. Contact store manager.",
                    "message_bn": "ক্যাশিয়াররা নতুন রিটেইল পণ্য তৈরি বা পরিবর্তন করতে পারবেন না। স্টোর ম্যানেজারের সাথে যোগাযোগ করুন।",
                }
            )

    def _check_tenant_status(self, request):
        """Guard against suspended tenants mutating retail items."""
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
                        "message_en": "Tenant account is suspended. Retail item modification access denied.",
                        "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত থাকায় রিটেইল পণ্য পরিবর্তন করা বন্ধ রয়েছে।",
                    }
                )

    def create(self, request) -> Response:
        """
        POST /api/v2/catalog/retail-items/
        Creates a new non-medicine retail item.
        """
        self._require_inventory_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = getattr(user, "tenant_id", None) if not self._is_platform_operator(user) else request.data.get("tenant_id")

        try:
            result = NonMedicineRetailService.create_item(
                data=request.data or {},
                actor=user,
                tenant_id=tenant_id,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    def list(self, request) -> Response:
        """
        GET /api/v2/catalog/retail-items/
        Lists non-medicine retail items. Accessible to all staff including cashiers for checkout lookup.
        """
        user = request.user
        tenant_id = getattr(user, "tenant_id", None) if not self._is_platform_operator(user) else request.query_params.get("tenant_id")
        category = request.query_params.get("category")
        is_active_raw = request.query_params.get("is_active")
        is_active = is_active_raw.lower() in ("true", "1") if is_active_raw is not None else None
        search = request.query_params.get("search")
        include_global = request.query_params.get("include_global", "true").lower() in ("true", "1")

        results = NonMedicineRetailService.list_items(
            tenant_id=tenant_id,
            category=category,
            is_active=is_active,
            search=search,
            include_global=include_global,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/catalog/retail-items/{id}/
        Retrieves a single non-medicine retail item.
        """
        item = NonMedicineRetailItem.objects.filter(item_id=str(pk).strip()).first()
        if not item:
            raise NotFound({"detail": f"Non-medicine retail item '{pk}' not found."})

        return Response(NonMedicineRetailService.serialize_item(item), status=status.HTTP_200_OK)

    def partial_update(self, request, pk=None) -> Response:
        """
        PATCH /api/v2/catalog/retail-items/{id}/
        Updates retail item attributes.
        """
        self._require_inventory_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = getattr(user, "tenant_id", None) if not self._is_platform_operator(user) else None

        try:
            result = NonMedicineRetailService.update_item(
                item_id=pk,
                data=request.data or {},
                actor=user,
                tenant_id=tenant_id,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None) -> Response:
        """
        POST /api/v2/catalog/retail-items/{id}/archive/
        Soft-archives a retail item.
        """
        self._require_inventory_role(request)
        self._check_tenant_status(request)

        user = request.user
        tenant_id = getattr(user, "tenant_id", None) if not self._is_platform_operator(user) else None
        reason = request.data.get("reason", "")

        try:
            result = NonMedicineRetailService.archive_item(
                item_id=pk,
                actor=user,
                tenant_id=tenant_id,
                reason=reason,
            )
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

    @action(detail=False, methods=["get"], url_path="categories")
    def categories(self, request) -> Response:
        """
        GET /api/v2/catalog/retail-items/categories/
        Returns list of valid non-medicine retail categories.
        """
        categories = [
            {"code": code, "label": label}
            for code, label in NonMedicineCategory.choices
        ]
        return Response({"categories": categories}, status=status.HTTP_200_OK)
