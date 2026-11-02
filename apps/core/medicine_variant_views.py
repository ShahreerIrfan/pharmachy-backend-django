"""
REST API ViewSet for Commercial Medicine Variant Structure & SKUs.
Architecture Decision: ADR-0126 (P126).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.medicine_variant_service import MedicineVariantService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger("pharmacy.core.medicine_variant.views")


class MedicineVariantViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for querying and managing commercial medicine variants (SKUs),
    packaging presentations, pack sizes, containers, and regulatory classification inheritance.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _check_tenant_status(self, request):
        """Blocks suspended tenant accounts from accessing catalog endpoints."""
        tenant = getattr(request, "tenant", None)
        if tenant and getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "message_en": "Tenant account is suspended. Catalog access denied.",
                    "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত আছে। ক্যাটালগে প্রবেশাধিকার সংরক্ষিত।",
                }
            )

    def list(self, request) -> Response:
        """GET /api/v2/medicines/variants/"""
        self._check_tenant_status(request)
        product_id = request.query_params.get("product_id")
        search = request.query_params.get("search")
        barcode = request.query_params.get("barcode")
        sku = request.query_params.get("sku")
        is_active_param = request.query_params.get("is_active")

        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        results = MedicineVariantService.list_variants(
            product_id=product_id,
            search=search,
            barcode=barcode,
            sku=sku,
            is_active=is_active,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/medicines/variants/{pk}/"""
        self._check_tenant_status(request)
        try:
            variant = MedicineVariantService.get_variant(variant_id=pk)
            return Response(variant, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request) -> Response:
        """POST /api/v2/medicines/variants/ (Platform Superadmin only)"""
        self._check_tenant_status(request)
        try:
            created = MedicineVariantService.create_variant(
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="lookup-barcode")
    def lookup_barcode(self, request) -> Response:
        """GET /api/v2/medicines/variants/lookup-barcode/?barcode=..."""
        self._check_tenant_status(request)
        barcode = request.query_params.get("barcode", "").strip()
        if not barcode:
            return Response(
                {
                    "error_code": "ERR_MISSING_BARCODE_QUERY",
                    "message_en": "barcode query parameter is required.",
                    "message_bn": "বারকোড তথ্য প্রদান করুন।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        results = MedicineVariantService.list_variants(barcode=barcode)
        if not results:
            return Response(
                {
                    "error_code": "ERR_BARCODE_NOT_FOUND",
                    "barcode": barcode,
                    "message_en": f"No medicine variant registered with barcode '{barcode}'.",
                    "message_bn": f"বারকোড '{barcode}' এর কোনো মেডিসিন ভ্যারিয়েন্ট নেই।",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(results[0], status=status.HTTP_200_OK)
