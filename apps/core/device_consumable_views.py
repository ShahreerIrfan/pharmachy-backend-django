"""
backend/apps/core/device_consumable_views.py
Architecture Decision: ADR-0151 (P151).
REST API ViewSet for Medical Device, Needle, and Consumable Editor.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.device_consumable_service import DeviceConsumableService

logger = logging.getLogger("pharmacy.core.device_consumable.views")


class DeviceConsumableEditorViewSet(viewsets.ViewSet):
    """
    ViewSet for Medical Device, Needle, Diagnostic Test Strip & Consumable Catalog.
    Provides schema definitions, physical dimension checks, sterility governance,
    rejection of medicine-only fields, and multi-tenant access control.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the dynamic schema and condition rules for device & consumable editor."""
        try:
            data = DeviceConsumableService.get_editor_schema()
            return Response(data, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.error("Failed to retrieve device/consumable schema: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_payload(self, request):
        """Performs non-destructive domain validation on device/consumable payload."""
        try:
            validated = DeviceConsumableService.validate_payload(request.data)
            return Response({"valid": True, "validated_payload": validated}, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists accessible device/consumable configurations."""
        product_id = request.query_params.get("product_id")
        tenant_id = request.query_params.get("tenant_id")
        category = request.query_params.get("device_category")
        try:
            configs = DeviceConsumableService.list_configs(
                user=request.user,
                product_id=product_id,
                tenant_id=tenant_id,
                device_category=category,
            )
            return Response(configs, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific device/consumable configuration."""
        try:
            cfg = DeviceConsumableService.get_config(config_id=pk, user=request.user)
            return Response(cfg, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def create(self, request):
        """Creates or upserts a device/consumable configuration."""
        try:
            result = DeviceConsumableService.create_or_update_config(
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
        """Updates an existing device/consumable configuration."""
        try:
            cfg = DeviceConsumableService.get_config(config_id=pk, user=request.user)
            merged = {
                "product_id": cfg["product_id"],
                "device_category": cfg["device_category"],
                "brand_name": cfg["brand_name"],
                "model_number": cfg["model_number"],
                "needle_gauge": cfg["needle_gauge"],
                "needle_length_mm": cfg["needle_length_mm"],
                "meter_compatibility": cfg["meter_compatibility"],
                "strip_count_per_pack": cfg["strip_count_per_pack"],
                "sample_volume_microliter": cfg["sample_volume_microliter"],
                "test_time_seconds": cfg["test_time_seconds"],
                "is_sterile": cfg["is_sterile"],
                "is_single_use": cfg["is_single_use"],
                "regulatory_device_class": cfg["regulatory_device_class"],
                "storage_temperature_celsius_min": cfg["storage_temperature_celsius_min"],
                "storage_temperature_celsius_max": cfg["storage_temperature_celsius_max"],
                "is_active": cfg["is_active"],
            }
            merged.update(request.data)
            result = DeviceConsumableService.create_or_update_config(
                payload=merged, user=request.user, target_config_id=pk
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
        """Archives or soft-deletes a device/consumable configuration."""
        hard_delete = request.query_params.get("hard_delete", "false").lower() == "true"
        try:
            res = DeviceConsumableService.delete_or_archive_config(
                config_id=pk, user=request.user, hard_delete=hard_delete
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
