"""
apps.developer_toolchain.views
Architecture Decision: ADR-0036
REST API ViewSet for Developer Quickstart Diagnostics, System Health, and Controlled Bootstrap.
"""

from __future__ import annotations

import time

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.developer_toolchain.engine import (
    DeveloperQuickstartEngine,
    QuickstartValidationError,
)
from apps.developer_toolchain.models import DeveloperBootstrapAudit
from apps.developer_toolchain.serializers import (
    DeveloperBootstrapAuditSerializer,
    QuickstartTriggerActionSerializer,
)


class DeveloperQuickstartViewSet(viewsets.ViewSet):
    """Developer toolchain ViewSet for system preflight diagnostics and bootstrap orchestration."""

    permission_classes = [permissions.AllowAny]

    ALLOWED_ROLES = {"ADMIN", "DEVELOPER", "PHARMACY_OWNER"}

    def check_permissions(self, request) -> None:
        super().check_permissions(request)

        # 1. RBAC Check: Restrict to ADMIN, DEVELOPER, or PHARMACY_OWNER
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()

        is_admin = getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False)
        if not is_admin and role not in self.ALLOWED_ROLES:
            raise PermissionDenied(
                "Access denied: Developer quickstart endpoints require ADMIN or PHARMACY_OWNER privileges."
            )

    def list(self, request):
        """GET /api/v2/developer/quickstart/ - Returns full preflight environment and pillar status."""
        engine = DeveloperQuickstartEngine()
        tenant_id = request.META.get("HTTP_X_TENANT_ID") or request.query_params.get("tenant_id")
        report = engine.execute_all(dry_run=True, tenant_id=tenant_id)
        return Response(report, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="trigger")
    def trigger(self, request):
        """POST /api/v2/developer/quickstart/trigger/ - Executes controlled quickstart action."""
        serializer = QuickstartTriggerActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        dry_run = data.get("dry_run", False)
        target_tenant = data.get("tenant_id")
        requested_action = data.get("action", "VERIFY_ALL")

        # 2. Cross-Tenant Boundary Check
        authorized_tenant = request.META.get("HTTP_X_TENANT_ID")
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()

        if authorized_tenant and target_tenant and target_tenant != authorized_tenant:
            if role != "ADMIN" and not getattr(request.user, "is_superuser", False):
                raise PermissionDenied(
                    f"Cross-tenant access forbidden: Cannot trigger quickstart for '{target_tenant}'."
                )

        engine = DeveloperQuickstartEngine()
        start_time = time.perf_counter()

        try:
            if requested_action == "MIGRATE":
                report = engine.run_migrations(dry_run=dry_run)
            elif requested_action == "SEED":
                report = engine.seed_fixtures(dry_run=dry_run, tenant_id=target_tenant)
            else:
                report = engine.execute_all(dry_run=dry_run, tenant_id=target_tenant)
        except QuickstartValidationError as exc:
            raise ValidationError(str(exc))

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # Audit log creation
        DeveloperBootstrapAudit.objects.create(
            operator_role=role or "ADMIN",
            tenant_id=target_tenant or authorized_tenant,
            action=requested_action,
            duration_ms=duration_ms,
            status=report.get("status", "SUCCESS"),
            report_json=report,
        )

        return Response(report, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="audits")
    def audits(self, request):
        """GET /api/v2/developer/quickstart/audits/ - Lists past bootstrap and quickstart executions."""
        qs = DeveloperBootstrapAudit.objects.all()[:20]
        serializer = DeveloperBootstrapAuditSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
