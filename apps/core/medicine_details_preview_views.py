"""
backend/apps/core/medicine_details_preview_views.py
Architecture Decision: ADR-0138 (P138).
REST API ViewSet for Unified Medicine Details Preview:
Form, Strength Taxonomy, Administration Route, Packaging Hierarchy,
Rx / AWaRe Status, Cold Chain Storage, Variants, and Recall Alerts.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.medicine_details_preview_service import MedicineDetailsPreviewService
from apps.core.models import MedicineProduct

logger = logging.getLogger("pharmacy.core.medicine_details_preview.views")


class MedicineDetailsPreviewViewSet(viewsets.ViewSet):
    """
    REST API ViewSet delivering a consolidated, clinical and operational preview
    of a medicine across pharmaceutical form, strength, administration route,
    packaging tiers, Rx classification, storage rules, variants, and active recall alerts.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def retrieve(self, request, pk=None) -> Response:
        """
        GET /api/v2/catalog/medicine-previews/{id}/
        Retrieves unified details preview for a medicine product.
        """
        user = request.user
        target_tenant = request.query_params.get("tenant_id")

        if target_tenant and not self._is_platform_operator(user):
            user_tenant = getattr(user, "tenant_id", None)
            if user_tenant and target_tenant.strip() != user_tenant.strip():
                raise PermissionDenied(
                    detail={
                        "error_code": "ERR_CROSS_TENANT_ACCESS",
                        "message_en": "Cannot query medicine details preview with overrides from another tenant.",
                        "message_bn": "অন্য ফার্মেসির মেটাডাটা বা কাস্টম ওভাররাইড সহ প্রিভিউ দেখার অনুমতি নেই।",
                    }
                )

        effective_tenant = target_tenant if self._is_platform_operator(user) and target_tenant else getattr(user, "tenant_id", None)

        try:
            preview_data = MedicineDetailsPreviewService.generate_preview(
                product_id=pk,
                tenant_id=effective_tenant,
                actor=user,
            )
            return Response(preview_data, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            msg = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            return Response(msg, status=status.HTTP_404_NOT_FOUND if "not found" in str(msg).lower() else status.HTTP_400_BAD_REQUEST)

    def list(self, request) -> Response:
        """
        GET /api/v2/catalog/medicine-previews/?search=...&limit=10
        Lists preview summaries matching search query.
        """
        user = request.user
        search = request.query_params.get("search") or request.query_params.get("q") or ""
        limit_raw = request.query_params.get("limit", "10")
        try:
            limit = min(50, max(1, int(limit_raw)))
        except ValueError:
            limit = 10

        effective_tenant = getattr(user, "tenant_id", None)

        qs = MedicineProduct.objects.all()
        if search.strip():
            q = search.strip()
            qs = qs.filter(brand_name__icontains=q) | qs.filter(generic_name__icontains=q)

        products = list(qs.order_by("brand_name")[:limit])
        results = []
        for p in products:
            try:
                preview = MedicineDetailsPreviewService.generate_preview(
                    product_id=p.product_id,
                    tenant_id=effective_tenant,
                    actor=user,
                )
                results.append(preview)
            except Exception as e:
                logger.warning(f"Error generating preview for {p.product_id}: {e}")

        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)
