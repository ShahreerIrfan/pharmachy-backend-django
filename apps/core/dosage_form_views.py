"""
REST API ViewSet for Dosage Form Metadata Registry.
Architecture Decision: ADR-0122 (P122).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.dosage_form_service import DosageFormRegistryService

logger = logging.getLogger("pharmacy.core.dosage_form.views")


class DosageFormViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for querying and managing dosage form metadata definitions,
    field groups, display order, and unit conversion applicability.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/dosage-forms/"""
        category = request.query_params.get("category")
        is_active_param = request.query_params.get("is_active")
        is_active = None
        if is_active_param is not None:
            is_active = is_active_param.lower() in ("true", "1")

        results = DosageFormRegistryService.list_forms(
            category=category,
            is_active=is_active,
        )
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """POST /api/v2/dosage-forms/ (Platform Superadmin only)"""
        try:
            created = DosageFormRegistryService.create_form(
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
