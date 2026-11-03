"""
backend/apps/core/suspension_reconstitution_views.py
Architecture Decision: ADR-0143 (P143).
REST API ViewSet for Dry Powder Suspension Reconstitution Editor.
Exposes dynamic schema introspection, payload validation, and CRUD operations with pharmacist authorization.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.suspension_reconstitution_service import SuspensionReconstitutionEditorService

logger = logging.getLogger("pharmacy.core.suspension_reconstitution.views")


class SuspensionReconstitutionEditorViewSet(viewsets.ViewSet):
    """
    ViewSet for Dry Powder Suspension Reconstitution Editor.
    Provides schema definitions, volume consistency checks, BUD calculation,
    pharmacist-only approval verification, and multi-tenant access control.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the dynamic schema and condition rules for suspension reconstitution editor."""
        try:
            data = SuspensionReconstitutionEditorService.get_editor_schema()
            return Response(data, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Failed to retrieve suspension reconstitution schema: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_payload(self, request):
        """Performs non-destructive domain validation on a suspension reconstitution payload."""
        try:
            validated = SuspensionReconstitutionEditorService.validate_payload(request.data)
            return Response({"valid": True, "validated_payload": validated}, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists accessible suspension reconstitution configurations."""
        product_id = request.query_params.get("product_id")
        tenant_id = request.query_params.get("tenant_id")
        try:
            configs = SuspensionReconstitutionEditorService.list_configs(
                user=request.user, product_id=product_id, tenant_id=tenant_id
            )
            return Response(configs, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific suspension reconstitution configuration."""
        try:
            cfg = SuspensionReconstitutionEditorService.get_config(config_id=pk, user=request.user)
            return Response(cfg, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def create(self, request):
        """Creates or upserts a suspension reconstitution configuration."""
        try:
            result = SuspensionReconstitutionEditorService.create_or_update_config(
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
        """Updates an existing suspension reconstitution configuration."""
        data = dict(request.data)
        data["config_id"] = pk
        try:
            result = SuspensionReconstitutionEditorService.create_or_update_config(
                payload=data, user=request.user
            )
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
        """Deletes a configuration."""
        try:
            res = SuspensionReconstitutionEditorService.delete_config(config_id=pk, user=request.user)
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

