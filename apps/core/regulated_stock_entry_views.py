"""
backend/apps/core/regulated_stock_entry_views.py
Architecture Decision: ADR-0154 (P154).
REST API ViewSet for Regulated Stock Entry, Expiry, Manufacturing Date, and Lot Controls.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.regulated_stock_entry_service import RegulatedStockEntryService

logger = logging.getLogger("pharmacy.core.regulated_stock_entry.views")


class RegulatedStockEntryViewSet(viewsets.ViewSet):
    """
    ViewSet for Regulated Stock Intake Records.
    Exposes manufacturing date, expiration date, lot reference,
    quarantine thresholds, and storage directives per DGDA guidelines.
    """

    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="schema")
    def schema(self, request):
        """Returns the canonical JSON schema and constraints for stock intake."""
        schema_data = RegulatedStockEntryService.get_stock_entry_schema()
        return Response(schema_data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="validate-intake")
    def validate_intake(self, request):
        """
        Dry-run validation of manufacturing date, expiration date,
        lot number, and quarantine flags without persisting.
        """
        try:
            result = RegulatedStockEntryService.validate_intake_payload(request.data)
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    def list(self, request):
        """Lists stock entry records scoped to the requesting tenant."""
        product_id = request.query_params.get("product_id")
        is_quarantined_param = request.query_params.get("is_quarantined")
        is_quarantined = None
        if is_quarantined_param is not None:
            is_quarantined = is_quarantined_param.lower() in ["true", "1"]
        lot_reference = request.query_params.get("lot_reference")

        try:
            entries = RegulatedStockEntryService.list_stock_entries(
                user=request.user,
                product_id=product_id,
                is_quarantined=is_quarantined,
                lot_reference=lot_reference,
            )
            return Response(entries, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific stock entry record."""
        try:
            entry = RegulatedStockEntryService.get_stock_entry(entry_id=pk, user=request.user)
            return Response(entry, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)

    def create(self, request):
        """Creates an audited regulated stock intake record and updates batch inventory."""
        try:
            result = RegulatedStockEntryService.create_stock_entry(
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
        """Updates storage condition, quarantine flag, or internal notes."""
        try:
            result = RegulatedStockEntryService.update_stock_entry(
                entry_id=pk, payload=request.data, user=request.user
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
        """Soft-deletes or archives a stock entry record."""
        hard_delete = request.query_params.get("hard_delete", "false").lower() == "true"
        try:
            res = RegulatedStockEntryService.archive_stock_entry(
                entry_id=pk, user=request.user, hard_delete=hard_delete
            )
            return Response(res, status=status.HTTP_200_OK)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_404_NOT_FOUND)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
