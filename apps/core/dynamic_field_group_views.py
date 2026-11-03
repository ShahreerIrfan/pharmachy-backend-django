"""
backend/apps/core/dynamic_field_group_views.py
Architecture Decision: ADR-0152 (P152).
REST API ViewSet for Dynamic Field Group Layout and Keyboard Accessibility.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.dynamic_field_group_service import DynamicFieldGroupService

logger = logging.getLogger("pharmacy.core.dynamic_field_group.views")


class DynamicFieldGroupLayoutViewSet(viewsets.ViewSet):
    """
    ViewSet for Dynamic Field Group Layouts.
    Exposes conditional group rendering, tab order traversal,
    keyboard accelerators, and inline error feedback.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="canonical")
    def canonical(self, request):
        """Returns default canonical field groups for a given dosage form family."""
        dosage_form = request.query_params.get("dosage_form_family", "ALL")
        groups = DynamicFieldGroupService.get_canonical_groups(dosage_form_family=dosage_form)
        return Response({"dosage_form_family": dosage_form, "groups": groups}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-inline")
    def validate_inline(self, request):
        """Evaluates form values and returns inline errors mapped per field and group."""
        dosage_form = request.data.get("dosage_form_family", "ALL")
        form_values = request.data.get("form_values", {})
        result = DynamicFieldGroupService.validate_form_data_with_inline_errors(
            dosage_form_family=dosage_form,
            form_values=form_values,
        )
        return Response(result, status=status.HTTP_200_OK)

    def list(self, request):
        """Resolves dynamic field groups with tenant overrides and conditional visibility."""
        dosage_form = request.query_params.get("dosage_form_family", "ALL")
        try:
            layout = DynamicFieldGroupService.resolve_layout(
                user=request.user,
                dosage_form_family=dosage_form,
                form_values=request.query_params.dict(),
            )
            return Response(layout, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request):
        """Creates or upserts a custom dynamic field group layout."""
        try:
            result = DynamicFieldGroupService.create_or_update_layout(
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
        """Updates an existing dynamic field group layout."""
        try:
            result = DynamicFieldGroupService.create_or_update_layout(
                payload=request.data, user=request.user, target_layout_id=pk
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
        """Archives or soft-deletes a custom dynamic field group layout."""
        hard_delete = request.query_params.get("hard_delete", "false").lower() == "true"
        try:
            res = DynamicFieldGroupService.delete_or_archive_layout(
                layout_id=pk, user=request.user, hard_delete=hard_delete
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
