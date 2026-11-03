"""
backend/apps/core/flexible_medicine_payload_views.py
Architecture Decision: ADR-0159 (P159).
REST API ViewSet for Typed, Versioned Dynamic Medicine Payloads.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.flexible_medicine_payload_service import FlexibleMedicinePayloadService

logger = logging.getLogger("pharmacy.core.flexible_payload.views")


class FlexibleMedicinePayloadViewSet(viewsets.ViewSet):
    """
    ViewSet for Typed, Versioned Dynamic Medicine Payloads.
    Enforces field incompatibility and unknown field rejection with field-level codes.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_payload(self, request):
        """
        Validates typed, versioned payload against strict schema constraints.
        Returns field-level rejection codes for incompatible or unknown fields.
        """
        payload = request.data
        result = FlexibleMedicinePayloadService.validate_flexible_payload(payload)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="submit")
    def submit_payload(self, request):
        """
        Validates and creates or updates a medicine monograph in master catalog.
        Requires authorized role (Cashier is strictly rejected).
        """
        try:
            result = FlexibleMedicinePayloadService.submit_flexible_payload(
                payload=request.data,
                user=request.user,
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="specifications")
    def specifications(self, request):
        """Returns allowed, mandatory, and incompatible attribute specifications for introspection."""
        dosage_form = request.query_params.get("dosage_form_family", "TABLET_CAPSULE")
        try:
            specs = FlexibleMedicinePayloadService.get_dosage_form_specification(dosage_form)
            return Response(specs, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

