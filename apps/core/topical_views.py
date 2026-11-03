"""
backend/apps/core/topical_views.py
Architecture Decision: ADR-0148 (P148).
REST API ViewSet for Cream, Ointment, Gel, and Lotion Topical Editor.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.topical_editor_service import TopicalEditorService

logger = logging.getLogger("pharmacy.core.topical.views")


class TopicalEditorViewSet(viewsets.ViewSet):
    """
    ViewSet for Topical Formulation Editor (Creams, Ointments, Gels, Lotions).
    Provides schema definitions, net weight parameters, ophthalmic sterility checks,
    and multi-tenant access control.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the dynamic schema and condition rules for topical editor."""
        try:
            data = TopicalEditorService.get_editor_schema()
            return Response(data, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Failed to retrieve topical schema: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_payload(self, request):
        """Performs non-destructive domain validation on a topical payload."""
        try:
            validated = TopicalEditorService.validate_payload(request.data)
            return Response({"valid": True, "validated_payload": validated}, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists accessible topical configurations."""
        product_id = request.query_params.get("product_id")
        tenant_id = request.query_params.get("tenant_id")
        try:
            configs = TopicalEditorService.list_configs(
                user=request.user, product_id=product_id, tenant_id=tenant_id
            )
            return Response(configs, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific topical configuration."""
        try:
            cfg = TopicalEditorService.get_config(config_id=pk, user=request.user)
            return Response(cfg, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def create(self, request):
        """Creates or upserts a topical configuration."""
        try:
            result = TopicalEditorService.create_or_update_config(
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
        """Updates an existing topical configuration."""
        cfg = TopicalEditorService.get_config(config_id=pk, user=request.user)
        payload = {
            "config_id": pk,
            "product_id": cfg["product_id"],
            "tenant_id": cfg["tenant_id"],
            "formulation_type": cfg["formulation_type"],
            "administration_route": cfg["administration_route"],
            "container_type": cfg["container_type"],
            "net_weight_grams": cfg["net_weight_grams"],
            "concentration_value": cfg["concentration_value"],
            "concentration_unit": cfg["concentration_unit"],
            "is_sterile": cfg["is_sterile"],
            "applicator_included": cfg["applicator_included"],
            "opened_shelf_life_days": cfg["opened_shelf_life_days"],
            "storage_condition": cfg["storage_condition"],
            "patient_instructions_en": cfg["patient_instructions_en"],
            "patient_instructions_bn": cfg["patient_instructions_bn"],
            "is_active": cfg["is_active"],
        }
        payload.update(request.data)
        try:
            result = TopicalEditorService.create_or_update_config(payload=payload, user=request.user)
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
        """Deletes a topical configuration."""
        try:
            TopicalEditorService.delete_config(config_id=pk, user=request.user)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

