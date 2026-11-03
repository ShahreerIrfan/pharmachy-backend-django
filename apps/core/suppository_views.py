"""
backend/apps/core/suppository_views.py
Architecture Decision: ADR-0150 (P150).
REST API ViewSet for Suppository and Pessary Dosage-Form Editor.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.suppository_editor_service import SuppositoryEditorService

logger = logging.getLogger("pharmacy.core.suppository.views")


class SuppositoryEditorViewSet(viewsets.ViewSet):
    """
    ViewSet for Suppository and Pessary Formulation Editor.
    Provides schema definitions, piece counts, strength, melt-protection storage checks,
    and multi-tenant access control.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the dynamic schema and condition rules for suppository editor."""
        try:
            data = SuppositoryEditorService.get_editor_schema()
            return Response(data, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Failed to retrieve suppository schema: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_payload(self, request):
        """Performs non-destructive domain validation on a suppository payload."""
        try:
            validated = SuppositoryEditorService.validate_payload(request.data)
            return Response({"valid": True, "validated_payload": validated}, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists accessible suppository configurations."""
        product_id = request.query_params.get("product_id")
        tenant_id = request.query_params.get("tenant_id")
        try:
            configs = SuppositoryEditorService.list_configs(
                user=request.user, product_id=product_id, tenant_id=tenant_id
            )
            return Response(configs, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific suppository configuration."""
        try:
            cfg = SuppositoryEditorService.get_config(config_id=pk, user=request.user)
            return Response(cfg, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def create(self, request):
        """Creates or upserts a suppository configuration."""
        try:
            result = SuppositoryEditorService.create_or_update_config(
                payload=request.data, user=request.user
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def partial_update(self, request, pk=None):
        """Updates an existing suppository configuration."""
        cfg = SuppositoryEditorService.get_config(config_id=pk, user=request.user)
        payload = {
            "config_id": pk,
            "product_id": cfg["product_id"],
            "tenant_id": cfg["tenant_id"],
            "suppository_base_type": cfg["suppository_base_type"],
            "administration_route": cfg["administration_route"],
            "strength_value": cfg["strength_value"],
            "strength_unit": cfg["strength_unit"],
            "pieces_per_strip": cfg["pieces_per_strip"],
            "strips_per_pack": cfg["strips_per_pack"],
            "total_pieces_per_pack": cfg["total_pieces_per_pack"],
            "loose_piece_sale_eligible": cfg["loose_piece_sale_eligible"],
            "requires_refrigeration": cfg["requires_refrigeration"],
            "storage_condition": cfg["storage_condition"],
            "patient_instructions_en": cfg["patient_instructions_en"],
            "patient_instructions_bn": cfg["patient_instructions_bn"],
            "is_active": cfg["is_active"],
        }
        payload.update(request.data)
        try:
            result = SuppositoryEditorService.create_or_update_config(payload=payload, user=request.user)
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def destroy(self, request, pk=None):
        """Deletes a suppository configuration."""
        try:
            SuppositoryEditorService.delete_config(config_id=pk, user=request.user)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

