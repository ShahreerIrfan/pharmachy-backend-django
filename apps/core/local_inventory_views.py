"""
backend/apps/core/local_inventory_views.py
Architecture Decision: ADR-0156 (P156).
REST API ViewSet for Local Inventory-Specific Fields.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.local_inventory_service import LocalInventoryService

logger = logging.getLogger("pharmacy.core.local_inventory.views")


class LocalInventoryViewSet(viewsets.ViewSet):
    """
    ViewSet for Local Inventory-Specific Fields.
    Exposes branch SKU, custom local selling prices, reorder levels,
    shelf locations, and store visibility.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the canonical JSON schema and constraints for local inventory configuration."""
        schema_data = LocalInventoryService.get_inventory_schema()
        return Response(schema_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-inventory")
    def validate_inventory(self, request):
        """Dry-run validation of local SKU, price, and reorder levels."""
        try:
            result = LocalInventoryService.validate_inventory_payload(request.data)
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists local inventory configurations scoped to the requesting tenant."""
        product_id = request.query_params.get("product_id")
        branch_id = request.query_params.get("branch_id")
        visible_param = request.query_params.get("is_visible")
        is_visible = None
        if visible_param is not None:
            is_visible = visible_param.lower() in ["true", "1"]
        search_sku = request.query_params.get("search_sku")

        try:
            configs = LocalInventoryService.list_inventory_configs(
                user=request.user,
                product_id=product_id,
                branch_id=branch_id,
                is_visible=is_visible,
                search_sku=search_sku,
            )
            return Response(configs, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific local inventory configuration."""
        try:
            config = LocalInventoryService.get_inventory_config(config_id=pk, user=request.user)
            return Response(config, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request):
        """Creates or upserts a local inventory configuration."""
        try:
            result = LocalInventoryService.create_or_update_inventory_config(
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
        """Partially updates an existing local inventory configuration."""
        try:
            result = LocalInventoryService.create_or_update_inventory_config(
                payload=request.data, user=request.user, target_config_id=pk
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            status_code = status.HTTP_404_NOT_FOUND if "not found" in str(detail).lower() else status.HTTP_422_UNPROCESSABLE_ENTITY
            return Response(detail, status=status_code)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def destroy(self, request, pk=None):
        """Soft-deletes or archives a local inventory configuration."""
        hard_delete = request.query_params.get("hard_delete", "false").lower() == "true"
        try:
            res = LocalInventoryService.archive_inventory_config(
                config_id=pk, user=request.user, hard_delete=hard_delete
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
