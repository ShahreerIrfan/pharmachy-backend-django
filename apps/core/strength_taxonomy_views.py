"""
REST API ViewSet for Strength Measurement Taxonomy & Conversion.
Architecture Decision: ADR-0123 (P123).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.strength_taxonomy_service import StrengthTaxonomyService

logger = logging.getLogger("pharmacy.core.strength_taxonomy.views")


class StrengthTaxonomyViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for strength measurement taxonomy, parsing, and incompatible conversion prevention.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/medicines/strength-taxonomy/"""
        catalog = StrengthTaxonomyService.get_taxonomy_catalog()
        return Response(catalog, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="convert")
    def convert_unit(self, request) -> Response:
        """POST /api/v2/medicines/strength-taxonomy/convert/"""
        data = request.data
        value = data.get("value")
        from_unit = data.get("from_unit")
        to_unit = data.get("to_unit")

        if value is None or not from_unit or not to_unit:
            return Response(
                {
                    "error_code": "ERR_INVALID_CONVERSION_INPUT",
                    "message_en": "value, from_unit, and to_unit are required.",
                    "message_bn": "মান, উৎস ইউনিট এবং লক্ষ্য ইউনিট আবশ্যক।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = StrengthTaxonomyService.convert_strength(
                value=value,
                from_unit=from_unit,
                to_unit=to_unit,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="parse")
    def parse_strength_string(self, request) -> Response:
        """POST /api/v2/medicines/strength-taxonomy/parse/"""
        raw = request.data.get("strength")
        if not raw:
            return Response(
                {
                    "error_code": "ERR_INVALID_STRENGTH_STRING",
                    "message_en": "strength parameter is required.",
                    "message_bn": "স্ট্রেংথ তথ্য প্রদান করুন।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            parsed = StrengthTaxonomyService.parse_strength(raw)
            return Response(parsed, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
