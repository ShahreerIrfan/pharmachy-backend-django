"""
backend/apps/core/medicine_search_views.py
Architecture Decision: ADR-0129 (P129).
API ViewSet for Medicine Search Autocomplete with Barcode Scanning,
Bilingual Matching, Store Overrides, and Latency Metrics.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.errors import CrossTenantViolationError
from apps.core.medicine_search_service import MedicineSearchAutocompleteService


class MedicineSearchAutocompleteViewSet(viewsets.ViewSet):
    """
    High-Speed Medicine Autocomplete & Search ViewSet.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _is_platform_operator(self, user) -> bool:
        if not user or not user.is_authenticated:
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or str(getattr(user, "role", "")).upper() in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]
        )

    def _get_target_tenant_id(self, request) -> str:
        """
        Validates that staff only search within their own authorized tenant.
        """
        user = getattr(request, "user", None)
        user_tenant = getattr(user, "tenant_id", None) or getattr(request, "tenant_id", None)
        is_platform = self._is_platform_operator(user)

        query_tenant = request.query_params.get("tenant_id")
        if is_platform:
            target = query_tenant or user_tenant or "default-tenant"
            return str(target).strip()

        if not user_tenant:
            raise PermissionDenied("User is not associated with an authorized pharmacy tenant.")

        if query_tenant and str(query_tenant).strip() != str(user_tenant).strip():
            raise CrossTenantViolationError(
                message_en=f"Cross-tenant access violation: Cannot search catalog for foreign tenant '{query_tenant}'.",
                message_bn="ভিন্ন ফার্মেসির ওষুধ ক্যাটালগ অনুসন্ধান করা সম্পূর্ণ নিষিদ্ধ।",
            )

        return str(user_tenant).strip()

    def list(self, request):
        """
        GET /api/v2/catalog/search/?q=<query>&limit=10&include_variants=true
        """
        return self.autocomplete(request)

    @action(detail=False, methods=["get"], url_path="autocomplete")
    def autocomplete(self, request):
        """
        GET /api/v2/catalog/search/autocomplete/?q=<query>&limit=10&include_variants=true
        """
        query = request.query_params.get("q") or request.query_params.get("query")
        if not query or not query.strip():
            raise ValidationError({"q": "Search query parameter 'q' is required and cannot be empty."})

        target_tenant = self._get_target_tenant_id(request)
        limit = request.query_params.get("limit", 10)
        include_variants = request.query_params.get("include_variants", "true").lower() in ("true", "1")

        try:
            results = MedicineSearchAutocompleteService.autocomplete(
                query=query.strip(),
                tenant_id=target_tenant,
                limit=limit,
                include_variants=include_variants,
            )
        except DjangoValidationError as err:
            err_dict = err.message_dict if hasattr(err, "message_dict") else {"error": list(err.messages)}
            raise ValidationError(detail=err_dict)

        return Response(
            {
                "success": True,
                "code": "MEDICINE_SEARCH_AUTOCOMPLETE_SUCCESS",
                "message_en": f"Found {results['total_matches']} medicine matches in {results['latency_ms']}ms.",
                "message_bn": f"{results['total_matches']}টি ওষুধের ফলাফল পাওয়া গেছে ({results['latency_ms']} মিলি-সেকেন্ডে)।",
                "data": results,
            },
            status=status.HTTP_200_OK,
        )
