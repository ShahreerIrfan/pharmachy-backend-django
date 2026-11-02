"""
Custom Role Builder & Permission Catalog REST API ViewSets (P103 / ADR-0103).
Provides endpoints for catalog exploration, custom role CRUD, cloning, archiving,
and dry-run permission preview with RBAC and safety guardrails.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.custom_role_service import CustomRoleService
from apps.core.postgres_service import CrossTenantOperationError

logger = logging.getLogger("pharmacy.custom_roles.views")


class PermissionCatalogViewSet(viewsets.ViewSet):
    """Exposes canonical catalog of permission keys grouped by domain."""

    permission_classes = [permissions.IsAuthenticated]

    def list(self, request) -> Response:
        catalog = CustomRoleService.get_permission_catalog()
        return Response(catalog, status=status.HTTP_200_OK)


class CustomRoleViewSet(viewsets.ViewSet):
    """
    Manages tenant custom role builder.
    - Owner/Superadmin: full create, edit, clone, archive authority.
    - Branch Manager, Cashier, Pharmacist, Clerk: view only or denied.
    """

    permission_classes = [permissions.IsAuthenticated]

    def _resolve_tenant_and_role(self, request) -> Tuple[str, str, Optional[str]]:
        user = request.user
        role = str(getattr(user, "role", "")).upper()

        if not user or not user.is_authenticated:
            raise PermissionDenied(
                {
                    "error_code": "ERR_AUTHENTICATION_REQUIRED",
                    "message_en": "Authentication credentials were not provided.",
                    "message_bn": "প্রবেশাধিকারের জন্য অথেনটিকেশন প্রয়োজন।",
                }
            )

        tenant_id = (
            getattr(user, "tenant_id", None)
            or getattr(user, "tenant", None)
            or getattr(request, "tenant_id", None)
        )
        if hasattr(tenant_id, "tenant_id"):
            tenant_id = tenant_id.tenant_id
        tenant_id = str(tenant_id) if tenant_id else None

        req_tenant = request.query_params.get("tenant_id") or request.data.get("tenant_id")
        if req_tenant and tenant_id and req_tenant != tenant_id and role not in ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR"]:
            raise PermissionDenied(
                {
                    "error_code": "ERR_CROSS_TENANT_VIOLATION",
                    "message_en": f"Access denied: cannot access foreign tenant '{req_tenant}'.",
                    "message_bn": f"অননুমোদিত টেন্যান্ট অ্যাক্সেস: '{req_tenant}' দেখার অনুমতি নেই।",
                }
            )

        if not tenant_id and req_tenant:
            tenant_id = req_tenant

        if not tenant_id:
            raise PermissionDenied(
                {
                    "error_code": "ERR_TENANT_CONTEXT_MISSING",
                    "message_en": "Tenant context missing.",
                    "message_bn": "টেন্যান্ট কনটেক্সট অনুপস্থিত।",
                }
            )

        user_branch_id = getattr(user, "branch_id", None)
        return str(tenant_id), role, str(user_branch_id) if user_branch_id else None

    def _assert_owner_or_admin(self, role: str) -> None:
        allowed = ["SUPERADMIN", "SUPER_ADMIN", "PLATFORM_OPERATOR", "PHARMACY_OWNER", "TENANT_OWNER"]
        if role not in allowed:
            raise PermissionDenied(
                {
                    "error_code": "ERR_PERMISSION_DENIED",
                    "message_en": f"Role '{role}' is not authorized to create or modify custom roles. Only pharmacy owners can manage roles.",
                    "message_bn": "শুধুমাত্র ফার্মেসি মালিকগণ কাস্টম রোল তৈরি বা পরিবর্তন করতে পারেন।",
                }
            )

    def list(self, request) -> Response:
        """Lists active and optional archived custom roles for tenant."""
        tenant_id, _, _ = self._resolve_tenant_and_role(request)
        include_archived = request.query_params.get("include_archived", "").lower() in ["true", "1"]
        roles = CustomRoleService.list_roles(tenant_id, include_archived=include_archived)
        return Response({"count": len(roles), "results": roles}, status=status.HTTP_200_OK)

    def create(self, request) -> Response:
        """Creates a new custom role definition."""
        tenant_id, caller_role, _ = self._resolve_tenant_and_role(request)
        self._assert_owner_or_admin(caller_role)

        data = request.data
        try:
            created = CustomRoleService.create_custom_role(
                tenant_id=tenant_id,
                role_code=data.get("role_code"),
                display_name=data.get("display_name"),
                base_role=data.get("base_role", "CASHIER"),
                permission_keys=data.get("permission_keys", []),
                requires_pcb_registration=bool(data.get("requires_pcb_registration", False)),
                description=data.get("description", ""),
                created_by_username=getattr(request.user, "username", ""),
            )
            return Response(created, status=status.HTTP_201_CREATED)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    def partial_update(self, request, pk=None) -> Response:
        """Updates display name, description, or permission keys of a custom role."""
        tenant_id, caller_role, _ = self._resolve_tenant_and_role(request)
        self._assert_owner_or_admin(caller_role)

        data = request.data
        try:
            updated = CustomRoleService.update_custom_role(
                tenant_id=tenant_id,
                role_id=pk,
                display_name=data.get("display_name"),
                description=data.get("description"),
                permission_keys=data.get("permission_keys"),
                requires_pcb_registration=data.get("requires_pcb_registration"),
                base_role=data.get("base_role"),
            )
            return Response(updated, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="clone")
    def clone(self, request, pk=None) -> Response:
        """Clones an existing role into a new custom role."""
        tenant_id, caller_role, _ = self._resolve_tenant_and_role(request)
        self._assert_owner_or_admin(caller_role)

        data = request.data
        try:
            cloned = CustomRoleService.clone_role(
                tenant_id=tenant_id,
                source_role_id=pk,
                new_role_code=data.get("new_role_code"),
                new_display_name=data.get("new_display_name"),
                description=data.get("description", ""),
                cloned_by_username=getattr(request.user, "username", ""),
            )
            return Response(cloned, status=status.HTTP_201_CREATED)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post", "delete"], url_path="archive")
    def archive(self, request, pk=None) -> Response:
        """Soft-archives a custom role."""
        tenant_id, caller_role, _ = self._resolve_tenant_and_role(request)
        self._assert_owner_or_admin(caller_role)

        try:
            archived = CustomRoleService.archive_role(
                tenant_id=tenant_id,
                role_id=pk,
                archived_by_username=getattr(request.user, "username", ""),
            )
            return Response(archived, status=status.HTTP_200_OK)
        except CrossTenantOperationError as exc:
            raise PermissionDenied({"error_code": "ERR_CROSS_TENANT_VIOLATION", "message_en": str(exc)})
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="preview")
    def preview(self, request) -> Response:
        """Evaluates prospective permission list for safety and SoD conflicts."""
        data = request.data
        try:
            result = CustomRoleService.preview_role_permissions(
                permission_keys=data.get("permission_keys", []),
                requires_pcb_registration=bool(data.get("requires_pcb_registration", False)),
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValidationError as exc:
            return Response(exc.detail if hasattr(exc, "detail") else str(exc), status=status.HTTP_400_BAD_REQUEST)
