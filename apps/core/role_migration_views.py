"""
REST API ViewSet for Role Migration & Grants Versioning.
Architecture Decision: ADR-0119 (P119).
Phase 06: Milestone M03 - Offline-First Desktop POS & Owner Visibility.
"""

from __future__ import annotations

import logging

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.models import RoleGrantMigrationReason
from apps.core.role_migration_service import RoleMigrationGrantsService

logger = logging.getLogger("pharmacy.core.role_migration.views")


class RoleMigrationGrantsViewSet(viewsets.ViewSet):
    """
    REST API ViewSet managing role migration, grant versioning, dry-run simulation,
    and invalid entitlement pruning.
    """

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        """GET /api/v2/roles/migrations/"""
        tenant_id = request.query_params.get("tenant_id")
        role_id = request.query_params.get("role_id")

        try:
            records = RoleMigrationGrantsService.list_migration_history(
                actor_user=request.user,
                tenant_id=tenant_id,
                role_id=role_id,
            )
            return Response({"count": len(records), "results": records}, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="migrate")
    def migrate_role(self, request) -> Response:
        """POST /api/v2/roles/migrations/migrate/"""
        role_id = request.data.get("role_id")
        reason = request.data.get("reason", RoleGrantMigrationReason.PLAN_DOWNGRADE)

        if not role_id:
            return Response(
                {
                    "error_code": "ERR_INVALID_MIGRATION_INPUT",
                    "message_en": "role_id is required.",
                    "message_bn": "রোল আইডি আবশ্যক।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = RoleMigrationGrantsService.reconcile_role_grants(
                actor_user=request.user,
                role_id=role_id,
                reason=reason,
                is_dry_run=False,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="dry-run")
    def dry_run(self, request) -> Response:
        """POST /api/v2/roles/migrations/dry-run/"""
        role_id = request.data.get("role_id")
        reason = request.data.get("reason", RoleGrantMigrationReason.PLAN_DOWNGRADE)

        if not role_id:
            return Response(
                {
                    "error_code": "ERR_INVALID_MIGRATION_INPUT",
                    "message_en": "role_id is required.",
                    "message_bn": "রোল আইডি আবশ্যক।",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = RoleMigrationGrantsService.reconcile_role_grants(
                actor_user=request.user,
                role_id=role_id,
                reason=reason,
                is_dry_run=True,
            )
            return Response(result, status=status.HTTP_200_OK)
        except PermissionDenied as exc:
            return Response(exc.detail, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
