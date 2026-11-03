"""
backend/apps/core/conditional_pricing_views.py
Architecture Decision: ADR-0153 (P153).
REST API ViewSet for Multi-Tier Unit-Aware Conditional Pricing.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.conditional_pricing_service import ConditionalPricingService

logger = logging.getLogger("pharmacy.core.conditional_pricing.views")


class ConditionalPricingViewSet(viewsets.ViewSet):
    """
    ViewSet for Unit-Aware Conditional Pricing Forms.
    Exposes packaging multipliers, piece-to-strip-to-pack price conversion,
    break-bulk controls, and Decimal precision calculations.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the canonical JSON schema and constraints for conditional pricing."""
        schema_data = ConditionalPricingService.get_pricing_schema()
        return Response(schema_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="calculate")
    def calculate(self, request):
        """
        Validates multipliers and computes strip & pack prices from piece cost/MRP.
        Returns calculated conversions without persisting.
        """
        try:
            result = ConditionalPricingService.calculate_conversions(request.data)
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists conditional pricing configurations scoped to the requesting tenant."""
        product_id = request.query_params.get("product_id")
        tenant_id = request.query_params.get("tenant_id")
        try:
            pricing_list = ConditionalPricingService.list_pricing(
                user=request.user,
                product_id=product_id,
                tenant_id=tenant_id,
            )
            return Response(pricing_list, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves pricing configuration details for a specific ID."""
        try:
            pricing = ConditionalPricingService.get_pricing(pricing_id=pk, user=request.user)
            return Response(pricing, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request):
        """Creates or upserts conditional pricing configuration."""
        try:
            result = ConditionalPricingService.create_or_update_pricing(
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
        """Partially updates an existing conditional pricing configuration."""
        try:
            result = ConditionalPricingService.create_or_update_pricing(
                payload=request.data, user=request.user, target_pricing_id=pk
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
        """Soft-deletes or archives a conditional pricing configuration."""
        hard_delete = request.query_params.get("hard_delete", "false").lower() == "true"
        try:
            res = ConditionalPricingService.delete_or_archive_pricing(
                pricing_id=pk, user=request.user, hard_delete=hard_delete
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

