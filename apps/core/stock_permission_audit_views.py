"""
backend/apps/core/stock_permission_audit_views.py
Architecture Decision: ADR-0219 (P219).
Phase 11: Inventory, Warehouse, Cold Chain & Transfers.

DRF ViewSet for Stock Permissions & Audits:
- Granular RBAC & Branch-Boundary Enforcement for Counting, Adjustment,
  Quarantine Release, Transfer, and Valuation Data.
- Immutable cryptographic audit logging on every evaluation check.
"""

from __future__ import annotations

from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.models import (
    Branch,
    StockPermissionAuditLog,
    Tenant,
)
from apps.core.stock_permission_audit_service import StockPermissionAuditService
from apps.identity.models import UserRole


class StockPermissionAuditLogSerializer(serializers.ModelSerializer):
    action_scope_display = serializers.CharField(source="get_action_scope_display", read_only=True)
    decision_display = serializers.CharField(source="get_decision_display", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    actor_email = serializers.CharField(source="actor.email", read_only=True)

    class Meta:
        model = StockPermissionAuditLog
        fields = [
            "audit_id",
            "tenant_id",
            "branch_id",
            "branch_name",
            "actor_email",
            "actor_role",
            "actor_assigned_branch_id",
            "action_scope",
            "action_scope_display",
            "resource_id",
            "decision",
            "decision_display",
            "error_code",
            "reason_en",
            "reason_bn",
            "sha256_signature",
            "created_at",
        ]
        read_only_fields = fields


class StockPermissionAuditViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API ViewSet for evaluating stock permissions and viewing immutable audit logs.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = StockPermissionAuditLogSerializer
    lookup_field = "audit_id"

    AUDIT_VIEWER_ROLES = {
        UserRole.SUPERADMIN,
        UserRole.PHARMACY_OWNER,
        UserRole.BRANCH_MANAGER,
        UserRole.AUDITOR,
        UserRole.STAFF_PHARMACIST,
        "SUPERADMIN",
        "PHARMACY_OWNER",
        "BRANCH_MANAGER",
        "AUDITOR",
        "STAFF_PHARMACIST",
    }

    def get_queryset(self):
        user = self.request.user
        role = getattr(user, "role", None)
        if role not in self.AUDIT_VIEWER_ROLES:
            raise PermissionDenied(
                {
                    "detail": "Viewing security audit logs requires Manager, Owner, Pharmacist, or Auditor role.",
                    "error_code": "ERR_AUDIT_VIEW_FORBIDDEN",
                }
            )

        tenant_id = getattr(user, "tenant_id", None) or getattr(user, "tenant", None)
        if not tenant_id:
            return StockPermissionAuditLog.objects.none()

        tenant = Tenant.objects.get(tenant_id=tenant_id) if not isinstance(tenant_id, Tenant) else tenant_id
        qs = (
            StockPermissionAuditLog.objects.filter(tenant=tenant)
            .select_related("branch", "actor")
            .order_by("-created_at")
        )

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        decision = self.request.query_params.get("decision")
        if decision:
            qs = qs.filter(decision=decision)

        return qs

    @action(detail=False, methods=["get"], url_path="matrix")
    def matrix(self, request: Request) -> Response:
        """
        Returns the authoritative role permission matrix across all 5 stock actions.
        GET /api/v2/inventory/permissions/matrix/
        """
        return Response(StockPermissionAuditService.get_role_matrix_summary(), status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="evaluate")
    def evaluate(self, request: Request) -> Response:
        """
        Evaluates user authorization for a branch and action scope, writing an immutable audit log.
        POST /api/v2/inventory/permissions/evaluate/
        """
        user = request.user
        branch_id = request.data.get("branch")
        action_scope = request.data.get("action_scope")
        resource_id = request.data.get("resource_id", "")
        enforce_http_error = request.data.get("enforce", True)

        if not branch_id:
            raise ValidationError({"branch": "Target branch ID is required."})
        if not action_scope:
            raise ValidationError({"action_scope": "Action scope is required."})

        try:
            branch = Branch.objects.select_related("tenant").get(branch_id=branch_id)
        except Branch.DoesNotExist:
            raise NotFound({"detail": f"Branch '{branch_id}' not found.", "error_code": "ERR_BRANCH_NOT_FOUND"})

        audit = StockPermissionAuditService.evaluate_and_audit(
            user=user,
            branch=branch,
            action_scope=action_scope,
            resource_id=resource_id,
            raise_on_deny=bool(enforce_http_error),
        )

        serializer = StockPermissionAuditLogSerializer(audit)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PUT", detail="Stock permission audit logs are strictly immutable.")

    def partial_update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PATCH", detail="Stock permission audit logs are strictly immutable.")

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="Stock permission audit logs are strictly immutable.")
