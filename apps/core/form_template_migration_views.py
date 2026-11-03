"""
backend/apps/core/form_template_migration_views.py
Architecture Decision: ADR-0139 (P139).
REST API ViewSet for Medicine Form Template Migration Plans, Declarative
Field Mappings, Dry-Run Validation, Execution, and Snapshot Rollbacks.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.form_template_migration_service import FormTemplateMigrationService

logger = logging.getLogger("pharmacy.core.form_template_migration.views")


class FormTemplateMigrationViewSet(viewsets.ViewSet):
    """
    ViewSet for Form Template Migration Plans.
    Enforces multi-tenant boundaries, subscription entitlements,
    dry-run safety validation, and historical dispensing immutability.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        """Lists accessible form template migration plans."""
        tenant_id = request.query_params.get("tenant_id")
        try:
            plans = FormTemplateMigrationService.list_plans(user=request.user, tenant_id=tenant_id)
            return Response(plans, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def retrieve(self, request, pk=None):
        """Retrieves details of a specific migration plan."""
        try:
            plan = FormTemplateMigrationService.get_plan(plan_id=pk, user=request.user)
            return Response(plan, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            if "not found" in msg.lower():
                return Response({"detail": msg}, status=status.HTTP_404_NOT_FOUND)
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    def create(self, request):
        """Creates a new form template migration plan in DRAFT status."""
        try:
            plan = FormTemplateMigrationService.create_plan(payload=request.data, user=request.user)
            return Response(plan, status=status.HTTP_201_CREATED)
        except DjangoValidationError as exc:
            msg_dict = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            return Response(msg_dict, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    @action(detail=True, methods=["post"], url_path="dry-run")
    def dry_run(self, request, pk=None):
        """Performs non-destructive dry-run validation of the migration plan."""
        try:
            result = FormTemplateMigrationService.validate_dry_run(plan_id=pk, user=request.user)
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            if "not found" in msg.lower():
                return Response({"detail": msg}, status=status.HTTP_404_NOT_FOUND)
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    @action(detail=True, methods=["post"], url_path="execute")
    def execute(self, request, pk=None):
        """Executes the migration plan, migrating medicines and updating version hashes."""
        try:
            result = FormTemplateMigrationService.execute_plan(plan_id=pk, user=request.user)
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            if "not found" in msg.lower():
                return Response({"detail": msg}, status=status.HTTP_404_NOT_FOUND)
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)

    @action(detail=True, methods=["post"], url_path="rollback")
    def rollback(self, request, pk=None):
        """Reverses an executed migration plan using the stored snapshot."""
        try:
            result = FormTemplateMigrationService.rollback_plan(plan_id=pk, user=request.user)
            return Response(result, status=status.HTTP_200_OK)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            if "not found" in msg.lower():
                return Response({"detail": msg}, status=status.HTTP_404_NOT_FOUND)
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as exc:
            detail = exc.detail if hasattr(exc, "detail") else {"detail": str(exc)}
            return Response(detail, status=status.HTTP_403_FORBIDDEN)
