"""
apps.audit_logging.views
Architecture Decision: ADR-0035
REST API ViewSet for Immutable Audit Logs with Action-Specific RBAC and Strict Tenant Scoping.
"""

from __future__ import annotations

from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from apps.audit_logging.models import AuditLogEntry
from apps.audit_logging.serializers import (
    AuditLogEntrySerializer,
)


class IsAuditLogAuthorized(permissions.BasePermission):
    """Allows access only to Pharmacy Owners, Compliance Officers, or Admins."""

    ALLOWED_ROLES = {"PHARMACY_OWNER", "COMPLIANCE_OFFICER", "ADMIN"}

    def has_permission(self, request, view) -> bool:
        # Check custom header role (e.g. from POS/Mobile session) or user role attribute
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()

        # If user is superuser/staff
        if getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False):
            return True

        if role in self.ALLOWED_ROLES:
            return True

        return False


class AuditLogEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only ViewSet for regulatory and security audit logs."""

    queryset = AuditLogEntry.objects.all()
    serializer_class = AuditLogEntrySerializer
    permission_classes = [permissions.AllowAny]  # We enforce explicit RBAC inside check_permissions

    def check_permissions(self, request) -> None:
        super().check_permissions(request)

        # 1. RBAC Check: Only PHARMACY_OWNER, COMPLIANCE_OFFICER or ADMIN allowed
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()

        is_admin = getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False)
        if not is_admin and role not in {"PHARMACY_OWNER", "COMPLIANCE_OFFICER", "ADMIN"}:
            raise PermissionDenied(
                "Access denied: Audit log queries require PHARMACY_OWNER or COMPLIANCE_OFFICER permissions."
            )

    def get_queryset(self):
        qs = super().get_queryset()

        # 2. Strict Tenant Scoping Boundary
        # Caller's authorized tenant
        authorized_tenant = self.request.META.get("HTTP_X_TENANT_ID")
        if not authorized_tenant and hasattr(self.request.user, "tenant_id"):
            authorized_tenant = getattr(self.request.user, "tenant_id")

        query_tenant = self.request.query_params.get("tenant_id")

        if authorized_tenant:
            # If caller tries to query another tenant's logs, reject immediately
            if query_tenant and query_tenant != authorized_tenant:
                raise PermissionDenied(f"Cross-tenant access forbidden: Cannot query audit logs for '{query_tenant}'.")
            qs = qs.filter(tenant_id=authorized_tenant)
        elif query_tenant:
            qs = qs.filter(tenant_id=query_tenant)

        # 3. Action and status filters
        action = self.request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)

        status_val = self.request.query_params.get("status")
        if status_val:
            qs = qs.filter(status=status_val)

        resource_type = self.request.query_params.get("resource_type")
        if resource_type:
            qs = qs.filter(resource_type=resource_type)

        return qs
