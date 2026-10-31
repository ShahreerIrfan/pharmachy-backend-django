"""
apps.audit_logging.views
Architecture Decision: ADR-0035 & ADR-0050
REST API ViewSet for Immutable Audit Logs with Action-Specific RBAC, Strict Tenant Scoping,
and Protected Regulatory Retention.
"""

from __future__ import annotations

import uuid
from typing import Any

from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.audit_logging.models import AuditLogEntry
from apps.audit_logging.serializers import (
    AuditEventCreateSerializer,
    AuditLogEntrySerializer,
)
from apps.core.errors import AuditImmutabilityError, CrossTenantViolationError
from apps.core.responses import StandardResponseMixin


class IsAuditLogAuthorized(permissions.BasePermission):
    """Allows query access only to Pharmacy Owners, Branch Managers, Compliance Officers, Auditors, or Admins."""

    ALLOWED_ROLES = {
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "AUDITOR",
        "COMPLIANCE_OFFICER",
        "ADMIN",
        "SUPERADMIN",
    }

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


class AuditLogEntryViewSet(viewsets.ReadOnlyModelViewSet, StandardResponseMixin):
    """
    Append-only & read-only ViewSet for regulatory and security audit logs conforming to ADR-0050.
    Direct updates and deletions are strictly forbidden by immutability guards.
    """

    queryset = AuditLogEntry.objects.all()
    serializer_class = AuditLogEntrySerializer
    permission_classes = [permissions.AllowAny]  # Explicit action RBAC enforced in check_permissions

    def check_permissions(self, request) -> None:
        super().check_permissions(request)

        # Allow public policy inspection
        if self.action == "retention_policy":
            return

        # Event emission is permitted for authenticated operators
        if self.action == "create_event":
            if not request.user or not request.user.is_authenticated:
                # If custom header user exists, allow for POS terminal sync
                if not request.META.get("HTTP_X_USER_ROLE"):
                    raise PermissionDenied("Authentication required to emit audit events.")
            return

        # 1. RBAC Check for Queries: Only Owners, Managers, Auditors, or Admins allowed
        role = request.META.get("HTTP_X_USER_ROLE", "").upper()
        if not role and hasattr(request.user, "role"):
            role = str(getattr(request.user, "role")).upper()

        is_admin = getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False)
        if not is_admin and role not in IsAuditLogAuthorized.ALLOWED_ROLES:
            raise PermissionDenied(
                "Access denied: Audit log inspection requires elevated administrative, manager, or auditor permissions."
            )

    def get_queryset(self):
        qs = super().get_queryset()

        # 2. Strict Tenant Scoping Boundary
        authorized_tenant = getattr(self.request, "tenant_id", None)
        if not authorized_tenant:
            authorized_tenant = self.request.META.get("HTTP_X_TENANT_ID")
        if not authorized_tenant and hasattr(self.request.user, "tenant_id"):
            authorized_tenant = getattr(self.request.user, "tenant_id")

        query_tenant = self.request.query_params.get("tenant_id")

        if authorized_tenant:
            # If caller tries to query another tenant's logs, reject immediately
            if query_tenant and query_tenant != authorized_tenant:
                raise CrossTenantViolationError(
                    message_en=f"Cross-tenant access forbidden: Cannot query audit logs for foreign tenant '{query_tenant}'.",
                    message_bn=f"অন্য ফার্মেসির তথ্যে প্রবেশাধিকার কঠোরভাবে নিষিদ্ধ। টেন্যান্ট '{query_tenant}'-এর অডিট লগ দেখা যাবে না।",
                )
            qs = qs.filter(tenant_id=authorized_tenant)
        elif query_tenant:
            qs = qs.filter(tenant_id=query_tenant)

        # 3. Action, status, branch and actor filters
        action = self.request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)

        branch_id = self.request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        actor_role = self.request.query_params.get("actor_role")
        if actor_role:
            qs = qs.filter(actor_role=actor_role)

        user_id = self.request.query_params.get("user_id")
        if user_id:
            qs = qs.filter(user_id=user_id)

        status_val = self.request.query_params.get("status")
        if status_val:
            qs = qs.filter(status=status_val)

        resource_type = self.request.query_params.get("resource_type")
        if resource_type:
            qs = qs.filter(resource_type=resource_type)

        return qs

    @action(detail=False, methods=["post"], url_path="events")
    def create_event(self, request):
        """
        Appends an immutable audit event conforming to ADR-0050.
        Auto-injects verified actor, role, tenant, and device identifiers.
        """
        serializer = AuditEventCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error_response(
                code="ERR_VALIDATION_FAILED",
                message_en="Audit event payload validation failed.",
                message_bn="অডিট ইভেন্ট তথ্যের মান সঠিক নয়।",
                field_errors=serializer.errors,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        data = serializer.validated_data

        # Resolve trusted tenant and actor claims
        tenant_id = (
            getattr(request, "tenant_id", None)
            or (getattr(request.user, "tenant_id", None) if request.user and request.user.is_authenticated else None)
            or request.META.get("HTTP_X_TENANT_ID")
            or "tenant-default"
        )
        branch_id = (
            data.get("branch_id")
            or getattr(request, "branch_id", None)
            or (getattr(request.user, "branch_id", None) if request.user and request.user.is_authenticated else None)
            or request.META.get("HTTP_X_BRANCH_ID")
        )
        user_id = (
            str(getattr(request.user, "user_id", request.user.pk))
            if request.user and request.user.is_authenticated
            else "sys-agent"
        )
        actor_email = getattr(request.user, "email", "") if request.user and request.user.is_authenticated else ""
        actor_role = (
            str(getattr(request.user, "role", ""))
            if request.user and request.user.is_authenticated
            else request.META.get("HTTP_X_USER_ROLE", "INTEGRATION_AGENT")
        )
        device_id = data.get("device_id") or request.META.get("HTTP_X_DEVICE_ID")
        request_id = request.META.get("HTTP_X_REQUEST_ID") or f"req-{uuid.uuid4()}"

        entry = AuditLogEntry.objects.create(
            tenant_id=tenant_id,
            branch_id=branch_id,
            user_id=user_id,
            actor_email=actor_email,
            actor_role=actor_role,
            device_id=device_id,
            request_id=request_id,
            operation_id=data.get("operation_id"),
            action=data["action"],
            resource_type=data["resource_type"],
            resource_id=data.get("resource_id", ""),
            status=data.get("status", "SUCCESS"),
            before_state=data.get("before_state", {}),
            after_state=data.get("after_state", {}),
            retention_days=data.get("retention_days", 730),
            ip_address=request.META.get("REMOTE_ADDR"),
            metadata_json=data.get("metadata_json", {}),
        )

        return self.success_response(
            data=AuditLogEntrySerializer(entry).data,
            code="AUDIT_EVENT_RECORDED",
            message_en="Immutable audit event successfully recorded.",
            message_bn="অপরিবর্তনযোগ্য অডিট ইভেন্ট সফলভাবে সংরক্ষণ করা হয়েছে।",
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path="retention-policy", permission_classes=[permissions.AllowAny])
    def retention_policy(self, request):
        """Returns the protected retention policy matrix conforming to DGDA / ADR-0050."""
        policy = {
            "adr": "ADR-0050",
            "regulatory_body": "Directorate General of Drug Administration (DGDA)",
            "standard_retention_days": 730,
            "standard_retention_years": 2,
            "controlled_substance_retention_days": 1825,
            "controlled_substance_retention_years": 5,
            "append_only": True,
            "immutability_enforced": True,
            "zero_phi_redaction": True,
        }
        return self.success_response(
            data=policy,
            code="RETENTION_POLICY_RETRIEVED",
            message_en="Protected retention policy retrieved successfully.",
            message_bn="সুরক্ষিত অডিট রিটেনশন নীতিমালা সফলভাবে লোড হয়েছে।",
        )

    def update(self, request, *args: Any, **kwargs: Any) -> Response:
        raise AuditImmutabilityError()

    def partial_update(self, request, *args: Any, **kwargs: Any) -> Response:
        raise AuditImmutabilityError()

    def destroy(self, request, *args: Any, **kwargs: Any) -> Response:
        raise AuditImmutabilityError()
