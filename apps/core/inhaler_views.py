"""
backend/apps/core/inhaler_views.py
Architecture Decision: ADR-0146 (P146).
REST API ViewSet for Inhaler Dosage-Form Editor (MDI, DPI, SMI).
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.inhaler_editor_service import InhalerEditorService

logger = logging.getLogger("pharmacy.core.inhaler.views")


class InhalerEditorViewSet(viewsets.ViewSet):
    """
    ViewSet for Inhaler Formulation and Actuation Dosing Editor.
    Provides schema definitions, actuation parameters, dimension consistency checks,
    and multi-tenant access control.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the dynamic schema and condition rules for inhaler editor."""
        try:
            data = InhalerEditorService.get_editor_schema()
            return Response(data, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Failed to retrieve inhaler schema: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_payload(self, request):
        """Performs non-destructive domain validation on an inhaler payload."""
        try:
            validated = InhalerEditorService.validate_payload(request.data)
            return Response({"valid": True, "validated_payload": validated}, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists accessible inhaler configurations."""
        product_id = request.query_params.get("product_id")
        tenant_id = request.query_params.get("tenant_id")
        try:
            configs = InhalerEditorService.list_configs(
                user=request.user, product_id=product_id, tenant_id=tenant_id
            )
            return Response(configs, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific inhaler configuration."""
        try:
            cfg = InhalerEditorService.get_config(config_id=pk, user=request.user)
            return Response(cfg, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def create(self, request):
        """Creates or upserts an inhaler configuration."""
        try:
            result = InhalerEditorService.create_or_update_config(
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
        """Updates an existing inhaler configuration."""
        cfg = InhalerEditorService.get_config(config_id=pk, user=request.user)
        payload = {
            "config_id": pk,
            "product_id": cfg["product_id"],
            "tenant_id": cfg["tenant_id"],
            "device_type": cfg["device_type"],
            "administration_route": cfg["administration_route"],
            "dose_per_actuation": cfg["dose_per_actuation"],
            "dose_unit": cfg["dose_unit"],
            "total_actuations": cfg["total_actuations"],
            "total_active_content": cfg["total_active_content"],
            "has_dose_counter": cfg["has_dose_counter"],
            "has_spacer_included": cfg["has_spacer_included"],
            "storage_condition": cfg["storage_condition"],
            "priming_required": cfg["priming_required"],
            "priming_puffs": cfg["priming_puffs"],
            "canister_pressure_warning": cfg["canister_pressure_warning"],
            "patient_instructions_en": cfg["patient_instructions_en"],
            "patient_instructions_bn": cfg["patient_instructions_bn"],
            "is_active": cfg["is_active"],
        }
        payload.update(request.data)
        try:
            result = InhalerEditorService.create_or_update_config(payload=payload, user=request.user)
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
        """Deletes an inhaler configuration."""
        try:
            InhalerEditorService.delete_config(config_id=pk, user=request.user)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

