"""
backend/apps/core/storage_conditional_views.py
Architecture Decision: ADR-0155 (P155).
REST API ViewSet for Conditional Cold-Chain Storage Fields and Handling Controls.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.storage_conditional_service import StorageConditionalService

logger = logging.getLogger("pharmacy.core.storage_conditional.views")


class StorageConditionalViewSet(viewsets.ViewSet):
    """
    ViewSet for Product Storage Condition Configurations.
    Exposes conditional cold-chain requirements, safe ranges, refrigerator placement,
    excursion handling, and handling alerts per DGDA rules.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the canonical JSON schema and constraints for storage conditions."""
        schema_data = StorageConditionalService.get_storage_schema()
        return Response(schema_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-storage")
    def validate_storage(self, request):
        """Dry-run validation of conditional cold-chain storage parameters."""
        try:
            result = StorageConditionalService.validate_storage_payload(request.data)
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists storage configurations scoped to the requesting tenant."""
        product_id = request.query_params.get("product_id")
        cold_chain_param = request.query_params.get("is_cold_chain_required")
        cold_chain = None
        if cold_chain_param is not None:
            cold_chain = cold_chain_param.lower() in ["true", "1"]

        try:
            configs = StorageConditionalService.list_storage_configs(
                user=request.user,
                product_id=product_id,
                is_cold_chain_required=cold_chain,
            )
            return Response(configs, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific storage condition configuration."""
        try:
            config = StorageConditionalService.get_storage_config(config_id=pk, user=request.user)
            return Response(config, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request):
        """Creates or upserts product storage condition configuration."""
        try:
            result = StorageConditionalService.create_or_update_storage_config(
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
        """Partially updates an existing storage condition configuration."""
        try:
            result = StorageConditionalService.create_or_update_storage_config(
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
        """Soft-deletes or archives a storage condition configuration."""
        hard_delete = request.query_params.get("hard_delete", "false").lower() == "true"
        try:
            res = StorageConditionalService.archive_storage_config(
                config_id=pk, user=request.user, hard_delete=hard_delete
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

