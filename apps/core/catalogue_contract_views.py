"""
backend/apps/core/catalogue_contract_views.py
Architecture Decision: ADR-0140 (P140).
REST API ViewSet for Catalogue Service Contract Suite.
Certifies compliance across Phase 07 (Milestone M04) operations and quality gates.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.catalogue_contract_suite_service import CatalogueContractSuiteService

logger = logging.getLogger("pharmacy.core.catalogue_contract.views")


class CatalogueContractSuiteViewSet(viewsets.ViewSet):
    """
    ViewSet for Phase 07 Milestone M04 Catalogue Service Contract Suite.
    Runs automated contract audit gates and verifies cross-platform guarantees.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        """Returns the current contract suite specification and status."""
        try:
            tenant_id = request.query_params.get("tenant_id")
            result = CatalogueContractSuiteService.execute_contract_audit(
                user=request.user, tenant_id=tenant_id
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            msg = exc.message if hasattr(exc, "message") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="run-audit")
    def run_audit(self, request):
        """Executes a full live audit of catalogue contracts."""
        try:
            tenant_id = request.data.get("tenant_id") if isinstance(request.data, dict) else None
            result = CatalogueContractSuiteService.execute_contract_audit(
                user=request.user, tenant_id=tenant_id
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
        except (ValidationError, DjangoValidationError) as exc:
            msg = exc.message if hasattr(exc, "message") else str(exc)
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
