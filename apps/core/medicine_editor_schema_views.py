"""
REST API ViewSet for Server-Driven Medicine Editor Schema & Validation.
Architecture Decision: ADR-0124 (P124).
Phase 07: Milestone M04 - Shared Medicine Catalog & Dynamic Packaging Units.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.dynamic_condition_evaluator import DynamicConditionEvaluator
from apps.core.medicine_editor_schema_service import MedicineEditorSchemaService
from apps.core.models import TenantLifecycleStatus

logger = logging.getLogger("pharmacy.core.medicine_editor_schema.views")


class MedicineEditorSchemaViewSet(viewsets.ViewSet):
    """
    REST API ViewSet for retrieving server-driven medicine editor schemas,
    conditional section configurations, and validating form payloads.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _check_tenant_status(self, request):
        """Guard against suspended tenants accessing schema services."""
        tenant = getattr(request, "tenant", None)
        if tenant and getattr(tenant, "status", None) == TenantLifecycleStatus.SUSPENDED:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_SUSPENDED",
                    "message_en": "Tenant account is suspended. Access denied.",
                    "message_bn": "ফার্মেসি অ্যাকাউন্টটি স্থগিত আছে। প্রবেশাধিকার সংরক্ষিত।",
                }
            )

    def list(self, request) -> Response:
        """GET /api/v2/medicines/editor-schema/"""
        self._check_tenant_status(request)
        schemas = MedicineEditorSchemaService.list_schemas()
        return Response({"count": len(schemas), "results": schemas}, status=status.HTTP_200_OK)

    def retrieve(self, request, pk=None) -> Response:
        """GET /api/v2/medicines/editor-schema/{pk}/"""
        self._check_tenant_status(request)
        try:
            schema = MedicineEditorSchemaService.get_active_schema(version=pk)
            return Response(
                {
                    "schema_id": schema.schema_id,
                    "schema_version": schema.schema_version,
                    "title_en": schema.title_en,
                    "title_bn": schema.title_bn,
                    "description": schema.description,
                    "is_active": schema.is_active,
                    "sections": schema.sections,
                    "conditional_rules": schema.conditional_rules,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["get"], url_path="active")
    def active(self, request) -> Response:
        """GET /api/v2/medicines/editor-schema/active/"""
        self._check_tenant_status(request)
        schema = MedicineEditorSchemaService.get_active_schema()
        return Response(
            {
                "schema_id": schema.schema_id,
                "schema_version": schema.schema_version,
                "title_en": schema.title_en,
                "title_bn": schema.title_bn,
                "description": schema.description,
                "is_active": schema.is_active,
                "sections": schema.sections,
                "conditional_rules": schema.conditional_rules,
            },
            status=status.HTTP_200_OK,
        )

    def create(self, request) -> Response:
        """POST /api/v2/medicines/editor-schema/ (Platform Superadmin only)"""
        self._check_tenant_status(request)
        try:
            created = MedicineEditorSchemaService.create_schema(
                actor_user=request.user,
                data=request.data,
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="validate")
    def validate_form(self, request) -> Response:
        """POST /api/v2/medicines/editor-schema/validate/"""
        self._check_tenant_status(request)
        payload = request.data.get("payload", request.data)
        version = request.data.get("schema_version")

        try:
            result = MedicineEditorSchemaService.validate_medicine_form(
                payload=payload,
                schema_version=version,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="evaluate-conditions")
    def evaluate_conditions(self, request) -> Response:
        """POST /api/v2/medicines/editor-schema/evaluate-conditions/ (P125)"""
        self._check_tenant_status(request)
        payload = request.data.get("payload", request.data)
        version = request.data.get("schema_version")
        strict_hidden_check = request.data.get("strict_hidden_check", True)

        try:
            result = DynamicConditionEvaluator.evaluate_and_validate(
                payload=payload,
                schema_version=version,
                strict_hidden_check=strict_hidden_check,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

